"""Synthetic drawings for the edge-case tests (no client data; usable as a public sample).

    uv run python tests/fixtures/make_synthetic.py      # writes tests/fixtures/synthetic/*

A two-room single-storey plan (10230 x 5230 outside, 230 brick walls drawn as wall lines):

    ROOM A 4770X4770 | ROOM B 4770X4770, door D1 (900X2100) in the partition, window W1 (1500X1200)
    in the south wall of room A, an RCC column 230x450 embedded in the partition, a beam (two
    A-BEAM lines) over the north wall, an overall dimension with its text overridden to 4550 over a
    4500 line, and a '<>' dimension. A door/window schedule sits in its own view to the right.

Variants:
    plan_base.dxf         the plan above
    plan_nobeam.dxf       without the beam lines
    plan_xref.dxf         + an xref insert (unloaded), an attached image, a PDF underlay and a line on A-XREF
    plan_dup_inplace.dxf  + every wall line, the D1 tag and the ROOM A label drawn a second time in place
    plan_copy.dxf         + a stale copy of the whole plan 25 m to the right ("GROUND FLOOR PLAN - OLD")
    shx_strokes.pdf       a sheet whose text was plotted as strokes (no text layer, no comments)
    shx_comments.pdf      the same strokes plus 'AutoCAD SHX Text' comments carrying the strings
"""
from __future__ import annotations

import sys
import zlib
from pathlib import Path

import ezdxf

OUT = Path(__file__).parent / "synthetic"

T = 230
W, H = 10230, 5230
PART = (5000, 5230)            # partition faces (x)
DOOR = (2000, 2900)            # D1 opening in the partition (y)
WIN = (1500, 3000)             # W1 opening in the south wall (x)


def wall_lines() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Wall faces as LINEs, each split in two (short lines keep the view clustering stable)."""
    out = []
    for (x0, y0), (x1, y1) in _wall_faces():
        if abs(x1 - x0) + abs(y1 - y0) > 2000:
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            out += [((x0, y0), (xm, ym)), ((xm, ym), (x1, y1))]
        else:
            out.append(((x0, y0), (x1, y1)))
    return out


def _wall_faces() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    L = []
    # outer faces (window gap in the south face)
    L += [((0, 0), (WIN[0], 0)), ((WIN[1], 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))]
    # room A inner faces
    L += [((T, T), (WIN[0], T)), ((WIN[1], T), (PART[0], T)), ((T, T), (T, H - T)), ((T, H - T), (PART[0], H - T)),
          ((PART[0], T), (PART[0], DOOR[0])), ((PART[0], DOOR[1]), (PART[0], H - T))]
    # room B inner faces
    L += [((PART[1], T), (W - T, T)), ((W - T, T), (W - T, H - T)), ((W - T, H - T), (PART[1], H - T)),
          ((PART[1], T), (PART[1], DOOR[0])), ((PART[1], DOOR[1]), (PART[1], H - T))]
    # jambs
    L += [((WIN[0], 0), (WIN[0], T)), ((WIN[1], 0), (WIN[1], T)),
          ((PART[0], DOOR[0]), (PART[1], DOOR[0])), ((PART[0], DOOR[1]), (PART[1], DOOR[1]))]
    return L


def _layers(doc):
    for name, col in (("A-WALL", 1), ("A-TEXT DETAIL", 2), ("A-DIM", 3), ("A-DW TAG", 4), ("A-COLUMN", 5),
                      ("A-BEAM", 6), ("A-WINDOW", 4), ("A-TEXT", 7), ("A-XREF", 8)):
        doc.layers.add(name, color=col)


def draw_plan(msp, dx: float = 0.0, title: str = "GROUND FLOOR PLAN", beam: bool = True, dims: bool = True):
    def P(x, y):
        return (x + dx, y)
    for a, b in wall_lines():
        msp.add_line(P(*a), P(*b), dxfattribs={"layer": "A-WALL"})
    # window glazing line in the W1 gap
    msp.add_line(P(WIN[0], T / 2), P(WIN[1], T / 2), dxfattribs={"layer": "A-WINDOW"})
    # embedded RCC column 230 x 450 in the partition (wall lines run past it)
    msp.add_lwpolyline([P(PART[0], 4550), P(PART[1], 4550), P(PART[1], 5000), P(PART[0], 5000)], close=True,
                       dxfattribs={"layer": "A-COLUMN"})
    if beam:
        # beam projection over the north wall (two face lines)
        msp.add_line(P(0, H - T), P(W, H - T), dxfattribs={"layer": "A-BEAM"})
        msp.add_line(P(0, H), P(W, H), dxfattribs={"layer": "A-BEAM"})
    for name, cx in (("ROOM A", (T + PART[0]) / 2), ("ROOM B", (PART[1] + W - T) / 2)):
        msp.add_mtext(f"{name}\\P4770X4770", dxfattribs={"layer": "A-TEXT DETAIL", "insert": P(cx, 2615),
                                                          "char_height": 150, "attachment_point": 5})
    msp.add_text("D1", dxfattribs={"layer": "A-DW TAG", "insert": P(5400, 2400), "height": 150})
    msp.add_text("W1", dxfattribs={"layer": "A-DW TAG", "insert": P(2200, 450), "height": 150})
    msp.add_text(title, dxfattribs={"layer": "A-TEXT", "insert": P(0, -600), "height": 300})
    if dims:
        # written 4550 over a 4500 line (override), and an automatic overall dimension
        d = msp.add_linear_dim(base=P(0, 6000), p1=P(0, H), p2=P(4500, H), text="4550", dxfattribs={"layer": "A-DIM"})
        d.render()
        d = msp.add_linear_dim(base=P(0, 6600), p1=P(0, H), p2=P(W, H), text="<>", dxfattribs={"layer": "A-DIM"})
        d.render()


def draw_schedule(msp, x0: float = 40000.0):
    msp.add_text("DOOR / WINDOW SCHEDULE", dxfattribs={"layer": "A-TEXT", "insert": (x0, 3000), "height": 300})
    msp.add_text("D1 (900X2100)", dxfattribs={"layer": "A-TEXT", "insert": (x0, 2000), "height": 150})
    msp.add_text("W1 (1500X1200)", dxfattribs={"layer": "A-TEXT", "insert": (x0, 1500), "height": 150})
    for k in range(4):
        msp.add_line((x0 - 200, 1300 + k * 600), (x0 + 4000, 1300 + k * 600), dxfattribs={"layer": "A-TEXT"})
    for x in (x0 - 200, x0 + 1900, x0 + 4000):
        msp.add_line((x, 1300), (x, 3100), dxfattribs={"layer": "A-TEXT"})


def new_doc():
    doc = ezdxf.new("R2010", units=4)
    _layers(doc)
    return doc


def make_base(path: Path, beam: bool = True) -> Path:
    doc = new_doc()
    msp = doc.modelspace()
    draw_plan(msp, beam=beam)
    draw_schedule(msp)
    doc.saveas(path)
    return path


def make_xref(path: Path) -> Path:
    doc = new_doc()
    msp = doc.modelspace()
    draw_plan(msp)
    draw_schedule(msp)
    doc.add_xref_def("..\\xrefs\\STRUCT-GRID.dwg", "STRUCT-GRID")
    msp.add_blockref("STRUCT-GRID", (0, 0), dxfattribs={"layer": "A-XREF"})
    img = doc.add_image_def("site_photo.jpg", (640, 480))
    msp.add_image(img, (12000, 0), (4000, 3000), dxfattribs={"layer": "A-XREF"})
    und = doc.add_underlay_def("survey.pdf", fmt="pdf", name="1")
    msp.add_underlay(und, (0, 8000), dxfattribs={"layer": "A-XREF"})
    msp.add_line((1000, 1000), (4000, 1000), dxfattribs={"layer": "A-XREF"})
    doc.saveas(path)
    return path


def make_dup_inplace(path: Path) -> Path:
    doc = new_doc()
    msp = doc.modelspace()
    draw_plan(msp)
    draw_schedule(msp)
    for a, b in wall_lines():                      # the wall set drawn a second time in place
        msp.add_line(a, b, dxfattribs={"layer": "A-WALL"})
    msp.add_text("D1", dxfattribs={"layer": "A-DW TAG", "insert": (5400, 2400), "height": 150})
    msp.add_mtext("ROOM A\\P4770X4770", dxfattribs={"layer": "A-TEXT DETAIL", "insert": ((T + PART[0]) / 2, 2615),
                                                     "char_height": 150, "attachment_point": 5})
    doc.saveas(path)
    return path


def make_copy(path: Path) -> Path:
    doc = new_doc()
    msp = doc.modelspace()
    draw_plan(msp)
    draw_plan(msp, dx=25000, title="GROUND FLOOR PLAN - OLD", dims=False)
    draw_schedule(msp, x0=60000)
    doc.saveas(path)
    return path


# ---------------------------------------------------------------- PDFs (written by hand, no PDF library)
def _glyph_strokes(x: float, y: float, s: float) -> list[tuple[float, float, float, float]]:
    """A few short strokes the size of one SHX glyph at (x, y), size s (pt)."""
    return [(x, y, x, y + s), (x, y + s, x + 0.6 * s, y + s), (x + 0.6 * s, y + s, x + 0.6 * s, y + 0.5 * s),
            (x, y + 0.5 * s, x + 0.6 * s, y + 0.5 * s)]


def _words():
    """(text, x, y, size) of the 'labels' drawn as strokes on the SHX sheet (PDF coords, y up)."""
    out = []
    labels = ["D1", "W1", "ROOM A", "4770X4770", "B1 230X450", "S1 150 THK", "C1", "C2", "GROUND FLOOR PLAN"]
    for k in range(60):
        t = labels[k % len(labels)]
        out.append((t, 60 + (k % 6) * 120, 60 + (k // 6) * 45, 8.0))
    return out


def write_pdf(path: Path, content: str, annots: list[dict] | None = None, size=(842, 595)) -> Path:
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    cat = add(b"")
    pages = add(b"")
    data = zlib.compress(content.encode("latin-1"))
    cont = add(f"<</Length {len(data)}/Filter/FlateDecode>>stream\n".encode() + data + b"\nendstream")
    aref = []
    for an in annots or []:
        x0, y0, x1, y1 = an["rect"]
        aref.append(add(f"<</Type/Annot/Subtype/Square/Rect[{x0} {y0} {x1} {y1}]/F 2/C[]/BS<</W 0>>"
                        f"/Contents({an['text']})/T({an.get('title', 'AutoCAD SHX Text')})>>".encode()))
    annots_s = f"/Annots[{' '.join(f'{k} 0 R' for k in aref)}]" if aref else ""
    page = add(f"<</Type/Page/Parent {pages} 0 R/MediaBox[0 0 {size[0]} {size[1]}]/Resources<<>>"
               f"/Contents {cont} 0 R{annots_s}>>".encode())
    objs[cat - 1] = f"<</Type/Catalog/Pages {pages} 0 R>>".encode()
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


def shx_content() -> str:
    ops = ["0.3 w 0 0 0 RG"]
    # the plan: a few long lines
    for x0, y0, x1, y1 in ((40, 40, 800, 40), (800, 40, 800, 560), (800, 560, 40, 560), (40, 560, 40, 40)):
        ops.append(f"{x0} {y0} m {x1} {y1} l S")
    for t, x, y, s in _words():
        for k, _ in enumerate(t):
            for a, b, c, d in _glyph_strokes(x + k * 0.9 * s, y, s):
                ops.append(f"{a:.2f} {b:.2f} m {c:.2f} {d:.2f} l S")
    return "\n".join(ops)


def make_shx_pdfs(out: Path) -> tuple[Path, Path]:
    c = shx_content()
    p1 = write_pdf(out / "shx_strokes.pdf", c)
    ann = [{"rect": (x, y, x + 0.9 * s * len(t), y + s), "text": t} for t, x, y, s in _words()]
    p2 = write_pdf(out / "shx_comments.pdf", c, ann)
    return p1, p2


def main(out: Path = OUT) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = [make_base(out / "plan_base.dxf"), make_base(out / "plan_nobeam.dxf", beam=False),
             make_xref(out / "plan_xref.dxf"), make_dup_inplace(out / "plan_dup_inplace.dxf"),
             make_copy(out / "plan_copy.dxf"), *make_shx_pdfs(out)]
    return files


if __name__ == "__main__":
    for f in main(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT):
        print(f)
