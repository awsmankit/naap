"""Draw Naap's icons (web/icons/) and, optionally, the share image (web/og.png).

    uv run --with playwright==1.65.0 python scripts/make_icons.py
    uv run --with playwright==1.65.0 python scripts/make_icons.py --og http://127.0.0.1:8790/

The mark is the favicon's: a yellow (#e9c46a) drafting stroke on prussian blue (#0d2233). The
SVG is written as text and rasterised in headless Chromium, so the PNGs are reproducible from
this file. Commit the outputs.

--og takes the URL of a running Naap (server mode, `PORT=8790 uv run python -m app`, or the built site), processes
the public sample drawing (samples/sample-house.dxf, a made-up house), builds the book, selects
one measurement line so its walls light up, and saves a 1200x630 screenshot as web/og.png.
Never point it at a session holding client drawings.
"""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "web" / "icons"
PRUSSIAN, SIGNAL = "#0d2233", "#e9c46a"
MARK = "M7 24V8h6M7 16h5M19 8v16h6"


def svg(rounded: bool = True, scale: float = 1.0) -> str:
    """The mark on a 32x32 tile. rounded=False fills the square (maskable / Apple icons, which the
    platform rounds itself); scale shrinks the mark about the centre to keep it in the safe zone."""
    rx = ' rx="5"' if rounded else ""
    tf = f' transform="translate(16 16) scale({scale:g}) translate(-16 -16)"' if scale != 1 else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            f'<rect width="32" height="32"{rx} fill="{PRUSSIAN}"/>'
            f'<path d="{MARK}" stroke="{SIGNAL}" stroke-width="2.4" fill="none"{tf}/></svg>\n')


# file -> (pixels, svg). Maskable icons keep the mark inside the central 80% circle.
PNGS = {
    "icon-192.png": (192, svg()),
    "icon-512.png": (512, svg()),
    "icon-512-maskable.png": (512, svg(rounded=False, scale=0.72)),
    "apple-touch-icon.png": (180, svg(rounded=False, scale=0.8)),
}


def icons(page) -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    (ICONS / "favicon.svg").write_text(svg())
    for name, (px, body) in PNGS.items():
        page.set_viewport_size({"width": px, "height": px})
        tile = body.replace("<svg ", f'<svg width="{px}" height="{px}" ')
        page.set_content(f'<html><body style="margin:0;background:transparent">{tile}</body></html>')
        page.screenshot(path=str(ICONS / name), omit_background=True,
                        clip={"x": 0, "y": 0, "width": px, "height": px})
        print(f"  web/icons/{name}")
    print("  web/icons/favicon.svg")


def og(page, url: str) -> None:
    page.goto(url, wait_until="networkidle")
    page.click("#btnSample")
    page.wait_for_function("() => window.__dwgmb && window.__dwgmb.geo && window.__dwgmb.viewer"
                           " && document.querySelector('#stageMsg').hidden", timeout=300_000)
    page.click("#btnBuild")
    page.wait_for_function("() => window.__dwgmb.mb && !document.querySelector('#ledger').hidden", timeout=300_000)
    assert "sample-house.dxf" in page.inner_text("header"), "only the public sample may go in the share image"
    page.wait_for_timeout(500)
    page.click("#ledger tr.row >> nth=1")       # 2.1 Gross wall: the walls of the ground floor light up
    page.wait_for_timeout(1200)
    page.screenshot(path=str(ROOT / "web" / "og.png"))
    print("  web/og.png")


def main(argv=None) -> None:
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--og", metavar="URL", help="also screenshot the sample house in a running Naap at URL")
    a = ap.parse_args(argv)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(device_scale_factor=1)
        icons(page)
        if a.og:   # a desktop layout (1440x756 CSS px) at 1200x630 device px
            og(b.new_page(viewport={"width": 1440, "height": 756}, device_scale_factor=1200 / 1440), a.og)
        b.close()


if __name__ == "__main__":
    main()
