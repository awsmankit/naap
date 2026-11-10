"""web/ has no build step, so check the wiring statically: every ES-module import resolves,
every #id the scripts look up exists in index.html, and the JS parses (when node is present)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
JS = sorted((WEB / "js").glob("*.js"))


def test_imports_resolve():
    for f in JS:
        for spec in re.findall(r'^import [^;]*? from "(\.[^"]+)";', f.read_text(), re.M):
            assert (f.parent / spec).resolve().exists(), f"{f.name}: {spec}"


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
    assert "/api/sample/sample-house.dxf" in (WEB / "js" / "main.js").read_text()


def test_ui_has_no_absolute_paths_or_cdns():
    for f in [WEB / "index.html", WEB / "css" / "app.css", *JS]:
        s = f.read_text()
        assert not re.search(r"/Users/|/tmp/|https?://(?!www\.w3\.org)", s), f.name
