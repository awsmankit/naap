# Naap: working notes for agents

Naap turns DWG/DXF/PDF drawings into an Indian CPWD-style Measurement Book (MB). Public repo, AGPL-3.0.
Read `README.md` and `web/README.md` for the product. This file holds the rules every change must follow.

## Hard rules
- **No client data in tracked files.** Real drawings, their names, numbers, coordinates, handles, schedules and screenshots stay outside git.
  - Local test drawings live under `/tmp` and `~/Downloads`. Client-derived tests live in `tests/private/` (git-ignored). Skip them cleanly when the files are absent.
  - Public tests, fixtures, samples and the UI mock use only synthetic data: `tests/fixtures/make_synthetic.py`, `samples/make_sample.py`.
  - Use generic wording in code comments and docs. Never name a client project.
- **Do not `git commit`, `git push` or change repo settings** unless the lead asks. The lead ships.
- Repo-local git identity is `awsmankit`. Do not change git config.
- Uploaded drawings are untrusted: validate type and size, never execute anything from them, run ad-hoc scripts with `python -I`.
- PyMuPDF is imported only in `app/pdf_backends/pymupdf_backend.py`. Third-party GPL/AGPL pieces stay behind adapters (swappable later).

## Layout
- `engine/` architectural DWG/DXF to MB. `engine_struct/` structural PDF to MB (RCC, steel). Both return rows with `refs` (bbox + handles) and `flags`.
- `app/` shared job API (`service.py`), FastAPI server (`main.py`), Pyodide entry (`browser.py`), DXF and PDF extraction.
- `web/` the only UI: plain ES modules, no build step. Two transports in `web/js/api.js`: `server` (fetch) and `local` (Web Worker with Pyodide).
- `scripts/build_site.py` builds `site/` for GitHub Pages. `scripts/browser_smoke.py` is the headless browser check.

## Commands
- Public tests (fast): `uv run pytest -q --ignore=tests/private`
- Private tests (about 3 min, needs local client files): `uv run pytest -q tests/private`
- Server: `uv run python -m app` on 127.0.0.1:8777
- Browser build: `uv run python scripts/build_site.py`, serve `site/`, then `uv run --with playwright==1.65.0 python scripts/browser_smoke.py`
- CLI: `uv run python -m engine.cli drawing.dxf --out mb.xlsx` and `uv run python -m engine_struct.cli sheet.pdf --out mb.xlsx`

## Conventions
- Never invent missing data (heights, slab thickness, cover, lap length). Flag it "missing" with an input, or use a visibly marked default (status "assumed").
- Every MB row keeps its refs so click-to-locate works, and a remark saying where the number came from.
- Quantities that rely on a scaled length are "assumed" with the standard "scaled from drawing – verify" remark.
- Row ids are stable hashes. Keep API shapes backward-compatible (additive changes only).
- Keep the desktop layout pixel-stable when changing phone layouts (and the reverse).
- Keep working changes small. When several agents work at once, each owns separate files. Say which files you touched in your report.

## Gotchas
- Docker cannot build locally behind some corporate TLS proxies. CI builds and smoke-tests the image instead.
- The MCP browser can only save screenshots under `~/.playwright-mcp` or the home folder.
