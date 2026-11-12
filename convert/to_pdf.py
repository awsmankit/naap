"""DXF (or DWG, via LibreDWG's dwg2dxf) -> vector PDF, fast.

Pipeline: ezdxf's drawing frontend resolves layers, blocks, dimensions, hatches, linetypes and
viewports into plain primitives; a recording backend collects them; the PDF is written through
app.pdf_backends.open_writer() (PyMuPDF stays in its backend module). The matplotlib backend is not
used (minutes on large drawings); this path is a single pass with one PDF operation per style group.

Deviations from an AutoCAD plot (also listed in report["deviations"]):
  * no pen table (CTB/STB): colours are ACI/true colours or black ("mono"); lineweights come from the
    DXF (layer/entity lineweight, default 0.25 mm), not from a plot style;
  * text uses the PDF base-14 Helvetica, not the DXF's SHX/TTF fonts: widths, obliquing and width
    factors differ; characters outside Latin-1 become "?";
  * hatch patterns are drawn by ezdxf as line patterns; gradients are drawn solid;
  * images/OLE/underlays are not drawn; 3D is flattened to the top view.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import units as ezunits
from ezdxf.addons.drawing.backend import Backend
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.addons.drawing.frontend import UniversalFrontend
from ezdxf.addons.drawing.pipeline import RenderPipeline2d, prepare_string_for_rendering
from ezdxf.addons.drawing.properties import RenderContext
from ezdxf.math import Vec2

from app.pdf_backends import open_writer

PAPERS = {"A0": (841.0, 1189.0), "A1": (594.0, 841.0), "A2": (420.0, 594.0), "A3": (297.0, 420.0), "A4": (210.0, 297.0)}
MM_PT = 72.0 / 25.4
MARGIN_MM = 10.0
MIN_LW_MM = 0.1
CAP_HEIGHT = 0.72
DEVIATIONS = [
    "No plot style table (CTB/STB): colours are the drawing's ACI/true colours (or black with mono) and lineweights "
    "come from the DXF, so pen assignments of an AutoCAD plot are not reproduced.",
    "Text is set in Helvetica, not the drawing's SHX/TrueType fonts; text width, obliquing and width factor can differ, "
    "and characters outside Latin-1 are replaced by '?'.",
    "Hatch patterns are drawn as line patterns by ezdxf and may differ from AutoCAD's pattern scale; gradients are solid.",
    "Raster images, OLE objects and underlays are not drawn; 3D content is flattened to the top view.",
]


class ConvertError(ValueError):
    pass


class _Recorder(Backend):
    """Collects drawing primitives in drawing coordinates, in paint order."""

    def __init__(self):
        super().__init__()
        self.ops: list[tuple] = []
        self.skipped: Counter = Counter()
        self.lo = [math.inf, math.inf]
        self.hi = [-math.inf, -math.inf]

    def _grow(self, x, y):
        if x < self.lo[0]:
            self.lo[0] = x
        if y < self.lo[1]:
            self.lo[1] = y
        if x > self.hi[0]:
            self.hi[0] = x
        if y > self.hi[1]:
            self.hi[1] = y

    @staticmethod
    def _key(p):
        return (p.color[:7], round(float(p.lineweight), 3))

    def set_background(self, color):
        pass

    def draw_point(self, pos, properties):
        self._grow(pos.x, pos.y)
        self.ops.append(("s", self._key(properties), [(pos.x, pos.y), (pos.x + 1e-9, pos.y)]))

    def draw_line(self, start, end, properties):
        self._grow(start.x, start.y)
        self._grow(end.x, end.y)
        self.ops.append(("s", self._key(properties), [(start.x, start.y), (end.x, end.y)]))

    def draw_solid_lines(self, lines, properties):
        k = self._key(properties)
        for s, e in lines:
            self._grow(s.x, s.y)
            self._grow(e.x, e.y)
            self.ops.append(("s", k, [(s.x, s.y), (e.x, e.y)]))

    def draw_path(self, path, properties):
        pts = [(v.x, v.y) for v in path.flattening(distance=self.config.max_flattening_distance)]
        if len(pts) >= 2:
            for x, y in pts:
                self._grow(x, y)
            self.ops.append(("s", self._key(properties), pts))

    def draw_filled_paths(self, paths, properties):
        polys = []
        for p in paths:
            pts = [(v.x, v.y) for v in p.flattening(distance=self.config.max_flattening_distance)]
            if len(pts) >= 3:
                for x, y in pts:
                    self._grow(x, y)
                polys.append(pts)
        if polys:
            self.ops.append(("f", self._key(properties), polys))

    def draw_filled_polygon(self, points, properties):
        pts = [(v.x, v.y) for v in points.vertices()]
        if len(pts) >= 3:
            for x, y in pts:
                self._grow(x, y)
            self.ops.append(("f", self._key(properties), [pts]))

    def draw_image(self, image_data, properties):
        self.skipped["images"] += 1

    def clear(self):
        pass

    def add_text(self, text, origin, ex, ey, size_drawing, color):
        self._grow(*origin)
        self.ops.append(("t", color[:7], (text, origin, ex, size_drawing)))


class _Pipeline(RenderPipeline2d):
    """Hands text to the recorder as text (the stock pipeline turns it into glyph outlines)."""

    def draw_text(self, text, transform, properties, cap_height, dxftype="TEXT"):
        text = prepare_string_for_rendering(text, dxftype)
        if not text.strip():
            return
        o = Vec2(transform.transform((0, 0, 0)))
        x = Vec2(transform.transform((1, 0, 0)))
        y = Vec2(transform.transform((0, 1, 0)))
        portal = self.clipping_portal
        if portal.is_active:                              # inside a viewport: map to the layout and clip
            po, px, py = portal.clip_point(o), portal.clip_point(x), portal.clip_point(y)
            if po is None or px is None or py is None:
                return
            o, x, y = po, px, py
        ex = x - o
        ey = y - o
        self.backend.add_text(text, (o.x, o.y), (ex.x, ex.y), (ey.x, ey.y), cap_height * ey.magnitude,
                              self.get_backend_properties(properties).color)


def to_dxf_path(src: Path, workdir: Path) -> Path:
    """Return a DXF for src: .dxf as is, .dwg converted with dwg2dxf (LibreDWG)."""
    ext = src.suffix.lower()
    if ext == ".dxf":
        return src
    if ext != ".dwg":
        raise ConvertError("input must be .dxf or .dwg")
    from engine.cli import convert_dwg
    tmp = workdir / "input.dwg"
    shutil.copyfile(src, tmp)
    try:
        return convert_dwg(tmp, workdir)
    except SystemExit as e:
        raise ConvertError(str(e)) from None


def _hex_rgb(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _layer_filter(layers, hide):
    allow = {s.lower() for s in layers} if layers else None
    deny = {s.lower() for s in hide} if hide else set()

    def ok(e):
        n = (e.dxf.layer or "0").lower()
        return (allow is None or n in allow) and n not in deny
    return ok if (allow is not None or deny) else None


def _render(doc, layout, cfg, flt):
    rec = _Recorder()
    ctx = RenderContext(doc)
    fe = UniversalFrontend(ctx, _Pipeline(rec), cfg)
    fe.draw_layout(layout, filter_func=flt)
    return rec


def dxf_to_pdf(src, out_path, paper: str = "A3", landscape: bool | None = None, mono: bool = False,
               layouts: str = "model", layers: list[str] | None = None, hide_layers: list[str] | None = None,
               margin_mm: float = MARGIN_MM, backend: str | None = None) -> dict:
    """Write src (.dxf/.dwg) as PDF. layouts: "model" | "paper" (one page per paper layout) | "all".
    landscape None picks the orientation that fits each page best. Returns a report dict."""
    paper = paper.upper()
    if paper not in PAPERS:
        raise ConvertError(f"paper must be one of {', '.join(PAPERS)}")
    if layouts not in ("model", "paper", "all"):
        raise ConvertError("layouts must be model, paper or all")
    src = Path(src)
    if not src.exists():
        raise ConvertError(f"{src} not found")
    with tempfile.TemporaryDirectory(prefix="naap-conv-") as td:
        dxf_path = to_dxf_path(src, Path(td))
        try:
            doc = ezdxf.readfile(str(dxf_path))
        except (ezdxf.DXFError, OSError) as e:
            raise ConvertError(f"cannot read DXF: {e}") from None
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE,
                        color_policy=ColorPolicy.BLACK if mono else ColorPolicy.COLOR,
                        min_lineweight=MIN_LW_MM / 25.4 * 300)
    flt = _layer_filter(layers, hide_layers)
    unit_mm = ezunits.conversion_factor(doc.units, ezunits.MM) if doc.units else 1.0
    notes: list[str] = []
    if not doc.units:
        notes.append("DXF has no $INSUNITS: drawing units taken as millimetres for the scale figure.")
    todo = []
    if layouts in ("model", "all"):
        todo.append(("Model", doc.modelspace()))
    if layouts in ("paper", "all"):
        for name in doc.layouts.names_in_taborder():
            if name != "Model":
                todo.append((name, doc.layouts.get(name)))

    w = open_writer(backend)
    pages, n_text, bad_text = [], 0, 0
    totals = Counter()
    pw0, ph0 = PAPERS[paper]
    for name, lay in todo:
        rec = _render(doc, lay, cfg, flt)
        if not rec.ops:
            pages.append({"layout": name, "empty": True})
            continue
        (x0, y0), (x1, y1) = rec.lo, rec.hi
        dw, dh = max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)
        land = (dw >= dh) if landscape is None else landscape
        pw, ph = (max(pw0, ph0), min(pw0, ph0)) if land else (min(pw0, ph0), max(pw0, ph0))
        avail_w, avail_h = pw - 2 * margin_mm, ph - 2 * margin_mm
        k = min(avail_w / dw, avail_h / dh)                    # paper mm per drawing unit
        ox = margin_mm + (avail_w - dw * k) / 2
        oy = margin_mm + (avail_h - dh * k) / 2

        def P(x, y):
            return ((ox + (x - x0) * k) * MM_PT, (oy + (y1 - y) * k) * MM_PT)

        w.add_page(pw * MM_PT, ph * MM_PT)
        i, ops = 0, rec.ops
        while i < len(ops):
            kind, key = ops[i][0], ops[i][1]
            j = i
            if kind == "s":
                lw = max(key[1], MIN_LW_MM) * MM_PT
                grp = []
                while j < len(ops) and ops[j][0] == "s" and ops[j][1] == key:
                    grp.append([P(*p) for p in ops[j][2]])
                    j += 1
                w.strokes(grp, _hex_rgb(key[0]), lw)
                totals["strokes"] += len(grp)
            elif kind == "f":
                grp = []
                while j < len(ops) and ops[j][0] == "f" and ops[j][1] == key:
                    grp.extend([P(*p) for p in poly] for poly in ops[j][2])
                    j += 1
                w.fills(grp, _hex_rgb(key[0]))
                totals["fills"] += len(grp)
            else:
                text, o, ex, size = ops[i][2]
                px, py = P(*o)
                ok = w.text((px, py), text, size * k * MM_PT / CAP_HEIGHT, math.degrees(math.atan2(ex[1], ex[0])),
                            _hex_rgb(key))
                n_text += 1
                bad_text += 0 if ok else 1
                j = i + 1
            i = j
        n_img = rec.skipped["images"]
        pages.append({"layout": name, "paper": f"{paper} {'landscape' if land else 'portrait'}",
                      "scale": f"1:{unit_mm / k:.4g}" if k > 0 else "", "extents": [round(v, 3) for v in (x0, y0, x1, y1)],
                      "primitives": len(rec.ops), "images_skipped": n_img})
    if not any(not p.get("empty") for p in pages):
        raise ConvertError("nothing to draw (the selected layout has no visible entities)")
    data = w.tobytes()
    Path(out_path).write_bytes(data)
    if bad_text:
        notes.append(f"{bad_text} text items contained characters outside Latin-1, shown as '?'.")
    if any(p.get("images_skipped") for p in pages):
        notes.append("raster images were skipped.")
    return {"ok": True, "output": str(out_path), "bytes": len(data), "pages": pages, "text_items": n_text,
            "stroke_groups": totals["strokes"], "fill_polygons": totals["fills"], "mono": mono,
            "deviations": DEVIATIONS, "notes": notes}
