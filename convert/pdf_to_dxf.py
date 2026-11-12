"""Vector PDF -> DXF (ezdxf). All PDF access goes through app.pdf_backends.

Output units are millimetres ($INSUNITS = 4). Real-world size needs a scale:
  * scale="1:100" (or 100), scale="auto" (read from the title block), or
  * calibration={"p1": [x, y], "p2": [x, y], "mm": L} with p1/p2 in PDF points measured from the
    top-left of the displayed page (same spec as engine_struct.calibrate.user_calib).
Without a scale the DXF is the paper size: 1 PDF point = 25.4/72 mm, and report["scaled"] is False.

Recovered where possible: LINE, ARC/CIRCLE (Bezier runs that fit a circle), LWPOLYLINE with bulges,
SOLID/HATCH (filled shapes), TEXT/MTEXT (position, height, rotation). Other Beziers are flattened to
polylines within `tolerance` mm. report["dropped"] lists what could not be carried over.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import colors as ezcolors

from app.pdf_backends import open_pdf
from app.pdf_extract import (NEAR_EMPTY_TEXT, SHX_TEXT_LAYER, ScannedPDFError, _image_cover, fix_text, merge_spans,
                             page_is_scanned, parse_title_block, shx_annotations, stroke_stats, text_as_geometry)

PT_MM = 25.4 / 72.0
MAX_ENTITIES = 1_500_000
CAP_HEIGHT = 0.72            # cap height / font size (Helvetica-like)
SHEET_GAP = 0.05
LINEWEIGHTS = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
_BAD_LAYER = re.compile(r'[<>/\\":;?*|=`,\x00-\x1f]')


class ConvertError(ValueError):
    pass


# ---------------------------------------------------------------- scale

def parse_scale(s) -> float | None:
    """'1:100' / '100' / 100 -> 100.0; None/'' -> None; 'auto' is handled by the caller."""
    if s in (None, "", "none"):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.fullmatch(r"\s*1\s*:\s*(\d+(?:\.\d+)?)\s*", str(s)) or re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(s))
    if not m:
        raise ConvertError(f"cannot read scale {s!r}; use 1:100 or auto")
    v = float(m.group(1))
    if v <= 0:
        raise ConvertError("scale must be positive")
    return v


def parse_calibration(s: str) -> dict:
    """'x1,y1,x2,y2,real_mm' (PDF points from the page's top-left, real length in mm)."""
    try:
        x1, y1, x2, y2, mm = (float(v) for v in s.split(","))
    except ValueError:
        raise ConvertError("calibration is 'x1,y1,x2,y2,real_mm' (PDF points, mm)") from None
    return {"p1": [x1, y1], "p2": [x2, y2], "mm": mm}


def _mm_per_pt(spec_scale, calibration, auto_scale) -> tuple[float, str]:
    """-> (drawing mm per PDF point, how it was decided)."""
    if calibration:
        from engine_struct.calibrate import user_calib
        c = user_calib(1, calibration)
        if not c or not c.pt_per_mm:
            raise ConvertError("calibration needs two distinct points and a real length")
        return 1.0 / c.pt_per_mm, "calibration (two points and a real length)"
    if auto_scale:
        return PT_MM * auto_scale, f"title block scale 1:{auto_scale:g}"
    if spec_scale:
        return PT_MM * spec_scale, f"scale 1:{spec_scale:g}"
    return PT_MM, "none: paper size (1 PDF point = 0.3528 mm), not real-world size"


def _title_scale(texts) -> float | None:
    recs = []
    for sp in texts:
        s = sp.text.strip()
        if s:
            x0, y0, x1, y1 = sp.bbox
            recs.append({"s": s, "bb": [x0, -y1, x1, -y0]})      # parse_title_block wants y up
    tb = parse_title_block(recs) if recs else {}
    if tb.get("not_to_scale"):
        return None
    m = re.search(r"1\s*:\s*(\d+(?:\.\d+)?)", tb.get("scale") or "")
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- geometry (all in y-up mm)

def _circumcircle(a, b, c):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def _bez_pt(p0, c1, c2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p3[1])


def flatten_bezier(p0, c1, c2, p3, tol: float) -> list:
    """Points after p0 so that the polyline stays within `tol` of the curve (error <= 3/4 d / n^2)."""
    d = max(math.hypot(p0[0] - 2 * c1[0] + c2[0], p0[1] - 2 * c1[1] + c2[1]),
            math.hypot(c1[0] - 2 * c2[0] + p3[0], c1[1] - 2 * c2[1] + p3[1]))
    n = max(1, min(64, math.ceil(math.sqrt(0.75 * d / max(tol, 1e-9)))))
    return [_bez_pt(p0, c1, c2, p3, i / n) for i in range(1, n)] + [p3]


def _bez_as_arc(p0, c1, c2, p3, tol):
    """('A', centre, r, ccw, sweep) when the cubic is a circular arc within tol, 'L' when straight, else None."""
    chord = math.dist(p0, p3)
    pm = _bez_pt(p0, c1, c2, p3, 0.5)
    dev = max(_dist_to_line(c1, p0, p3), _dist_to_line(c2, p0, p3)) if chord > 1e-9 else math.dist(c1, p0)
    if dev <= tol * 0.5 and chord > 1e-9:
        return "L"
    cc = _circumcircle(p0, pm, p3)
    if not cc:
        return None
    (cx, cy), r = cc
    if r > 1e5 or r < 1e-6:
        return None
    for t in (0.25, 0.75, 0.1, 0.9):
        q = _bez_pt(p0, c1, c2, p3, t)
        if abs(math.hypot(q[0] - cx, q[1] - cy) - r) > tol:
            return None
    cross = (pm[0] - p0[0]) * (p3[1] - pm[1]) - (pm[1] - p0[1]) * (p3[0] - pm[0])
    ccw = cross > 0
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a3 = math.atan2(p3[1] - cy, p3[0] - cx)
    sweep = ((a3 - a0) if ccw else (a0 - a3)) % (2 * math.pi)
    if sweep < 1e-6:
        sweep = 2 * math.pi if chord < tol else sweep
    return ("A", (cx, cy), r, ccw, sweep)


def _dist_to_line(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    return abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / n if n else math.dist(p, a)


def path_runs(segs, tol: float) -> list[list[tuple]]:
    """segs (y-up mm) -> runs of connected items: ('L', p0, p1) | ('A', p0, p1, centre, r, ccw, sweep) | ('P', p0, pts)."""
    runs: list[list[tuple]] = []
    cur: list[tuple] = []
    end = None
    for kind, ps in segs:
        if kind == "re":
            if cur:
                runs.append(cur)
                cur, end = [], None
            runs.append([("L", ps[i], ps[(i + 1) % 4]) for i in range(4)])
            continue
        if end is None or math.dist(end, ps[0]) > max(1e-4, tol * 0.02):
            if cur:
                runs.append(cur)
            cur = []
        if kind == "l":
            if math.dist(ps[0], ps[1]) > 1e-9:
                cur.append(("L", ps[0], ps[1]))
        else:
            a = _bez_as_arc(*ps, tol)
            if a == "L":
                cur.append(("L", ps[0], ps[3]))
            elif a:
                if (cur and cur[-1][0] == "A" and cur[-1][5] == a[3] and math.dist(cur[-1][3], a[1]) <= tol
                        and abs(cur[-1][4] - a[2]) <= tol and cur[-1][2] == ps[0]):
                    last = cur[-1]
                    cur[-1] = ("A", last[1], ps[3], last[3], last[4], last[5], last[6] + a[4])
                else:
                    cur.append(("A", ps[0], ps[3], a[1], a[2], a[3], a[4]))
            else:
                cur.append(("P", ps[0], flatten_bezier(*ps, tol)))
        if cur:
            end = cur[-1][2] if cur[-1][0] != "P" else cur[-1][2][-1]
    if cur:
        runs.append(cur)
    return runs


def run_points(run, tol: float) -> tuple[list[list[float]], bool]:
    """run -> LWPOLYLINE vertices [x, y, bulge] and whether it closes."""
    pts: list[list[float]] = []
    for it in run:
        if it[0] == "L":
            if not pts:
                pts.append([it[1][0], it[1][1], 0.0])
            pts.append([it[2][0], it[2][1], 0.0])
        elif it[0] == "A":
            if not pts:
                pts.append([it[1][0], it[1][1], 0.0])
            if it[6] >= 1.98 * math.pi:           # a full turn has no bulge: go through the opposite point
                c, r = it[3], it[4]
                a0 = math.atan2(it[1][1] - c[1], it[1][0] - c[0])
                sgn = 1 if it[5] else -1
                pts[-1][2] = sgn
                pts.append([c[0] + r * math.cos(a0 + sgn * math.pi), c[1] + r * math.sin(a0 + sgn * math.pi), sgn])
                pts.append([it[2][0], it[2][1], 0.0])
            else:
                pts[-1][2] = math.tan(it[6] / 4) * (1 if it[5] else -1)
                pts.append([it[2][0], it[2][1], 0.0])
        else:
            if not pts:
                pts.append([it[1][0], it[1][1], 0.0])
            pts.extend([q[0], q[1], 0.0] for q in it[2])
    closed = len(pts) >= 3 and math.dist(pts[0][:2], pts[-1][:2]) <= max(1e-4, tol * 0.02)
    if closed:
        pts.pop()
    return pts, closed


def _convex(pts) -> bool:
    s = 0
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        z = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(z) > 1e-9:
            if s and (z > 0) != (s > 0):
                return False
            s = z
    return True


class _RectGrid:
    """Is a path entirely inside one of the rectangles? (grid index, rects in PDF points)"""

    def __init__(self, rects, cell: float = 50.0):
        self.cell, self.rects, self.g = cell, rects, {}
        for i, (x0, y0, x1, y1) in enumerate(rects):
            for cx in range(int(x0 // cell), int(x1 // cell) + 1):
                for cy in range(int(y0 // cell), int(y1 // cell) + 1):
                    self.g.setdefault((cx, cy), []).append(i)

    def contains(self, segs, pad: float = 0.6) -> bool:
        pts = [p for _, ps in segs for p in ps]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bx0, by0, bx1, by1 = min(xs), min(ys), max(xs), max(ys)
        for i in self.g.get((int(bx0 // self.cell), int(by0 // self.cell)), ()):
            x0, y0, x1, y1 = self.rects[i]
            if bx0 >= x0 - pad and bx1 <= x1 + pad and by0 >= y0 - pad and by1 <= y1 + pad:
                return True
        return False


# ---------------------------------------------------------------- DXF helpers

def _rgb255(rgb):
    return tuple(max(0, min(255, int(round(c * 255)))) for c in rgb[:3])


def _layer_name(s: str) -> str:
    s = _BAD_LAYER.sub("_", s).strip()[:255]
    return s or "0"


def _weight_code(width_pt: float) -> int:
    mm100 = width_pt * PT_MM * 100
    if mm100 <= 0:
        return -3                        # BYLAYER default weight
    return min(LINEWEIGHTS[1:], key=lambda w: abs(w - mm100))


class _Sink:
    def __init__(self, doc, tol: float, max_entities: int):
        self.doc, self.tol, self.cap = doc, tol, max_entities
        self.n = Counter()
        self.layers: dict[str, tuple] = {}
        self.truncated = False

    def total(self) -> int:
        return sum(self.n.values())

    def layer(self, name: str, rgb=None) -> str:
        name = _layer_name(name)
        if name not in self.layers:
            self.layers[name] = rgb
            if name not in self.doc.layers:
                lay = self.doc.layers.add(name)
                if rgb is not None and _rgb255(rgb) != (0, 0, 0):
                    lay.rgb = _rgb255(rgb)
        return name

    def attribs(self, layer, rgb, width_pt=0.0, dashed=False) -> dict:
        a = {"layer": layer, "lineweight": _weight_code(width_pt)}
        if rgb is not None:
            c = _rgb255(rgb)
            if c == (0, 0, 0):
                a["color"] = 7
            else:
                a["true_color"] = ezcolors.rgb2int(c)
        if dashed:
            a["linetype"] = "DASHED"
        return a

    def room(self) -> bool:
        if self.total() >= self.cap:
            self.truncated = True
            return False
        return True


def _write_path(sink: _Sink, tgt, runs, pth, layer: str, area_page: float) -> None:
    tol = sink.tol
    rgb = pth.stroke
    at = sink.attribs(layer, rgb, pth.width, pth.dashed)
    if pth.fill is not None:
        loops = []
        for run in runs:
            pts, closed = run_points(run, tol)
            if len(pts) >= 2 and (closed or len(pts) >= 3 or any(abs(p[2]) > 0 for p in pts)):
                loops.append(pts)
        if loops and sink.room():
            fa = sink.attribs(layer, pth.fill)
            if (len(loops) == 1 and len(loops[0]) in (3, 4) and all(p[2] == 0 for p in loops[0]) and _convex(loops[0])):
                p = loops[0]
                q = [(v[0], v[1]) for v in (p if len(p) == 3 else [p[0], p[1], p[3], p[2]])]
                if len(q) == 3:
                    q.append(q[2])
                tgt.add_solid(q, dxfattribs=fa)
                sink.n["SOLID"] += 1
            else:
                h = tgt.add_hatch(dxfattribs=fa)
                h.set_solid_fill(color=fa.get("color", 7), rgb=ezcolors.int2rgb(fa["true_color"]) if "true_color" in fa else None)
                for pts in loops:
                    h.paths.add_polyline_path([tuple(p) for p in pts], is_closed=True)
                sink.n["HATCH"] += 1
        if rgb is None:
            return
    for run in runs:
        if not sink.room():
            return
        if len(run) == 1 and run[0][0] == "L":
            tgt.add_line(run[0][1] + (0,), run[0][2] + (0,), dxfattribs=at)
            sink.n["LINE"] += 1
        elif len(run) == 1 and run[0][0] == "A":
            _, p0, p1, c, r, ccw, sweep = run[0]
            if sweep >= 1.98 * math.pi:
                tgt.add_circle(c, r, dxfattribs=at)
                sink.n["CIRCLE"] += 1
            else:
                a0 = math.degrees(math.atan2(p0[1] - c[1], p0[0] - c[0]))
                a1 = math.degrees(math.atan2(p1[1] - c[1], p1[0] - c[0]))
                tgt.add_arc(c, r, a0 if ccw else a1, a1 if ccw else a0, dxfattribs=at)
                sink.n["ARC"] += 1
        else:
            pts, closed = run_points(run, tol)
            if len(pts) >= 2:
                e = tgt.add_lwpolyline([tuple(p) for p in pts], format="xyb", close=closed, dxfattribs=at)
                sink.n["LWPOLYLINE"] += 1


def _write_text(sink: _Sink, tgt, text: str, x, y, height, rot, layer, rgb=None) -> None:
    if not sink.room():
        return
    at = sink.attribs(layer, rgb)
    at.pop("lineweight", None)
    if "\n" in text:
        m = tgt.add_mtext(text.replace("\n", "\\P"), dxfattribs={**at, "char_height": height, "rotation": rot,
                                                                 "insert": (x, y), "attachment_point": 7})
        sink.n["MTEXT"] += 1
    else:
        tgt.add_text(text, height=height, rotation=rot, dxfattribs={**at, "insert": (x, y)})
        sink.n["TEXT"] += 1


# ---------------------------------------------------------------- main

def pdf_to_dxf(pdf_path, out_path, page: int | None = None, scale=None, calibration: dict | None = None,
               tolerance: float = 0.2, layer_by: str = "auto", sheets: str = "offset",
               max_entities: int = MAX_ENTITIES, backend: str | None = None) -> dict:
    """Convert a vector PDF to DXF (R2018). page=None converts every page (sheets="offset": side by side;
    "blocks": one BLOCK per page, inserted side by side). Returns a report dict."""
    if layer_by not in ("auto", "ocg", "color", "weight"):
        raise ConvertError("layer_by must be auto, ocg, color or weight")
    if sheets not in ("offset", "blocks"):
        raise ConvertError("sheets must be offset or blocks")
    auto = isinstance(scale, str) and scale.strip().lower() == "auto"
    fixed = None if auto else parse_scale(scale)
    if tolerance <= 0:
        raise ConvertError("tolerance must be positive (mm)")

    if not Path(pdf_path).exists():
        raise ConvertError(f"{pdf_path} not found")
    try:
        doc_in = open_pdf(pdf_path, backend)
    except ValueError:
        raise
    except Exception as e:
        raise ConvertError(f"cannot open PDF: {e}") from None
    try:
        if page is not None and not 1 <= page <= doc_in.page_count:
            raise ConvertError(f"page {page} out of range (the PDF has {doc_in.page_count} pages)")
        idx = [page - 1] if page else list(range(doc_in.page_count))
        dxf = ezdxf.new("R2018", setup=True)
        dxf.units = 4
        dxf.header["$MEASUREMENT"] = 1
        sink = _Sink(dxf, tolerance, max_entities)
        msp = dxf.modelspace()
        ocg = bool(doc_in.ocg_layers())
        mode = layer_by if layer_by != "auto" else ("ocg" if ocg else "color")
        rep_pages, dropped = [], Counter()
        notes: list[str] = []
        xoff = 0.0
        scanned_pages = []
        scale_how = ""
        for i in idx:
            pg = doc_in.page(i)
            info = pg.info
            W, H = info.width, info.height
            paths, spans, images = pg.paths(), pg.text_spans(), pg.images()
            cover = _image_cover(images, W, H)
            if page_is_scanned(len(paths), len(spans), cover):
                scanned_pages.append(info.number)
                rep_pages.append({"page": info.number, "scanned": True})
                continue
            spans_m = merge_spans(spans)
            a_scale = _title_scale(spans_m) if auto else None
            mpp, how = _mm_per_pt(fixed, calibration, a_scale)
            if auto and not a_scale and not calibration:
                how = "none: no readable scale in the title block; paper size, not real-world size"
            scale_how = scale_how or how
            if how.startswith("none") and idx.index(i) == 0:
                notes.append("No scale given: drawing units are PDF points converted to millimetres (paper size). "
                             "Pass a scale (1:100) or a two-point calibration for real-world size.")

            tgt, blk = msp, None
            if sheets == "blocks" and len(idx) > 1:
                blk = dxf.blocks.new(f"SHEET_{info.number}")
                tgt = blk
            ox = 0.0 if blk is not None else xoff

            def T(p, ox=ox, mpp=mpp, H=H):
                return (p[0] * mpp + ox, (H - p[1]) * mpp)

            n0 = sink.total()
            shx = []
            try:
                shx = shx_annotations(pg.annotations())
            except Exception:
                pass
            grid = _RectGrid([an.rect for _, an in shx]) if shx else None
            n_glyph = 0
            # --- paths
            area = W * H
            for pth in paths:
                if not pth.segs or not (pth.stroke or pth.fill):
                    continue
                if grid is not None and pth.fill is None and grid.contains(pth.segs):
                    n_glyph += 1                      # glyph strokes of text that is written back as TEXT
                    continue
                if pth.fill is not None and pth.stroke is None and min(pth.fill) > 0.97:
                    xs = [p[0] for _, ps in pth.segs for p in ps]
                    ys = [p[1] for _, ps in pth.segs for p in ps]
                    if (max(xs) - min(xs)) * (max(ys) - min(ys)) >= 0.9 * area:
                        dropped["white page-background fills"] += 1
                        continue
                segs = [(k, [T(p) for p in ps]) for k, ps in pth.segs]
                runs = path_runs(segs, tolerance)
                if pth.closed and runs and runs[-1] and runs[-1][0][0] and pth.stroke is not None:
                    r = runs[-1]
                    s0 = r[0][1]
                    e = r[-1][2] if r[-1][0] != "P" else r[-1][2][-1]
                    if math.dist(s0, e) > 1e-4 and len(runs) == 1:
                        r.append(("L", e, s0))
                if mode == "ocg":
                    lname = pth.layer or "0"
                elif mode == "weight":
                    lname = f"WEIGHT_{_weight_code(pth.width) / 100:.2f}mm".replace("-", "") if pth.width > 0 else "WEIGHT_default"
                else:
                    c = pth.stroke if pth.stroke is not None else pth.fill
                    lname = "COLOR_%02X%02X%02X" % _rgb255(c)
                lay = sink.layer(lname, pth.stroke if pth.stroke is not None else pth.fill)
                _write_path(sink, tgt, runs, pth, lay, area)
                if sink.truncated:
                    break
            # --- text
            n_text = 0
            for sp in spans_m:
                t = sp.text.strip()
                if not t or sp.size <= 0:
                    continue
                x, y = T(sp.origin)
                rot = math.degrees(math.atan2(-sp.dir[1], sp.dir[0]))
                lay = sink.layer(sp.layer if (mode == "ocg" and sp.layer) else "TEXT")
                _write_text(sink, tgt, t, x, y, sp.size * mpp * CAP_HEIGHT, round(rot, 3) % 360, lay)
                n_text += 1
            for txt, an in shx:
                x0, y0, x1, y1 = an.rect
                w, h = x1 - x0, y1 - y0
                vertical = h > 1.2 * w and len(txt) > 1
                size = (w if vertical else h) * 0.8
                bx, by = (x0 + size, y1) if vertical else (x0, y1)
                x, y = T((bx, by))
                _write_text(sink, tgt, txt, x, y, size * mpp * CAP_HEIGHT, 90.0 if vertical else 0.0,
                            sink.layer(SHX_TEXT_LAYER))
            stats = stroke_stats(paths)
            tag = (not shx) and text_as_geometry(n_text, stats)
            if tag:
                notes.append(f"page {info.number}: text looks plotted as line strokes (SHX/outline fonts without "
                             "comments); it was written as LINE entities, not TEXT, and cannot be searched or edited.")
            if shx:
                notes.append(f"page {info.number}: {len(shx)} SHX text strings recovered from PDF comments and written "
                             f"as TEXT on layer '{SHX_TEXT_LAYER}' (position approximate); {n_glyph} glyph stroke paths inside those "
                             "text boxes were left out so the text is not drawn twice.")
                dropped["SHX glyph stroke paths (replaced by TEXT)"] += n_glyph
            if images:
                dropped["images"] += len(images)
            rep_pages.append({"page": info.number, "size_pt": [round(W, 1), round(H, 1)], "paths": len(paths),
                              "text_spans": len(spans), "images": len(images), "entities": sink.total() - n0,
                              "text_as_lines": bool(tag), "shx_texts": len(shx), "mm_per_pt": round(mpp, 6),
                              "scale_source": how, "offset_mm": round(ox, 3)})
            if blk is not None:
                msp.add_blockref(blk.name, (xoff, 0))
            xoff += W * mpp * (1 + SHEET_GAP)
            if sink.truncated:
                notes.append(f"stopped at {max_entities} entities (limit)")
                break
        if idx and len(scanned_pages) == len(idx):
            raise ScannedPDFError(
                "scanned PDF not supported: the pages are raster images with no vector paths or text. "
                "Export a vector PDF from CAD, or upload the DWG/DXF.")
        if scanned_pages:
            notes.append(f"pages {scanned_pages} are raster scans and were skipped.")
        if dropped["images"]:
            notes.append("raster images were not converted (no IMAGE entities written).")
        notes.append("PDF clipping paths, shadings/gradients and transparency are not carried over; clipped content is "
                     "written unclipped and shadings are absent. Dash patterns become the standard DASHED linetype.")
        scaled = not scale_how.startswith("none")
        dxf.header.custom_vars.append("NAAP_SCALE", scale_how)
        dxf.saveas(str(out_path))
        return {"ok": True, "output": str(out_path), "units": "mm", "scaled": scaled, "scale_source": scale_how,
                "tolerance_mm": tolerance, "layers": sorted(sink.layers), "layer_mode": mode,
                "entities": dict(sink.n), "entity_total": sink.total(), "truncated": sink.truncated,
                "dropped": dict(dropped), "pages": rep_pages, "notes": notes}
    finally:
        doc_in.close()
