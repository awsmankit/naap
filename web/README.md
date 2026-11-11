# web/ — Naap UI (no build step)

Plain ES modules + self-hosted fonts (Barlow, Osifont ISO 3098 lettering; licences in `fonts/`).
No CDNs. Serve the folder as static files; it talks to the FastAPI app under `/api`.

```sh
uv run python -m app            # API + this UI on http://127.0.0.1:8777
uv run python web/dev_server.py # UI on http://127.0.0.1:8778 (proxies /api + /openapi.json)
```

- `?mock=1` uses `mock/mb.json`, a saved engine run on `samples/sample-house.dxf` (a made-up house). With a `#job=` it
  uses that job's geometry and real `/check` flags; without one it loads `mock/geometry.json`.
- Rebuild the mock (after engine changes; `tests/test_sample.py` checks it): `uv run python web/mock/build_mock.py`.
- "Process the sample house" on the upload page fetches `GET /api/sample/sample-house.dxf` and uploads it like any file.

## Server or in-browser
`js/api.js` is the only module that knows the transport. `config.js` sets `mode`: `"server"` (fetch to the FastAPI app,
the repo default) or `"local"` (the same calls answered by `worker/naap-worker.js`, which runs `app/browser.py` ->
`app/service.py` on Pyodide; DWG goes through `worker/dwg-worker.js`, LibreDWG as WebAssembly). `?mode=` overrides it.
`scripts/build_site.py` writes the site with `mode: "local"` plus `vendor/` (Pyodide, wheels, LibreDWG), `py/naap.zip`
and `worker/assets.json`; web/ itself has no vendor files, so local mode needs the built site.
Local-only routes: `GET .../project` (a `.naap.json` download) and project import (`openProject` in api.js).

## API used
`GET /api/meta`, `POST /api/jobs`, `GET /api/jobs[/{id}]`, `GET .../geometry`, `GET|PUT .../config`,
`POST .../check`, `POST .../review`, `POST .../build`, `GET .../mb`, `GET .../mb.xlsx`, `GET /openapi.json` (to detect `/mb`).

`GET /api/jobs/{id}/mb` → `{rows, abstract, rooms, openings, dimensions, flags, meta}` where
`rows[] = {id, sno, trade, item, desc?, location, nos, l, b, h, qty, unit, remarks, status, refs, flag_ids}`,
`refs[] = {bbox:[x0,y0,x1,y1], handles:[], kind, label}`. Refs with handles highlight those entities
(geometry `handle`, children share the parent's); otherwise entities inside the bbox, filtered by
`kind` (wall, room, opening, parapet, slab, dimension, …) via the layer roles. Without `/mb` the UI
falls back to the `/build` response (no row-level book). Row `id`s should be stable across runs.

## PDF (structural) jobs
`.pdf` uploads run `app/pdf_extract.py` + `engine_struct`; `/mb` then has `kind: "structural"` plus
`steel`, `schedules`, `calibration`. Extra routes: `GET .../sheets`, `GET|PUT .../calibration`
(`{"<sheet>": {p1, p2, mm} | {scale} | {pt_per_mm} | null}`, rebuilds), `GET .../sheet/{n}.png?dpi=&clip=x0,y0,x1,y1`
(page points, y down). The UI adds a sheet strip, a raster backdrop toggle, a two-point calibration tool and the
Steel / Schedules / Calibration tabs (`js/struct.js`). Keys: `[` `]` previous/next sheet.

## Review step
`POST /api/jobs/{id}/review` (body: the config, or none for the saved one) returns `meta.review` of an engine run
(`engine/review.py`; `engine_struct/build.py:struct_review` for PDFs), reusing the cached run when the config is unchanged:
`views[]` (`purpose`: measured | heights | finishes | sizes | legend | unused, `reason`, `warn`, `overlaps`), `plans[]`
(floors, multiplier, storey height and the flag ids that confirm them), `building.text`, `heights` (rows per block /
storey / roof level with value, source and `assumed`; wall types; slab, beam depth, lintel) and `questions[]`.
`js/review.js` writes answers where the engine reads them: `config.flags[id]` (as on the Flags tab) plus the region's
`floors`, `multiplier`, `floor_to_floor_mm` (`*_source: "user (Review)"`). A non-plan view ticked on Review gets
`measure: true` and is measured as a plan (the UI warns that it double-counts).
