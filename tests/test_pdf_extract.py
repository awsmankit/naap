"""PDF extraction on synthetic vector/raster PDFs written in-test. The PDFs are written by hand
so the tests do not depend on any PDF library beyond the backend under test."""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from app.pdf_extract import ScannedPDFError, extract_pdf, fix_text, floor_scope, merge_spans, parse_title_block



# ---------------------------------------------------------------- tiny PDF writer

def write_pdf(path: Path, content: str, size=(842, 595), image: bytes | None = None, img_wh=(8, 8), rotate: int = 0,
              ocg: str | None = None) -> Path:
    """One-page PDF. content = page content stream (PDF operators). Helvetica is /F1.
    image = raw 8-bit grey samples for /Im0. ocg = name of an optional-content group /L0."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    cat = add(b"")  # placeholder 1
    pages = add(b"")  # 2
    font = add(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    res = [f"/Font<</F1 {font} 0 R>>"]
    if image is not None:
        w, h = img_wh
        im = add(f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}/ColorSpace/DeviceGray/BitsPerComponent 8"
                 f"/Length {len(image)}>>stream\n".encode() + image + b"\nendstream")
        res.append(f"/XObject<</Im0 {im} 0 R>>")
    ocprops = ""
    if ocg:
        oc = add(f"<</Type/OCG/Name({ocg})>>".encode())
        res.append(f"/Properties<</L0 {oc} 0 R>>")
        ocprops = f"/OCProperties<</OCGs[{oc} 0 R]/D<</ON[{oc} 0 R]/Order[{oc} 0 R]>>>>"
    data = zlib.compress(content.encode("latin-1"))
    cont = add(f"<</Length {len(data)}/Filter/FlateDecode>>stream\n".encode() + data + b"\nendstream")
    page = add(f"<</Type/Page/Parent {pages} 0 R/MediaBox[0 0 {size[0]} {size[1]}]/Rotate {rotate}"
               f"/Resources<<{''.join(res)}>>/Contents {cont} 0 R>>".encode())
    objs[cat - 1] = f"<</Type/Catalog/Pages {pages} 0 R{ocprops}>>".encode()
    objs[pages - 1] = f"<</Type/Pages/Kids[{page} 0 R]/Count 1>>".encode()
    out = bytearray(b"%PDF-1.5\n")
    offs = []
    for i, o in enumerate(objs, start=1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objs) + 1}/Root {cat} 0 R>>\nstartxref\n{x}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


def text(x, y, s, size=10):
    return f"BT /F1 {size} Tf {x} {y} Td ({s}) Tj ET\n"


def circle(cx, cy, r):
    k = 0.5523 * r
    return (f"{cx + r} {cy} m {cx + r} {cy + k} {cx + k} {cy + r} {cx} {cy + r} c "
            f"{cx - k} {cy + r} {cx - r} {cy + k} {cx - r} {cy} c {cx - r} {cy - k} {cx - k} {cy - r} {cx} {cy - r} c "
            f"{cx + k} {cy - r} {cx + r} {cy - k} {cx + r} {cy} c S\n")


def vector_sheet(tmp: Path, rotate=0) -> Path:
    c = "0.5 w\n"
    c += "100 100 m 300 100 l S\n"                          # a line
    c += "100 150 m 200 150 l 200 250 l h S\n"              # a polyline
    c += circle(400, 300, 10)                               # a circle
    c += "500 100 20 30 re f\n"                             # filled rect
    c += "/OC /L0 BDC 50 50 m 60 60 l S EMC\n"              # line on an OCG layer
    # title block
    c += text(600, 120, "DRG. NO.", 6) + text(600, 105, "ABC-STR-07", 9)
    c += text(600, 90, "DRG. TITLE:", 6) + text(600, 78, "SLAB AND BEAM DETAIL", 8) + text(600, 68, "(2nd to 6th floor)", 8)
    c += text(600, 50, "SCALE", 6) + text(600, 40, "NOT TO SCALE", 8)
    # glyph-split tag: 'B' and '3' emitted as two text objects
    c += text(300, 400, "B", 12) + text(307.5, 400, "3", 12)
    return write_pdf(tmp / f"vec{rotate}.pdf", c, rotate=rotate, ocg="BEAM")


# ---------------------------------------------------------------- unit tests

def test_floor_scope_and_fix_text():
    assert floor_scope("SLAB AND BEAM DETAIL (2nd to 6th  floor)")["multiplier"] == 5
    assert floor_scope("SLAB AND BEAM DETAIL (GROUND FLOOR)") == {"from": 0, "to": 0, "multiplier": 1, "label": "Ground floor"}
    assert floor_scope("1st to 6th FLOOR SLAB")["multiplier"] == 6
    assert floor_scope("COLUMN DETAIL") is None
    assert fix_text("\x14\x18\x13") == "150"
    assert fix_text("B12") == "B12"


def test_vector_pdf_extraction(tmp_path):
    g = extract_pdf(vector_sheet(tmp_path))
    assert g["source"] == "pdf" and g["units"] == "pt" and len(g["sheets"]) == 1
    ts = {e["t"] for e in g["entities"]}
    assert {"L", "P", "C", "H", "T"} <= ts
    c = next(e for e in g["entities"] if e["t"] == "C")
    assert c["c"] == [400.0, 300.0] and abs(c["r"] - 10) < 0.05
    line = next(e for e in g["entities"] if e["t"] == "L" and g["layers"][e["l"]]["name"] == "BEAM")
    assert line["p"] == [50.0, 50.0, 60.0, 60.0]
    assert all(e["handle"].startswith("p1:") and e["sheet"] == 1 for e in g["entities"])
    assert len({e["handle"] for e in g["entities"]}) == len(g["entities"])
    texts = [e["s"] for e in g["entities"] if e["t"] == "T"]
    assert "B3" in texts, texts                                  # glyph runs merged
    tb = g["sheets"][0]["title_block"]
    assert tb["drawing_no"] == "ABC-STR-07"
    assert tb["not_to_scale"] and tb["floor_scope"]["multiplier"] == 5


def test_rotated_page_is_upright(tmp_path):
    """A /Rotate 90 page: geometry is returned as displayed (y up), not as stored."""
    g0 = extract_pdf(vector_sheet(tmp_path, 0))
    g9 = extract_pdf(vector_sheet(tmp_path, 90))
    assert g9["sheets"][0]["size"] == [595.0, 842.0]
    c0 = next(e for e in g0["entities"] if e["t"] == "C")
    c9 = next(e for e in g9["entities"] if e["t"] == "C")
    # /Rotate 90 turns the page clockwise: user (x, y) shows at (y, W - x), y up
    assert c9["c"] == [c0["c"][1], 842 - c0["c"][0]]


def test_scanned_pdf_is_rejected(tmp_path):
    img = bytes((i * 7) % 256 for i in range(64 * 64))
    p = write_pdf(tmp_path / "scan.pdf", "q 842 0 0 595 0 0 cm /Im0 Do Q\n", image=img, img_wh=(64, 64))
    with pytest.raises(ScannedPDFError, match="scanned PDF not supported yet"):
        extract_pdf(p)


def test_cli_reports_scanned(tmp_path, capsys):
    from engine_struct.cli import main
    img = bytes(64 * 64)
    p = write_pdf(tmp_path / "scan.pdf", "q 842 0 0 595 0 0 cm /Im0 Do Q\n", image=img, img_wh=(64, 64))
    assert main([str(p), "--out", str(tmp_path / "x.xlsx")]) == 2
    assert "scanned PDF not supported yet" in capsys.readouterr().err


def test_no_pymupdf_outside_backend():
    """Licensing: only app/pdf_backends/pymupdf_backend.py may import PyMuPDF."""
    root = Path(__file__).resolve().parents[1]
    bad = []
    for d in ("app", "engine_struct", "engine"):
        for f in (root / d).rglob("*.py"):
            if f.name == "pymupdf_backend.py":
                continue
            s = f.read_text(errors="ignore")
            if "import fitz" in s or "import pymupdf" in s or "from pymupdf" in s or "from fitz" in s:
                bad.append(str(f))
    assert not bad, bad


