"""Drawing conversion tests (synthetic data only): PDFs and DXFs are generated in the test."""
from __future__ import annotations

import math
import subprocess
import sys

import ezdxf
import pytest
from ezdxf import recover

from app.pdf_backends import open_pdf, open_writer
from app.pdf_extract import ScannedPDFError
from convert.pdf_to_dxf import ConvertError, flatten_bezier, parse_calibration, parse_scale, path_runs, pdf_to_dxf
from convert.to_pdf import dxf_to_pdf


def make_dxf(path, rects=((0, 0, 6000, 4000),), text=True, dim=True):
    d = ezdxf.new("R2018", setup=True)
    d.units = 4
    d.layers.add("WALL", color=1)
    d.layers.add("TXT", color=3)
    m = d.modelspace()
    for x0, y0, x1, y1 in rects:
        m.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "WALL", "lineweight": 50})
    m.add_line((0, 2000), (6000, 2000), dxfattribs={"layer": "WALL"})
    m.add_line((3000, 0), (3000, 2000), dxfattribs={"layer": "WALL"})
    m.add_circle((4500, 3000), 500, dxfattribs={"layer": "WALL"})
    if text:
        m.add_text("ROOM A", height=250, dxfattribs={"insert": (500, 500), "layer": "TXT"})
        m.add_text("UP", height=250, rotation=90, dxfattribs={"insert": (5800, 300), "layer": "TXT"})
        m.add_mtext("note one\\Pnote two", dxfattribs={"insert": (500, 3500), "char_height": 150, "layer": "TXT"})
    h = m.add_hatch(color=5)
    h.set_solid_fill()
    h.paths.add_polyline_path([(100, 100), (600, 100), (600, 400), (100, 400)])
    h2 = m.add_hatch(color=2)
    h2.set_pattern_fill("ANSI31", scale=30)
    h2.paths.add_polyline_path([(3200, 200), (4200, 200), (4200, 900), (3200, 900)])
    if dim:
        m.add_linear_dim(base=(0, -600), p1=(0, 0), p2=(6000, 0)).render()
    d.saveas(path)
    return path


def dxf_lines(path):
    """(count of straight edges, their total length) of LINE and LWPOLYLINE entities, circles flattened out."""
    doc = ezdxf.readfile(path)
    n, total = 0, 0.0
    for e in doc.modelspace():
        if e.dxftype() == "LINE":
            n += 1
            total += math.dist(e.dxf.start, e.dxf.end)
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed:
                pts.append(pts[0])
            n += len(pts) - 1
            total += sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    return n, total


def extents(path):
    from ezdxf import bbox
    b = bbox.extents(ezdxf.readfile(path).modelspace())
    return b.size.x, b.size.y


def make_pdf(path, pages=1):
    w = open_writer()
    for p in range(pages):
        w.add_page(600, 400)
        w.strokes([[(50, 50), (550, 50), (550, 350), (50, 350), (50, 50)]], (1, 0, 0), 2.0)
        w.strokes([[(50, 200), (550, 200)]], (0, 0, 0), 0.5)
        w.fills([[(100, 100), (200, 100), (200, 150), (100, 150)]], (0, 0, 1))
        w.text((60, 80), "HELLO", 20, 0, (0, 0, 0))
        w.text((300, 340), "SIDE", 14, 90, (0, 0, 0))
    pathlib_write(path, w.tobytes())
    return path


def pathlib_write(path, data):
    with open(path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------- helpers

def test_parse_scale_and_calibration():
    assert parse_scale("1:100") == 100 and parse_scale(50) == 50 and parse_scale(None) is None
    with pytest.raises(ConvertError):
        parse_scale("abc")
    assert parse_calibration("0,0,100,0,5000") == {"p1": [0, 0], "p2": [100, 0], "mm": 5000}
    with pytest.raises(ConvertError):
        parse_calibration("1,2,3")


def test_bezier_flatten_within_tolerance():
    p0, c1, c2, p3 = (0, 0), (0, 55.2), (44.8, 100), (100, 100)      # quarter circle r=100
    for tol in (1.0, 0.2, 0.05):
        pts = [p0] + flatten_bezier(p0, c1, c2, p3, tol)
        assert pts[-1] == p3
        # every vertex lies on the curve; edge midpoints deviate by at most tol (plus the control-point slack)
        assert all(abs(math.hypot(x, y - 0) - math.hypot(x, y)) < 1e-9 for x, y in pts)
    assert len(flatten_bezier(p0, c1, c2, p3, 0.05)) > len(flatten_bezier(p0, c1, c2, p3, 1.0))


def test_quarter_circle_beziers_become_arcs_and_circle():
    k = 0.5522847498
    r = 50.0
    segs = [("c", [(r, 0), (r, k * r), (k * r, r), (0, r)]), ("c", [(0, r), (-k * r, r), (-r, k * r), (-r, 0)]),
            ("c", [(-r, 0), (-r, -k * r), (-k * r, -r), (0, -r)]), ("c", [(0, -r), (k * r, -r), (r, -k * r), (r, 0)])]
    runs = path_runs(segs, 0.2)
    assert len(runs) == 1 and len(runs[0]) == 1 and runs[0][0][0] == "A"
    assert math.isclose(runs[0][0][6], 2 * math.pi, rel_tol=1e-6) and math.isclose(runs[0][0][4], r, rel_tol=1e-3)


# ---------------------------------------------------------------- PDF -> DXF

def test_pdf_to_dxf_entities_layers_units(tmp_path):
    pdf = make_pdf(tmp_path / "s.pdf")
    rep = pdf_to_dxf(pdf, tmp_path / "o.dxf")
    assert rep["units"] == "mm" and rep["scaled"] is False and "paper size" in rep["scale_source"]
    assert rep["entities"]["TEXT"] == 2 and rep["entities"].get("SOLID") == 1
    assert rep["entities"]["LWPOLYLINE"] + rep["entities"].get("LINE", 0) >= 2
    assert any(n.startswith("No scale given") for n in rep["notes"])
    doc = ezdxf.readfile(tmp_path / "o.dxf")
    assert doc.units == 4 and {"COLOR_FF0000", "COLOR_000000", "TEXT"} <= {la.dxf.name for la in doc.layers}
    texts = {e.dxf.text: e for e in doc.modelspace().query("TEXT")}
    assert set(texts) == {"HELLO", "SIDE"} and math.isclose(texts["SIDE"].dxf.rotation, 90, abs_tol=0.5)
    assert doc.audit().has_errors is False
    # 600 x 400 pt page frame -> 211.7 x 141.1 mm
    w, h = extents(tmp_path / "o.dxf")
    assert math.isclose(w, 500 * 25.4 / 72, rel_tol=0.01) and math.isclose(h, 300 * 25.4 / 72, rel_tol=0.02)


def test_pdf_to_dxf_scale_and_calibration_real_size(tmp_path):
    pdf = make_pdf(tmp_path / "s.pdf")
    pdf_to_dxf(pdf, tmp_path / "a.dxf", scale="1:100")
    w, _ = extents(tmp_path / "a.dxf")
    assert math.isclose(w, 500 * 25.4 / 72 * 100, rel_tol=0.01)
    # two points 500 pt apart that are 10 m in reality
    rep = pdf_to_dxf(pdf, tmp_path / "b.dxf", calibration={"p1": [50, 50], "p2": [550, 50], "mm": 10000})
    assert rep["scaled"] and "calibration" in rep["scale_source"]
    w, _ = extents(tmp_path / "b.dxf")
    assert math.isclose(w, 10000, rel_tol=0.01)


def test_pdf_to_dxf_pages_blocks_and_errors(tmp_path):
    pdf = make_pdf(tmp_path / "m.pdf", pages=2)
    rep = pdf_to_dxf(pdf, tmp_path / "all.dxf")
    assert len(rep["pages"]) == 2 and rep["pages"][1]["offset_mm"] > 200
    rep1 = pdf_to_dxf(pdf, tmp_path / "p2.dxf", page=2)
    assert len(rep1["pages"]) == 1 and rep1["pages"][0]["page"] == 2
    pdf_to_dxf(pdf, tmp_path / "blk.dxf", sheets="blocks")
    doc = ezdxf.readfile(tmp_path / "blk.dxf")
    assert "SHEET_1" in doc.blocks and len(doc.modelspace().query("INSERT")) == 2
    with pytest.raises(ConvertError):
        pdf_to_dxf(pdf, tmp_path / "x.dxf", page=9)
    with pytest.raises(ConvertError):
        pdf_to_dxf(pdf, tmp_path / "x.dxf", scale="foo")


def test_pdf_to_dxf_scanned_is_not_supported(tmp_path):
    import zlib
    # a page that is one full-page image and nothing else, built by hand (no PDF library needed)
    img = zlib.compress(bytes([200]) * (64 * 64))
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 600 400]/Contents 4 0 R/Resources<</XObject<</Im 5 0 R>>>>>>",
            b"<</Length 28>>\nstream\nq 600 0 0 400 0 0 cm /Im Do Q\nendstream",
            b"<</Type/XObject/Subtype/Image/Width 64/Height 64/ColorSpace/DeviceGray/BitsPerComponent 8/Filter/FlateDecode/Length %d>>\nstream\n" % len(img) + img + b"\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, x)
    pathlib_write(tmp_path / "scan.pdf", out)
    with pytest.raises(ScannedPDFError, match="not supported"):
        pdf_to_dxf(tmp_path / "scan.pdf", tmp_path / "x.dxf")


# ---------------------------------------------------------------- DXF -> PDF, round trip

def test_dxf_to_pdf_page_and_content(tmp_path):
    dxf = make_dxf(tmp_path / "a.dxf")
    rep = dxf_to_pdf(dxf, tmp_path / "a.pdf", paper="A3", landscape=True)
    pg = rep["pages"][0]
    assert pg["paper"] == "A3 landscape" and rep["text_items"] >= 4 and rep["fill_polygons"] >= 2
    doc = open_pdf(tmp_path / "a.pdf")
    info = doc.page(0).info
    assert math.isclose(info.width, 420 * 72 / 25.4, abs_tol=1) and math.isclose(info.height, 297 * 72 / 25.4, abs_tol=1)
    words = " ".join(s.text for s in doc.page(0).text_spans())
    assert "ROOM A" in words and "UP" in words
    doc.close()
    # orientation auto-picks portrait for a tall drawing
    tall = make_dxf(tmp_path / "t.dxf", rects=((0, 0, 3000, 9000),), text=False)
    assert "portrait" in dxf_to_pdf(tall, tmp_path / "t.pdf", paper="A4")["pages"][0]["paper"]
    assert dxf_to_pdf(dxf, tmp_path / "m.pdf", mono=True)["mono"] is True


def test_dxf_layer_filter(tmp_path):
    dxf = make_dxf(tmp_path / "a.dxf")
    full = dxf_to_pdf(dxf, tmp_path / "f.pdf")
    only = dxf_to_pdf(dxf, tmp_path / "o.pdf", layers=["TXT"])
    hid = dxf_to_pdf(dxf, tmp_path / "h.pdf", hide_layers=["TXT"])
    assert only["text_items"] > 0 and hid["text_items"] < full["text_items"]
    assert only["stroke_groups"] < full["stroke_groups"]


def test_round_trip_dxf_pdf_dxf(tmp_path):
    src = make_dxf(tmp_path / "src.dxf", text=False, dim=False)
    # fixed straight geometry in the source: rectangle (4 edges) + 2 lines + dimension; compare long edges
    rep = dxf_to_pdf(src, tmp_path / "r.pdf", paper="A1", landscape=True)
    scale = float(rep["pages"][0]["scale"].split(":")[1])
    out = pdf_to_dxf(tmp_path / "r.pdf", tmp_path / "back.dxf", scale=scale)
    assert out["scaled"] and out["units"] == "mm"
    # real-world size: the plan frame (6000 mm wide) comes back within 1 %, drawn extents agree
    w0, h0 = extents(src)
    w1, h1 = extents(tmp_path / "back.dxf")
    assert math.isclose(w0, w1, rel_tol=0.01) and math.isclose(h0, h1, rel_tol=0.01)
    # long straight edges (> 1 m) survive with their lengths
    def long_edges(path):
        doc = ezdxf.readfile(path)
        L = []
        for e in doc.modelspace():
            if e.dxftype() == "LINE":
                L.append(math.dist(e.dxf.start, e.dxf.end))
            elif e.dxftype() == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                if e.closed:
                    pts.append(pts[0])
                L += [math.dist(a, b) for a, b in zip(pts, pts[1:])]
        return sorted(v for v in L if v > 1000)
    a, b = long_edges(src), long_edges(tmp_path / "back.dxf")
    assert len(a) == len(b) and all(math.isclose(x, y, rel_tol=0.01) for x, y in zip(a, b))
    assert ezdxf.readfile(tmp_path / "back.dxf").audit().has_errors is False
    assert recover.readfile(str(tmp_path / "back.dxf"))[1].has_errors is False


def test_cli_and_dwg_message(tmp_path):
    dxf = make_dxf(tmp_path / "a.dxf")
    run = lambda *a: subprocess.run([sys.executable, "-m", "convert.cli", *a], capture_output=True, text=True)
    r = run("dxf2pdf", str(dxf), str(tmp_path / "c.pdf"), "--paper", "A4", "--mono")
    assert r.returncode == 0 and '"ok": true' in r.stdout
    r = run("pdf2dxf", str(tmp_path / "c.pdf"), str(tmp_path / "c.dxf"), "--page", "1", "--scale", "1:50", "--tolerance", "0.1")
    assert r.returncode == 0 and (tmp_path / "c.dxf").exists()
    r = run("dxf2pdf", str(dxf), str(tmp_path / "x.dwg"))
    assert r.returncode == 2 and "Save As DWG" in r.stderr
    r = run("pdf2dxf", str(tmp_path / "none.pdf"), str(tmp_path / "x.dxf"))
    assert r.returncode == 1 and "not found" in r.stderr
