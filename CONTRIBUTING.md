# Contributing to Naap

Thanks for helping. Issues and pull requests are welcome.

## Set up

```sh
uv sync                      # Python 3.12+, installs the dev tools too
uv run pytest -q             # the public test suite
uv run python -m app         # http://127.0.0.1:8777
```

DWG uploads need LibreDWG's `dwg2dxf` (Homebrew: `brew install libredwg`). Without it, DXF and PDF
still work and the DWG test is skipped.

## Rules

- **Never commit real project drawings or anything derived from them.** That includes drawings,
  PDFs, workbooks, screenshots, job folders, and numbers, names or coordinates copied from a client
  drawing. Tests use the synthetic fixtures (`tests/fixtures/make_synthetic.py`), the sample
  (`samples/make_sample.py`) or drawings built inside the test. Checks against your own drawings go
  in `tests/private/`, which is git-ignored. Use absolute paths there and skip when the file is
  missing.
- Keep the engine generic. Nothing may be hard-coded to one drawing. If the drawing does not give a
  value, raise a flag. Don't guess silently.
- Every MB row needs `remarks`, a `status` and `refs` that point back to the drawing.
- Only `app/pdf_backends/pymupdf_backend.py` may import PyMuPDF (a test checks this).
- After engine changes, rebuild the web mock with `uv run python web/mock/build_mock.py`.
  `tests/test_sample.py` fails if it is stale.
- Keep pull requests small and add a test for each behaviour you change.

By contributing you agree that your contribution is licensed under the AGPL-3.0 (see `LICENSE`).
