"""Edge cases that can produce wrong quantities, on synthetic drawings (tests/fixtures/synthetic, made by
tests/fixtures/make_synthetic.py; no client data):

1. dimension text overrides (written vs measured); PDF: written dims vs the scaled length
2. external references (xrefs, images, underlays) and xref-named layers
3. duplicates in the same place (removed) and whole-plan copies elsewhere (flagged, never dropped silently)
4. brickwork per IS 1200 (beam soffit, embedded columns, lintels)
5. PDFs whose text was plotted as strokes (SHX), with and without 'AutoCAD SHX Text' comments
+ stable MB row ids and the optional row `desc`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.extract import extract
from engine.cli import effective_config
from engine.mb import build_mb, result_json

SYN = Path(__file__).parent / "fixtures" / "synthetic"

OFF = {"is1200_beam_soffit": False, "deduct_columns": False, "deduct_lintels": False}
ZONE = [{"name": "Block", "bbox": [-1000, -1000, 100000, 10000], "wall_height_mm": 3000}]


def _geo(name: str) -> dict:
    return extract(SYN / f"{name}.dxf")


def _cfg(geo: dict, **extra) -> dict:
    return effective_config(geo, {"zones": ZONE, **extra}, "synthetic")


def _brick(res) -> float:
    return sum(r.qty for r in res.mb if r.item.startswith("Brick masonry (") and "superstructure" in r.item)


def _flag(res, pred):
    return [f for f in res.flags if pred(f)]


@pytest.fixture(scope="module")
def base():
    geo = _geo("plan_base")
    return geo, build_mb(geo, _cfg(geo, brickwork=OFF))


def test_fixture_script_runs(tmp_path):
    spec = importlib.util.spec_from_file_location("make_synthetic", Path(__file__).parent / "fixtures" / "make_synthetic.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    files = mod.main(tmp_path)
    assert {f.name for f in files} == {p.name for p in SYN.iterdir() if p.suffix in (".dxf", ".pdf")}
    geo = extract(tmp_path / "plan_base.dxf")
    assert any(e["t"] == "D" and e.get("s") == "4550" for e in geo["entities"])


# ------------------------------------------------------------------ 1. dimension overrides
def test_dimension_override_flagged_and_written_value_used(base):
    geo, res = base
    f = _flag(res, lambda f: f.category == "dimension")
    assert len(f) == 1 and f[0].severity == "error"
    assert "4550" in f[0].title and "4500" in f[0].title
    dim = next(e for e in geo["entities"] if e["t"] == "D" and e.get("s") == "4550")
    assert f"dim:{dim['handle']}" in f[0].refs and f[0].location["bbox"]
    d = next(d for d in res.dimensions if d.written)
    assert d.measurement == 4550 and d.measured == pytest.approx(4500) and d.status == "assumed"
    assert "written dimensions govern" in d.remarks
    auto = next(d for d in res.dimensions if d.text == "<>")      # '<>' shows the measured value: no flag
    assert auto.measurement == pytest.approx(10230) and auto.status == "ok"
    js = result_json(res)["dimensions"]
    assert any(x["status"] == "assumed" and x["measured"] == pytest.approx(4500) for x in js)


def test_dimension_choice_measured(base):
    geo, res = base
    fid = next(f.id for f in res.flags if f.category == "dimension")
    for cfg in (_cfg(geo, brickwork=OFF, dimensions={"use": "measured"}),
                _cfg(geo, brickwork=OFF, flags={fid: {"status": "resolved", "value": "measured"}})):
        d = next(d for d in build_mb(geo, cfg).dimensions if d.written)
        assert d.measurement == pytest.approx(4500) and d.status == "ok"


def test_written_value_parsing():
    from engine.dims import written_value
    assert written_value("", 4500.0) == (4500.0, "auto")
    assert written_value("<>", 4500.0) == (4500.0, "auto")
    assert written_value("4550", 4500.0) == (4550.0, "override")
    assert written_value("{\\H1.5;4550}", 4500.0) == (4550.0, "override")
    assert written_value("<> mm wide exp. gap", 4500.0) == (4500.0, "mixed")
    assert written_value("4550 (<>)", 4500.0) == (4550.0, "override")
    assert written_value("as/schedule", 4500.0) == (None, "words")


def test_pdf_calibration_outliers():
    from engine_struct.calibrate import consensus
    smp = [{"source": "dimension", "value_mm": v, "len_pt": v * 0.02, "ratio": 0.02, "text": str(v), "at": [0, 0]}
           for v in (1000, 2000, 3000, 4000, 5000)]
    smp.append({"source": "dimension", "value_mm": 1800, "len_pt": 1985 * 0.02, "ratio": 1985 * 0.02 / 1800,
                "text": "1800", "at": [5, 5]})
    cal = consensus(1, smp, "dimensions")
    assert [o["value_mm"] for o in cal.outliers] == [1800] and cal.outliers[0]["scaled_mm"] == 1985
    assert cal.to_dict()["outliers"]


# ------------------------------------------------------------------ 2. external references
def test_xrefs_images_underlays_flagged():
    geo = _geo("plan_xref")
    kinds = {(x["kind"], x["name"], x["path"]) for x in geo["xrefs"]}
    assert ("xref", "STRUCT-GRID", "..\\xrefs\\STRUCT-GRID.dwg") in kinds
    assert ("image", "site_photo.jpg", "site_photo.jpg") in kinds
    assert any(k == "pdf underlay" and p == "survey.pdf" for k, _, p in kinds)
    res = build_mb(geo, _cfg(geo, brickwork=OFF))
    f = next(f for f in res.flags if "external reference" in f.title)
    assert f.severity == "missing" and "not measured" in f.title
    ev = " ".join(f.evidence)
    assert "STRUCT-GRID" in ev and "STRUCT-GRID.dwg" in ev and "survey.pdf" in ev and "site_photo.jpg" in ev
    lay = next(f for f in res.flags if "A-XREF" in f.title)
    assert lay.severity == "warning" and "inside a measured plan" in lay.title
    assert res.meta["external_refs"]["external"]
    # the rest of the plan is still measured exactly as without the references
    g0 = _geo("plan_base")
    assert _brick(res) == pytest.approx(_brick(build_mb(g0, _cfg(g0, brickwork=OFF))))


# ------------------------------------------------------------------ 3. duplicates
def test_same_place_duplicates_removed(base):
    _, ref = base
    geo = _geo("plan_dup_inplace")
    res = build_mb(geo, _cfg(geo, brickwork=OFF))
    f = next(f for f in res.flags if f.category == "duplicate")
    assert f.severity == "warning" and "duplicates removed" in f.title
    assert "line(s) on wall-brick layers" in " ".join(f.evidence) and "D1" in " ".join(f.evidence)
    assert res.meta["duplicates_removed"] >= 20
    assert _brick(res) == pytest.approx(_brick(ref))
    assert {o.tag: o.count for o in res.openings} == {"D1": 1, "W1": 1}
    assert sorted(r.name for r in res.rooms) == ["ROOM A", "ROOM B"]
    # without de-duplication the second D1 tag and ROOM A label are counted
    raw = build_mb(geo, _cfg(geo, brickwork=OFF, dedupe=False))
    assert {o.tag: o.count for o in raw.openings}["D1"] == 2
    assert len(raw.rooms) == 3


def test_whole_plan_copy_flagged_not_dropped(base):
    _, ref = base
    geo = _geo("plan_copy")
    cfg = _cfg(geo, brickwork=OFF)
    res = build_mb(geo, cfg)
    f = next(f for f in res.flags if f.category == "duplicate" and "is a copy of plan" in f.title)
    assert "GROUND FLOOR PLAN - OLD" in f.title and f.input
    assert _brick(res) == pytest.approx(2 * _brick(ref), rel=1e-6)          # both still measured
    res2 = build_mb(geo, {**cfg, "flags": {f.id: {"status": "resolved", "value": "stale: GROUND FLOOR PLAN - OLD"}}})
    assert _brick(res2) == pytest.approx(_brick(ref))
    assert res2.meta["plans"] == ["GROUND FLOOR PLAN"]
    res3 = build_mb(geo, {**cfg, "flags": {f.id: {"status": "accepted"}}})   # separate block: kept
    assert _brick(res3) == pytest.approx(2 * _brick(ref), rel=1e-6)


# ------------------------------------------------------------------ 4. brickwork (IS 1200 rules)
def _rows(res, desc_prefix):
    return [r for r in res.mb if r.trade.startswith("B.") and r.desc.startswith(desc_prefix)]


def test_brick_switches_off_matches_plain(base):
    _, res = base
    assert not _rows(res, "Deduct RCC columns") and not _rows(res, "Deduct lintel")
    assert not any(r.desc.endswith("(under beam)") for r in res.mb)


def test_brick_embedded_column_deducted(base):
    geo, ref = base
    res = build_mb(geo, _cfg(geo, brickwork={**OFF, "deduct_columns": True}))
    rows = _rows(res, "Deduct RCC columns")
    assert rows and all("IS 1200 (Part 3)" in r.remarks for r in rows)
    ded = -sum(r.qty for r in rows)
    assert ded == pytest.approx(0.23 * 0.45 * 3.0, rel=0.08)               # 230x450 column x 3.0 m
    assert _brick(res) == pytest.approx(_brick(ref) - ded)


def test_brick_beam_over_wall(base):
    geo, ref = base
    res = build_mb(geo, _cfg(geo, brickwork={**OFF, "is1200_beam_soffit": True, "beam_depth_mm": 300}))
    under = [r for r in res.mb if r.desc == "Gross wall (under beam)"]
    assert under and all(r.h == pytest.approx(2.7) and "underside of the beam" in r.remarks for r in under)
    # only the north wall (10230 x 230) sits under the beam lines
    drop = _brick(ref) - _brick(res)
    assert drop == pytest.approx(10.23 * 0.23 * 0.3, rel=0.12)
    # no depth: a missing flag, rows unresolved, nothing deducted
    res2 = build_mb(geo, _cfg(geo, brickwork={**OFF, "is1200_beam_soffit": True}))
    f = next(f for f in res2.flags if f.title.startswith("Beam depth not given"))
    assert f.severity == "missing" and f.input
    assert _brick(res2) == pytest.approx(_brick(ref))
    assert any(f.id in r.flag_ids and r.status == "unresolved" for r in _rows(res2, "Gross wall"))
    res3 = build_mb(geo, _cfg(geo, brickwork={**OFF, "is1200_beam_soffit": True}, flags={f.id: {"status": "resolved", "value": 300}}))
    assert _brick(res3) == pytest.approx(_brick(res))


def test_brick_beam_default_without_lines():
    geo = _geo("plan_nobeam")
    ref = build_mb(geo, _cfg(geo, brickwork=OFF))
    res = build_mb(geo, _cfg(geo, brickwork={**OFF, "is1200_beam_soffit": True,
                                             "beam_depth_by_region": {"GROUND FLOOR PLAN": 300}}))
    f = next(f for f in res.flags if f.title.startswith("No beam lines on the plan"))
    assert f.severity == "warning"
    gross = [r for r in res.mb if r.desc.startswith("Gross wall")]
    assert gross and all(r.h == pytest.approx(2.7) and r.status == "assumed" for r in gross)
    assert _brick(res) == pytest.approx(_brick(ref) * 0.9, rel=0.01)       # every wall 3000 -> 2700


def test_brick_lintels(base):
    geo, ref = base
    res = build_mb(geo, _cfg(geo, brickwork={**OFF, "deduct_lintels": True, "lintel_depth_mm": 150}))
    rows = _rows(res, "Deduct lintel")
    assert {r.desc for r in rows} == {"Deduct lintel over D1", "Deduct lintel over W1"}
    expect = (0.9 + 0.3) * 0.23 * 0.15 + (1.5 + 0.3) * 0.23 * 0.15
    assert -sum(r.qty for r in rows) == pytest.approx(expect)
    assert _brick(res) == pytest.approx(_brick(ref) - expect)
    # size unknown: the drawing check's 'Lintel sizes not given' flag rides on the opening deductions
    res2 = build_mb(geo, _cfg(geo, brickwork={**OFF, "deduct_lintels": True}))
    lf = next(f for f in res2.flags if "lintel" in f.refs)
    ded = [r for r in res2.mb if r.desc.startswith("Deduct D1") and r.trade.startswith("B.")]
    assert ded and lf.id in ded[0].flag_ids and "lintel NOT deducted" in ded[0].remarks
    res3 = build_mb(geo, _cfg(geo, brickwork={**OFF, "deduct_lintels": True}, flags={lf.id: {"status": "resolved", "value": "230X150"}}))
    assert _brick(res3) == pytest.approx(_brick(res))


def test_plaster_cites_is1200_part12(base):
    _, res = base
    assert any("IS 1200 Part 12" in r.remarks for r in res.mb if r.trade.startswith("D.") and r.nos and r.nos < 0)


# ------------------------------------------------------------------ 5. SHX text plotted as strokes (PDF)
def test_pdf_text_as_geometry_flagged():
    from app.pdf_extract import extract_pdf
    from engine_struct.build import TEXT_AS_GEOMETRY, build_struct_mb
    g = extract_pdf(SYN / "shx_strokes.pdf")
    sh = g["sheets"][0]
    assert sh["text_as_geometry"] and sh["texts"] == 0 and sh["stroke_stats"]["short_strokes"] >= 300
    res = build_struct_mb(g, {})
    f = next(f for f in res.flags if "text plotted as geometry" in f.title)
    assert f.severity == "error" and TEXT_AS_GEOMETRY in f.title and "SHX text as comments" in f.title


def test_pdf_shx_comments_recovered():
    from app.pdf_extract import SHX_TEXT_LAYER, extract_pdf
    from engine_struct.build import build_struct_mb
    g = extract_pdf(SYN / "shx_comments.pdf")
    sh = g["sheets"][0]
    assert not sh["text_as_geometry"] and sh["shx_texts"] == 60 and sh["text_source"] == "shx-comments"
    texts = [e for e in g["entities"] if e["t"] == "T"]
    assert {"D1", "W1", "ROOM A", "B1 230X450"} <= {e["s"] for e in texts}
    assert all(g["layers"][e["l"]]["name"] == SHX_TEXT_LAYER and e.get("shx") for e in texts)
    assert not any("text plotted as geometry" in f.title for f in build_struct_mb(g, {}).flags)


# ------------------------------------------------------------------ stable row ids + desc
def test_row_ids_stable_and_unique(base):
    geo, res = base
    ids = [r.id for r in res.mb]
    assert len(ids) == len(set(ids)) and all(i.startswith("r") for i in ids)
    again = build_mb(geo, _cfg(geo, brickwork=OFF))
    assert [r.id for r in again.mb] == ids
    # adding rows (lintel deductions) and changing a height keeps the ids of the other rows
    zone = [{**ZONE[0], "wall_height_mm": 3300}]
    more = build_mb(geo, effective_config(geo, {"zones": zone, "brickwork": {**OFF, "deduct_lintels": True,
                                                                             "lintel_depth_mm": 150}}, "synthetic"))
    by_key = {(r.trade, r.item, r.location, r.desc): r.id for r in res.mb}
    kept = [by_key[(r.trade, r.item, r.location, r.desc)] == r.id for r in more.mb
            if (r.trade, r.item, r.location, r.desc) in by_key]
    assert kept and all(kept)
    assert {r.id for r in more.mb} - set(ids)                       # the lintel rows are new
    for ab in res.abstract:
        assert set(ab.row_ids) <= set(ids)


def test_row_desc_in_json(base):
    _, res = base
    rows = result_json(res)["rows"]
    descs = {r["desc"] for r in rows}
    assert {"Gross wall", "Deduct D1 (900x2100)", "Add wall across W1"} <= descs
    assert all("desc" in r for r in rows)


