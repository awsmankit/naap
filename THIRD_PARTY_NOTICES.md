# Third-party notices

Naap is licensed under the GNU Affero General Public License v3.0 (see `LICENSE`). It uses the
components below. Versions are pinned in `uv.lock`; check each project for its current terms.

## Runtime dependencies (installed by `uv sync`, not vendored in this repository)

| Component | Licence | How Naap uses it |
|---|---|---|
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) (MuPDF) | AGPL-3.0 (commercial licences from Artifex) | Reads vector PDFs and renders sheet images. Only `app/pdf_backends/pymupdf_backend.py` imports it (a test enforces this), so another backend can replace it. |
| [ezdxf](https://github.com/mozman/ezdxf) | MIT | Reads DXF files; the tests and `samples/make_sample.py` also write DXF with it. |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | MIT | Writes the Measurement Book workbook (.xlsx). |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Web API. |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI toolkit under FastAPI (static files, responses). |
| [uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI server. |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 | Parses file uploads. |
| [NumPy](https://numpy.org) / [SciPy](https://scipy.org) | BSD-3-Clause | Raster geometry (room spaces, wall faces, clustering). |
| [pytest](https://pytest.org), [HTTPX](https://www.python-httpx.org) (dev only) | MIT, BSD-3-Clause | Tests. |

## External program (not bundled, not linked)

| Component | Licence | How Naap uses it |
|---|---|---|
| [GNU LibreDWG](https://www.gnu.org/software/libredwg/) | GPL-3.0-or-later | Naap runs its `dwg2dxf` command as a separate process to turn `.dwg` uploads into DXF. It is not part of this repository and is not linked into Naap. Install it yourself (Homebrew `libredwg`, or build it from source). Set `DWG2DXF=/path/to/dwg2dxf` if it is not on `PATH`. The `Dockerfile` builds it from the official release tarball in a separate stage and copies the binaries and library into the image. If you distribute that image, you also distribute LibreDWG under the GPL-3.0, and its source is the tarball named in the `Dockerfile`. |

## Fonts (self-hosted in `web/fonts/`)

| Font | Licence | Licence file |
|---|---|---|
| Barlow and Barlow Semi Condensed (The Barlow Project Authors) | SIL Open Font License 1.1 | `web/fonts/Barlow-OFL.txt` |
| osifont, ISO 3098 CAD lettering (Zefram Cochrane, hikikomori82) | GNU LGPL-3.0 with the GPL font exception | `web/fonts/osifont-LICENSE.txt` (notice, font exception, LGPL-3.0 and GPL-3.0 texts) |

The fonts are served unchanged as WOFF2 files. Naap loads no fonts, scripts or styles from CDNs.
