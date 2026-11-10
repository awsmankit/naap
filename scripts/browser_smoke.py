"""Run the in-browser Naap end to end in headless Chromium (local mode, no server).

    uv run --with playwright python scripts/browser_smoke.py               # the sample house
    uv run --with playwright python scripts/browser_smoke.py --file X.dxf  # time any drawing

It serves site/ (build it first: scripts/build_site.py), drops the drawing in, waits for the
review step, builds the book, exports the workbook and saves a project, then prints one JSON line:
stage timings, MB rows, the longest main-thread task (the UI must not freeze: the engine runs in a
worker) and the peak memory of the browser's renderer process. With the sample it also checks the
book has the expected 57 rows. Exit code 1 on any failure.

Use --file only with drawings you may process on this machine; nothing is written next to them.
"""
from __future__ import annotations

import argparse
import functools
import json
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_ROWS = 57

LONGTASKS = """
window.__longest = 0;
try {
  new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__longest = Math.max(window.__longest, e.duration); })
    .observe({ type: "longtask", buffered: true });
} catch (e) {}
// main-thread stalls, measured directly too: how late a 50 ms timer fires
window.__stall = 0;
(() => { let last = performance.now(); setInterval(() => { const now = performance.now(); window.__stall = Math.max(window.__stall, now - last - 50); last = now; }, 50); })();
"""


class Quiet(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript",
                      ".wasm": "application/wasm", ".json": "application/json", ".woff2": "font/woff2"}

    def log_message(self, *a):
        pass


class Server(ThreadingHTTPServer):
    request_queue_size = 64   # the worker fetches the runtime files in parallel


def serve(site: Path) -> tuple[ThreadingHTTPServer, str]:
    srv = Server(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


class PeakRSS:
    """Largest resident size of any Chromium renderer process (the page and its workers live there)."""

    def __init__(self, browser_pid_hint: str = "headless"):
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(["ps", "-A", "-o", "rss=,command="], capture_output=True, text=True, timeout=5).stdout
            except Exception:
                out = ""
            for ln in out.splitlines():
                rss, _, cmd = ln.strip().partition(" ")
                if "--type=renderer" in cmd and ("chrom" in cmd.lower() or "headless" in cmd.lower()):
                    self.peak = max(self.peak, int(rss) * 1024)
            self._stop.wait(0.25)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def run(site: Path, drawing: Path | None, timeout_s: float, headed: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    srv, base = serve(site)
    errors: list[str] = []
    logs: list[str] = []
    out: dict = {"file": drawing.name if drawing else "sample-house.dxf"}
    try:
        with sync_playwright() as p, PeakRSS() as mem:
            browser = p.chromium.launch(headless=not headed)
            ctx = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
            ctx.add_init_script(LONGTASKS)
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
            page.goto(base, wait_until="networkidle")
            t0 = time.time()
            try:
                _steps(page, drawing, timeout_s, out, t0)
            except Exception as exc:
                where = page.evaluate("() => [document.querySelector('#stages')?.innerText, document.querySelector('#upErr')?.innerText, document.querySelector('#toast')?.innerText]")
                raise RuntimeError(f"{exc}\n  page: {where}\n  console: {logs[-8:]}") from None
            out["longest_main_thread_task_ms"] = round(page.evaluate("() => Math.max(window.__longest, window.__stall)"))
            browser.close()
        out["peak_renderer_mb"] = round(mem.peak / 1e6)
    finally:
        srv.shutdown()
    bad = [e for e in errors if "favicon" not in e]
    if bad:
        out["errors"] = bad[:5]
    return out


def _steps(page, drawing, timeout_s, out, t0):
    if drawing:
        page.set_input_files("#fileInput", str(drawing))
    else:
        page.click("#btnSample")
    # the job id appears once the engine is loaded and the file handed to it
    page.wait_for_function("() => (window.__dwgmb && window.__dwgmb.jobId) || !document.querySelector('#upErr').hidden", timeout=timeout_s * 1000)
    if not page.locator("#upErr").is_hidden():
        raise RuntimeError(f"the engine did not start: {page.locator('#upErr').inner_text()}")
    out["engine_ready_s"] = round(time.time() - t0, 1)
    page.wait_for_function("""() => !document.querySelector('#reviewPanel').hidden
        || !document.querySelector('#upErr').hidden""", timeout=timeout_s * 1000)
    if not page.locator("#upErr").is_hidden():
        raise RuntimeError(f"upload failed: {page.locator('#upErr').inner_text()}")
    page.wait_for_function("() => window.__dwgmb.geo && window.__dwgmb.viewer && document.querySelector('#stageMsg').hidden", timeout=timeout_s * 1000)
    out["review_s"] = round(time.time() - t0, 1)
    job = page.evaluate("() => window.__dwgmb.job")
    out["timings"] = job.get("timings")
    out["entities"] = (job.get("stats") or {}).get("entities")
    t1 = time.time()
    page.click("#btnBuild")
    page.wait_for_function("() => window.__dwgmb.mb && !document.querySelector('#ledger').hidden", timeout=timeout_s * 1000)
    out["build_s"] = round(time.time() - t1, 1)
    out["rows"] = page.evaluate("() => window.__dwgmb.mb.rows.length")
    t2 = time.time()
    with page.expect_download(timeout=timeout_s * 1000) as dl:
        page.click("#btnXlsx")
    with tempfile.TemporaryDirectory() as tmp:
        x = Path(tmp) / "mb.xlsx"
        dl.value.save_as(x)
        out["xlsx_s"] = round(time.time() - t2, 1)
        assert zipfile.is_zipfile(x), "the workbook is not an xlsx"
        with page.expect_download(timeout=timeout_s * 1000) as dl2:
            page.click("#btnSaveProject")
        proj = Path(tmp) / "p.naap.json"
        dl2.value.save_as(proj)
        with proj.open() as fh:
            head = fh.read(200)
        assert '"format": "naap-project"' in head, head[:80]
        out["project_mb"] = round(proj.stat().st_size / 1e6, 1)
    out["total_s"] = round(time.time() - t0, 1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--site", type=Path, default=ROOT / "site")
    ap.add_argument("--file", type=Path, help="a drawing to time instead of the sample")
    ap.add_argument("--timeout", type=float, default=900, help="seconds per stage")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args(argv)
    if not (a.site / "worker" / "assets.json").exists() or not json.loads((a.site / "worker" / "assets.json").read_text()).get("vendored"):
        print(f"{a.site} is not a full build: run `uv run python scripts/build_site.py` first", file=sys.stderr)
        return 2
    res = run(a.site.resolve(), a.file.resolve() if a.file else None, a.timeout, a.headed)
    print(json.dumps(res))
    ok = not res.get("errors") and res.get("rows", 0) > 0
    if not a.file:
        ok = ok and res["rows"] == SAMPLE_ROWS
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
