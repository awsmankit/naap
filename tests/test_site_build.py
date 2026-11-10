"""The in-browser site (scripts/build_site.py) and its Python entry point (app/browser.py).

The build is checked without the runtime downloads (--no-vendor). The browser itself is
exercised by scripts/browser_smoke.py (the Pages workflow runs it; set NAAP_BROWSER_SMOKE=1 to run
it here against a full site/ build)."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_site_assembles_without_downloads(tmp_path):
    import build_site

    out = tmp_path / "site"
    s = build_site.build(out, tmp_path / "cache", with_vendor=False)
    assert s["total"] > 0
    for f in ["index.html", "config.js", "js/api.js", "js/main.js", "css/app.css", "worker/naap-worker.js",
              "worker/dwg-worker.js", "samples/sample-house.dxf", "LICENSE.txt", "THIRD_PARTY_NOTICES.md",
              "fonts/osifont-LICENSE.txt", "mock/mb.json", ".nojekyll"]:
        assert (out / f).is_file(), f
    assert 'mode: "local"' in (out / "config.js").read_text()
    assert 'mode: "server"' in (ROOT / "web" / "config.js").read_text()   # the repo default stays server
    assert not list(out.rglob("*.py")), "no server-side sources in the site"
    names = zipfile.ZipFile(out / "py" / "naap.zip").namelist()
    for mod in ["app/browser.py", "app/service.py", "app/jobs.py", "app/extract.py", "app/pdf_backends/pymupdf_backend.py",
                "engine/mb.py", "engine/export_xlsx.py", "engine_struct/build.py"]:
        assert mod in names, mod
    assert "app/main.py" not in names
    m = json.loads((out / "worker" / "assets.json").read_text())
    assert m["meta"]["runtime"] == "browser" and ".dwg" in m["meta"]["accept"]
    assert set(m["packages"]) >= {"numpy", "scipy", "ezdxf", "openpyxl"} and m["pdf_packages"] == ["pymupdf"]


def test_vendor_downloads_are_pinned():
    import build_site

    pins = [build_site.PYODIDE_NPM, build_site.LIBREDWG_NPM, *build_site.LICENCES.values(),
            *[(u, h) for u, h, *_ in build_site.PYPI_WHEELS.values()]]
    for url, digest in pins:
        assert url.startswith("https://") and len(digest) == 64, url
    # the scipy subpackages the engine imports must survive the trim
    for keep in ("ndimage", "spatial", "special", "linalg", "sparse", "_lib", "constants"):
        assert keep not in build_site.SCIPY_DROP


def test_browser_service_round_trip(tmp_path, monkeypatch):
    """app/browser.py answers the UI's calls like the server, on the sample house (no Pyodide needed)."""
    monkeypatch.setenv("DWG_MB_DATA", str(tmp_path / "jobs"))
    from app import jobs, service
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    # init() patches these for the browser; put them back for the other tests
    for mod, name in ((jobs, "update_state"), (jobs, "_convert"), (service, "GEOMETRY_CACHE")):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    browser = importlib.import_module("app.browser")
    states = []
    meta = json.loads(browser.init(lambda s: states.append(json.loads(s)["status"])))
    assert meta["max_upload_mb"] > 0

    env = json.loads(browser.new_job("sample-house.dxf", 1000))
    jid = env["body"]["id"]
    shutil.copy(ROOT / "samples" / "sample-house.dxf", jobs.DATA_DIR / jid / "input.dxf")
    assert json.loads(browser.accept_input(jid))["body"]["status"] == "queued"
    assert json.loads(browser.process(jid))["status"] == "ready"
    assert {"parsing", "clustering", "ready"} <= set(states)
    assert not (jobs.DATA_DIR / jid / "input.dxf").exists()   # only the geometry is kept

    def call(method, url, body=None):
        return json.loads(browser.request(method, url, json.dumps(body) if body is not None else None))

    cfg = call("GET", f"/api/jobs/{jid}/config")["body"]
    built = call("POST", f"/api/jobs/{jid}/build", cfg)
    assert built["status"] == 200 and built["body"]["counts"]["mb_rows"] == 57
    mb = call("GET", f"/api/jobs/{jid}/mb")
    assert mb["type"] == "application/json" and json.loads(Path(mb["file"]).read_text())["kind"] == "architectural"
    x = call("GET", f"/api/jobs/{jid}/mb.xlsx")
    assert x["filename"] == "MB - sample-house.xlsx" and zipfile.is_zipfile(x["file"])
    assert call("GET", f"/api/jobs/{jid}/sheets")["status"] == 404          # DXF jobs have no sheets
    assert call("GET", "/api/jobs/" + "0" * 32)["status"] == 404
    assert call("PATCH", f"/api/jobs/{jid}/config")["status"] == 405
    assert call("PUT", f"/api/jobs/{jid}/config", [1])["status"] == 400

    # a saved project opens as a new job with the same answers and book
    proj = call("GET", f"/api/jobs/{jid}/project")
    copy = tmp_path / "copy.naap.json"
    shutil.copy(proj["file"], copy)
    new = json.loads(browser.import_project(str(copy)))["body"]["id"]
    assert new != jid and call("GET", f"/api/jobs/{new}")["body"]["status"] == "ready"
    assert call("POST", f"/api/jobs/{new}/build", cfg)["body"]["counts"]["mb_rows"] == 57
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "something-else"}')
    assert json.loads(browser.import_project(str(bad)))["status"] == 400

    assert json.loads(browser.new_job("notes.txt", 10))["status"] == 400
    fake = json.loads(browser.new_job("fake.pdf", 10))["body"]["id"]
    (jobs.DATA_DIR / fake / "input.pdf").write_bytes(b"not a pdf")
    assert json.loads(browser.accept_input(fake))["status"] == 400
    assert not (jobs.DATA_DIR / fake).exists()


@pytest.mark.skipif(os.environ.get("NAAP_BROWSER_SMOKE") != "1", reason="set NAAP_BROWSER_SMOKE=1 (needs a full site/ and Playwright)")
def test_browser_smoke():
    r = subprocess.run(["uv", "run", "--with", "playwright==1.65.0", "python", str(ROOT / "scripts" / "browser_smoke.py")],
                       capture_output=True, text=True, timeout=1800, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
