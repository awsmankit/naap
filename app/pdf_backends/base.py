"""Backend-neutral PDF page model.

Everything outside a backend module works only with these plain types, so the PDF library
can be swapped (PyMuPDF is AGPL/commercial; pypdfium2 or pdf.js are alternatives, see
engine_struct/INTEGRATION.md).

Coordinates: the *displayed* page (page /Rotate applied), origin top-left, y down, in PDF
points (1/72 inch). app/pdf_extract.py flips y to the CAD convention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

Point = tuple[float, float]


@dataclass
class PdfPath:
    """One painted path. segs: ("l", [p0, p1]) | ("c", [p0, c1, c2, p3]) | ("re", [p0, p1, p2, p3])
    ("re" = a rectangle given as its four corners in drawing order)."""
    segs: list[tuple[str, list[Point]]]
    layer: str = ""                 # optional-content group (OCG) name, "" if none
    stroke: tuple | None = None     # RGB 0..1 or None (not stroked)
    fill: tuple | None = None       # RGB 0..1 or None (not filled)
    width: float = 0.0
    closed: bool = False
    dashed: bool = False
    seq: int = 0                    # paint order on the page (stable for a given file)


@dataclass
class TextSpan:
    text: str
    bbox: tuple[float, float, float, float]
    origin: Point                   # baseline start
    dir: Point                      # unit writing direction in display space (y down)
    size: float
    font: str = ""
    layer: str = ""
    seq: int = 0


@dataclass
class PdfAnnot:
    """A page annotation (comment). AutoCAD's 'SHX text as comments' export puts the text of SHX
    fonts (drawn as strokes) into invisible /Square annotations titled 'AutoCAD SHX Text'."""
    kind: str                       # annotation subtype, e.g. "Square", "FreeText", "Text"
    rect: tuple[float, float, float, float]   # displayed page coordinates (y down)
    text: str = ""                  # /Contents
    title: str = ""                 # /T (author / title)
    subject: str = ""               # /Subj


@dataclass
class PdfPageInfo:
    number: int                     # 1-based
    width: float
    height: float
    rotation: int = 0


class PdfPage(Protocol):
    info: PdfPageInfo

    def paths(self) -> list[PdfPath]: ...
    def text_spans(self) -> list[TextSpan]: ...
    def images(self) -> list[tuple[float, float, float, float]]: ...
    def annotations(self) -> list[PdfAnnot]: ...      # optional: backends without it return []
    def ocg_layers(self) -> list[str]: ...
    def render_png(self, clip: tuple[float, float, float, float] | None = None, dpi: float = 72,
                   layers: Iterable[str] | None = None) -> bytes: ...
    def render_gray(self, clip: tuple[float, float, float, float] | None = None, dpi: float = 72,
                    layers: Iterable[str] | None = None) -> tuple[int, int, bytes]: ...
        # (width, height, 8-bit grey samples row by row); layers=None renders everything,
        # otherwise only those OCG layers (backends without OCG control raise RenderUnsupported)


class PdfDocument(Protocol):
    page_count: int

    def page(self, index: int) -> PdfPage: ...          # 0-based index
    def ocg_layers(self) -> list[str]: ...
    def close(self) -> None: ...


@dataclass
class RenderUnsupported(Exception):
    reason: str = "not supported by this PDF backend"


@dataclass
class BackendInfo:
    name: str
    licence: str
    features: list[str] = field(default_factory=list)
