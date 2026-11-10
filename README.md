# Naap (नाप)

**Drawing → Measurement Book.** Naap turns a DWG, DXF or PDF drawing into a CPWD-style Measurement Book
(MB). Every quantity in the book can be traced back to the drawing.

*Naap* is Hindi for "measurement". It is a local web app. You upload a drawing, review what the engine
read and fix what the drawing leaves open, then export an Excel workbook. Uploaded drawings are not
sent to any external service.

> **Naap gives a quantity book taken from the drawing. It is not a measurement book of work executed
> on site.** Quantities follow the drawing and the answers you enter. Check them against the site and
> the contract before you certify or pay anything.

## Screenshots

<!-- Add public, client-free images under docs/media/ (other image files are git-ignored). -->
*Coming soon: a short GIF of the sample house going from upload to review to export.*

## Features

- **Traceable quantities.** Every MB row has Nos × L × B × H, remarks that say where each value came
  from, a status (`ok`, `assumed` or `unresolved`) and references (handles and a bounding box) to the
  entities it was measured from. Click a row to see it on the drawing. Click the drawing to find its
  rows. Row ids stay the same when you rebuild.
- **Flags instead of guesses.** If something is wrong or missing in the drawing, Naap raises a flag and
  does not quietly invent a value. Examples:
  - a room label that does not match the walls around it
  - an opening size that differs between the schedule and the plan
  - a tag that is in the schedule but not on the plan, or on the plan but not in the schedule
  - a wall gap with no tag
  - a dimension whose text has been overridden
  - an xref or underlay that was not measured
  - an element drawn twice
  - wall heights, slab depth, beam depth, lintel size or finishes that are not given

  You answer a flag in the UI (or in the config) and the answer goes into the next build. Rows that
  depend on an open flag say so.
- **IS 1200 / CPWD deductions.** Brickwork stops under beams, and RCC columns and lintels inside the
  masonry are deducted. Plaster follows the IS 1200 Part 12 rules for openings. Each rule can be
  switched off in the config, and the remarks cite the rule.
- **Multi-storey drawings.** Plans are split by paper-space viewports. Floors are read from titles such
  as "TYPICAL (2nd-4th) FLOOR PLAN" or "6 TO 9", and storey heights from level labels. Typical floors
  are multiplied, and the book is listed floor by floor with an abstract per floor.
- **Structural PDFs with steel.** From vector PDF sheets, Naap reads beam, slab, column and footing
  schedules, beams between columns, slab panels, notes (grade, covers, development lengths) and the
  sheet scale from written dimensions. It then gives concrete and reinforcement quantities per bar
  diameter.
- **Excel export.** The workbook has these sheets: MB, Abstract (plus Abstract by floor for
  multi-storey drawings), Room schedule, Door-Window schedule, All dimensions, Assumptions and Flags. Quantities and totals are Excel
  formulas.

## Quick start

### Local

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). DWG input also needs
[LibreDWG](https://www.gnu.org/software/libredwg/) (`brew install libredwg` on macOS). DXF and PDF
input work without it.

```sh
git clone https://github.com/awsmankit/naap && cd naap
uv sync
uv run python -m app            # http://127.0.0.1:8777 (localhost only)
```

Settings (environment variables):

| Variable | Default | What it sets |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. The app has no login, so read `SECURITY.md` before you change it. |
| `PORT` | `8777` | Port. |
| `DWG_MB_DATA` | `data/jobs/` | Folder for uploads and job results. |
| `DWG2DXF` | `dwg2dxf` on `PATH` | Path to LibreDWG's `dwg2dxf`. |
| `DWG_MB_MAX_UPLOAD_MB` | `300` | Upload size limit. |
| `DWG_MB_MAX_PDF_MB` | `100` | PDF size limit. |
| `MB_ENGINE` | `engine.mb` | Swap in another engine module. |

### Docker

```sh
docker compose up --build       # http://localhost:8000, jobs kept in the naap-data volume
```

The image builds LibreDWG from source in a separate stage, so DWG uploads work in the container.

### Command line

```sh
uv run python -m engine.cli drawing.dwg --out mb.xlsx --flags-json flags.json [--config answers.json]
```

## Try the sample

`samples/sample-house.dxf` is a made-up single-storey 2BHK house, written by
`samples/make_sample.py`. It has:

- 230 and 115 mm walls with legend hatches, and corner RCC columns
- door and window tags with a schedule, room labels and dimensions
- a terrace plan with a parapet note

One room label is wrong on purpose.

1. Start the app and click **Process the sample house** on the upload page. You can also drop the
   file in, or open `?mock=1` to see a saved run without the server.
2. **Review.** Naap finds three views: GROUND FLOOR PLAN, TERRACE PLAN and DOOR / WINDOW SCHEDULE. It
   also guesses the layer roles. The Flags tab shows:
   - an **error**: *BEDROOM-2: labelled 3600X3000 but drawn 3600 x 3115*
   - a **warning**: the roof plan could not be placed over the floor plan, because there is no
     section to align them
   - **missing** items: wall height, slab, beam depth, lintel and floor finishes
3. Answer the flags. For example, enter `3600X3115` for the bedroom and a wall height. Watch the
   affected rows change.
4. **Export** the workbook. The book has 57 rows over 12 abstract items, among them 230 brickwork,
   half-brick, parapet, roof slab, plaster, coping, coba, khurras and openings.

## How measurement works

1. **Read.** DWG files are converted to DXF with LibreDWG. DXF is read with ezdxf, and blocks,
   dimensions and multilines are exploded. PDFs are read through a pluggable backend (PyMuPDF by
   default).
2. **Views and roles.** Entities are grouped into views (plans, sections, elevations and schedules)
   and named from their titles. Layer roles (brick or AAC wall, parapet, column, door, window, room
   text, dimension and so on) are guessed from the layer names. You confirm the views and roles in
   Review.
3. **Measure.** Plans are rasterised. Masonry comes from the wall hatches named in the legend and from
   the wall lines. Tags are matched to wall gaps of the scheduled width, and rooms are the enclosed
   spaces that their labels fall in. Wall faces give the plaster areas. The terrace plan gives the
   slab, parapet, coping, coba and khurras. Written dimensions take precedence over measured lengths.
4. **Check and flag.** The generic checks shown above run on every drawing. Nothing is tuned to one
   drawing.
5. **Book.** You get the MB rows, an abstract (also per floor), schedules, and the assumptions made.

### Limits

- The result is only as good as the drawing. Messy layers, exploded hatches or labels placed away
  from their rooms need fixing in Review.
- Values that are not on the drawing are not invented. These include heights without a section,
  foundations, finishes, and beam or lintel sizes. They stay as flags or as stated defaults until
  you answer them.
- Scanned (raster) PDFs are rejected. PDFs whose text was plotted as strokes are flagged.
- Structural PDF reading expects ruled schedule tables and conventional notation. The quantities do
  not include a full bar bending schedule.
- Quantities do not include rates.

## Roadmap

- A browser-only version on GitHub Pages (no server; the drawing stays in the browser)
- CPWD DSR rates for the abstract, giving an estimate
- RA bills: measurements recorded against the book over time
- BBS (bar bending schedule) export from the structural reading
- Scanned PDFs through vectorisation or OCR

## Development

```sh
uv run pytest -q                 # public suite
uv run python samples/make_sample.py && uv run python web/mock/build_mock.py   # regenerate sample + UI mock
```

- `app/`: FastAPI app, job pipeline, DXF/PDF extraction, clustering and role guesses
- `engine/`: architectural measurement, checks and the xlsx export
- `engine_struct/`: structural PDF engine
- `web/`: the UI. It is plain ES modules with self-hosted fonts and has no build step (see
  `web/README.md`).
- `tests/`: public tests on synthetic drawings
- `tests/private/`: your own drawings (git-ignored)

The engine interface is `engine.mb.build_mb(geometry, user_config) -> MBResult`. `engine/types.py`
defines the result types, `engine/mb.py` lists the config keys, and `engine_struct/INTEGRATION.md`
describes the PDF side. See `CONTRIBUTING.md` for the house rules, especially the one about never
committing client drawings.

## Licence

Naap is licensed under [AGPL-3.0](LICENSE). It uses PyMuPDF (AGPL-3.0), ezdxf, openpyxl and FastAPI
(MIT), and runs LibreDWG (GPL-3.0) as an external program. The fonts are under OFL-1.1 and LGPL-3.0
with the font exception. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
