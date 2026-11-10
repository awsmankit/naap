"""Structural PDF end to end on a made-up floor sheet written in-test: the PDF backend, beam and slab
schedules, beam strips between columns, notes (grade, covers, development lengths), concrete and
steel rows, stable row ids, and the API for a structural job. No client files."""
from __future__ import annotations

import pytest

from app.pdf_backends import open_pdf
from app.pdf_extract import extract_pdf
from engine_struct.build import build_struct_mb
from engine_struct.export import result_json
from tests.test_pdf_extract import text, write_pdf

S = 0.03                                   # sheet scale: 0.03 pt per mm (written dimensions calibrate it)
XS = [60, 60 + 4000 * S, 60 + 8000 * S]    # column grid 4000 x 5000, 300 x 300 columns
YS = [80, 80 + 5000 * S]
COL = 300 * S
BEAM_W = 230


def _table(x0, ytop, widths, rows, rh=14, size=6):
    c, xs = "", [x0]
    for w in widths:
        xs.append(xs[-1] + w)
    for k in range(len(rows) + 1):
        c += f"{xs[0]} {ytop - k * rh} m {xs[-1]} {ytop - k * rh} l S\n"
    for x in xs:
        c += f"{x} {ytop} m {x} {ytop - len(rows) * rh} l S\n"
    for k, row in enumerate(rows):
        for x, s in zip(xs, row):
            if s:
                c += text(x + 3, ytop - (k + 1) * rh + 4, s, size)
    return c


def _dim(x0, x1, y, value):
    c = f"{x0 - 2} {y} m {x1 + 2} {y} l S\n{x0} {y - 6} m {x0} {y + 3} l S\n{x1} {y - 6} m {x1} {y + 3} l S\n"
    return c + text((x0 + x1) / 2 - 8, y + 2, str(value), 5)


def floor_sheet(tmp_path):
    """One sheet: 2 x 2 bays of beams (dashed double lines) on 6 columns, B1/B2/B3 and S1 tags,
    a beam schedule (with the code row), a slab schedule, notes and a title block."""
    c = "0.4 w\n"
    for x in XS:
        for y in YS:
            c += f"{x - COL / 2} {y - COL / 2} {COL} {COL} re f\n"
    bw = BEAM_W * S / 2
    c += "[3 2] 0 d\n"
    for y in YS:
        for a, b in zip(XS, XS[1:]):
            c += f"{a + COL / 2} {y - bw} m {b - COL / 2} {y - bw} l S {a + COL / 2} {y + bw} m {b - COL / 2} {y + bw} l S\n"
    for x in XS:
        c += (f"{x - bw} {YS[0] + COL / 2} m {x - bw} {YS[1] - COL / 2} l S "
              f"{x + bw} {YS[0] + COL / 2} m {x + bw} {YS[1] - COL / 2} l S\n")
    c += "[] 0 d\n"
    for a, b in zip(XS, XS[1:]):
        c += text((a + b) / 2, YS[0] + 6, "B1", 6) + text((a + b) / 2, YS[1] + 6, "B2", 6)
        c += text((a + b) / 2, (YS[0] + YS[1]) / 2, "S1", 7)
    for x in XS:
        c += text(x + 6, (YS[0] + YS[1]) / 2, "B3", 6)
    c += _dim(XS[0], XS[1], 40, 4000) + _dim(XS[1], XS[2], 40, 4000) + _dim(XS[0], XS[2], 25, 8000)
    c += text(420, 560, "BEAM SCHEDULE", 8)
    c += _table(420, 550, [40, 25, 25, 40, 40, 50, 50], [
        ("B.NO.", "B", "D", "TOP", "BOTTOM", "STIRRUPS", ""),
        ("B.NO.", "B", "D", "T1", "B1", "S1", "S2"),
        ("B1", "230", "450", "2-12", "3-16", "8@100C/C", "8@150C/C"),
        ("B2", "230", "450", "2-12", "3-16", "8@100C/C", "8@150C/C"),
        ("B3", "230", "600", "2-16", "3-25", "8@100C/C", "8@150C/C")])
    c += text(420, 420, "SLAB SCHEDULE", 8)
    c += _table(420, 410, [40, 50, 60, 60], [("SLAB", "THICKNESS", "SHORT SPAN", "LONG SPAN"),
                                          ("S1", "125", "8@150C/C", "8@200C/C")])
    notes = ["NOTES:", "CONCRETE MIX M25, STEEL FE500", "CLEAR COVER:", "BEAMS 25 MM", "SLABS 20 MM",
             "DEVELOPMENT LENGTH SHALL FOLLOW:", "8 400 320", "10 500 400", "12 600 480", "16 800 640",
             "20 1000 800", "25 1250 1000",
             "LAP LENGTH 50 X BAR DIA."]
    for k, ln in enumerate(notes):
        c += text(420, 340 - 9 * k, ln, 5)
    c += text(600, 120, "DRG. NO.", 6) + text(600, 105, "ABC-STR-03", 9)
    c += text(600, 90, "DRG. TITLE:", 6) + text(600, 78, "FIRST FLOOR SLAB AND BEAM LAYOUT", 8)
    c += text(600, 50, "SCALE", 6) + text(600, 40, "1:100", 8)
    return write_pdf(tmp_path / "floor.pdf", c)


@pytest.fixture(scope="module")
def sheet_pdf(tmp_path_factory):
    return floor_sheet(tmp_path_factory.mktemp("struct"))


def test_backend_reads_the_synthetic_pdf(sheet_pdf):
    doc = open_pdf(sheet_pdf)
    try:
        assert doc.page_count == 1
        page = doc.page(0)
        spans = {s.text.strip() for s in page.text_spans()}
        assert {"B.NO.", "B1", "S1", "ABC-STR-03", "8@100C/C"} <= spans
        paths = page.paths()
        assert any(p.dashed for p in paths) and any(p.fill for p in paths)
        assert page.images() == []
    finally:
        doc.close()


def test_struct_rows_and_steel(sheet_pdf):
    g = extract_pdf(sheet_pdf)
    res = build_struct_mb(g, {})
    js = result_json(res)
    beams = {r.location.split()[0]: r for r in res.mb if r.item == "RCC M25 in beams"}
    assert set(beams) == {"B1", "B2", "B3"}
    # B1: two clear spans between 300 columns (2 x 3.7 m) x 0.23 x 0.45
    assert beams["B1"].qty == pytest.approx(2 * 3.7 * 0.23 * 0.45, rel=0.03)
    slabs = [r for r in res.mb if r.item == "RCC M25 in slabs"]
    assert len(slabs) == 2 and all("S1 125 thk" in r.location for r in slabs)
    steel = js["steel"]
    assert steel["total_kg"] > 0 and set(steel["by_dia_kg"]) >= {8, 12, 16, 25}
    assert any(r.item.startswith("Steel reinforcement") for r in res.mb)
    assert all(ref.get("sheet") == 1 for r in res.mb for ref in r.refs)
    assert not any(f.title.startswith("Concrete grade not written") for f in res.flags)
    assert not any("beam schedule not found" in f.title for f in res.flags)


def test_struct_rows_stable_ids(sheet_pdf):
    g = extract_pdf(sheet_pdf)
    a, b = build_struct_mb(g, {}), build_struct_mb(g, {})
    assert a.mb and [r.id for r in a.mb] == [r.id for r in b.mb]
    assert len({r.id for r in a.mb}) == len(a.mb)
    assert all(set(ab.row_ids) <= {r.id for r in a.mb} for ab in a.abstract)


def test_struct_job_over_api(sheet_pdf, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import jobs
    from app import main as appmain
    from tests.test_api_pdf import _upload, _wait
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    client = TestClient(appmain.app)
    jid = _upload(client, sheet_pdf).json()["id"]
    assert _wait(client, jid)["status"] == "ready"
    mb = client.get(f"/api/jobs/{jid}/mb").json()
    assert mb["kind"] == "structural" and mb["steel"]["total_kg"] > 0
    assert any(t["name"].lower().startswith("beam") for t in mb["schedules"]["tables"])
    assert client.post(f"/api/jobs/{jid}/build", json=client.get(f"/api/jobs/{jid}/config").json()).status_code == 200
    assert client.get(f"/api/jobs/{jid}/mb.xlsx").content[:2] == b"PK"
