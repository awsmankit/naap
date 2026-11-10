"""DXF -> compact geometry JSON.

The output is consumed by the browser viewer and by the measurement engine
(engine/mb.py). Shape (all coordinates are WCS, drawing units, rounded to 0.1):

{
  "version": 1,
  "units": "mm" | "m" | ...,       # from $INSUNITS; unitless is assumed mm
  "insunits": int,
  "extents": [minx, miny, maxx, maxy],
  "truncated": bool,               # True if the entity cap was hit
  "layers": [{"name", "color": "#rrggbb", "off": bool, "counts": {TYPE: n}, "total": n}],
  "layouts": [{"name", "tab", "titles": [{"s", "p", "h"}],     # paper-space sheets (tab order)
               "viewports": [{"window": [x0, y0, x1, y1], "paper": [cx, cy, w, h], "scale"}]}],
  "entities": [ ... ]              # see below; "l" is an index into layers
}

Entity records (keys kept short because big drawings produce a lot of them):
  {"t": "L", "l", "p": [x1, y1, x2, y2]}                         LINE
  {"t": "P", "l", "p": [x, y, x, y, ...], "c": closed}           LWPOLYLINE/POLYLINE/SPLINE/ELLIPSE/LEADER
  {"t": "A", "l", "c": [cx, cy], "r": r, "a": [start, end], "p": [...]}   ARC (p = flattened)
  {"t": "C", "l", "c": [cx, cy], "r": r, "p": [...]}            CIRCLE (p = flattened)
  {"t": "T", "l", "p": [x, y], "s": text, "h": height, "r": rot_deg}     TEXT/MTEXT/ATTRIB
  {"t": "D", "l", "m": measurement, "s": text_override, "dt": dimtype,
         "p1": [x, y], "p2": [x, y], "tp": [x, y]}               DIMENSION
  {"t": "I", "l", "n": block_name, "p": [x, y], "r": rot_deg, "s": [sx, sy]}   INSERT
  {"t": "H", "l", "p": [[x, y, ...], ...], "pn": pattern, "solid": bool}   HATCH boundary loops

DIMENSION records also carry "lf" (DIMLFAC) when it is not 1, so the displayed value is m * lf.

"xrefs": external content the drawing points to but does not contain (never measured):
  [{"kind": "xref" | "image" | "pdf underlay" | "dwf underlay" | "dgn underlay", "name", "path",
    "handle", "layer", "p": [x, y], "loaded": bool, "in_block": bool}]
  xref block definitions that are never inserted in model space are listed with "handle": None.

Every record carries "handle": the DXF handle of the top-level entity (exploded
children share their parent's handle). Entities produced by exploding
INSERT/DIMENSION/MLINE carry "v": 1 and "pi": <index of the top-level parent entity>. Layer "0" inside blocks is
resolved to the parent's layer.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from pathlib import Path

from ezdxf import recover
from ezdxf import path as ezpath
from ezdxf.colors import aci2rgb

from engine.geom import entity_bbox

UNITS = {0: "mm", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
MAX_ENTITIES = 400_000
MAX_DEPTH = 6
EXPLODE = {"INSERT", "DIMENSION", "MLINE", "ACAD_TABLE", "MULTILEADER", "MLEADER"}
SKIP = {"WIPEOUT", "IMAGE", "VIEWPORT", "OLE2FRAME", "POINT", "XLINE", "RAY", "SOLID", "3DFACE", "TRACE"}
EXTERNAL = {"IMAGE": "image", "PDFUNDERLAY": "pdf underlay", "DWFUNDERLAY": "dwf underlay", "DGNUNDERLAY": "dgn underlay"}


def _is_xref_block(doc, name: str) -> bool:
    try:
        blk = doc.blocks.get(name)
        if blk is None:
            return False
        if blk.block_record.is_xref:
            return True
        return bool((blk.block.dxf.get("flags", 0) or 0) & (4 | 8))   # 4 = xref, 8 = xref overlay
    except Exception:
        return False


def _xref_path(doc, name: str) -> str:
    try:
        blk = doc.blocks.get(name)
        return str(blk.block.dxf.get("xref_path", "") or "")
    except Exception:
        return ""


def _r(v: float) -> float:
    return round(float(v), 1)


def _flat(pts) -> list[float]:
    out: list[float] = []
    for p in pts:
        out.append(_r(p[0]))
        out.append(_r(p[1]))
    return out


def _path_pts(entity) -> list[list[float]]:
    """Flatten any curve entity to WCS points via ezdxf.path (handles OCS)."""
    p = ezpath.make_path(entity)
    if not len(p) and p.start == p.end:
        return []
    try:
        bb = ezpath.bbox([p])
        size = max(bb.size.x, bb.size.y) if bb.has_data else 1.0
    except Exception:
        size = 1.0
    dist = max(size * 0.005, 1e-3)
    return [[v.x, v.y] for v in p.flattening(dist, segments=8)]


def _layer_color(layer) -> str:
    try:
        if layer.rgb is not None:
            r, g, b = layer.rgb
            return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        pass
    aci = abs(layer.color or 7)
    if aci in (7, 0, 256, 257):
        return "#e8e8e8"
    try:
        r, g, b = aci2rgb(aci)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#e8e8e8"


class _Extractor:
    def __init__(self, doc, max_entities: int):
        self.doc = doc
        self.max_entities = max_entities
        self.layers: list[dict] = []
        self.layer_idx: dict[str, int] = {}
        self.counts: dict[str, Counter] = defaultdict(Counter)
        self.entities: list[dict] = []
        self.truncated = False
        self._hd = None
        self._tpl: dict = {}
        self.xrefs: list[dict] = []
        for layer in doc.layers:
            self._layer(layer.dxf.name, layer)

    def _layer(self, name: str, layer=None) -> int:
        key = name.upper()
        if key in self.layer_idx:
            return self.layer_idx[key]
        if layer is None and self.doc.layers.has_entry(name):
            layer = self.doc.layers.get(name)
        info = {"name": name, "color": "#e8e8e8", "off": False}
        if layer is not None:
            info["color"] = _layer_color(layer)
            try:
                info["off"] = bool(layer.is_off() or layer.is_frozen())
            except Exception:
                pass
        self.layer_idx[key] = len(self.layers)
        self.layers.append(info)
        return self.layer_idx[key]

    def _add(self, rec: dict, virtual: bool, parent: int | None):
        if len(self.entities) >= self.max_entities:
            self.truncated = True
            return
        if virtual:
            rec["v"] = 1
            rec["pi"] = parent
            ph = self.entities[parent].get("handle") if parent is not None and parent < len(self.entities) else None
            if ph:
                rec["handle"] = ph  # exploded children share the top-level parent's handle
        elif self._hd:
            rec["handle"] = self._hd  # DXF handle of the entity
        self.entities.append(rec)

    def run(self, msp):
        for e in msp:
            if len(self.entities) >= self.max_entities:
                self.truncated = True
                break
            self.counts[e.dxf.get("layer", "0")][e.dxftype()] += 1
            self.handle(e, None, False, None, 0)

    def handle(self, e, parent_layer: str | None, virtual: bool, parent: int | None, depth: int):
        t = e.dxftype()
        if t in EXTERNAL or (t == "INSERT" and _is_xref_block(self.doc, e.dxf.get("name", ""))):
            self._external(e, t, virtual, parent)
            return
        if t in SKIP or (t == "LEADER" and not virtual):
            return  # top-level leaders are annotation arrows (never extracted; keeps results stable)
        self._hd = None if virtual else e.dxf.get("handle")
        lname = e.dxf.get("layer", "0") or "0"
        if parent_layer and lname == "0":
            lname = parent_layer
        li = self._layer(lname)
        idx = len(self.entities)
        if t == "INSERT" and depth < MAX_DEPTH and self._fast_insert(e, lname, li, virtual, parent, idx, depth):
            return
        try:
            if t == "INSERT":
                self._add(_insert_rec(e, _r) | {"l": li}, virtual, parent)
                for a in e.attribs:
                    self.handle(a, lname, True, parent if virtual else idx, depth + 1)
            else:
                rec = _convert(e, t, _r)
                if rec is not None:
                    rec["l"] = li
                    self._add(rec, virtual, parent)
        except Exception:
            # Malformed entity: skip it, never fail the whole drawing.
            return

        if t in EXPLODE and depth < MAX_DEPTH:
            top = parent if virtual else idx
            try:
                children = list(e.virtual_entities())
            except Exception:
                children = []
            for child in children:
                if len(self.entities) >= self.max_entities:
                    self.truncated = True
                    return
                self.handle(child, lname, True, top, depth + 1)

    def _external(self, e, t: str, virtual: bool, parent: int | None):
        """Record an xref insert / attached image / underlay (their content is not in this file)."""
        try:
            lname = e.dxf.get("layer", "0") or "0"
            handle = e.dxf.get("handle") if not virtual else (self.entities[parent].get("handle")
                                                               if parent is not None and parent < len(self.entities) else None)
            p = None
            if e.dxf.hasattr("insert"):
                v = e.dxf.insert
                p = [_r(v[0]), _r(v[1])]
            if t == "INSERT":
                name = e.dxf.get("name", "")
                try:
                    loaded = len(self.doc.blocks.get(name)) > 0
                except Exception:
                    loaded = False
                rec = {"kind": "xref", "name": name, "path": _xref_path(self.doc, name), "loaded": loaded}
            elif t == "IMAGE":
                d = e.image_def
                path = str(d.dxf.get("filename", "") or "") if d is not None else ""
                rec = {"kind": EXTERNAL[t], "name": Path(path.replace("\\", "/")).name or "image", "path": path,
                       "loaded": False}
            else:
                d = e.get_underlay_def()
                path = str(d.dxf.get("filename", "") or "") if d is not None else ""
                rec = {"kind": EXTERNAL[t], "name": str(d.dxf.get("name", "") or "") if d is not None else "",
                       "path": path, "loaded": False}
            rec.update({"handle": handle, "layer": lname, "p": p, "in_block": bool(virtual)})
            self.xrefs.append(rec)
        except Exception:
            return

    # ---------------------------------------------------------- block templates (fast INSERT explode)
    # A block definition is converted once into compact records in block coordinates; every plain
    # (planar, uniformly scaled, single) INSERT of it is then a 2D affine copy of those records.
    # Anything else (MINSERT, non-uniform scale, blocks holding dimensions/tables) falls back to
    # ezdxf's virtual_entities(), which is exact but copies every entity per insert.
    def _fast_insert(self, e, lname, li, virtual, parent, idx, depth) -> bool:
        try:
            if getattr(e, "mcount", 1) > 1:
                return False
            aff = _affine(e)
            tpl = self._template(e.dxf.name, depth) if aff else None
            if tpl is None:
                return False
            self._add(_insert_rec(e, _r) | {"l": li}, virtual, parent)
            top = parent if virtual else idx
            for a in e.attribs:
                self.handle(a, lname, True, top, depth + 1)
        except Exception:
            return False
        for rec, ln in tpl:
            if len(self.entities) >= self.max_entities:
                self.truncated = True
                return True
            try:
                out = _xform(rec, aff)
            except Exception:
                continue
            out["l"] = self._layer(ln if ln is not None else lname)
            self._add(out, True, top)
        return True

    def _template(self, name: str, depth: int):
        key = (name, depth)
        if key in self._tpl:
            return self._tpl[key]
        self._tpl[key] = None          # recursion guard; stays None if unsupported
        try:
            blk = self.doc.blocks.get(name)
        except Exception:
            blk = None
        if blk is None:
            return None
        out = []
        for be in blk:
            try:
                t = be.dxftype()
                if t in EXTERNAL or (t == "INSERT" and _is_xref_block(self.doc, be.dxf.get("name", ""))):
                    return None        # external content inside the block: exact path records it
                if t == "ATTDEF" or t in SKIP:
                    continue
                if t in EXPLODE and t != "INSERT":
                    return None        # dimensions, tables, mlines: exact path
                ln = be.dxf.get("layer", "0") or "0"
                ln = None if ln == "0" else ln
                if t == "INSERT":
                    if getattr(be, "mcount", 1) > 1:
                        return None
                    aff = _affine(be)
                    if aff is None:
                        return None
                    out.append((_insert_rec(be, float), ln))
                    for a in be.attribs:
                        if a.dxf.get("flags", 0) & 1:
                            continue
                        al = a.dxf.get("layer", "0") or "0"
                        rec = _convert(a, "ATTRIB", float)
                        if rec is not None:
                            out.append((rec, al if al != "0" else ln))
                    if depth + 1 < MAX_DEPTH:
                        sub = self._template(be.dxf.name, depth + 1)
                        if sub is None:
                            return None
                        for rec, sl in sub:
                            out.append((_xform(rec, aff, rd=float), sl if sl is not None else ln))
                    continue
                rec = _convert(be, t, float)
            except Exception:
                continue
            if rec is not None:
                out.append((rec, ln))
        self._tpl[key] = out
        return out


def _flat_rd(pts, rd) -> list[float]:
    out: list[float] = []
    for p in pts:
        out.append(rd(p[0]))
        out.append(rd(p[1]))
    return out


def _insert_rec(e, rd) -> dict:
    ins = e.ocs().to_wcs(e.dxf.insert)
    sx, sy = e.dxf.get("xscale", 1.0), e.dxf.get("yscale", 1.0)
    if rd is _r:
        return {"t": "I", "n": e.dxf.name, "p": [_r(ins.x), _r(ins.y)], "r": round(e.dxf.get("rotation", 0.0), 1),
                "s": [round(sx, 3), round(sy, 3)]}
    return {"t": "I", "n": e.dxf.name, "p": [float(ins.x), float(ins.y)], "r": float(e.dxf.get("rotation", 0.0)),
            "s": [float(sx), float(sy)]}


def _convert(e, t: str, rd) -> dict | None:
    """Compact record (without layer) of a non-INSERT entity; rd rounds coordinates (or float)."""
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return {"t": "L", "p": [rd(s.x), rd(s.y), rd(en.x), rd(en.y)]}
    if t in ("LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
        if t == "POLYLINE" and not (e.is_2d_polyline or e.is_3d_polyline):
            return None
        pts = _path_pts(e)
        if len(pts) < 2:
            return None
        closed = bool(getattr(e, "closed", False) or (t == "POLYLINE" and e.is_closed))
        return {"t": "P", "p": _flat_rd(pts, rd), "c": closed}
    if t == "ARC":
        c = e.ocs().to_wcs(e.dxf.center)
        return {"t": "A", "c": [rd(c.x), rd(c.y)], "r": rd(e.dxf.radius),
                "a": [round(e.dxf.start_angle, 2), round(e.dxf.end_angle, 2)], "p": _flat_rd(_path_pts(e), rd)}
    if t == "CIRCLE":
        c = e.ocs().to_wcs(e.dxf.center)
        return {"t": "C", "c": [rd(c.x), rd(c.y)], "r": rd(e.dxf.radius), "p": _flat_rd(_path_pts(e), rd)}
    if t in ("TEXT", "ATTRIB"):
        if t == "ATTRIB" and e.dxf.get("flags", 0) & 1:  # invisible attribute
            return None
        ins = e.ocs().to_wcs(e.dxf.insert)
        txt = e.plain_text()
        if not txt.strip():
            return None
        return {"t": "T", "p": [rd(ins.x), rd(ins.y)], "s": txt, "h": rd(e.dxf.get("height", 1.0)),
                "r": round(e.dxf.get("rotation", 0.0), 1)}
    if t == "MTEXT":
        ins = e.dxf.insert
        txt = e.plain_text(split=False)
        if not txt.strip():
            return None
        return {"t": "T", "p": [rd(ins.x), rd(ins.y)], "s": txt, "h": rd(e.dxf.get("char_height", 1.0)),
                "r": round(e.get_rotation(), 1), "ap": e.dxf.get("attachment_point", 1)}
    if t == "LEADER":
        pts = [[v[0], v[1]] for v in e.vertices]
        return {"t": "P", "p": _flat_rd(pts, rd), "c": False} if len(pts) >= 2 else None
    if t == "HATCH":
        loops = []
        for p in ezpath.from_hatch(e):
            if rd is _r:
                dist = max(1e-3, 1.0)
            else:   # template: block units, scale unknown yet -> relative tolerance
                try:
                    bb = ezpath.bbox([p])
                    dist = max(1e-6, 0.001 * max(bb.size.x, bb.size.y)) if bb.has_data else 1e-3
                except Exception:
                    dist = 1e-3
                dist = min(dist, 1.0)
            pts = [[v.x, v.y] for v in p.flattening(dist, segments=8)]
            if len(pts) >= 3:
                loops.append(_flat_rd(pts[:2000], rd))
        if not loops:
            return None
        return {"t": "H", "p": loops[:50], "pn": e.dxf.get("pattern_name", ""), "solid": bool(e.dxf.get("solid_fill", 0))}
    if t == "DIMENSION":
        rec = {"t": "D", "dt": e.dimtype, "s": e.dxf.get("text", "")}
        try:
            lf = float(e.override().get("dimlfac", 1.0) or 1.0)
            if abs(lf - 1.0) > 1e-9:
                rec["lf"] = lf
        except Exception:
            pass
        try:
            m = e.get_measurement()
            rec["m"] = round(float(m), 2) if isinstance(m, (int, float)) else None
        except Exception:
            rec["m"] = None
        for key, attr in (("p1", "defpoint2"), ("p2", "defpoint3"), ("dp", "defpoint"), ("tp", "text_midpoint")):
            v = e.dxf.get(attr)
            if v is not None:
                rec[key] = [rd(v.x), rd(v.y)]
        return rec
    return None


def _affine(e):
    """(a, b, c, d, tx, ty, scale) with x' = a x + c y + tx, y' = b x + d y + ty, for a planar,
    uniformly scaled INSERT; None otherwise."""
    m = e.matrix44()
    o = m.transform((0, 0, 0))
    ux = m.transform((1, 0, 0)) - o
    uy = m.transform((0, 1, 0)) - o
    uz = m.transform((0, 0, 1)) - o
    nx, ny = math.hypot(ux.x, ux.y), math.hypot(uy.x, uy.y)
    if nx < 1e-12 or ny < 1e-12:
        return None
    tol = 1e-6 * max(nx, ny)
    if abs(ux.z) > tol or abs(uy.z) > tol or abs(uz.x) > 1e-6 * abs(uz.z or 1) or abs(uz.y) > 1e-6 * abs(uz.z or 1):
        return None
    if abs(nx - ny) > 1e-6 * nx or abs(ux.x * uy.x + ux.y * uy.y) > 1e-6 * nx * ny:
        return None
    return (ux.x, ux.y, uy.x, uy.y, o.x, o.y, nx)


def _xform(rec: dict, A, rd=_r) -> dict:
    a, b, c, d, tx, ty, sc = A

    def pts(flat):
        out = []
        for k in range(0, len(flat) - 1, 2):
            x, y = flat[k], flat[k + 1]
            out.append(rd(a * x + c * y + tx))
            out.append(rd(b * x + d * y + ty))
        return out

    def ang(deg):
        r = math.radians(deg)
        return math.degrees(math.atan2(b * math.cos(r) + d * math.sin(r), a * math.cos(r) + c * math.sin(r)))

    t = rec["t"]
    out = {k: v for k, v in rec.items()}
    if t in ("L", "P"):
        out["p"] = pts(rec["p"])
    elif t in ("A", "C"):
        out["c"] = pts(rec["c"])
        out["r"] = rd(rec["r"] * sc)
        out["p"] = pts(rec["p"])
        if t == "A" and len(out["p"]) >= 4:
            cx, cy = out["c"]
            p0 = math.degrees(math.atan2(out["p"][1] - cy, out["p"][0] - cx)) % 360
            p1 = math.degrees(math.atan2(out["p"][-1] - cy, out["p"][-2] - cx)) % 360
            out["a"] = [round(p0, 2), round(p1, 2)] if a * d - b * c > 0 else [round(p1, 2), round(p0, 2)]
    elif t == "T":
        out["p"] = pts(rec["p"])
        out["h"] = rd(rec["h"] * sc)
        out["r"] = round(ang(rec.get("r", 0.0)) % 360, 1)
    elif t == "H":
        out["p"] = [pts(lp) for lp in rec["p"]]
    elif t == "I":
        out["p"] = pts(rec["p"])
        out["r"] = round(ang(rec.get("r", 0.0)) % 360, 1) if rd is _r else ang(rec.get("r", 0.0))
        s0 = rec.get("s") or [1.0, 1.0]
        out["s"] = [round(s0[0] * sc, 3), round(s0[1] * sc, 3)] if rd is _r else [s0[0] * sc, s0[1] * sc]
    elif t == "D":
        for k in ("p1", "p2", "dp", "tp"):
            if k in rec:
                out[k] = pts(rec[k])
    return out


def _paper_texts(layout, limit: int = 40) -> list[dict]:
    out = []
    for e in layout:
        if len(out) >= limit:
            break
        try:
            t = e.dxftype()
            if t == "TEXT":
                s, ins, h = e.plain_text(), e.dxf.insert, e.dxf.get("height", 1.0)
            elif t == "MTEXT":
                s, ins, h = e.plain_text(split=False), e.dxf.insert, e.dxf.get("char_height", 1.0)
            else:
                continue
        except Exception:
            continue
        s = " ".join(str(s).split())
        if s:
            out.append({"s": s[:200], "p": [_r(ins.x), _r(ins.y)], "h": _r(h)})
    return out


def _layouts(doc) -> list[dict]:
    """Paper-space layouts: name, title texts and the model-space window each viewport shows.

    The model window of a viewport is view_target + view_center (DCS, top view only), with the
    viewport's paper size scaled by view_height / paper height. The sheet's own paper-space
    viewport (scale 1, centred on itself) is skipped."""
    out = []
    try:
        names = [n for n in doc.layout_names_in_taborder() if n.lower() != "model"]
    except Exception:
        names = []
    for tab, name in enumerate(names):
        try:
            lay = doc.layouts.get(name)
            vps = list(lay.query("VIEWPORT"))
        except Exception:
            continue
        views = []
        for v in vps:
            try:
                d = v.dxf
                w, h = float(d.width), float(d.height)
                vh = float(d.view_height)
                if w <= 0 or h <= 0 or vh <= 0:
                    continue
                c, vc = d.center, d.view_center_point
                tgt = d.get("view_target_point", (0, 0, 0))
                vdir = d.get("view_direction_vector", (0, 0, 1))
                if abs(vdir[0]) > 1e-6 or abs(vdir[1]) > 1e-6 or abs(float(d.get("view_twist_angle", 0) or 0)) > 1e-3:
                    continue  # not a plain top view
                scale = vh / h
                if abs(scale - 1) < 1e-3 and abs(vc[0] - c[0]) < 1e-3 * w + 1e-6 and abs(vc[1] - c[1]) < 1e-3 * h + 1e-6:
                    continue  # the sheet's own paper-space viewport
                cx, cy = tgt[0] + vc[0], tgt[1] + vc[1]
                mw, mh = w * scale, h * scale
                views.append({"window": [_r(cx - mw / 2), _r(cy - mh / 2), _r(cx + mw / 2), _r(cy + mh / 2)],
                              "paper": [_r(c[0]), _r(c[1]), _r(w), _r(h)], "scale": round(scale, 3)})
            except Exception:
                continue
        out.append({"name": name, "tab": tab, "titles": _paper_texts(lay), "viewports": views})
    return out


def extract(dxf_path: str | Path, max_entities: int = MAX_ENTITIES) -> dict:
    doc, auditor = recover.readfile(str(dxf_path))
    msp = doc.modelspace()
    ex = _Extractor(doc, max_entities)
    ex.run(msp)

    used: Counter = Counter(e["l"] for e in ex.entities)
    layers = []
    for i, info in enumerate(ex.layers):
        counts = dict(ex.counts.get(info["name"], {}))
        info = {**info, "counts": counts, "total": sum(counts.values()), "rendered": used.get(i, 0)}
        layers.append(info)

    minx = miny = math.inf
    maxx = maxy = -math.inf
    for e in ex.entities:
        bb = entity_bbox(e)
        if bb:
            minx, miny = min(minx, bb[0]), min(miny, bb[1])
            maxx, maxy = max(maxx, bb[2]), max(maxy, bb[3])
    if minx == math.inf:
        minx = miny = 0.0
        maxx = maxy = 1.0

    insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    xrefs = list(ex.xrefs)
    inserted = {x["name"] for x in xrefs if x["kind"] == "xref"}
    try:
        for blk in doc.blocks:
            if _is_xref_block(doc, blk.name) and blk.name not in inserted:
                xrefs.append({"kind": "xref", "name": blk.name, "path": _xref_path(doc, blk.name), "loaded": len(blk) > 0,
                              "handle": None, "layer": "", "p": None, "in_block": False, "not_inserted": True})
    except Exception:
        pass
    return {
        "version": 1,
        "units": UNITS.get(insunits, "mm"),
        "insunits": insunits,
        "dxfversion": doc.dxfversion,
        "extents": [_r(minx), _r(miny), _r(maxx), _r(maxy)],
        "truncated": ex.truncated,
        "audit_errors": len(auditor.errors),
        "layers": layers,
        "layouts": _layouts(doc),
        "xrefs": xrefs,
        "entities": ex.entities,
    }
