"""web/ has no build step, so check the wiring statically: every ES-module import resolves,
every #id the scripts look up exists in index.html, and the JS parses (when node is present)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
JS = sorted((WEB / "js").glob("*.js")) + sorted((WEB / "worker").glob("*.js")) + [WEB / "config.js"]


def test_imports_resolve():
    for f in JS:
        for spec in re.findall(r'^import [^;]*? from "(\.[^"]+)";', f.read_text(), re.M):
            if "/vendor/" in spec:  # runtime files the site build adds (scripts/build_site.py)
                continue
            assert (f.parent / spec).resolve().exists(), f"{f.name}: {spec}"


def test_only_api_js_knows_the_transport():
    """The UI talks to the engine through api.js only (server fetch or the in-browser worker)."""
    for f in JS:
        if f.parent.name != "js" or f.name == "api.js":
            continue
        s = f.read_text()
        assert "new Worker" not in s and "XMLHttpRequest" not in s and "fetch(" not in s, f.name


def test_ids_exist_in_index():
    html = (WEB / "index.html").read_text()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set()
    for f in JS:
        used |= set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', f.read_text()))
    missing = sorted(used - ids)
    assert not missing, missing


def test_upload_accepts_pdf():
    html = (WEB / "index.html").read_text()
    assert 'accept=".dwg,.dxf,.pdf"' in html


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_js_parses():
    for f in JS:
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


def test_branding_and_sample_links():
    html = (WEB / "index.html").read_text()
    assert "<title>Naap" in html and "Naap" in html.split("</header>")[0]
    assert 'href="?mock=1"' in html and 'id="btnSample"' in html
    api = (WEB / "js" / "api.js").read_text()
    assert "/api/sample/${name}" in api and "samples/${name}" in api and 'sampleFile("sample-house.dxf")' in (WEB / "js" / "main.js").read_text()


def test_ui_has_no_absolute_paths_or_cdns():
    for f in [WEB / "index.html", WEB / "css" / "app.css", *JS]:
        s = f.read_text()
        # index.html may name the site's own address (Open Graph tags need absolute URLs)
        assert not re.search(r"/Users/|/tmp/|https?://(?!www\.w3\.org" + (r"|awsmankit\.github\.io/naap/" if f.name == "index.html" else "") + ")", s), f.name
