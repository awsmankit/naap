"""The Review step summary (engine/review.py, meta["review"], POST /api/jobs/{id}/review) on synthetic
drawings: what each view is used for, floors per plan, heights with their sources, and that answers
given on Review (region fields + config.flags) round-trip through the engine like flag answers."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app import main as appmain
from app.cluster import cluster
from app.extract import extract
from engine.cli import effective_config
from engine.mb import build_mb
from engine.sections import roof_height_from_sections
from engine.storeys import _parse_floor_value
from tests.fixtures.make_synthetic import draw_plan, draw_schedule, new_doc
from tests.test_storeys import _two_storey_dxf

ROOT = Path(__file__).resolve().parent.parent


def _views_dxf(path: Path) -> Path:
    """The two-room plan and its schedule, plus a section with level labels, an empty section, an
    elevation with groove lines, an empty elevation, a detail and a key plan."""
    doc = new_doc()
    doc.layers.add("A-GROOVE", color=2)
    msp = doc.modelspace()
    draw_plan(msp)
    draw_schedule(msp)

    def box(x0, y0, title, extra=()):
        for k in range(6):
            msp.add_line((x0, y0 + k * 800), (x0 + 6000, y0 + k * 800), dxfattribs={"layer": "A-TEXT"})
        msp.add_line((x0, y0), (x0, y0 + 4000), dxfattribs={"layer": "A-TEXT"})
        msp.add_line((x0 + 6000, y0), (x0 + 6000, y0 + 4000), dxfattribs={"layer": "A-TEXT"})
        msp.add_text(title, dxfattribs={"layer": "A-TEXT", "insert": (x0, y0 - 700), "height": 300})
        for s, (dx, dy) in extra:
            msp.add_text(s, dxfattribs={"layer": "A-TEXT", "insert": (x0 + dx, y0 + dy), "height": 150})

    box(0, -20000, "SECTION - A", [("Ground Floor Lvl.+450", (200, 300)), ("First Floor Lvl.+3450", (200, 3300))])
    box(20000, -20000, "SECTION - Q")
    box(40000, -20000, "FRONT ELEVATION")
    for k in range(4):
        msp.add_line((40500, -19500 + k * 900), (45500, -19500 + k * 900), dxfattribs={"layer": "A-GROOVE"})
    box(60000, -20000, "SIDE ELEVATION")
    box(0, -40000, "STAIR DETAIL")
    box(20000, -40000, "KEY PLAN")
    doc.saveas(path)
    return path


@pytest.fixture(scope="module")
def views_job(tmp_path_factory):
    geo = extract(_views_dxf(tmp_path_factory.mktemp("rv") / "views.dxf"))
    cfg = effective_config(geo, {"layer_roles": {"A-GROOVE": "groove"}}, "synthetic")
    return geo, cfg


def _by_name(cfg, rv):
    names = {r["id"]: r["name"] for r in cfg["regions"]}
    return {names[v["id"]]: v for v in rv["views"]}


def test_purposes_come_from_what_the_engine_uses(views_job):
    geo, cfg = views_job
    rv = build_mb(geo, cfg).meta["review"]
    v = _by_name(cfg, rv)
    assert v["GROUND FLOOR PLAN"]["purpose"] == "measured" and v["GROUND FLOOR PLAN"]["label"] == "Measured"
    assert v["DOOR / WINDOW SCHEDULE"]["purpose"] == "sizes" and "D1" in v["DOOR / WINDOW SCHEDULE"]["reason"]
    # the level labels written in section A give the storey heights; section Q gives nothing
    assert v["SECTION - A"]["purpose"] == "heights" and "level labels" in v["SECTION - A"]["reason"].lower()
    assert v["SECTION - Q"]["purpose"] == "unused"
    assert v["FRONT ELEVATION"]["purpose"] == "finishes" and "grooves" in v["FRONT ELEVATION"]["reason"].lower()
    assert v["SIDE ELEVATION"]["purpose"] == "unused"
    assert v["STAIR DETAIL"]["purpose"] == "unused" and "Detail" in v["STAIR DETAIL"]["reason"]
    assert v["KEY PLAN"]["purpose"] == "unused" and "Key plan" in v["KEY PLAN"]["reason"]
    # every view gets a one-line reason
    assert all(x["reason"] for x in rv["views"])


def test_ticking_a_non_plan_view_measures_it_and_warns(views_job):
    geo, cfg = views_job
    regions = [dict(r) for r in cfg["regions"]]
    sec = next(r for r in regions if r["name"] == "SECTION - A")
    sec.update(include=True, measure=True)
    rv = build_mb(geo, {**cfg, "regions": regions}).meta["review"]
    v = _by_name(cfg, rv)["SECTION - A"]
    assert v["purpose"] == "measured"
    assert v["warn"].startswith("This will double-count walls already measured in GROUND FLOOR PLAN")
    # unticking the only plan: not measured, and the engine says no plan is included
    plan = next(r for r in regions if r["name"] == "GROUND FLOOR PLAN")
    plan["include"] = False
    sec.update(include=False, measure=False)
    res = build_mb(geo, {**cfg, "regions": regions})
    assert _by_name(cfg, res.meta["review"])["GROUND FLOOR PLAN"]["reason"].startswith("Unticked")
    assert any("No included region is marked as a plan" in f.title for f in res.flags)


def test_heights_rows_and_questions(views_job):
    geo, cfg = views_job
    res = build_mb(geo, cfg)
    h = res.meta["review"]["heights"]
    blocks = [r for r in h["rows"] if r["kind"] == "block"]
    assert blocks and blocks[0]["assumed"] and "default" in blocks[0]["source"]
    assert h["beam"]["value"] is None or h["beam"]["source"]
    qs = res.meta["review"]["questions"]
    assert {q["topic"] for q in qs} >= {"heights", "structure"}
    # answering a block height on Review = resolving its flag: it changes the brickwork and the row says so
    fid = blocks[0]["flag_ids"][0]
    brick = lambda r: sum(x.qty for x in r.mb if x.item.startswith("Brick masonry (") and "superstructure" in x.item)
    res2 = build_mb(geo, {**cfg, "flags": {fid: {"status": "resolved", "value": 2400}}})
    row = next(r for r in res2.meta["review"]["heights"]["rows"] if fid in r["flag_ids"])
    assert row["height_mm"] == 2400 and row["source"] == "your answer" and not row["assumed"]
    assert brick(res2) < brick(res)
    f = [x for x in res2.flags if x.id == fid]
    assert len(f) == 1 and f[0].status == "resolved"


def test_floors_and_storey_height_round_trip(tmp_path):
    geo = extract(_two_storey_dxf(tmp_path / "t.dxf"))
    cl = cluster(geo["entities"], gap=3000, min_entities=3, layouts=geo["layouts"])
    cfg = {"regions": cl["regions"], "layer_roles": {"A-WALL": "wall-brick"}}
    rv = build_mb(geo, cfg).meta["review"]
    plans = {p["name"]: p for p in rv["plans"]}
    typ = next(p for p in plans.values() if p["floors"] == [1, 2, 3, 4])
    assert typ["multiplier"] == 4 and typ["flag_floors"]
    assert rv["building"]["multi"] and rv["building"]["typical_groups"] >= 1

    # what the Review page writes for "Floors 1-3, ×2, storey 3300": region fields + the confirm flag
    regions = [dict(r) for r in cl["regions"]]
    reg = next(r for r in regions if r["id"] == typ["id"])
    reg.update(floors=[1, 2, 3], multiplier=2, floors_source="user (Review)",
               floor_to_floor_mm=3300, floor_to_floor_source="user (Review)")
    flags = {typ["flag_floors"]: {"status": "resolved", "value": "1-3 x2"}}
    res = build_mb(geo, {**cfg, "regions": regions, "flags": flags})
    p2 = next(p for p in res.meta["review"]["plans"] if p["id"] == typ["id"])
    assert p2["floors"] == [1, 2, 3] and p2["multiplier"] == 2
    assert p2["ftf_mm"] == 3300 and p2["ftf_user"]
    # the MB flag shows as resolved (same id, once), not as a second open question
    same = [f for f in res.flags if f.id == typ["flag_floors"]]
    assert len(same) == 1 and same[0].status == "resolved"
    q = [x for x in res.meta["review"]["questions"] if x["id"] == typ["flag_floors"]]
    assert len(q) == 1 and q[0]["status"] == "resolved"
    row = next(r for r in res.meta["review"]["heights"]["rows"] if r["rid"] == typ["id"])
    assert row["ftf_mm"] == 3300 and row["source"] == "your answer"
    # the multiplier reaches the book: rows of that plan are counted x2
    groups = {r.floor_group for r in res.mb if r.plan == typ["name"]}
    assert groups == {"Typical floors 1–3 (x2)"}
    assert {r.nos for r in res.mb if r.plan == typ["name"] and r.item.startswith("Cement plaster to ceiling")} == {2}


def test_floor_answer_with_explicit_multiplier():
    assert _parse_floor_value("6-9") == ([6, 7, 8, 9], 4)
    assert _parse_floor_value("6-9 x3") == ([6, 7, 8, 9], 3)
    assert _parse_floor_value("Ground floor") == ([0], 1)
    assert _parse_floor_value("3,4,5") == ([3, 4, 5], 3)


def test_roof_height_from_section_levels():
    secs = [{"rid": 7, "name": "SECTION - P", "tos": {3450.0: 1, 4800.0: 2}, "floor": {450.0: (3, "PLINTH"), 300.0: (4, "PLINTH")},
             "nums": {3000.0, 4500.0}},
            {"rid": 8, "name": "SECTION - R", "tos": {7000.0: 5}, "floor": {300.0: (6, "PLINTH")}, "nums": set()}]
    a = roof_height_from_sections(secs, 3450)
    assert a["to_slab_top_mm"] == 3000 and a["dimensioned"] and a["section"] == "SECTION - P"
    assert roof_height_from_sections(secs, 4800)["to_slab_top_mm"] == 4500
    b = roof_height_from_sections(secs, 7000)          # one floor level in that section, no dimension
    assert b["to_slab_top_mm"] == 6700 and not b["dimensioned"]
    assert roof_height_from_sections(secs, 9999) is None


# ---------------------------------------------------------------- the endpoint (server and browser share service.review)
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    return TestClient(appmain.app)


def test_review_endpoint_on_the_sample(client):
    with (ROOT / "samples" / "sample-house.dxf").open("rb") as fh:
        jid = client.post("/api/jobs", files={"file": ("sample-house.dxf", fh, "application/dxf")}).json()["id"]
    t = time.time()
    while client.get(f"/api/jobs/{jid}").json()["status"] not in ("ready", "error"):
        assert time.time() - t < 60
        time.sleep(0.2)
    cfg = client.get(f"/api/jobs/{jid}/config").json()
    rv = client.post(f"/api/jobs/{jid}/review", json=cfg).json()
    purposes = {v["id"]: v["purpose"] for v in rv["views"]}
    names = {r["id"]: r["name"] for r in cfg["regions"]}
    assert {names[i]: p for i, p in purposes.items()} == {
        "TERRACE PLAN": "measured", "GROUND FLOOR PLAN": "measured", "DOOR / WINDOW SCHEDULE": "sizes"}
    assert rv["building"]["text"].startswith("Single storey")
    assert rv["questions"] and rv["summary"]["open"]["missing"] > 0
    # an unchanged config reuses the cached run; no body = the saved config
    again = client.post(f"/api/jobs/{jid}/review").json()
    assert again["views"] == rv["views"]
    # the in-browser worker routes the same call to the same function
    from app import browser
    assert any(name == "review" for _, _, name in browser._ROUTES)


# ---------------------------------------------------------------- QA regressions (sample house)
@pytest.fixture(scope="module")
def sample_house():
    geo = extract(ROOT / "samples" / "sample-house.dxf")
    cfg = effective_config(geo, {}, "sample")
    return geo, cfg


def _parapet(res):
    row = next(r for r in res.mb if r.item.startswith("Brick masonry in parapet"))
    par = next(t for t in res.meta["review"]["heights"]["wall_types"] if t["role"] == "parapet")
    return row, par


def test_parapet_height_field_source_and_book_agree(sample_house):
    """Review showed 600 (the config default) next to "from note '900MM HIGH …'" while the book used 900."""
    geo, cfg = sample_house
    row, par = _parapet(build_mb(geo, cfg))
    assert par["height_mm"] == 900 and par["height_source"].startswith("from note '900MM HIGH")
    assert par["brick_height_mm"] == 800 and par["coping_mm"] == 100
    assert row.h == pytest.approx(par["brick_height_mm"] / 1000)          # MB H 0.8 = 900 - 100 coping
    # a value entered on Review wins over the note, and the book follows it
    row, par = _parapet(build_mb(geo, {**cfg, "roof": {"parapet_height_mm": 1200}}))
    assert par["height_mm"] == 1200 and par["height_source"] == "your value"
    assert row.h == pytest.approx(1.1) and "your value" in row.remarks
    # no note: the config default is used, said to be a default, and the row is assumed
    nonote = {**geo, "entities": [e for e in geo["entities"] if "PARAPET" not in str(e.get("s", "")).upper()]}
    row, par = _parapet(build_mb(nonote, cfg))
    assert par["height_mm"] == 600 and par["height_source"].startswith("default") and par["height_assumed"]
    assert row.status == "assumed" and "ASSUMED" in row.remarks


def test_one_house_is_one_wall_height_block(sample_house):
    """The sample house's wall lines fell into two fragments, asked as 'Block: BEDROOM-1, BEDROOM-2' and
    'Block: TOILET-2' (the living room, kitchen and toilet-1 in neither). One building, one question."""
    geo, cfg = sample_house
    res = build_mb(geo, cfg)
    wh = [f for f in res.flags if "wall-height" in f.refs]
    assert len(wh) == 1
    assert wh[0].title.startswith("Wall height missing for all walls of GROUND FLOOR PLAN (6 rooms")
    rows = res.meta["review"]["heights"]["rows"]
    block = [r for r in rows if r["kind"] == "block"]
    assert len(block) == 1 and block[0]["name"].startswith("All walls of GROUND FLOOR PLAN")
    assert "Block:" not in block[0]["name"]
    # the open block row stands for its walls: no second "other walls" row at the same default
    assert not next(r for r in rows if r["kind"] == "default")["used"]


def test_separate_buildings_keep_separate_wall_height_blocks(tmp_path):
    """Two houses with a clear gap between them are asked separately and named by their rooms."""
    from engine.checks import block_label
    assert block_label("SITE PLAN", ["BED-1", "BED-2"], True, [0, 0, 1, 1]) == "walls around BED-1, BED-2"
    assert block_label("GF PLAN", list("ABCDEF"), False, [0, 0, 1, 1]) == "all walls of GF PLAN (6 rooms: A, B, C, D and 2 more)"


def test_key_plan_area_with_level_labels_says_what_is_read(tmp_path):
    """An area named after a KEY PLAN title that also holds level labels was shown as 'KEY PLAN: used for
    heights' with no explanation (a key plan has no levels)."""
    doc = new_doc()
    msp = doc.modelspace()
    draw_plan(msp)
    draw_schedule(msp)
    x0, y0 = 20000, -40000
    for k in range(6):
        msp.add_line((x0, y0 + k * 800), (x0 + 6000, y0 + k * 800), dxfattribs={"layer": "A-TEXT"})
    msp.add_line((x0, y0), (x0, y0 + 4000), dxfattribs={"layer": "A-TEXT"})
    msp.add_line((x0 + 6000, y0), (x0 + 6000, y0 + 4000), dxfattribs={"layer": "A-TEXT"})
    msp.add_text("KEY PLAN", dxfattribs={"layer": "A-TEXT", "insert": (x0, y0 - 700), "height": 300})
    for s, dy in (("Ground Floor Lvl.+450", 300), ("First Floor Lvl.+3450", 3300)):
        msp.add_text(s, dxfattribs={"layer": "A-TEXT", "insert": (x0 + 200, y0 + dy), "height": 150})
    doc.saveas(tmp_path / "kp.dxf")
    geo = extract(tmp_path / "kp.dxf")
    cfg = effective_config(geo, {}, "synthetic")
    rv = build_mb(geo, cfg).meta["review"]
    kp = _by_name(cfg, rv)["KEY PLAN"]
    assert kp["purpose"] == "heights"            # the engine does read those labels
    assert kp["reason"].startswith("Not the key plan itself") and "Ground floor +450" in kp["reason"]
