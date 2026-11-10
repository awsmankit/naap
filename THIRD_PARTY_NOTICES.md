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

## Bundled in the browser version (the GitHub Pages site built by `scripts/build_site.py`)

The site in `site/` (not in this repository) contains these files unchanged. Each download is
pinned by SHA-256 in `scripts/build_site.py`. Licence texts are copied to `site/licenses/`: each
wheel's own licence files, plus the Pyodide, CPython and LibreDWG texts listed below.

| Component | Licence | What is bundled |
|---|---|---|
| [Pyodide](https://github.com/pyodide/pyodide) 0.29.5 | MPL-2.0 (`licenses/pyodide/LICENSE`) | The CPython-on-WebAssembly runtime: `pyodide.mjs`, `pyodide.asm.js`, `pyodide.asm.wasm`, `python_stdlib.zip`. |
| CPython 3.13 (inside Pyodide) | PSF License (`licenses/cpython/LICENSE`) | The Python interpreter and standard library. |
| NumPy, SciPy | BSD-3-Clause | Pyodide's WebAssembly wheels. SciPy is trimmed to the subpackages the engine imports (ndimage, spatial and what they need). |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) 1.26.3 (MuPDF) | AGPL-3.0 | Pyodide's WebAssembly wheel. Loaded only when a PDF is opened. |
| ezdxf, openpyxl, et-xmlfile, fontTools, pyparsing, typing-extensions | MIT, MIT, MIT, MIT, MIT, PSF | Pure-Python wheels. |
| [GNU LibreDWG](https://www.gnu.org/software/libredwg/), compiled to WebAssembly by [@mlightcad/libredwg-web](https://github.com/mlightcad/libredwg-web) 0.8.8 | GPL-3.0-or-later (`licenses/libredwg/COPYING`) | `vendor/libredwg/libredwg-web.{js,wasm}`, which turn a DWG into DXF in the browser. The corresponding source is the libredwg-web repository at tag `v0.8.8` (commit `26f9929134c88d6058bd48d201efebe638e21d89`). See `licenses/libredwg/SOURCE.txt`. Naap (AGPL-3.0) is compatible with the GPL-3.0. Anyone who distributes the site distributes LibreDWG under the GPL-3.0 as well. |

pdf.js is not used: PyMuPDF's WebAssembly build reads PDFs in the browser through the same backend
(`app/pdf_backends/pymupdf_backend.py`) as the server.

## Fonts (self-hosted in `web/fonts/`)

| Font | Licence | Licence file |
|---|---|---|
| Barlow and Barlow Semi Condensed (The Barlow Project Authors) | SIL Open Font License 1.1 | `web/fonts/Barlow-OFL.txt` |
| osifont, ISO 3098 CAD lettering (Zefram Cochrane, hikikomori82) | GNU LGPL-3.0 with the GPL font exception | `web/fonts/osifont-LICENSE.txt` (notice, font exception, LGPL-3.0 and GPL-3.0 texts) |

The fonts are served unchanged as WOFF2 files. Naap loads no fonts, scripts, styles or runtime files from CDNs. The browser version fetches only files from its own site.
