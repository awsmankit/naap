"""Several PDFs read as ONE drawing set: merge_geometries, the multi-file upload API, and the wiring."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import browser, jobs, service
from app import main as appmain
from app.pdf_extract import extract_pdf, extract_pdfs, merge_geometries
from tests.test_pdf_extract import vector_sheet, write_pdf

WEB = Path(__file__).resolve().parents[1] / "web"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    return TestClient(appmain.app)


def _two(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    pa = vector_sheet(a)
    pb = write_pdf(b / "second.pdf", "0.5 w\n100 100 m 300 100 l S\n50 50 m 60 60 l S\n")
    return pa, pb


def _wait(client, jid, timeout=120):
    t = time.time()
    while time.time() - t < timeout:
        st = client.get(f"/api/jobs/{jid}").json()
        if st["status"] in ("ready", "error"):
            return st
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_merge_geometries_offsets_handles_layers(tmp_path):
    pa, pb = _two(tmp_path)
    ga, gb = extract_pdf(pa), extract_pdf(pb)
    na, nb = len(ga["entities"]), len(gb["entities"])
    g = merge_geometries([ga, gb], ["a.pdf", "second.pdf"])
    assert [s["sheet"] for s in g["sheets"]] == [1, 2]
    assert [(s["file"], s["file_index"], s["file_page"]) for s in g["sheets"]] == [("a.pdf", 0, 1), ("second.pdf", 1, 1)]
    assert len(g["entities"]) == na + nb
    s1, s2 = g["sheets"]
    assert s2["bbox"][0] >= s1["bbox"][2]  # side by side, no overlap
    assert all(e["handle"].startswith(f"p{e['sheet']}:") for e in g["entities"])
    assert len({e["handle"] for e in g["entities"]}) == len(g["entities"])
    names = [l["name"] for l in g["layers"]]
    assert len(names) == len(set(names)) and all(0 <= e["l"] < len(names) for e in g["entities"])
    for e in g["entities"]:  # every entity lies inside its own sheet's bbox (x offset applied)
        if e["sheet"] == 2 and "bb" in e:
            assert e["bb"][0] >= s2["bbox"][0] - 1


def test_single_pdf_sheets_carry_file(tmp_path):
    pa, _ = _two(tmp_path)
    g = extract_pdfs([pa])
    assert g["sheets"][0]["file"] == "vec0.pdf" and g["sheets"][0]["file_index"] == 0


def test_multi_upload_api(client, tmp_path):
    pa, pb = _two(tmp_path)
    with pa.open("rb") as fa, pb.open("rb") as fb:
        r = client.post("/api/jobs", files=[("file", ("plan a.pdf", fa, "application/pdf")), ("file", ("second.pdf", fb, "application/pdf"))])
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    st = _wait(client, jid)
    assert st["status"] == "ready", st
    assert st["files"] == ["plan a.pdf", "second.pdf"] and st["stats"]["sheets"] == 2
    d = jobs.DATA_DIR / jid
    assert (d / "input_1.pdf").exists() and (d / "input_2.pdf").exists() and not (d / "input.pdf").exists()
    sheets = client.get(f"/api/jobs/{jid}/sheets").json()
    assert [(s["sheet"], s["file"], s["file_page"]) for s in sheets] == [(1, "plan a.pdf", 1), (2, "second.pdf", 1)]
    assert client.get(f"/api/jobs/{jid}/sheet/2.png?dpi=20").content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{jid}/sheet/3.png").status_code == 404


def test_multi_upload_rejects_mixed_and_bad(client, tmp_path):
    pa, pb = _two(tmp_path)
    dxf = tmp_path / "x.dxf"
    dxf.write_text("0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
    with pa.open("rb") as fa, dxf.open("rb") as fx:
        r = client.post("/api/jobs", files=[("file", ("a.pdf", fa, "x/y")), ("file", ("x.dxf", fx, "x/y"))])
    assert r.status_code == 400 and "several PDFs" in r.json()["detail"]
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf")
    with pa.open("rb") as fa, fake.open("rb") as ff:
        r = client.post("/api/jobs", files=[("file", ("a.pdf", fa, "x/y")), ("file", ("fake.pdf", ff, "x/y"))])
    assert r.status_code == 400 and "fake.pdf" in r.json()["detail"]
    assert not any(jobs.DATA_DIR.iterdir())


def test_multi_upload_total_limit(client, tmp_path, monkeypatch):
    pa, pb = _two(tmp_path)
    monkeypatch.setattr(appmain, "MAX_PDF_MB", 0.01)  # ~10 KB in total
    big = tmp_path / "big.pdf"
    big.write_bytes(pa.read_bytes() + b" " * 12_000)
    with big.open("rb") as f1, pb.open("rb") as f2:
        r = client.post("/api/jobs", files=[("file", ("big.pdf", f1, "x/y")), ("file", ("b.pdf", f2, "x/y"))])
    assert r.status_code == 413
    assert not any(jobs.DATA_DIR.iterdir())


def test_browser_set_round_trip(tmp_path, monkeypatch):
    """Pyodide's entry points, run natively: set upload, process, save a project, reopen it."""
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    pa, pb = _two(tmp_path)
    r = json.loads(browser.new_job("a.pdf", 5000, json.dumps(["a.pdf", "second.pdf"])))
    assert r["status"] == 200
    jid = r["body"]["id"]
    d = jobs.DATA_DIR / jid
    (d / "input_1.pdf").write_bytes(pa.read_bytes())
    (d / "input_2.pdf").write_bytes(pb.read_bytes())
    assert json.loads(browser.accept_input(jid))["status"] == 200
    st = json.loads(browser.process(jid))
    assert st["status"] == "ready" and st["stats"]["sheets"] == 2
    proj = json.loads(browser.export_project(jid))
    data = json.loads(Path(proj["file"]).read_text())
    assert len(data["input_pdfs"]) == 2 and data["job"]["files"] == ["a.pdf", "second.pdf"]
    imp = json.loads(browser.import_project(proj["file"]))
    assert imp["status"] == 200
    d2 = jobs.DATA_DIR / imp["body"]["id"]
    assert (d2 / "input_1.pdf").read_bytes() == pa.read_bytes() and (d2 / "input_2.pdf").exists()
    assert service.sheet_png(d2, 2, 20)[:4] == b"\x89PNG"
    assert [s["file"] for s in service.sheets(d2)] == ["a.pdf", "second.pdf"]


def test_ui_wiring():
    html = (WEB / "index.html").read_text()
    assert re.search(r'<input[^>]*id="fileInput"[^>]*\bmultiple\b', html)
    for i in ("setBox", "setTitle", "setFiles", "setGo", "setCancel"):
        assert f'id="{i}"' in html
    js = (WEB / "js" / "main.js").read_text()
    assert "files as one set" in js and "sheetSource" in js
    assert "sheetSource" in (WEB / "js" / "review.js").read_text()
