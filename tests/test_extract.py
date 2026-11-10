from pathlib import Path

import ezdxf
import pytest

from app.cluster import cluster
from app.extract import extract
from engine.export_xlsx import write_xlsx
from engine.mb import build_mb

def make_dxf(path: Path) -> Path:
    doc = ezdxf.new("R2010", units=4)
    doc.layers.add("A-WALL", color=1)
    doc.layers.add("A-TEXT DETAIL", color=2)
    doc.layers.add("A-DIM", color=3)
    doc.layers.add("A-DW TAG", color=4)
    msp = doc.modelspace()
    # view 1: a 6000 x 4200 room with label, tag and a dimension
    msp.add_lwpolyline([(0, 0), (6000, 0), (6000, 4200), (0, 4200)], close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_lwpolyline([(-230, -230), (6230, -230), (6230, 4430), (-230, 4430)], close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_mtext("HALL-1\\P6000X4200", dxfattribs={"layer": "A-TEXT DETAIL", "insert": (2500, 2100), "char_height": 150})
    msp.add_text("D1", dxfattribs={"layer": "A-DW TAG", "insert": (100, 100), "height": 150})
    msp.add_text("GROUND FLOOR PLAN", dxfattribs={"layer": "A-TEXT DETAIL", "insert": (0, -1500), "height": 400})
    msp.add_linear_dim(base=(0, -800), p1=(0, 0), p2=(6000, 0), dxfattribs={"layer": "A-DIM"}).render()
    for i in range(8):
        msp.add_line((i * 100, 0), (i * 100, 50), dxfattribs={"layer": "A-WALL"})
    # view 2 far away: a schedule
    for i in range(10):
        msp.add_line((100000, i * 500), (104000, i * 500))
    msp.add_text("D1 (1200X2100)", dxfattribs={"insert": (100100, 100), "height": 150})
    blk = doc.blocks.new("TAGBLK")
    blk.add_circle((0, 0), 200)
    msp.add_blockref("TAGBLK", (500, 500), dxfattribs={"layer": "A-DW TAG"})
    doc.saveas(path)
    return path


def test_extract_cluster_build(tmp_path):
    geo = extract(make_dxf(tmp_path / "t.dxf"))
    assert geo["units"] == "mm"
    names = {l["name"] for l in geo["layers"]}
    assert {"A-WALL", "A-TEXT DETAIL", "A-DIM"} <= names
    types = {e["t"] for e in geo["entities"]}
    assert {"P", "L", "T", "D", "I", "C"} <= types
    dim = next(e for e in geo["entities"] if e["t"] == "D")
    assert dim["m"] == pytest.approx(6000)
    texts = [e["s"] for e in geo["entities"] if e["t"] == "T"]
    assert "HALL-1\n6000X4200" in texts
    circle = next(e for e in geo["entities"] if e["t"] == "C")
    assert circle.get("v") == 1 and circle["l"] == [l["name"] for l in geo["layers"]].index("A-DW TAG")

    cl = cluster(geo["entities"], gap=2000, min_entities=3)
    assert len(cl["regions"]) == 2
    plan = next(r for r in cl["regions"] if r["kind"] == "plan")
    assert plan["name"] == "GROUND FLOOR PLAN"

    cfg = {"layer_roles": {"A-WALL": "wall-brick", "A-TEXT DETAIL": "room text", "A-DIM": "dimension", "A-DW TAG": "door"},
           "regions": [{**r, "include": True} for r in cl["regions"]]}
    res = build_mb(geo, cfg)
    assert [(r.name, r.width_mm, r.length_mm) for r in res.rooms] == [("HALL-1", 6000, 4200)]
    d1 = next(o for o in res.openings if o.tag == "D1")
    assert (d1.count, d1.width_mm, d1.height_mm) == (1, 1200, 2100)
    assert len(res.dimensions) == 1
    out = tmp_path / "mb.xlsx"
    write_xlsx(res, out)
    from openpyxl import load_workbook
    assert load_workbook(out).sheetnames == ["MB", "Abstract", "Room schedule", "Door-Window schedule",
                                             "All dimensions", "Assumptions", "Flags"]


def test_cli_on_synthetic(tmp_path):
    import json

    from engine.cli import main
    dxf = make_dxf(tmp_path / "t.dxf")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "layer_roles": {"A-WALL": "wall-brick", "A-TEXT DETAIL": "room text", "A-DW TAG": "door"},
        "regions": [{"id": 0, "name": "GROUND FLOOR PLAN", "kind": "plan", "include": True, "bbox": [-1000, -3000, 10000, 6000]},
                    {"id": 1, "name": "Schedule", "kind": "schedule", "include": True, "bbox": [99000, -500, 105000, 5000]}]}))
    out = tmp_path / "mb.xlsx"
    assert main([str(dxf), "--config", str(cfg), "--out", str(out), "--flags-json", str(tmp_path / "f.json"),
                 "--result-json", str(tmp_path / "r.json")]) == 0
    res = json.loads((tmp_path / "r.json").read_text())
    assert out.exists() and res["rows"] and all("refs" in r and "status" in r for r in res["rows"])
    assert any(r["item"].startswith("Brick masonry") for r in res["rows"])
    brick = sum(r["qty"] for r in res["rows"] if r["item"].startswith("Brick masonry"))
    # 230 thick line-only wall around a 6000 x 4200 room, default height 3000 (flagged); 20 mm raster -> 5%
    assert brick == pytest.approx(2 * (6.23 + 4.43) * 0.23 * 3.0, rel=0.05)
