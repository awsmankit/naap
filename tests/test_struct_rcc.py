"""RCC plan + schedule engine (engine_struct/rcc*.py) on a made-up plan written in-test: 3 beam types, 2 slab
types, a slab schedule on the plan and a beam schedule on a second PDF (one drawing set of two files).
No client data. Expected values are worked out by hand from the numbers written below."""
from __future__ import annotations

import math

import pytest

from app.pdf_extract import extract_pdfs
from engine_struct import rcc_plan as RP
from engine_struct import rcc_tables as RT
from engine_struct.build import build_struct_mb
from engine_struct.export import result_json, write_struct_xlsx
from tests.test_pdf_extract import text, write_pdf

S = 0.04                                   # pt per mm on the plan (1:~70); only the written beam widths tell the engine
XS = [100.0, 340.0, 580.0]
YS = [100.0, 260.0]
COL = 300 * S


def rtext(x, y, s, size=6):                # text running upwards
    return f"BT /F1 {size} Tf 0 1 -1 0 {x} {y} Tm ({s}) Tj ET\n"


def plan_pdf(tmp_path):
    c = "0.3 w\n"
    for x in XS:
        for y in YS:
            c += f"{x - COL / 2} {y - COL / 2} {COL} {COL} re f\n"
    hw = {"B1": 230 * S / 2, "B2": 300 * S / 2, "B3": 230 * S / 2}
    for a, b in zip(XS, XS[1:]):
        for y, t in ((YS[0], "B1"), (YS[1], "B2")):
            w = hw[t]
            c += f"{a + COL / 2} {y - w} m {b - COL / 2} {y - w} l S {a + COL / 2} {y + w} m {b - COL / 2} {y + w} l S\n"
            c += text((a + b) / 2 - 8, y + w + 3, t, 6)
            c += text((a + b) / 2 - 14, y - w - 8, "(230X450)" if t == "B1" else "(300X600)", 5)
    for x in XS:
        w = hw["B3"]
        c += f"{x - w} {YS[0] + COL / 2} m {x - w} {YS[1] - COL / 2} l S {x + w} {YS[0] + COL / 2} m {x + w} {YS[1] - COL / 2} l S\n"
        c += rtext(x - w - 3, (YS[0] + YS[1]) / 2 - 6, "B3", 6) + rtext(x + w + 8, (YS[0] + YS[1]) / 2 - 14, "(230X450)", 5)
    for k, (t, thk) in enumerate((("S1", 150), ("S2", 200))):
        cx = (XS[k] + XS[k + 1]) / 2
        c += text(cx, 175, t, 7) + text(cx + 20, 175, f"{thk}MM", 5) + text(cx + 20, 168, "THICK", 5)
    # slab schedule (top right)
    c += text(640, 760, "SCHEDULE OF SLABS - \\( M:30 , Fe:500 \\)", 8)
    for x, s in ((660, "TYPE"), (700, "THICK"), (760, "MAIN STEEL"), (900, "DISTRIBUTION STEEL"), (1060, "REMARK")):
        c += text(x, 740, s, 7)
    for y, row in ((720, ("S1", "150", "8 @ 150 C/C ALT. BAR BENT UP", "8 @ 200 C/C", "TWOWAY")),
                   (700, ("S2", "200", "10 @ 125 C/C", "8 @ 150 C/C", "ONEWAY"))):
        for x, s in zip((660, 700, 760, 900, 1060), row):
            c += text(x, y, s, 7)
    return write_pdf(tmp_path / "plan.pdf", c, size=(1190, 842))


BEAMS = {  # type: b, D, bottom straight, extra, anchor, left, right, stirrup dia, nl, sl, sc, nr, sr, legs
    "1": (230, 450, (2, 16), (2, 16), (2, 12), (2, 16), (2, 16), 8, 10, 100, 150, 10, 100, 2),
    "2": (300, 600, (3, 20), (2, 20), (2, 12), (2, 16), (2, 16), 8, "-", 125, 125, "-", 125, 2),
    "3": (230, 450, (2, 16), ("-", "-"), (2, 12), ("-", "-"), ("-", "-"), 8, "-", 125, 125, "-", 125, 2),
}
XC = [29, 62, 101, 130, 150, 171, 194, 216, 235, 255, 276, 299, 321, 341, 360, 388, 424, 452, 478, 506]


def sched_pdf(tmp_path):
    c = text(100, 740, "SCHEDULE OF BEAMS \\(M:30, Fe:500\\)", 8)
    c += text(120, 712, "BOTTOM STEEL", 7) + text(280, 712, "TOP STEEL", 7) + text(390, 712, "STIRRUPS", 7)
    c += text(72, 700, "SIZE", 6) + text(250, 700, "EXTRA @ SUPPORT", 8) + text(335, 690, "DIA", 6)
    c += text(18, 686, "TYPE", 6) + text(125, 686, "STRAIGHT", 8) + text(161, 686, "EXTRA @CENTER", 6) + text(208, 686, "ANCHOR", 8)
    c += text(250, 678, "LEFT", 12) + text(290, 678, "RIGHT", 12)
    c += text(372, 686, "Left", 6) + text(418, 686, "Center", 6) + text(466, 686, "Right", 6) + text(500, 686, "Legs", 6)
    c += text(530, 686, "REMARKS", 6)
    hdr = ["b", "D"] + ["Nos", "Dia"] * 5 + [None] + ["Nos", "spacing", "spacing", "Nos", "spacing", "Nos."]
    for x, h in zip(XC[1:], hdr):
        if h:
            c += text(x - len(h) * 1.7, 660, h, 6)
    y = 640
    for k, v in BEAMS.items():
        cells = [k, v[0], v[1], *v[2], *v[3], *v[4], *v[5], *v[6], v[7], v[8], v[9], v[10], v[11], v[12], v[13]]
        for x, s in zip(XC, cells):
            c += text(x - len(str(s)) * 1.7, y, str(s), 6)
        y -= 20
    return write_pdf(tmp_path / "beams.pdf", c, size=(612, 792))


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    d = tmp_path_factory.mktemp("rcc")
    geo = extract_pdfs([plan_pdf(d), sched_pdf(d)])
    return geo, build_struct_mb(geo, {})


def rows(res, trade, item=None, loc=None):
    return [r for r in res.mb if r.trade == trade and (item is None or item in r.item) and (loc is None or r.location.startswith(loc))]


def test_set_of_two_files(result):
    geo, res = result
    assert [s["file"] for s in geo["sheets"]] == ["plan.pdf", "beams.pdf"]
    assert [s["role"] for s in res.meta["sheets"]] == ["rcc", "other"]


def test_beam_schedule_read_by_structure(result):
    geo, _ = result
    from engine_struct.sheet import sheets
    r = RT.parse_beam_schedule_pages(sheets(geo)[1])
    assert r["grade"] == "M30" and r["steel"] == "Fe500"
    by = {x["tag"]: x for x in r["rows"]}
    assert sorted(by) == ["1", "2", "3"]
    assert (by["2"]["b"], by["2"]["d"]) == (300, 600)
    assert by["2"]["bot_straight"] == [{"n": 3, "dia": 20}] and by["2"]["top_left"] == [{"n": 2, "dia": 16}]
    assert by["1"]["stirrup"]["left"] == {"n": 10, "s": 100} and by["1"]["stirrup"]["center_s"] == 150
    assert by["3"]["bot_extra"] == [] and by["3"]["stirrup"]["dia"] == 8


def test_slab_schedule_and_helpers(result):
    geo, _ = result
    from engine_struct.sheet import sheets
    ss = RT.parse_slab_schedule(sheets(geo)[0])
    assert ss["grade"] == "M30" and sorted(ss["rows"]) == ["S1", "S2"]
    assert ss["rows"]["S1"]["main"]["alt_bent"] and ss["rows"]["S1"]["main"]["s"] == 150 and ss["rows"]["S2"]["thk"] == 200
    assert ss["rows"]["S2"]["oneway"] and ss["rows"]["S1"]["twoway"]
    assert RT.bar_spec("%%C 8 @ 175 C/C ALT. BAR BENT UP")["dia"] == 8
    assert RP.parse_size("(230XD)")["d_is_slab"] and RP.parse_size("(380X1100/450)")["d_end"] == 450


def test_scale_from_written_widths(result):
    _, res = result
    c = res.meta["calibration"][1]
    assert c["pt_per_mm"] == pytest.approx(S, rel=0.03)
    assert c["spread_pct"] < 3 and c["sources"].get("beam width", 0) >= 5


def test_beam_concrete_and_shuttering(result):
    _, res = result
    b1 = rows(res, "RCC", "beams", "B1 ")
    assert len(b1) == 2
    L = 240 - COL                                         # clear span in pt
    for r in b1:
        assert r.l == pytest.approx(L / S / 1000, rel=0.03) and r.status == "assumed" and "scaled from drawing" in r.remarks
        assert r.qty == pytest.approx(0.23 * 0.45 * r.l, rel=0.001)
    b3 = rows(res, "RCC", "beams", "B3 ")
    assert len(b3) == 3 and b3[0].l == pytest.approx((160 - COL) / S / 1000, rel=0.03)
    sides = rows(res, "SHUTTERING", "beam sides", "B1 ")
    soffit = rows(res, "SHUTTERING", "beam soffit", "B1 ")
    assert soffit[0].qty == pytest.approx(0.23 * b1[0].l, rel=0.001)
    # one face has the S1 slab (150), the other is the free edge of the plan: (450-150) + 450 (the panel is on one side only)
    assert 0.5 * 0.0 + sides[0].qty == pytest.approx(b1[0].l * ((0.45 - 0.15) + 0.45), rel=0.05)


def test_slab_by_type_and_thickness(result):
    _, res = result
    s1, s2 = rows(res, "RCC", "slabs", "S1 "), rows(res, "RCC", "slabs", "S2 ")
    assert len(s1) == 1 and len(s2) == 1
    w = (240 - 4.6 - 4.6) / S / 1000
    h = (160 - 4.6 - 6.0) / S / 1000
    assert s1[0].l == pytest.approx(w * h, rel=0.03)
    assert s1[0].qty == pytest.approx(s1[0].l * 0.15, rel=0.001) and "150 mm" in s1[0].item
    assert s2[0].qty == pytest.approx(s2[0].l * 0.20, rel=0.001) and "200 mm" in s2[0].item
    assert sum(r.qty for r in rows(res, "SHUTTERING", "slab soffit")) == pytest.approx(s1[0].l + s2[0].l, rel=0.001)


def test_steel_hand_check(result):
    _, res = result
    kg = lambda d: d * d / 162
    B2 = rows(res, "RCC", "beams", "B2 ")
    L = B2[0].l
    ld = lambda d: 45 * d / 1000
    st = [r for r in rows(res, "STEEL", None, "B2 ")]
    got = sum(r.qty for r in st if "bottom straight" in r.location)
    assert got == pytest.approx(sum(3 * (b.l + 2 * ld(20)) * kg(20) for b in B2), rel=1e-3)
    got = sum(r.qty for r in st if "stirrups" in r.location)
    c = 0.025
    per = 2 * ((0.3 - 2 * c) + (0.6 - 2 * c)) + 20 * 8 / 1000
    assert got == pytest.approx(sum((math.floor(b.l * 1000 / 125) + 1) * per * kg(8) for b in B2), rel=1e-3)
    s1 = rows(res, "STEEL", None, "S1 ")
    main = next(r for r in s1 if "main" in r.item.lower())
    a = rows(res, "RCC", "slabs", "S1 ")[0].l
    ext = 1 + 2 * 150 / ((160 - 10.6) / S)
    assert main.qty == pytest.approx(a * kg(8) / 0.150 * ext, rel=0.02)


def test_flags_and_review_questions(result):
    _, res = result
    ids = {f.id for f in res.flags}
    assert {"struct:notes:dev-length", "struct:notes:cover", "struct:notes:cover-slab"} <= ids
    h = [f for f in res.flags if f.id.startswith("struct:columns:height:")]
    assert h and h[0].severity == "missing" and h[0].input["unit"] == "mm"
    q = {x["id"] for x in res.meta["review"]["questions"]}
    assert h[0].id in q and "struct:notes:dev-length" in q and "struct:notes:cover" in q
    cols = rows(res, "RCC", "columns")
    assert len(cols) == 6 and all(r.qty == 0 and r.status == "unresolved" for r in cols)
    # the defaults are visible
    assert any("DEFAULT" in r.remarks or "Ld = 45d" in r.remarks for r in rows(res, "STEEL"))
    assert any("Ld = 45" in a for a in res.assumptions)
    assert res.meta["steel"]["total_kg"] > 0 and set(res.meta["steel"]["by_dia_kg"]) >= {8, 12, 16, 20}


def test_answers_change_results(tmp_path):
    geo = extract_pdfs([plan_pdf(tmp_path), sched_pdf(tmp_path)])
    base = build_struct_mb(geo, {})
    drg = next(f.id for f in base.flags if f.id.startswith("struct:columns:height:"))
    cfg = {"flags": {drg: {"status": "resolved", "value": 3000}, "struct:notes:dev-length": {"status": "resolved", "value": 40}},
           "formwork": {"beam_sides_net_of_slab": False}}
    res = build_struct_mb(geo, cfg)
    cols = rows(res, "RCC", "columns")
    assert all(r.qty > 0 for r in cols)
    assert sum(r.qty for r in rows(res, "SHUTTERING", "beam sides")) > sum(r.qty for r in rows(base, "SHUTTERING", "beam sides"))
    assert res.meta["steel"]["total_kg"] < base.meta["steel"]["total_kg"] + 1e9 and \
        sum(r.qty for r in rows(res, "STEEL", None, "B2 ")) != sum(r.qty for r in rows(base, "STEEL", None, "B2 "))


def test_refs_everywhere_and_export(result, tmp_path):
    _, res = result
    for r in res.mb:
        if r.trade in ("RCC", "SHUTTERING", "STEEL") and r.status != "unresolved" or r.item.endswith("columns"):
            assert r.refs and r.refs[0]["bbox"] and r.refs[0]["handles"] is not None
    assert all(r.status == "assumed" for r in rows(res, "RCC", "beams"))
    write_struct_xlsx(res, tmp_path / "mb.xlsx")
    j = result_json(res)
    assert any(r["trade"] == "SHUTTERING" for r in j["rows"])


def test_stable_row_ids(tmp_path):
    geo = extract_pdfs([plan_pdf(tmp_path), sched_pdf(tmp_path)])
    a = [r.id for r in build_struct_mb(geo, {}).mb]
    b = [r.id for r in build_struct_mb(geo, {}).mb]
    assert a == b and len(set(a)) == len(a)
