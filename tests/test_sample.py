"""The public sample drawing (samples/sample-house.dxf, made by samples/make_sample.py) end to end:
parsing, regions, rooms, openings, the deliberate wrong room label, flags feeding back, the API, a
DWG round trip through LibreDWG (skipped without it) and the web mock built from the sample."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import ezdxf
import pytest

from app.extract import extract
from engine.cli import effective_config
from engine.mb import build_mb, result_json

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "sample-house.dxf"
MOCK = ROOT / "web" / "mock"


def _make_sample_module():
    spec = importlib.util.spec_from_file_location("make_sample", ROOT / "samples" / "make_sample.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _brick(res) -> float:
    return sum(r.qty for r in res.mb if r.item.startswith("Brick masonry (") and "superstructure" in r.item)


@pytest.fixture(scope="module")
def sample():
    geo = extract(SAMPLE)
    cfg = effective_config(geo, {}, "sample-house")
    return geo, cfg, build_mb(geo, cfg)


def test_sample_script_reproduces_the_committed_drawing(tmp_path):
    out = _make_sample_module().make(tmp_path / "s.dxf")
    a, b = extract(out), extract(SAMPLE)
    strip = lambda g: [{k: v for k, v in e.items() if k != "handle"} for e in g["entities"]]  # noqa: E731
    assert strip(a) == strip(b), "samples/sample-house.dxf is stale: run uv run python samples/make_sample.py"


def test_sample_regions_and_layers(sample):
    geo, cfg, res = sample
    regions = {r["name"]: r for r in cfg["regions"]}
    assert regions["GROUND FLOOR PLAN"]["kind"] == "plan" and regions["TERRACE PLAN"]["kind"] == "plan"
    assert regions["DOOR / WINDOW SCHEDULE"]["kind"] == "schedule"
    roles = cfg["layer_roles"]
    assert roles["A-WALL"] == "wall-brick" and roles["A-WALL PARAPET"] == "parapet" and roles["A-COLUMN"] == "column"
    assert roles["A-DW TAG"] == "door" and roles["A-TEXT DETAIL"] == "room text" and roles["A-DIM"] == "dimension"
    assert res.meta["plans"] == ["GROUND FLOOR PLAN"] and res.meta["roof_plan"] == "TERRACE PLAN"


def test_sample_rooms_and_openings(sample):
    _, _, res = sample
    assert sorted(r.name for r in res.rooms) == ["BEDROOM-1", "BEDROOM-2", "KITCHEN", "LIVING / DINING",
                                                 "TOILET-1", "TOILET-2"]
    assert {o.tag: o.count for o in res.openings} == {"D1": 1, "D2": 3, "D3": 2, "W1": 3, "W2": 1, "V1": 2}
    assert {o.tag: (o.width_mm, o.height_mm) for o in res.openings}["W2"] == (1200, 1200)
    items = {a.item for a in res.abstract}
    for prefix in ("Brick masonry (230", "Half-brick masonry", "Brick masonry in parapet", "RCC roof slab",
                   "Cement plaster to internal", "Coping over parapet", "Khurra"):
        assert any(i.startswith(prefix) for i in items), prefix
    khurra = next(a for a in res.abstract if a.item == "Khurra")
    assert khurra.qty == 2


def test_sample_wrong_label_flagged(sample):
    _, _, res = sample
    f = [f for f in res.flags if f.category == "room-size"]
    assert len(f) == 1 and f[0].severity == "error"
    assert "BEDROOM-2" in f[0].title and "3600X3000" in f[0].title and "3115" in f[0].title
    assert f[0].location and f[0].input


def test_sample_flags_well_formed(sample):
    _, _, res = sample
    ids = [f.id for f in res.flags]
    assert len(ids) == len(set(ids))
    for f in res.flags:
        assert f.severity in ("error", "warning", "missing") and f.status == "open"
    assert any(f.title.startswith("Lintel sizes not given") for f in res.flags)
    for r in res.mb:
        assert r.remarks and r.status in ("ok", "assumed", "unresolved")
    assert all(a.row_ids for a in res.abstract)
    assert sum(1 for r in res.mb if r.refs) > 0.8 * len(res.mb)


def test_sample_resolving_flags_feeds_back(sample):
    geo, cfg, res = sample
    room = next(f for f in res.flags if f.category == "room-size")
    r2 = build_mb(geo, {**cfg, "flags": {room.id: {"status": "resolved", "value": "3600X3115"}}})
    assert next(f for f in r2.flags if f.id == room.id).status == "resolved"
    bed = next(r for r in r2.rooms if r.name == "BEDROOM-2")
    assert (bed.width_mm, bed.length_mm) == (3600, 3115)
    # a wall-height answer changes the brickwork
    wh = next(f for f in res.flags if f.title.startswith("Wall height missing"))
    vals = [_brick(build_mb(geo, {**cfg, "flags": {wh.id: {"status": "resolved", "value": h}}})) for h in (3000, 3600)]
    assert vals[1] > vals[0] + 1.0


def test_sample_over_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import jobs
    from app import main as appmain
    from tests.test_api_pdf import _wait
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    client = TestClient(appmain.app)
    r = client.get("/api/sample/sample-house.dxf")
    assert r.status_code == 200 and r.content == SAMPLE.read_bytes()
    assert client.get("/api/sample/..%2Fpyproject.toml").status_code == 404
    assert client.get("/api/sample/nope.dxf").status_code == 404
    jid = client.post("/api/jobs", files={"file": ("sample-house.dxf", r.content, "application/dxf")}).json()["id"]
    st = _wait(client, jid)
    assert st["status"] == "ready" and st["stats"]["regions"] >= 3
    cfg = client.get(f"/api/jobs/{jid}/config").json()
    assert cfg["project"]["name"] == "sample-house"
    mb = client.get(f"/api/jobs/{jid}/mb").json()
    assert mb["kind"] == "architectural" and mb["rows"]
    assert any(f["category"] == "room-size" for f in mb["flags"])
    assert client.post(f"/api/jobs/{jid}/build", json=cfg).status_code == 200
    assert client.get(f"/api/jobs/{jid}/mb.xlsx").content[:2] == b"PK"


def _exe(name: str) -> str | None:
    return shutil.which(name) or next((str(p) for p in (Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name)
                                       if p.exists()), None)


@pytest.mark.skipif(not (_exe("dxf2dwg") and _exe("dwg2dxf")), reason="LibreDWG (dxf2dwg/dwg2dxf) not installed")
def test_dwg_upload_converts(tmp_path, monkeypatch):
    """A small DXF -> DWG with LibreDWG's dxf2dwg, uploaded as .dwg: the upload is accepted (DWG signature)
    and the job's conversion step runs dwg2dxf. Only the conversion is checked: DWGs written by the
    experimental dxf2dwg do not always read back cleanly, which says nothing about real CAD DWGs."""
    from fastapi.testclient import TestClient

    from app import jobs
    from app import main as appmain
    doc = ezdxf.new("R2000", units=4)
    msp = doc.modelspace()
    doc.layers.add("A-WALL")
    for a, b in (((0, 0), (5000, 0)), ((5000, 0), (5000, 4000)), ((5000, 4000), (0, 4000)), ((0, 4000), (0, 0))):
        msp.add_line(a, b, dxfattribs={"layer": "A-WALL"})
    msp.add_text("STUDY 4770X3770", dxfattribs={"insert": (1000, 2000), "height": 150})
    dxf, dwg = tmp_path / "t.dxf", tmp_path / "t.dwg"
    doc.saveas(dxf)
    subprocess.run([_exe("dxf2dwg"), "-y", "-o", str(dwg), str(dxf)], capture_output=True, timeout=120)
    if not dwg.exists():
        pytest.skip("dxf2dwg could not write the test DWG")
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "DWG2DXF", _exe("dwg2dxf"))
    monkeypatch.setattr(jobs, "submit", lambda job_id: None)       # convert by hand below
    (tmp_path / "jobs").mkdir()
    client = TestClient(appmain.app)
    r = client.post("/api/jobs", files={"file": ("plan.dwg", dwg.read_bytes(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    d = jobs.DATA_DIR / r.json()["id"]
    assert (d / "input.dwg").exists() and jobs.read_state(d)["kind"] == "dwg"
    out = jobs._convert(d)
    assert out == d / "input.dxf" and "STUDY 4770X3770" in out.read_text(errors="replace")
    assert (d / "convert.log").exists()


def test_web_mock_is_built_from_the_sample():
    mb = json.loads((MOCK / "mb.json").read_text())
    cfg = json.loads((MOCK / "config.json").read_text())
    geo = json.loads((MOCK / "geometry.json").read_text())
    assert "samples/sample-house.dxf" in mb["meta"]["source"]
    assert cfg["project"]["name"] == "sample-house"
    names = sorted(r["name"] for r in mb["rooms"])
    assert names == ["BEDROOM-1", "BEDROOM-2", "KITCHEN", "LIVING / DINING", "TOILET-1", "TOILET-2"]
    assert len(geo["entities"]) == len(extract(SAMPLE)["entities"])
    blob = json.dumps([mb, cfg]) + json.dumps(geo)
    assert not re.search(r"/tmp/|/Users/|[A-Z]:\\\\", blob)


def test_mock_rows_match_the_engine():
    """web/mock/mb.json is a saved engine run on the sample with the app's default config
    (web/mock/config.json): rebuild it after engine changes (uv run python web/mock/build_mock.py)."""
    geo = extract(SAMPLE)
    cfg = json.loads((MOCK / "config.json").read_text())
    mb = json.loads((MOCK / "mb.json").read_text())
    now = result_json(build_mb(geo, cfg))
    assert [r["id"] for r in mb["rows"]] == [r["id"] for r in now["rows"]]
    assert [round(a["qty"], 3) for a in mb["abstract"]] == [round(a["qty"], 3) for a in now["abstract"]]
