"""Structural engine pieces on synthetic data (no client files needed)."""
from __future__ import annotations

import math

from app.pdf_extract import extract_pdf
from engine_struct import calibrate as C
from engine_struct import plans as P
from engine_struct.details import bulb_volume_m3
from engine_struct.sheet import Sheet
from engine_struct.tables import bar_sets, grid_table, spacing
from tests.test_pdf_extract import text, write_pdf


def test_bar_notation():
    assert bar_sets("4-12") == [{"dia": 12, "n": 4}]                     # beam schedule: count-dia
    assert bar_sets("#16-2NOS. +#12-2NOS.") == [{"dia": 16, "n": 2}, {"dia": 12, "n": 2}]
    assert bar_sets("#25-4 Nos. + #20-8 Nos.") == [{"dia": 25, "n": 4}, {"dia": 20, "n": 8}]
    assert bar_sets("12-16")[0].get("ambiguous")                          # both could be sizes
    assert bar_sets("-") == []
    assert spacing("8#@100C/C") == {"dia": 8, "s": 100}
    assert spacing("#12@100C/C") == {"dia": 12, "s": 100}
    assert spacing("10@120C/C") == {"dia": 10, "s": 120}


def test_bulb_volume():
    # zero cone heights -> nothing extra; 45° cones on a 400/1000 bulb: two frusta minus shaft
    assert bulb_volume_m3(400, 1000, 0, 0) == 0
    v = bulb_volume_m3(400, 1000, 300, 300)
    R, r, h = 0.5, 0.2, 0.3
    assert math.isclose(v, 2 * math.pi * h / 3 * (R * R + R * r + r * r) - math.pi * r * r * 0.6)


def _dim(x0, x1, y, value, ext=2.0):
    """Horizontal CAD-style dimension: line overshooting the extension lines by `ext`."""
    c = f"{x0 - ext} {y} m {x1 + ext} {y} l S\n"
    c += f"{x0} {y - 6} m {x0} {y + 3} l S\n{x1} {y - 6} m {x1} {y + 3} l S\n"
    return c + text((x0 + x1) / 2 - 8, y + 2, str(value), 5)


def test_calibration_from_written_dimensions(tmp_path):
    # 1:100-ish: 0.02 pt/mm; dims overshoot their extension lines (must be ignored)
    c = "0.3 w\n" + _dim(100, 160, 300, 3000) + _dim(200, 236, 300, 1800) + _dim(300, 318, 300, 900)
    c += _dim(100, 190, 200, 4510)          # 1 % off: within the spread
    g = extract_pdf(write_pdf(tmp_path / "d.pdf", c))
    sh = Sheet(g, g["sheets"][0])
    s = C.dim_samples(sh, (0, 0, 842, 595))
    assert len(s) == 4
    cal = C.consensus(1, s, "dimensions")
    assert abs(cal.pt_per_mm - 0.02) < 0.0002 and cal.spread < 0.01
    # user calibration: two points + a known length
    u = C.user_calib(1, {"p1": [0, 0], "p2": [30, 40], "mm": 2500})
    assert math.isclose(u.pt_per_mm, 0.02)
    assert C.user_calib(1, {"scale": 100}).scale_1_to == 100


def test_grid_table_and_plan_reading(tmp_path):
    # a 3-column table: MARK | THK | BARS, rows S1, S2, ruled
    c = "0.5 w\n"
    xs, ys = [50, 100, 160, 260], [500, 480, 460, 440]
    for y in ys:
        c += f"{xs[0]} {y} m {xs[-1]} {y} l S\n"
    for x in xs:
        c += f"{x} {ys[0]} m {x} {ys[-1]} l S\n"
    c += text(55, 486, "SLAB", 7) + text(105, 486, "THICKNESS", 7) + text(165, 486, "A", 7)
    c += text(55, 466, "S1", 7) + text(105, 466, "150", 7) + text(165, 466, "8@150C/C", 7)
    c += text(55, 446, "S2", 7) + text(105, 446, "125", 7) + text(165, 446, "10@200C/C", 7)
    # a plan: 4 filled columns, two beams (double lines), a slab tag in the middle
    for x, y in ((100, 100), (300, 100), (100, 250), (300, 250)):
        c += f"{x - 5} {y - 5} 10 10 re f\n"
    for y in (100, 250):
        c += f"105 {y - 3} m 295 {y - 3} l S 105 {y + 3} m 295 {y + 3} l S\n"
    for x in (100, 300):
        c += f"{x - 3} 105 m {x - 3} 245 l S {x + 3} 105 m {x + 3} 245 l S\n"
    c += text(195, 170, "S1", 7) + text(195, 260, "B1", 7)
    g = extract_pdf(write_pdf(tmp_path / "t.pdf", c))
    sh = Sheet(g, g["sheets"][0])
    t = grid_table(sh, "Slab", r"^SLAB$", r"S\d")
    assert [r["label"] for r in t.rows] == ["S1", "S2"]
    assert t.rows[1]["cells"] == ["S2", "125", "10@200C/C"]
    assert t.col("THICK") == 1
    bl = P.column_blobs(sh)
    reg = (90, 90, 310, 260)
    lines, _ = P.merge_lines(sh.segs_in(P.grow(reg, 2)))
    strips = P.beam_strips(lines, bl, 4, 8, 20)
    assert len(strips) == 4 and all(abs(s.width_pt - 6) < 0.01 for s in strips)
    horiz = sorted(s.length for s in strips if s.horiz)
    assert horiz == [190, 190]                      # clear span between column faces
    orphan = P.assign_beam_tags(strips, [x for x in sh.texts if x.s == "B1"], 30)
    assert not orphan and sum(1 for s in strips if s.tag == "B1") == 1
    pan = P.slab_panels(reg, lines, bl, [x for x in sh.texts if x.s == "S1"], [])
    assert len(pan) == 1 and not pan[0].leaked
    # clear panel between inner beam lines: (297-103) x (247-103)
    assert abs(pan[0].area_pt2 / (194 * 144) - 1) < 0.03


def test_concrete_grade_note_wordings():
    """'GRADE OF CONCRETE M25' was missed (only 'CONCRETE MIX M25' was read) and flagged as not written."""
    from engine_struct.notes import concrete_grade
    assert concrete_grade("CONCRETE MIX M25, STEEL FE500") == "M25"
    assert concrete_grade("1. GRADE OF CONCRETE M25.") == "M25"
    assert concrete_grade("CONCRETE GRADE: M-30") == "M30"
    assert concrete_grade("LEAN CONCRETE M10 UNDER FOOTINGS. RCC M25") == "M25"
    assert concrete_grade("PCC M10") is None
