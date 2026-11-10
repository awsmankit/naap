"""Storey parsing and layout-viewport region splitting (synthetic, no real drawing needed)."""
import ezdxf

from app.cluster import cluster
from app.extract import extract
from engine.storeys import group_name, level_table, parse_floors, parse_level, storey_heights


def test_parse_floors():
    assert parse_floors("TYPICAL (2nd-4th) FLOOR PLAN") == ([2, 3, 4], None)
    assert parse_floors("TYPICAL FLOOR PLAN (2ND TO 4TH)") == ([2, 3, 4], None)
    assert parse_floors("6 TO 9") == ([6, 7, 8, 9], None)
    assert parse_floors("GROUND  FLOOR PLAN") == ([0], None)
    assert parse_floors("FIRST FLOOR PLAN") == ([1], None)
    assert parse_floors("SECOND TO FOURTH FLOOR PLAN") == ([2, 3, 4], None)
    assert parse_floors("TERRACE PLAN") == (None, "terrace")
    assert parse_floors("MUMTY ROOF PLAN") == (None, "mumty")
    assert parse_floors("SECTION A-A'") == (None, None)
    assert group_name([3, 4, 5]) == "Typical floors 3–5 (x3)"
    assert group_name([0]) == "Ground floor" and group_name([11]) == "11th floor"


def test_levels_and_storey_heights():
    assert parse_level("First Floor| Lvl.+3450 ")[1:] == (1, None, 3450.0)
    assert parse_level("Terrace |Lvl.+15600 ")[1:] == (None, "terrace", 15600.0)
    assert parse_level("F.G.L. ±00") is None
    assert parse_level("LVL +2100") is None          # door head level, not a floor
    texts = [(i, {"s": s}, None) for i, s in enumerate(
        ["Ground Floor Lvl.+450", "First Floor Lvl.+3450", "Second Floor Lvl.+6600", "Terrace Lvl.+9600"])]
    lv = level_table(texts)
    h = storey_heights(lv)
    assert {n: v for n, (v, _) in h.items()} == {0: 3000, 1: 3150, 2: 3000}
    # a gap in the level list is not bridged by guessing
    lv2 = level_table([t for t in texts if "First" not in t[1]["s"]])
    assert 0 not in storey_heights(lv2)


def _two_storey_dxf(path):
    """Two identical plans 2 m apart (merged by raster clustering) shown on two sheets."""
    doc = ezdxf.new("R2010", units=4)
    msp = doc.modelspace()
    for k, (y0, title) in enumerate(((0, "GROUND FLOOR PLAN"), (-12000, "TYPICAL FLOOR PLAN (1ST TO 4TH)"))):
        for x in range(0, 10001, 2500):
            msp.add_line((x, y0), (x, y0 + 8000), dxfattribs={"layer": "A-WALL"})
        for y in (0, 4000, 8000):
            msp.add_line((0, y0 + y), (10000, y0 + y), dxfattribs={"layer": "A-WALL"})
        msp.add_text("ROOM\\P2500X4000", dxfattribs={"insert": (1000, y0 + 2000), "height": 150})
        msp.add_mtext("ROOM\\P2500X4000", dxfattribs={"insert": (3500, y0 + 2000), "char_height": 150})
        msp.add_text(title, dxfattribs={"insert": (0, y0 - 1500), "height": 400})
    # a third copy whose sheet has a range only in its name
    for x in range(0, 10001, 2500):
        msp.add_line((x, -24000), (x, -16000), dxfattribs={"layer": "A-WALL"})
    for name, cy in (("GF", 3000), ("1 TO 4", -9000), ("5 TO 7", -21000)):
        lay = doc.layouts.new(name)
        lay.add_viewport(center=(150, 100), size=(280, 180), view_center_point=(5000, cy), view_height=12000)
    doc.saveas(path)
    return path


def test_layout_viewports_split_stacked_plans(tmp_path):
    geo = extract(_two_storey_dxf(tmp_path / "t.dxf"))
    sheets = {lay["name"]: lay for lay in geo["layouts"]}
    assert {"GF", "1 TO 4", "5 TO 7"} <= set(sheets)
    assert all(len(sheets[n]["viewports"]) == 1 for n in ("GF", "1 TO 4", "5 TO 7"))
    plain = cluster(geo["entities"], gap=3000, min_entities=3)
    assert len([r for r in plain["regions"] if r["count"] > 10]) == 1      # raster clustering merges them
    cl = cluster(geo["entities"], gap=3000, min_entities=3, layouts=geo["layouts"])
    by = {r.get("layout"): r for r in cl["regions"] if r.get("layout")}
    assert set(by) == {"GF", "1 TO 4", "5 TO 7"}
    assert by["GF"]["floors"] == [0] and by["GF"]["multiplier"] == 1
    assert by["1 TO 4"]["floors"] == [1, 2, 3, 4] and by["1 TO 4"]["multiplier"] == 4
    assert by["5 TO 7"]["multiplier"] == 3 and by["5 TO 7"]["kind"] == "plan"
    assert by["1 TO 4"]["bbox"][3] < by["GF"]["bbox"][1]
