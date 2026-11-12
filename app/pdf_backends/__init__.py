"""Pluggable PDF readers. Select with PDF_BACKEND=<name> (default "pymupdf").

Only the backend modules import a PDF library; callers get the plain types of base.py.
"""
from __future__ import annotations

import importlib
import os

BACKENDS = {"pymupdf": "app.pdf_backends.pymupdf_backend"}


def open_pdf(path, backend: str | None = None):
    name = backend or os.environ.get("PDF_BACKEND", "pymupdf")
    if name not in BACKENDS:
        raise ValueError(f"unknown PDF backend {name!r}; available: {sorted(BACKENDS)}")
    return importlib.import_module(BACKENDS[name]).open_pdf(str(path))


def open_writer(backend: str | None = None):
    """A new, empty PDF writer (see base.PdfWriter) from the selected backend."""
    name = backend or os.environ.get("PDF_BACKEND", "pymupdf")
    if name not in BACKENDS:
        raise ValueError(f"unknown PDF backend {name!r}; available: {sorted(BACKENDS)}")
    return importlib.import_module(BACKENDS[name]).open_writer()
