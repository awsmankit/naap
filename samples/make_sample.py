"""Write samples/sample-house.dxf: a small, made-up single-storey 2BHK house for trying Naap.

    uv run python samples/make_sample.py [out.dxf]

Everything here is invented for the sample (no real project). Units are millimetres.

Ground floor plan (outer faces 10920 x 6805, 230 brick outer walls):

    +-------------+----------------+------------+
    | BEDROOM-2   |                | TOILET-2   |
    | 3600 x 3115 |                |------------|  <- 115 half-brick partition
    |-------------| LIVING/DINING  | TOILET-1   |
    | BEDROOM-1   | 4000 x 6345    |------------|
    | 3600 x 3000 |                | KITCHEN    |
    |             |                | 2400x3000  |
    +-------------+----------------+------------+

* walls: lines on A-WALL (230) and A-WALL 115, wall hatches (ANSI31) on A-HATCH WALL, a legend
  swatch "BRICK MASONRY" so the engine knows ANSI31 = brick
* RCC columns 230 x 450 on A-COLUMN at the four corners (outline + solid hatch)
* doors as blocks (leaf + swing) on A-DOOR, glazing lines on A-WINDOW, tags on A-DW TAG
* a door/window schedule table, room labels "NAME / WxL", dimensions, a terrace plan with a
  parapet outline and a parapet note
* one deliberately wrong label: BEDROOM-2 says 3600X3000 but is drawn 3600 x 3115, so the
  room-size check raises a flag
"""
from __future__ import annotations

import sys
from pathlib import Path

import ezdxf

OUT = Path(__file__).parent / "sample-house.dxf"

T, HB = 230, 115                 # full brick / half brick
X = [0, 230, 3830, 4060, 8060, 8290, 10690, 10920]   # vertical wall faces (x)
Y_OUT = 6805                     # outer north face
YW = [0, 230, 3230, 3460, 6575, 6805]               # west column wall faces (y)
YE = [0, 230, 3230, 3460, 4960, 5075, 6575, 6805]   # east column wall faces (y)

# openings: (tag, axis, wall position, from, to)  axis 'h' = in a horizontal wall at y, 'v' = vertical wall at x
DOORS = [("D1", "h", 0, 4560, 5560),                 # main door, living (south wall)
         ("D2", "v", X[2], 2000, 2900),              # bedroom-1
         ("D2", "v", X[2], 3800, 4700),              # bedroom-2
         ("D2", "v", X[4], 1000, 1900),              # kitchen
         ("D3", "v", X[4], 3600, 4350),              # toilet-1
         ("D3", "v", X[4], 5450, 6200)]              # toilet-2
WINDOWS = [("W1", "h", 0, 1300, 2800),               # bedroom-1 (south)
           ("W1", "h", Y_OUT, 1300, 2800),           # bedroom-2 (north)
           ("W1", "h", 0, 6300, 7800),               # living (south)
           ("W2", "v", X[7], 900, 2100),             # kitchen (east)
           ("V1", "v", X[7], 3910, 4510),            # toilet-1 ventilator
           ("V1", "v", X[7], 5525, 6125)]            # toilet-2 ventilator
SCHEDULE = [("D1", "1000X2100", "MAIN DOOR, TEAK PANELLED"), ("D2", "900X2100", "FLUSH DOOR"),
            ("D3", "750X2100", "PVC DOOR"), ("W1", "1500X1200", "ALUMINIUM SLIDING WINDOW"),
            ("W2", "1200X1200", "ALUMINIUM SLIDING WINDOW"), ("V1", "600X450", "LOUVERED VENTILATOR")]
ROOMS = [("BEDROOM-1", "3600X3000", (2030, 1730)),
         ("BEDROOM-2", "3600X3000", (2030, 5017)),        # deliberately wrong: drawn 3600 x 3115
         ("LIVING / DINING", "4000X6345", (6060, 3400)),
         ("KITCHEN", "2400X3000", (9490, 1730)),
         ("TOILET-1", "2400X1500", (9490, 4210)),
         ("TOILET-2", "2400X1500", (9490, 5825))]

LAYERS = (("A-WALL", 1), ("A-WALL 115", 1), ("A-HATCH WALL", 8), ("A-COLUMN", 5), ("A-DOOR", 3),
          ("A-WINDOW", 4), ("A-DW TAG", 6), ("A-TEXT DETAIL", 2), ("A-TEXT", 7), ("A-DIM", 3),
          ("A-WALL PARAPET", 1))


def _rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _cut(lo, hi, gaps):
    """Split the run lo..hi around the (a, b) gaps."""
    out, cur = [], lo
    for a, b in sorted(gaps):
        if b <= lo or a >= hi:
            continue
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return out


def walls() -> list[tuple[str, tuple[float, float, float, float]]]:
    """Wall rectangles (layer, x0, y0, x1, y1), cut at the openings."""
    ops = DOORS + WINDOWS
    hg = lambda y: [(a, b) for _, ax, p, a, b in ops if ax == "h" and p == y]  # noqa: E731
    vg = lambda x: [(a, b) for _, ax, p, a, b in ops if ax == "v" and p == x]  # noqa: E731
    W = []
    # outer walls
    for a, b in _cut(0, X[7], hg(0)):
        W.append(("A-WALL", (a, 0, b, T)))
    for a, b in _cut(0, X[7], hg(Y_OUT)):
        W.append(("A-WALL", (a, Y_OUT - T, b, Y_OUT)))
    W.append(("A-WALL", (0, T, T, Y_OUT - T)))
    for a, b in _cut(T, Y_OUT - T, vg(X[7])):
        W.append(("A-WALL", (X[6], a, X[7], b)))
    # internal 230 walls between the columns of rooms
    for a, b in _cut(T, Y_OUT - T, vg(X[2])):
        W.append(("A-WALL", (X[2], a, X[3], b)))
    for a, b in _cut(T, Y_OUT - T, vg(X[4])):
        W.append(("A-WALL", (X[4], a, X[5], b)))
    W.append(("A-WALL", (T, YW[2], X[2], YW[3])))          # bedroom-1 / bedroom-2
    W.append(("A-WALL", (X[5], YE[2], X[6], YE[3])))       # kitchen / toilet-1
    W.append(("A-WALL 115", (X[5], YE[4], X[6], YE[5])))   # toilet-1 / toilet-2 (half brick)
    return W


def draw_plan(msp):
    for layer, (x0, y0, x1, y1) in walls():
        # each wall as its two long faces plus end lines, hatched with the legend's brick pattern
        msp.add_lwpolyline(_rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": layer})
        h = msp.add_hatch(dxfattribs={"layer": "A-HATCH WALL"})
        h.set_pattern_fill("ANSI31", scale=20)
        h.paths.add_polyline_path(_rect(x0, y0, x1, y1), is_closed=True)
    # RCC columns 230 x 450 at the corners
    for x0, y0, x1, y1 in ((0, 0, 450, 230), (X[7] - 450, 0, X[7], 230), (0, Y_OUT - 230, 450, Y_OUT),
                           (X[7] - 450, Y_OUT - 230, X[7], Y_OUT)):
        msp.add_lwpolyline(_rect(x0, y0, x1, y1), close=True, dxfattribs={"layer": "A-COLUMN"})
        s = msp.add_hatch(color=8, dxfattribs={"layer": "A-COLUMN"})
        s.paths.add_polyline_path(_rect(x0, y0, x1, y1), is_closed=True)
    # doors: leaf + swing arc block, tag beside the opening
    for tag, ax, p, a, b in DOORS:
        w = b - a
        name = f"DOOR{w}"
        if name not in msp.doc.blocks:
            blk = msp.doc.blocks.new(name)
            blk.add_line((0, 0), (0, w))
            blk.add_arc((0, 0), w, 0, 90)
        if ax == "h":
            msp.add_blockref(name, (a, p + T), dxfattribs={"layer": "A-DOOR"})
            msp.add_text(tag, dxfattribs={"layer": "A-DW TAG", "insert": ((a + b) / 2 - 100, p + T + 250),
                                          "height": 150})
        else:
            msp.add_blockref(name, (p + T, a), dxfattribs={"layer": "A-DOOR", "rotation": 0})
            msp.add_text(tag, dxfattribs={"layer": "A-DW TAG", "insert": (p - 450, (a + b) / 2), "height": 150})
    # windows: two glazing lines in the gap, tag outside
    for tag, ax, p, a, b in WINDOWS:
        inner = p + T if p == 0 else p - T
        mid = (p + inner) / 2
        for off in (-35, 35):
            if ax == "h":
                msp.add_line((a, mid + off), (b, mid + off), dxfattribs={"layer": "A-WINDOW"})
            else:
                msp.add_line((mid + off, a), (mid + off, b), dxfattribs={"layer": "A-WINDOW"})
        if ax == "h":
            ty = p - 450 if p == 0 else p + 250
            msp.add_text(tag, dxfattribs={"layer": "A-DW TAG", "insert": ((a + b) / 2 - 100, ty), "height": 150})
        else:
            msp.add_text(tag, dxfattribs={"layer": "A-DW TAG", "insert": (p + 250, (a + b) / 2), "height": 150})
    for name, size, (cx, cy) in ROOMS:
        msp.add_mtext(f"{name}\\P{size}", dxfattribs={"layer": "A-TEXT DETAIL", "insert": (cx, cy),
                                                      "char_height": 150, "attachment_point": 5})
    msp.add_text("GROUND FLOOR PLAN", dxfattribs={"layer": "A-TEXT", "insert": (0, -1900), "height": 350})
    msp.add_text("LVL +450 (FFL)", dxfattribs={"layer": "A-TEXT", "insert": (5000, 2400), "height": 120})
    # dimensions: overall and room-by-room along the south and west
    dims = msp.doc.dimstyles.get("Standard")
    dims.dxf.dimtxt = 150
    dims.dxf.dimasz = 100
    for x0, x1 in ((0, X[7]),):
        msp.add_linear_dim(base=(0, -1100), p1=(x0, 0), p2=(x1, 0), dxfattribs={"layer": "A-DIM"}).render()
    for x0, x1 in zip(X[1:-1], X[2:-1]):
        if x1 - x0 > 500:
            msp.add_linear_dim(base=(0, -600), p1=(x0, 0), p2=(x1, 0), dxfattribs={"layer": "A-DIM"}).render()
    msp.add_linear_dim(base=(-1100, 0), p1=(0, 0), p2=(0, Y_OUT), angle=90, dxfattribs={"layer": "A-DIM"}).render()
    for y0, y1 in ((YW[1], YW[2]), (YW[3], YW[4])):
        msp.add_linear_dim(base=(-600, 0), p1=(0, y0), p2=(0, y1), angle=90, dxfattribs={"layer": "A-DIM"}).render()


def draw_terrace(msp, dx: float = 0.0, dy: float = 12000.0):
    """Terrace plan above the house: a 230 parapet on the outer walls, a note and a rain-water khurra."""
    o = _rect(dx, dy, dx + X[7], dy + Y_OUT)
    i = _rect(dx + T, dy + T, dx + X[7] - T, dy + Y_OUT - T)
    msp.add_lwpolyline(o, close=True, dxfattribs={"layer": "A-WALL PARAPET"})
    msp.add_lwpolyline(i, close=True, dxfattribs={"layer": "A-WALL PARAPET"})
    msp.add_mtext("900MM HIGH\\PAND 230MM THK.\\PPARAPET WALL", dxfattribs={
        "layer": "A-TEXT", "insert": (dx + 900, dy + Y_OUT - 600), "char_height": 150})
    # khurras (rain-water outlets) at two corners, with slope arrows towards them
    for kx, ky in ((dx + T, dy + T), (dx + X[7] - T - 450, dy + T)):
        msp.add_lwpolyline(_rect(kx, ky, kx + 450, ky + 450), close=True, dxfattribs={"layer": "A-TEXT"})
        msp.add_text("450X450 KHURRA", dxfattribs={"layer": "A-TEXT", "insert": (kx, ky + 600), "height": 120})
    for x0 in (dx + 2500, dx + X[7] - 2500):
        msp.add_line((x0, dy + 3400), (x0, dy + 1200), dxfattribs={"layer": "A-TEXT"})
        msp.add_line((x0, dy + 1200), (x0 - 100, dy + 1400), dxfattribs={"layer": "A-TEXT"})
        msp.add_line((x0, dy + 1200), (x0 + 100, dy + 1400), dxfattribs={"layer": "A-TEXT"})
    msp.add_text("SLOPE 1:100", dxfattribs={"layer": "A-TEXT", "insert": (dx + 4500, dy + 2000), "height": 120})
    msp.add_text("TERRACE PLAN", dxfattribs={"layer": "A-TEXT", "insert": (dx, dy - 550), "height": 350})


def draw_schedule_and_legend(msp, x0: float = 18000.0, y0: float = 6000.0):
    msp.add_text("DOOR / WINDOW SCHEDULE", dxfattribs={"layer": "A-TEXT", "insert": (x0, y0 + 700), "height": 250})
    rows = [("TYPE", "SIZE", "DESCRIPTION"), *SCHEDULE]
    cols = (x0, x0 + 1200, x0 + 3200, x0 + 8000)
    rh = 450
    for k in range(len(rows) + 1):
        msp.add_line((cols[0], y0 - k * rh + 300), (cols[-1], y0 - k * rh + 300), dxfattribs={"layer": "A-TEXT"})
    for x in cols:
        msp.add_line((x, y0 + 300), (x, y0 - len(rows) * rh + 300), dxfattribs={"layer": "A-TEXT"})
    for k, (tag, size, desc) in enumerate(rows):
        y = y0 - k * rh
        for x, s in zip(cols, (tag, size, desc)):
            msp.add_text(s, dxfattribs={"layer": "A-TEXT", "insert": (x + 100, y), "height": 150})
    # legend: a hatched swatch with its material word to the right
    ly = y0 - (len(rows) + 2) * rh
    msp.add_text("LEGEND", dxfattribs={"layer": "A-TEXT", "insert": (x0, ly + 400), "height": 200})
    sw = _rect(x0, ly - 300, x0 + 800, ly)
    h = msp.add_hatch(dxfattribs={"layer": "A-HATCH WALL"})
    h.set_pattern_fill("ANSI31", scale=20)
    h.paths.add_polyline_path(sw, is_closed=True)
    msp.add_lwpolyline(sw, close=True, dxfattribs={"layer": "A-TEXT"})
    msp.add_text("BRICK MASONRY IN CM 1:6", dxfattribs={"layer": "A-TEXT", "insert": (x0 + 1000, ly - 220), "height": 150})
    msp.add_text("NOTES: ALL DIMENSIONS ARE IN MM. SLAB 125 THK.", dxfattribs={
        "layer": "A-TEXT", "insert": (x0, ly - 1100), "height": 150})


def make(path: Path = OUT) -> Path:
    doc = ezdxf.new("R2010", units=4)
    for name, col in LAYERS:
        doc.layers.add(name, color=col)
    msp = doc.modelspace()
    draw_plan(msp)
    draw_terrace(msp)
    draw_schedule_and_legend(msp)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    print(make(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
