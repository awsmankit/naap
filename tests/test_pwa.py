"""The installable, offline-capable site: web app manifest, icons, share tags in <head>, and the
service worker that scripts/build_site.py versions (web/sw.js, registered by web/js/pwa.js)."""
from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SITE_URL = "https://awsmankit.github.io/naap/"
sys.path.insert(0, str(ROOT / "scripts"))


def head() -> str:
    html = (WEB / "index.html").read_text()
    return html[html.index("<head>"):html.index("</head>")]


def png_size(p: Path) -> tuple[int, int]:
    data = p.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", p
    return struct.unpack(">II", data[16:24])


def test_manifest_is_valid_and_its_icons_exist():
    m = json.loads((WEB / "manifest.webmanifest").read_text())
    assert m["name"] == "Naap · Drawing to Measurement Book" and m["short_name"] == "Naap"
    assert m["start_url"] == "./" and m["scope"] == "./" and m["display"] == "standalone"
    assert m["theme_color"].lower() == m["background_color"].lower() == "#0d2233"
    purposes = set()
    for icon in m["icons"]:
        f = WEB / icon["src"]
        assert f.is_file(), icon["src"]
        purposes |= set(icon.get("purpose", "any").split())
        if icon["type"] == "image/png":
            w, h = map(int, icon["sizes"].split("x"))
            assert png_size(f) == (w, h), icon["src"]
    assert {"any", "maskable"} <= purposes
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_head_links_icons_manifest_and_share_tags():
    h = head()
    assert 'name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-content"' in h
    assert re.search(r'<meta name="description" content="[^"]{50,170}">', h)
    assert '<meta name="theme-color" content="#0d2233">' in h
    assert '<link rel="manifest" href="manifest.webmanifest">' in h
    assert "data:image" not in h, "the favicon is a file now (web/icons/favicon.svg)"
    links = dict(re.findall(r'<link rel="(icon|apple-touch-icon)" href="([^"]+)"', h))
    assert links["icon"].endswith(".svg") and png_size(WEB / links["apple-touch-icon"]) == (180, 180)
    for p in links.values():
        assert (WEB / p).is_file(), p
    og = dict(re.findall(r'<meta property="(og:[\w:]+)" content="([^"]*)">', h))
    for k in ("og:title", "og:description", "og:url", "og:image", "og:image:alt", "og:type"):
        assert og.get(k), k
    assert og["og:url"] == SITE_URL and og["og:image"] == SITE_URL + "og.png"
    assert png_size(WEB / "og.png") == (int(og["og:image:width"]), int(og["og:image:height"])) == (1200, 630)
    assert '<meta name="twitter:card" content="summary_large_image">' in h
    # the only absolute URL in the page is the public site's own (for link previews)
    for url in re.findall(r"https?://[^\s\"'<>]+", (WEB / "index.html").read_text()):
        assert url.startswith((SITE_URL, "http://www.w3.org/")), url


def test_sw_is_versioned_by_the_site_build(tmp_path):
    import build_site

    src = (WEB / "sw.js").read_text()
    for key in ('"__NAAP_BUILD__"', '"__NAAP_VENDOR__"', '["__NAAP_PRECACHE__"]'):
        assert key in src, key
    out = tmp_path / "site"
    s = build_site.build(out, tmp_path / "cache", with_vendor=False)
    sw = (out / "sw.js").read_text()
    assert "__NAAP_" not in sw, "placeholders left in the built sw.js"
    build = re.search(r'const BUILD = "([0-9a-f]{16})";', sw).group(1)
    assert build == s["build"]
    shell = json.loads(re.search(r"const PRECACHE = (\[.*?\]);", sw).group(1))
    assert shell[0] == "./" and {"config.js", "js/api.js", "js/pwa.js", "worker/naap-worker.js",
                                  "worker/assets.json", "py/naap.zip", "manifest.webmanifest"} <= set(shell)
    assert not [p for p in shell if p.startswith(("vendor/", "api/", "mock/"))], "the runtime is cached lazily"
    for p in shell[1:]:
        assert (out / p).is_file(), p
    for f in ("manifest.webmanifest", "og.png", "icons/favicon.svg", "icons/icon-512-maskable.png", "js/pwa.js"):
        assert (out / f).is_file(), f

    # any change to the site gives a new version
    (out / "worker" / "assets.json").write_text((out / "worker" / "assets.json").read_text() + " ")
    (out / "sw.js").write_text(src)
    assert build_site.stamp_sw(out) != build


def test_sw_never_caches_api_ranges_or_writes():
    sw = (WEB / "sw.js").read_text()
    assert 'req.method !== "GET"' in sw and 'headers.has("range")' in sw and '"api/"' in sw
    assert "caches.delete" in sw and "skipWaiting" in sw and "clients.claim" in sw
    assert not re.search(r"https?://(?!www\.w3\.org)", sw)


def test_registration_is_local_mode_only():
    js = (WEB / "js" / "pwa.js").read_text()
    assert "export function registerSW" in js and 'from "../config.js"' in js
    assert 'params.get("mode")' in js and 'params.get("mock") === "1"' in js
    assert "serviceWorker.register" in js and "localhost" in js and "https:" in js
    assert "fetch(" not in js and "new Worker" not in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_sw_parses():
    for f in (WEB / "sw.js", WEB / "js" / "pwa.js"):
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"
