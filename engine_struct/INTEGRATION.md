# Wiring PDF input + the structural MB into the app

Everything below is new code that is already in place: `app/pdf_extract.py`,
`app/pdf_backends/`, `engine_struct/`, `tests/test_pdf_*.py` and `tests/test_struct_*.py`.
No existing file was changed except `pyproject.toml`/`uv.lock` (`uv add pymupdf`).
This note lists what still has to be added to existing files.

## 1. Upload and file-type dispatch (`app/jobs.py`, `app/main.py`)

- `jobs.ALLOWED_EXT`: add `".pdf"`. In `main.upload`, change the 400 message to
  "Only .dwg, .dxf and .pdf files are accepted".
- `jobs.sniff`: for `.pdf`, accept when `head.startswith(b"%PDF-")`. Save the upload as
  `input.pdf` and set `kind = "pdf"`.
- `jobs._process`: when `st["kind"] == "pdf"`, do this:
  ```python
  from app.pdf_extract import extract_pdf, ScannedPDFError
  geometry = extract_pdf(d / "input.pdf")        # raises ScannedPDFError for raster scans
  ```
  - Catch `ScannedPDFError` and call `update_state(status="error", error=str(exc))`. The
    message starts with "scanned PDF not supported yet".
  - Skip `cluster()`. A PDF already comes split into sheets, so each sheet is a region:
    `regions = [{"id": i, "name": s["title_block"]["drawing_no"] or f"Sheet {s['sheet']}",
    "bbox": s["bbox"], "kind": "sheet", "include": True, "sheet": s["sheet"]} for i, s in enumerate(geometry["sheets"])]`.
  - Write `config.json` with `{"engine": "struct", "regions": regions, "flags": {}, "calibration": {}}`.
  - Set `stats.units = "pt"` and `stats.sheets = len(geometry["sheets"])`.
- `main._run` (or wherever `build_mb` is called): dispatch on the job kind or on
  `cfg["engine"] == "struct"`:
  ```python
  from engine_struct.build import build_struct_mb
  from engine_struct.export import write_struct_xlsx, result_json as struct_result_json
  result = build_struct_mb(geometry, cfg)
  write_struct_xlsx(result, d / "mb.xlsx", project=...)
  cache: struct_result_json(result)            # same keys as engine.mb.result_json + steel/schedules/calibration
  ```
  `_flag_summary(result)` and `asdict(result.abstract/flags)` work unchanged, because
  `build_struct_mb` returns `engine.types.MBResult`. The `rooms`, `openings` and
  `dimensions` lists are empty.
- `GET /api/jobs/{id}/entities?handles=`: no change is needed. PDF handles look like
  `p5:1234` (page:paint order, with `.k` for a sub-path). The route upper-cases both sides
  when it compares them.

## 2. Routes to add (`app/main.py`)

| Route | Purpose |
|---|---|
| `GET /api/jobs/{id}/sheets` | `geometry["sheets"]`: page size, offset in the shared space, title block (drawing no, title, NOT TO SCALE, floor scope and multiplier), and path/text/image counts. |
| `GET /api/jobs/{id}/calibration` | `result.meta["calibration"]`: per sheet, pt/mm, ≈1:N, number of samples, spread %, method, and samples. Each sample has `at` and `handles`, so the UI can draw them. |
| `PUT /api/jobs/{id}/calibration` | Body `{sheet: {"p1": [x, y], "p2": [x, y], "mm": L}}`, or `{"scale": N}` / `{"pt_per_mm": r}`. Merge it into `config["calibration"]` and rebuild. |
| `GET /api/jobs/{id}/sheet/{n}.png?dpi=&layers=` | Optional raster backdrop or crop, from `open_pdf(...).page(n-1).render_png(clip, dpi, layers)`. |

Everything else (`/build`, `/mb`, `/mb.xlsx`, `/config`) is reused as-is.

## 3. UI needs (`web/`, `static/`)

- **Upload**: accept `.pdf`. Show the scanned-PDF error text as it comes.
- **Viewer**: the units are points, y goes up, and the sheets sit side by side. Each entity
  has `sheet`. Text records have `bb` (y-up bbox) and `f` (font size). Filled paths are
  `H` with `solid: true`, and the circle records have `c` and `r`. Add a sheet picker that
  zooms to `sheets[i].bbox`. Layers are the PDF OCG names; an OCG name used on several pages
  is listed once.
- **MB tab**: the rows have the same shape as before. Each `refs[]` item also carries
  `sheet`, which you can use to jump. Show `status` (ok / assumed / unresolved) and
  `floor_group`. Typical floors show "×5" and the multiplier is already in `nos`.
- **New tabs**:
  - *Steel*: show `meta.steel.by_dia_kg`, `by_element_kg`, `total_kg` and `assumed_kg`.
  - *Schedules*: show `meta.schedules.tables[]` (`name`, `sheet`, `columns`, `rows[{label, cells}]`)
    and `meta.schedules.pile`.
  - *Calibration*: list the sheets with their spread. Add a "Calibrate" tool: click two points,
    then type the mm value, which sends `PUT .../calibration`.
- **Flags**: same shape as before. `input.type` is `number` or `text`. The value is saved in
  `config.flags[id] = {status: "resolved", value}`. These ids are worth showing first:
  - `struct:foundation:pile-vs-raft`: the value is `piled_raft`, `piles_only` or `raft_only`.
  - `struct:columns:height:<DRG>`: the height in mm. Until it is answered, the column rows have qty 0.
  - `struct:calibration:<DRG>`: the value is `1:N`.
  - `struct:beams:untagged:<DRG>`: the value is `230x450`.

## 4. PDF backend (licensing)

- Only `app/pdf_backends/pymupdf_backend.py` imports PyMuPDF (AGPL or commercial).
  `tests/test_pdf_extract.py::test_no_pymupdf_outside_backend` enforces this.
- To pick a backend, set `PDF_BACKEND=<name>`. To register a new one, add it to
  `app/pdf_backends/__init__.py:BACKENDS`.
- The interface is `open_pdf(path)`, which gives a doc with `page_count`, `page(i)`,
  `ocg_layers()` and `close()`. Each page has these:
  - `info`: number, width, height, rotation;
  - `paths()` → `PdfPath(segs, layer, stroke, fill, width, closed, dashed, seq)`;
  - `text_spans()` → `TextSpan(text, bbox, origin, dir, size, font, layer, seq)`;
  - `images()` → bboxes;
  - `render_png(clip, dpi, layers)`;
  - `render_gray(clip, dpi, layers)`.

  All coordinates are in the displayed page space: /Rotate is applied, y points down, and
  the units are points. The extraction logic (glyph-run merging, circle fitting, the
  control-code text fix, title blocks) only uses these plain types.
- A **pypdfium2** backend (Apache/BSD) would need:
  - Paths: `FPDFPage_GetObject` with type PATH, then `FPDFPath_CountSegments` /
    `FPDFPathSegment_GetPoint` / `GetType` / `GetClose`. Multiply by `FPDFPageObj_GetMatrix`,
    and get fill and stroke from `FPDFPath_GetDrawMode`. Recurse into form XObjects
    (`FPDFFormObj_*`).
  - OCG layer: `FPDFPageObj_GetMark(obj, i)`. The mark is named `OC` and its `/Properties`
    resource name has to be resolved to the OCG `/Name`. pdfium exposes the mark parameter,
    but the lookup in the resource dictionary must be done by hand with the raw object API.
  - Text: an `FPDFText` page with char boxes, grouped into runs; the font size comes from
    `FPDFText_GetFontSize`.
  - Rendering with layers: pdfium cannot toggle OCGs. Use `render_gray(layers=None)` and
    raise `RenderUnsupported` for layer filters. The only caller is the independent
    pile-count test.
- A **pdf.js** backend (browser build, Pyodide): `page.getOperatorList()` gives
  `constructPath` ops (moveTo, lineTo, curveTo, rectangle) with the current transform,
  `beginMarkedContentProps` with `OC` (the layer comes from
  `optionalContentConfig.getGroup(id).name`), and `fill` / `stroke` ops. Text comes from
  `page.getTextContent()`: `transform` gives the origin and direction, and `width/height`
  give the box. Apply `viewport = page.getViewport({rotation: page.rotate})` to every point
  so the shapes come out as described above. Rendering uses `page.render` with an
  `optionalContentConfigPromise` in which only the wanted groups are visible.
