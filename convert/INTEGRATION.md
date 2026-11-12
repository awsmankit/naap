# Drawing conversion: what to wire up

Backend and CLI are done and tested (`convert/`, `tests/test_convert.py`). Nothing in `web/`, `app/main.py`
or `app/service.py` was touched. This file says what to add there.

## API (pure functions, JSON-safe reports)

```python
from convert.pdf_to_dxf import pdf_to_dxf, ConvertError      # raises app.pdf_extract.ScannedPDFError for scans
from convert.to_pdf import dxf_to_pdf

pdf_to_dxf(pdf, out_dxf, page=None|N, scale=None|"1:100"|"auto"|100, calibration={"p1":[x,y],"p2":[x,y],"mm":L},
           tolerance=0.2, layer_by="auto|ocg|color|weight", sheets="offset|blocks") -> report
dxf_to_pdf(src_dxf_or_dwg, out_pdf, paper="A3", landscape=None|True|False, mono=False,
           layouts="model|paper|all", layers=[...], hide_layers=[...]) -> report
```

Reports: `entities` (counts per DXF type), `dropped`, `notes`, `scaled`/`scale_source` (pdf2dxf);
`pages[].scale`, `deviations`, `notes` (dxf2pdf). Show `notes`, `dropped` and `deviations` to the user verbatim.
Calibration points are PDF points from the page's top-left (the same coordinates the PDF viewer already uses).

## Server routes (app/main.py, app/service.py)

Both are one-shot downloads, no job store needed beyond the existing upload validation (type, size, never executed).

| Route | Input | Output |
|---|---|---|
| `POST /api/convert/pdf-to-dxf` | multipart `file` (PDF), form `page`, `scale`, `calib` ("x1,y1,x2,y2,mm"), `tolerance`, `layers`, `sheets` | `{"report": {...}, "dxf_url": "/api/convert/<id>/out.dxf"}` or 422 with the "not supported" message for scans |
| `POST /api/convert/to-pdf` | multipart `file` (.dxf or .dwg), form `paper`, `orientation` (auto/landscape/portrait), `mono`, `layouts`, `hide_layers` | `{"report": {...}, "pdf_url": "..."}` |
| `GET /api/convert/<id>/out.(dxf|pdf)` | | file, `Content-Disposition: attachment` |

Run the conversion in the same worker pool as the existing jobs (a large DXF to PDF takes about 10 s, a 20-page PDF
to DXF about 7 s). DWG input uses `convert.to_pdf.to_dxf_path` (dwg2dxf, same sandbox env as `app/jobs.py`).
Return an error if a client asks for DWG output: **"DWG output is not supported. Write DXF, open it in AutoCAD and
use Save As DWG."** LibreDWG's dxf2dwg writes files AutoCAD rejects ("Invalid handle 0").
The commercial option for real DWG output is the ODA (Open Design Alliance) Teigha/Drawings SDK or ODA File Converter
(licensed, redistribution terms apply); not planned.

## UI (web/)

A "Convert" panel (or two actions on the upload card):
- PDF loaded: "Download as DXF". Options: page (all / one), scale (none / 1:N / from title block / two-point calibration,
  reuse the existing calibration picker), tolerance (mm, default 0.2). Show after conversion: entity counts, layers, the
  dropped list and notes, and a warning banner when `scaled` is false ("paper size, not real-world size").
- DXF or DWG loaded: "Download as PDF". Options: paper A0..A4, orientation (auto/landscape/portrait), colour/black,
  layouts (model / paper-space / all), layer visibility (reuse the layer list). Show the `deviations` list under the button.
- DWG output: a disabled "Download as DWG" with the tooltip above.
- Scanned PDF: show the returned message, offer nothing else.

## Pyodide worker (local mode)

- Add `convert.*` to the files copied into the worker bundle (`scripts/build_site.py`), plus a worker message
  `{op: "convert", kind: "pdf2dxf"|"dxf2pdf", bytes, options}` that writes the input to the Pyodide FS, calls the function,
  and returns `{report, bytes}`; transfer the output ArrayBuffer back and offer it as a Blob download.
- Packages: `dxf2pdf` imports `ezdxf.addons.drawing`, which needs **pillow** (new dependency, already added to
  `pyproject.toml`) and numpy and fonttools (already listed). Add `pillow` to `PYODIDE_PACKAGES` (or load it with PyMuPDF on first
  conversion). PyMuPDF is already the lazy PDF package and is needed by both directions (the PDF writer is in its backend).
- DWG input is not possible in the browser (no dwg2dxf): the panel must be DXF-only in local mode and say so.
- `engine.cli` is imported only for DWG; keep that import lazy (it is, inside `to_dxf_path`).

## Known limits to surface in the UI copy

- pdf2dxf: images, clipping paths, shadings and transparency are not carried over; text is TEXT at the PDF font size
  (cap height = 0.72 x size, no width factor); text plotted as lines stays lines (reported per page); SHX text recovered from
  PDF comments is positioned from the comment box (approximate) and the glyph strokes inside those boxes are omitted.
- dxf2pdf: no pen tables, Helvetica instead of drawing fonts, text sits above geometry in the page stack, hatch
  patterns follow ezdxf's rendering, gradients are solid, images are skipped.
