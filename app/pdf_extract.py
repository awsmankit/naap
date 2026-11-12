"""Vector PDF -> compact geometry JSON (same schema as app/extract.py, plus sheets).

All PDF access goes through app.pdf_backends (PyMuPDF by default, swappable). Pages are
laid side by side in one drawing space (y up, units = PDF points) so the existing viewer can
show them; every record carries "sheet" (1-based page number) and a stable synthetic
"handle" "p<page>:<paint order>".

Extra keys compared with app/extract.py:
  geometry["source"] = "pdf", geometry["units"] = "pt"
  geometry["sheets"] = [{"sheet", "offset": [dx, 0], "size": [w, h], "bbox", "paths", "texts", "images",
                         "image_cover", "scanned", "title_block": {...}}]
  text records: "bb": [x0, y0, x1, y1] (y up), "f": font size
  filled paths: {"t": "H", "solid": true, "p": [[...]]}; stroked+filled paths also give the outline.
  every record: "sheet", "handle"; circles from 4-bezier closed paths: {"t": "C", "c", "r", "p"}

Scanned (raster) PDFs raise ScannedPDFError.

SHX text: AutoCAD plots SHX-font text as vector strokes (no extractable text). With "SHX text as
comments" the original strings are kept as invisible /Square annotations titled "AutoCAD SHX Text";
those are read back as text records (layer SHX_TEXT_LAYER, "shx": 1). A sheet whose text layer is
near-empty while it is full of short glyph-sized strokes, and that has no such comments, is marked
sheet["text_as_geometry"] = True (labels, tags and schedules cannot be read); sheet["shx_texts"] counts
the recovered strings and sheet["text_source"] is "text", "text+shx-comments" or "shx-comments".
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from app.pdf_backends import open_pdf

MAX_ENTITIES = 600_000
SHX_TEXT_LAYER = "SHX text (PDF comments)"
SHORT_STROKE_PT = 8.0     # glyph strokes of plotted SHX text are a few points long
NEAR_EMPTY_TEXT = 15      # a sheet with fewer real text runs than this has "no text layer"
DENSE_STROKES = 300       # ... and at least this many short strokes, mostly in clusters, is text drawn as lines
SHEET_GAP = 0.05          # gap between sheets, fraction of the sheet width
FLATTEN_SEGS = 8          # points per bezier when flattening


class ScannedPDFError(ValueError):
    """The PDF holds page images rather than vector drawings."""


def _r(v: float) -> float:
    return round(float(v), 2)


# ---------------------------------------------------------------- text helpers

def fix_text(s: str) -> str:
    """Repair spans whose font has no Unicode map and comes out shifted into control codes
    (AutoCAD SHX->PDF: '\\x14\\x18\\x13' is '150', offset 29)."""
    if s and all(0x10 <= ord(c) < 0x20 or c == " " for c in s) and any(c != " " for c in s):
        return "".join(c if c == " " else chr(ord(c) + 29) for c in s)
    return s.replace("\x00", "")


ORD_WORDS = {"GROUND": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6,
             "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "GF": 0, "FF": 1, "SF": 2, "TF": 3}
_ORD = r"(\d+|GROUND|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s*(?:ST|ND|RD|TH)?"


def _ord(tok: str) -> int:
    tok = tok.upper()
    return int(tok) if tok.isdigit() else ORD_WORDS[tok]


def floor_scope(text: str) -> dict | None:
    """'2nd to 6th floor' -> {"from": 2, "to": 6, "multiplier": 5, "label": ...}; 'GROUND FLOOR' -> x1."""
    t = re.sub(r"\s+", " ", text.upper())
    m = re.search(_ORD + r"\s*(?:TO|-|–|&)\s*" + _ORD + r"\s*FLOOR", t)
    if m:
        a, b = _ord(m.group(1)), _ord(m.group(2))
        if b >= a:
            return {"from": a, "to": b, "multiplier": b - a + 1, "label": re.sub(r"\s+", " ", m.group(0).strip()).capitalize()}
    m = re.search(_ORD + r"\s*FLOOR", t) or re.search(r"\b(GF|FF|SF|TF)\b", t)
    if m:
        a = _ord(m.group(1))
        return {"from": a, "to": a, "multiplier": 1, "label": re.sub(r"\s+", " ", m.group(0).strip()).capitalize()}
    return None


def parse_title_block(texts: list[dict]) -> dict:
    """texts: [{"s", "bb": [x0, y0, x1, y1] (y up)}] of one sheet. Finds the drawing number,
    title, scale and floor scope from the labelled cells of the title block."""
    def below(lbl: dict, max_dy: float, stop=None) -> list[dict]:
        x0, y0, x1, _ = lbl["bb"]
        h = y0 - max_dy
        out = []
        for t in texts:
            if t is lbl:
                continue
            tx0, ty0, tx1, ty1 = t["bb"]
            if ty1 <= y0 + 1 and ty0 >= h and tx1 > x0 - 20 and tx0 < x0 + 260:
                if stop and stop(t):
                    continue
                out.append(t)
        return sorted(out, key=lambda t: -t["bb"][3])

    def find(rx: str) -> list[dict]:
        return [t for t in texts if re.fullmatch(rx, t["s"].strip(), re.I)]

    tb: dict = {"drawing_no": "", "title": "", "scale": "", "not_to_scale": False, "floor_scope": None}
    lbls = find(r"DRG\.?\s*NO\.?:?|DRAWING\s*NO\.?:?|DWG\.?\s*NO\.?:?")
    if lbls:
        lbl = max(lbls, key=lambda t: t["bb"][0])
        cand = [t for t in below(lbl, 60) if re.search(r"\d", t["s"])]
        if cand:
            tb["drawing_no"] = cand[0]["s"].strip()
            tb["_refs"] = [cand[0]]
    if not tb["drawing_no"]:
        m = [t for t in texts if re.fullmatch(r"[A-Z]+(?:-[A-Z]+)*-\d+[A-Z]?", t["s"].strip())]
        if m:
            tb["drawing_no"] = max(m, key=lambda t: t["bb"][0])["s"].strip()
    lbls = find(r"(DRG\.?|DRAWING)\s*TITLE\s*:?")
    if lbls:
        lbl = max(lbls, key=lambda t: t["bb"][0])
        stop_rx = re.compile(r"SCALE|DRG\.?\s*NO|DATE|PROJECT", re.I)
        lines = []
        for t in below(lbl, 70):
            if stop_rx.search(t["s"]):
                break
            lines.append(t["s"].strip())
        tb["title"] = " ".join(x for x in lines if x)
    scale = [t for t in texts if re.search(r"NOT\s+TO\s+SCALE|\bN\.?T\.?S\b|\b1\s*:\s*\d{1,4}\b", t["s"], re.I)]
    if scale:
        s = max(scale, key=lambda t: t["bb"][0])["s"].strip()
        tb["scale"] = s
        tb["not_to_scale"] = bool(re.search(r"NOT\s+TO\s+SCALE|N\.?T\.?S", s, re.I))
    tb["floor_scope"] = floor_scope(tb["title"]) if tb["title"] else None
    tb.pop("_refs", None)
    return tb


# ---------------------------------------------------------------- geometry helpers

def _bez(p0, c1, c2, p3, n=FLATTEN_SEGS):
    out = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * p0[0] + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t ** 3 * p3[0],
                    u ** 3 * p0[1] + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t ** 3 * p3[1]))
    return out


def _circle(segs) -> tuple[float, float, float] | None:
    """Closed path of 4 (or 8) beziers approximating a circle -> (cx, cy, r) in input coords."""
    if len(segs) not in (4, 8) or any(k != "c" for k, _ in segs):
        return None
    pts = [p for _, ps in segs for p in (ps[0], ps[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w <= 0 or abs(w - h) > 0.03 * max(w, h) or math.dist(segs[0][1][0], segs[-1][1][3]) > 0.02 * w:
        return None
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    r = (w + h) / 4
    mid = _bez(*segs[0][1], n=2)[0]          # a mid-curve point must also lie on the circle
    if abs(math.dist(mid, (cx, cy)) - r) > 0.05 * r:
        return None
    return cx, cy, r


def _polylines(segs) -> list[list[tuple[float, float]]]:
    """Path segments -> connected point runs."""
    runs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for kind, ps in segs:
        if kind == "re":
            if cur:
                runs.append(cur)
                cur = []
            runs.append(list(ps) + [ps[0]])
            continue
        start = ps[0]
        if not cur or math.dist(cur[-1], start) > 1e-3:
            if cur:
                runs.append(cur)
            cur = [start]
        cur.extend([ps[1]] if kind == "l" else _bez(*ps))
    if cur:
        runs.append(cur)
    return runs


def _hex(rgb) -> str:
    if not rgb:
        return "#e8e8e8"
    r, g, b = (max(0, min(255, int(round(c * 255)))) for c in rgb[:3])
    if r + g + b < 60:          # black on a light sheet -> light grey on the dark viewer
        return "#e8e8e8"
    return f"#{r:02x}{g:02x}{b:02x}"


def merge_spans(spans: list) -> list:
    """Join glyph runs that a PDF writer emitted as separate spans ('B' + '3' -> 'B3'):
    same direction, size, font and layer, on one baseline, touching along the writing
    direction. A gap wider than a quarter of the font size becomes a space."""
    from dataclasses import replace
    out = []
    for sp in sorted(spans, key=lambda s: s.seq):
        sp = replace(sp, text=fix_text(sp.text))
        if sp.parts:
            sp = replace(sp, parts=tuple((fix_text(t), b) for t, b in sp.parts))
        if out:
            pr = out[-1]
            dx, dy = sp.dir
            same = (abs(pr.dir[0] - dx) < 1e-3 and abs(pr.dir[1] - dy) < 1e-3 and abs(pr.size - sp.size) < 0.05 * sp.size
                    and pr.font == sp.font and pr.layer == sp.layer)
            if same:
                nx, ny = -dy, dx
                perp = (sp.origin[0] - pr.origin[0]) * nx + (sp.origin[1] - pr.origin[1]) * ny
                corners = lambda b: [(b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])]
                pend = max(x * dx + y * dy for x, y in corners(pr.bbox))
                nstart = min(x * dx + y * dy for x, y in corners(sp.bbox))
                gap = nstart - pend
                if abs(perp) < 0.2 * sp.size and -0.15 * sp.size <= gap <= 0.6 * sp.size:
                    bb = (min(pr.bbox[0], sp.bbox[0]), min(pr.bbox[1], sp.bbox[1]),
                          max(pr.bbox[2], sp.bbox[2]), max(pr.bbox[3], sp.bbox[3]))
                    sep = " " if gap > 0.25 * sp.size and not pr.text.endswith(" ") and not sp.text.startswith(" ") else ""
                    parts = (pr.parts or ((pr.text, pr.bbox),)) + ((sp.text, sp.bbox),)
                    out[-1] = replace(pr, text=pr.text + sep + sp.text, bbox=bb, parts=parts)
                    continue
        out.append(sp)
    return out


def stroke_stats(paths) -> dict:
    """Short straight strokes and how clustered they are (SHX glyphs come in tight clusters)."""
    cells: dict = {}
    n = 0
    for pth in paths:
        for kind, ps in pth.segs:
            if kind != "l":
                continue
            (x0, y0), (x1, y1) = ps[0], ps[1]
            L = math.hypot(x1 - x0, y1 - y0)
            if 0 < L <= SHORT_STROKE_PT:
                n += 1
                k = (int((x0 + x1) / 2 // 4), int((y0 + y1) / 2 // 4))
                cells[k] = cells.get(k, 0) + 1
    clustered = sum(c for k, c in cells.items()
                    if c + sum(cells.get((k[0] + dx, k[1] + dy), 0) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                               if dx or dy) >= 4)
    return {"short_strokes": n, "clustered": round(clustered / n, 3) if n else 0.0}


def text_as_geometry(n_text: int, stats: dict) -> bool:
    return n_text < NEAR_EMPTY_TEXT and stats["short_strokes"] >= DENSE_STROKES and stats["clustered"] >= 0.5


def shx_annotations(annots) -> list:
    """Annotations carrying SHX text (AutoCAD 'SHX text as comments'), de-duplicated."""
    out, seen = [], set()
    for an in annots or ():
        txt = " ".join((an.text or "").split())
        if not txt or "SHX" not in f"{an.title} {an.subject}".upper():
            continue
        key = (txt, tuple(round(v, 1) for v in an.rect))
        if key in seen:
            continue
        seen.add(key)
        out.append((txt, an))
    return out


def page_is_scanned(n_paths: int, n_texts: int, image_cover: float) -> bool:
    return image_cover >= 0.5 and n_paths < 200 and n_texts < 20


def _image_cover(images, w, h) -> float:
    area = 0.0
    for x0, y0, x1, y1 in images:
        area += max(0.0, min(x1, w) - max(x0, 0)) * max(0.0, min(y1, h) - max(y0, 0))
    return min(1.0, area / (w * h)) if w * h else 0.0


# ---------------------------------------------------------------- main

def extract_pdf(pdf_path: str | Path, backend: str | None = None, max_entities: int = MAX_ENTITIES,
                pages: list[int] | None = None) -> dict:
    doc = open_pdf(pdf_path, backend)
    try:
        return _extract(doc, max_entities, pages)
    finally:
        doc.close()


def _extract(doc, max_entities: int, pages: list[int] | None) -> dict:
    layers: list[dict] = []
    lidx: dict[str, int] = {}
    counts: dict[str, Counter] = {}

    def layer(name: str, rgb=None) -> int:
        name = name or "0"
        if name not in lidx:
            lidx[name] = len(layers)
            layers.append({"name": name, "color": _hex(rgb), "off": False})
            counts[name] = Counter()
        return lidx[name]

    for n in doc.ocg_layers():
        layer(n)
    ents: list[dict] = []
    sheets: list[dict] = []
    truncated = False
    xoff = 0.0
    idx = range(doc.page_count) if pages is None else [p - 1 for p in pages]
    scanned_pages = []
    for i in idx:
        page = doc.page(i)
        pinfo = page.info
        W, H = pinfo.width, pinfo.height
        paths = page.paths()
        spans = page.text_spans()
        images = page.images()
        cover = _image_cover(images, W, H)
        scanned = page_is_scanned(len(paths), len(spans), cover)
        n0 = len(ents)
        pn = pinfo.number

        def X(p):
            return (_r(p[0] + xoff), _r(H - p[1]))

        def add(rec: dict, lname: str, seq: int, sub: int | None = None):
            nonlocal truncated
            if len(ents) >= max_entities:
                truncated = True
                return
            rec["l"] = layer(lname)
            rec["sheet"] = pn
            rec["handle"] = f"p{pn}:{seq}" + (f".{sub}" if sub else "")
            counts[layers[rec["l"]]["name"]][rec["t"]] += 1
            ents.append(rec)

        if not scanned:
            for pth in paths:
                if not pth.segs:
                    continue
                if pth.layer and layers[layer(pth.layer)]["color"] == "#e8e8e8" and (pth.stroke or pth.fill):
                    layers[lidx[pth.layer]]["color"] = _hex(pth.stroke or pth.fill)
                circ = _circle(pth.segs)
                if circ and pth.stroke is not None and pth.fill is None:
                    cx, cy, r = circ
                    pts = [X((cx + r * math.cos(a * math.pi / 16), cy + r * math.sin(a * math.pi / 16)))
                           for a in range(33)]
                    add({"t": "C", "c": list(X((cx, cy))), "r": _r(r), "p": [v for q in pts for v in q]},
                        pth.layer, pth.seq)
                    continue
                runs = _polylines(pth.segs)
                if pth.fill is not None:
                    loops = [[v for q in run for v in X(q)] for run in runs if len(run) >= 3]
                    if loops:
                        rec = {"t": "H", "p": loops[:50], "pn": "SOLID", "solid": True}
                        if circ:
                            rec["c"], rec["r"] = list(X(circ[:2])), _r(circ[2])
                        add(rec, pth.layer, pth.seq)
                    if pth.stroke is None:
                        continue
                for k, run in enumerate(runs):
                    if len(run) == 2:
                        a, b = X(run[0]), X(run[1])
                        add({"t": "L", "p": [a[0], a[1], b[0], b[1]]} | ({"dash": 1} if pth.dashed else {}),
                            pth.layer, pth.seq, k)
                    elif len(run) > 2:
                        closed = math.dist(run[0], run[-1]) < 1e-3 or pth.closed
                        add({"t": "P", "p": [v for q in run for v in X(q)], "c": closed}
                            | ({"dash": 1} if pth.dashed else {}), pth.layer, pth.seq, k)
            for sp in merge_spans(spans):
                s = sp.text.strip()
                if not s:
                    continue
                x0, y0, x1, y1 = sp.bbox
                a, b = X((x0, y1)), X((x1, y0))
                ang = math.degrees(math.atan2(-sp.dir[1], sp.dir[0]))
                h = (y1 - y0) if abs(sp.dir[0]) >= abs(sp.dir[1]) else (x1 - x0)
                rec = {"t": "T", "p": list(X(sp.origin)), "s": s, "h": _r(h * 0.75), "f": _r(sp.size),
                       "r": round(ang, 1), "bb": [a[0], a[1], b[0], b[1]]}
                if len(sp.parts) > 1:      # the original spans, so table cells glued by the merge can be split again
                    rec["parts"] = [[t.strip(), *X((bx[0], bx[3])), *X((bx[2], bx[1]))] for t, bx in sp.parts if t.strip()]
                add(rec, sp.layer, 1_000_000 + sp.seq)
        else:
            scanned_pages.append(pn)
        n_text = sum(1 for e in ents[n0:] if e["t"] == "T")
        shx = []
        stats = {"short_strokes": 0, "clustered": 0.0}
        if not scanned:
            try:
                shx = shx_annotations(page.annotations())
            except Exception:
                shx = []
            for k, (txt, an) in enumerate(shx):
                x0, y0, x1, y1 = an.rect
                w, h = x1 - x0, y1 - y0
                vertical = h > 1.2 * w and len(txt) > 1
                size = (w if vertical else h) * 0.8
                a, b = X((x0, y1)), X((x1, y0))
                add({"t": "T", "p": [a[0] + (size if vertical else 0), a[1]], "s": txt, "h": _r(size * 0.75), "f": _r(size),
                     "r": 90.0 if vertical else 0.0, "bb": [a[0], a[1], b[0], b[1]], "shx": 1}, SHX_TEXT_LAYER, 2_000_000 + k)
            stats = stroke_stats(paths)
        texts = [e for e in ents[n0:] if e["t"] == "T"]
        sheets.append({"sheet": pn, "offset": [_r(xoff), 0.0], "size": [_r(W), _r(H)],
                       "bbox": [_r(xoff), 0.0, _r(xoff + W), _r(H)], "rotation": pinfo.rotation,
                       "paths": len(paths), "texts": len(spans), "images": len(images),
                       "image_cover": round(cover, 3), "scanned": scanned,
                       "title_block": parse_title_block(texts) if texts else {},
                       "shx_texts": len(shx), "stroke_stats": stats,
                       "text_source": ("shx-comments" if shx and n_text < NEAR_EMPTY_TEXT else
                                       "text+shx-comments" if shx else "text"),
                       "text_as_geometry": (not scanned) and not shx and text_as_geometry(n_text, stats)})
        xoff += W * (1 + SHEET_GAP)
    if sheets and len(scanned_pages) == len(sheets):
        raise ScannedPDFError(
            "scanned PDF not supported yet: the pages are images with no vector paths or text "
            f"({sheets[0]['paths']} paths, {sheets[0]['texts']} text spans, image cover "
            f"{sheets[0]['image_cover']:.0%} on page 1). Export the drawing from CAD as a vector PDF, or upload the DWG/DXF.")

    used = Counter(e["l"] for e in ents)
    out_layers = []
    for i, info in enumerate(layers):
        c = dict(counts.get(info["name"], {}))
        out_layers.append({**info, "counts": c, "total": sum(c.values()), "rendered": used.get(i, 0)})
    ext = [0.0, 0.0, max(xoff, 1.0), max((s["size"][1] for s in sheets), default=1.0)]
    return {
        "version": 1,
        "source": "pdf",
        "units": "pt",
        "insunits": 0,
        "extents": [_r(v) for v in ext],
        "truncated": truncated,
        "layers": out_layers,
        "layouts": [{"name": (s["title_block"] or {}).get("drawing_no") or f"Sheet {s['sheet']}", "tab": k,
                     "sheet": s["sheet"], "titles": [], "viewports": [], "bbox": s["bbox"]}
                    for k, s in enumerate(sheets)],
        "sheets": sheets,
        "scanned_pages": scanned_pages,
        "entities": ents,
    }


def merge_geometries(geos: list[dict], names: list[str]) -> dict:
    """Several extracted PDFs -> ONE geometry (one drawing set). Sheets are renumbered 1..N in file order,
    laid side by side in the shared space, and each sheet carries "file" (name) and "file_index" and
    "file_page" (its page in that file). Handles are renumbered with the new sheet number ("p<N>:...")."""
    if len(geos) == 1:
        g = geos[0]
        for s in g["sheets"]:
            s.setdefault("file", names[0])
            s.setdefault("file_index", 0)
            s.setdefault("file_page", s["sheet"])
        return g
    layers: list[dict] = []
    lidx: dict[str, int] = {}
    ents: list[dict] = []
    sheets: list[dict] = []
    scanned: list[int] = []
    xoff = 0.0
    n0 = 0
    for fi, (g, name) in enumerate(zip(geos, names)):
        remap = {}
        for i, l in enumerate(g["layers"]):
            if l["name"] not in lidx:
                lidx[l["name"]] = len(layers)
                layers.append({**l, "counts": dict(l.get("counts", {}))})
            else:
                tgt = layers[lidx[l["name"]]]
                for k, v in (l.get("counts") or {}).items():
                    tgt["counts"][k] = tgt["counts"].get(k, 0) + v
                tgt["total"] = tgt.get("total", 0) + l.get("total", 0)
                tgt["rendered"] = tgt.get("rendered", 0) + l.get("rendered", 0)
            remap[i] = lidx[l["name"]]
        dx = xoff - min((s["bbox"][0] for s in g["sheets"]), default=0.0)
        new = {s["sheet"]: n0 + k + 1 for k, s in enumerate(g["sheets"])}
        for s in g["sheets"]:
            ns = new[s["sheet"]]
            bb = [s["bbox"][0] + dx, s["bbox"][1], s["bbox"][2] + dx, s["bbox"][3]]
            sheets.append({**s, "file_page": s["sheet"], "sheet": ns, "bbox": bb, "offset": [s["offset"][0] + dx, 0.0],
                           "file": name, "file_index": fi})
        for e in g["entities"]:
            e = dict(e)
            old = e["sheet"]
            e["sheet"] = new[old]
            e["handle"] = f"p{new[old]}:" + e["handle"].split(":", 1)[1]
            e["l"] = remap[e["l"]]
            for key in ("p", "c", "bb"):
                if key in e and dx and isinstance(e[key], list):
                    v = e[key]
                    if key == "p" and e["t"] == "H":
                        e[key] = [[(c + dx if j % 2 == 0 else c) for j, c in enumerate(loop)] for loop in v]
                    else:
                        e[key] = [(c + dx if j % 2 == 0 else c) for j, c in enumerate(v)]
            if dx and e.get("parts"):
                e["parts"] = [[pt[0], pt[1] + dx, pt[2], pt[3] + dx, pt[4]] for pt in e["parts"]]
            ents.append(e)
        scanned += [new[p] for p in g.get("scanned_pages", []) if p in new]
        n0 += len(g["sheets"])
        xoff = max(s["bbox"][2] for s in sheets) * 1.0 + 0.05 * max(s["size"][0] for s in g["sheets"])
    return {"version": 1, "source": "pdf", "units": "pt", "insunits": 0,
            "extents": [0.0, 0.0, max(s["bbox"][2] for s in sheets), max(s["size"][1] for s in sheets)],
            "truncated": any(g.get("truncated") for g in geos), "layers": layers,
            "layouts": [{"name": (s["title_block"] or {}).get("drawing_no") or f"Sheet {s['sheet']}", "tab": k,
                         "sheet": s["sheet"], "titles": [], "viewports": [], "bbox": s["bbox"], "file": s["file"]}
                        for k, s in enumerate(sheets)],
            "sheets": sheets, "scanned_pages": scanned, "entities": ents}


def extract_pdfs(pdf_paths: list, backend: str | None = None, max_entities: int = MAX_ENTITIES,
                 names: list[str] | None = None) -> dict:
    """Extract one or several PDFs as ONE drawing set (see merge_geometries). A set where every
    file is scanned raises ScannedPDFError; scanned files in a mixed set are skipped with a note
    in geometry["scanned_pages"] and geometry["skipped_files"]. `names` are the display names
    (default: the file names); each sheet's file_index is the index in `pdf_paths`."""
    paths = [Path(p) for p in pdf_paths]
    names = list(names) if names else [p.name for p in paths]
    geos, kept, errs = [], [], []
    for i, p in enumerate(paths):
        try:
            geos.append(extract_pdf(p, backend, max_entities))
            kept.append(i)
        except ScannedPDFError as e:
            errs.append(e)
    if not geos:
        raise errs[0]
    geo = merge_geometries(geos, [names[i] for i in kept])
    for s in geo["sheets"]:
        s["file_index"] = kept[s["file_index"]]
    if len(kept) < len(paths):
        geo["skipped_files"] = [names[i] for i in range(len(paths)) if i not in kept]
    return geo


def sheet_entities(geo: dict, sheet: int) -> list[dict]:
    return [e for e in geo["entities"] if e.get("sheet") == sheet]


if __name__ == "__main__":
    import json
    import sys
    g = extract_pdf(sys.argv[1])
    print(json.dumps({"sheets": g["sheets"], "layers": len(g["layers"]), "entities": len(g["entities"])}, indent=1))
