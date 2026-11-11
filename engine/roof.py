"""Roof / terrace plan: slab outline per T.O.S. level, parapets, coba, khurras, tanks.

Generic rules (no drawing-specific constants):
- Parapets are pairs of parallel parapet-role lines whose spacing is close to the
  parapet thickness (from a "NNN MM THK ... PARAPET" note, else the config).
- The roofed outline is everything not reachable from outside the roof plan without
  crossing a parapet line (raster flood fill). Terrace interiors are the wide enclosed
  spaces; each takes the level of the "T.O.S. +NNNN" text inside it (else the nearest).
- Tanks: the smallest closed polyline around each "...TANK" text; its footprint is
  deducted from the terrace (coba) area.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .pairs import Strip, strips_from_segments
from .raster import Grid, touches_border

TOS_RE = re.compile(r"T\.?\s*O\.?\s*S\.?\D{0,12}?([+-]?\s*\d{3,6})", re.I)
PARAPET_NOTE = re.compile(r"(\d{3,4})\s*MM\s*(?:HIGH|HT)\.?.{0,40}?(\d{2,3})\s*MM\s*TH(?:K|ICK)\.?.{0,20}PARAPET", re.I | re.S)
PARAPET_NOTE2 = re.compile(r"(\d{2,3})\s*MM\s*TH(?:K|ICK)\.?.{0,30}?(\d{3,4})\s*MM\s*(?:HIGH|HT)", re.I | re.S)
KHURRA_RE = re.compile(r"KHURRA", re.I)
TANK_RE = re.compile(r"\bTANK\b", re.I)


@dataclass
class Level:
    tos_mm: float | None
    text: str
    text_ent: int | None
    cells: int = 0                 # slab cells assigned to this level
    slab_corr: float = 0.0         # half of the outer boundary line cells (lines straddle the face)
    tank_cells: int = 0            # tank footprint on the terrace (outside parapets)
    bbox: list | None = None       # roof-plan coords
    strips: list = field(default_factory=list)   # parapet strips on this level


@dataclass
class RoofResult:
    region: int
    grid: Grid
    roofed: np.ndarray             # bool, roof-plan grid
    level_of: np.ndarray           # int16 level index per cell (-1 = not roofed)
    levels: list[Level]
    strips: list[Strip]
    parapet_t_mm: float
    parapet_h_mm: float | None     # total height (incl. coping): the user's, else the note's, else the config default
    parapet_note: str
    khurras: list[tuple[int, dict]]
    tanks: list[dict]
    skipped_diag: int = 0
    notes: list[str] = field(default_factory=list)
    parapet_h_source: str = "note"   # "user" (Review / config roof.parapet_height_mm), "note" or "default"
    parapet_t_source: str = "note"


def _segments(a, rid, roles=("parapet",)):
    ents = a.geometry["entities"]
    for i, e in enumerate(ents):
        if e["t"] not in ("L", "P") or a.roles[e["l"]] not in roles:
            continue
        p = e["p"]
        if a.region_of(p[0], p[1]) != rid:
            continue
        pts = list(zip(p[0::2], p[1::2]))
        if e["t"] == "P" and e.get("c") and len(pts) > 2:
            pts.append(pts[0])
        src = e["pi"] if e.get("v") and e.get("pi") is not None else i
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            yield x1, y1, x2, y2, src


def parapet_spec(a, rid, cfg_t: float, cfg_h: float | None):
    for _, t, r in a.texts:
        if r != rid and a.region_name(r) != a.region_name(rid):
            continue
        s = " ".join(t["s"].split())
        m = PARAPET_NOTE.search(s)
        if m:
            return float(m.group(2)), float(m.group(1)), s
        m = PARAPET_NOTE2.search(s) if "PARAPET" in s.upper() else None
        if m:
            return float(m.group(1)), float(m.group(2)), s
    return cfg_t, cfg_h, ""


def analyse_roof(a, rid: int, cfg_t_mm: float, cfg_h_mm: float | None,
                 user_t_mm: float | None = None, user_h_mm: float | None = None) -> RoofResult | None:
    """Parapet size: the user's value (config roof.parapet_*_mm) wins, then the drawing's note, then the
    config default (walls.parapet, marked as assumed in the book)."""
    unit = 1 / (a.to_m * 1000)
    note_t, note_h, note = parapet_spec(a, rid, None, None)
    t_src = "user" if user_t_mm else "note" if note_t else "default"
    h_src = "user" if user_h_mm else "note" if note_h else "default"
    t_mm = user_t_mm or note_t or cfg_t_mm
    h_mm = user_h_mm or note_h or cfg_h_mm
    t = t_mm * unit
    segs = list(_segments(a, rid))
    if not segs:
        return None
    strips, skipped = strips_from_segments(segs, 0.6 * t, 1.4 * t, 2 * unit, 300 * unit)

    xs = [v for s in segs for v in (s[0], s[2])]
    ys = [v for s in segs for v in (s[1], s[3])]
    cell = 20 * unit
    g = Grid((min(xs), min(ys), max(xs), max(ys)), cell, pad=1000 * unit)
    bar = g.zeros()
    for x1, y1, x2, y2, _ in segs:
        g.draw_segment(bar, x1, y1, x2, y2)
    free = ~bar
    lab, n = ndimage.label(free)
    outside_ids = touches_border(lab)
    outside = np.isin(lab, list(outside_ids))
    roofed = ~outside
    extra_notes = []
    if roofed.sum() < 0.25 * bar.size:
        # the parapet outline leaks (line ends a few cm apart, or it runs into stair/lift room walls):
        # close small gaps (<= ~100 mm), then add the wall-role lines of this plan
        it = max(int(math.ceil(50 * unit / cell)), 1)
        wbar = bar.copy()
        for k, extra in enumerate((False, True)):
            if extra:
                for x1, y1, x2, y2, _ in _segments(a, rid, ("wall-brick", "wall-AAC")):
                    if g.inside(g.row(y1), g.col(x1)) and g.inside(g.row(y2), g.col(x2)):
                        g.draw_segment(wbar, x1, y1, x2, y2)
            closed_bar = ndimage.binary_dilation(wbar, iterations=it)
            lab2, n2 = ndimage.label(~closed_bar)
            out2 = np.isin(lab2, list(touches_border(lab2)))
            if (~out2).sum() > 0.25 * bar.size and (~out2).sum() > 2 * roofed.sum():
                out2 = ndimage.binary_dilation(out2, iterations=it) & ~wbar   # back to the line itself
                free, lab, n = ~closed_bar, lab2, n2
                outside_ids = touches_border(lab)
                outside, roofed = out2, ~out2
                extra_notes.append(f"parapet outline closed across gaps up to {2 * it * cell / unit:.0f} mm"
                                   + (" and with the wall lines (stair/lift rooms)" if extra else ""))
                break

    # interiors: wide enclosed free components
    dist = ndimage.distance_transform_edt(free) * cell
    comp_ids = [k for k in range(1, n + 1) if k not in outside_ids]
    maxd = ndimage.maximum(dist, lab, comp_ids) if comp_ids else []
    interior_ids = [k for k, d in zip(comp_ids, maxd) if d > 0.6 * t]

    # levels from T.O.S. texts
    levels: list[Level] = []
    comp_level: dict[int, int] = {}
    tos_texts = []
    for i, tx, r in a.texts:
        if r != rid:
            continue
        m = TOS_RE.search(tx["s"])
        if m:
            tos_texts.append((i, tx, float(m.group(1).replace(" ", ""))))
    for i, tx, v in tos_texts:
        j, k = g.row(tx["p"][1]), g.col(tx["p"][0])
        comp = int(lab[j, k]) if g.inside(j, k) else 0
        if comp in interior_ids and comp not in comp_level:
            comp_level[comp] = len(levels)
            levels.append(Level(v, " ".join(tx["s"].split()), i))
    # interiors without a T.O.S. text: nearest text, else an "unknown" level
    centres = ndimage.center_of_mass(np.ones_like(lab), lab, interior_ids) if interior_ids else []
    unknown = None
    for comp, (cj, ci) in zip(interior_ids, centres):
        if comp in comp_level:
            continue
        cx, cy = float(g.xc(ci)), float(g.yc(cj))
        best = None
        for i, tx, v in tos_texts:
            d = math.dist((cx, cy), tx["p"])
            if best is None or d < best[0]:
                best = (d, i, tx, v)
        lvl_idx = None
        if best:
            lvl_idx = next((k for k, L in enumerate(levels) if L.text_ent == best[1]), None)
        if lvl_idx is None:
            if unknown is None:
                unknown = len(levels)
                levels.append(Level(None, "level not given", None))
            lvl_idx = unknown
        comp_level[comp] = lvl_idx

    interior = np.isin(lab, interior_ids)
    level_of = np.full(g.shape, -1, dtype=np.int16)
    if interior_ids:
        # higher roofs claim the parapets they share with lower ones, then nearest interior
        reach = int(math.ceil(1.3 * t / cell)) + 2
        order = sorted(range(len(levels)), key=lambda k: -(levels[k].tos_mm or -1e9))
        for li in order:
            comps = [c for c, l2 in comp_level.items() if l2 == li]
            own = np.isin(lab, comps)
            grow = ndimage.binary_dilation(own, iterations=reach) & roofed & (level_of < 0)
            level_of[grow] = li
        lut = np.full(n + 1, -1, dtype=np.int16)
        for comp, li in comp_level.items():
            lut[comp] = li
        _, (jj, ii) = ndimage.distance_transform_edt(~interior, return_indices=True)
        nearest = lut[lab[jj, ii]]
        level_of = np.where(level_of >= 0, level_of, np.where(roofed, nearest, -1)).astype(np.int16)

    strip_mask = g.zeros()
    for s_ in strips:
        g.fill_rect(strip_mask, s_.rect())
    out_d = ndimage.binary_dilation(outside)

    # tanks: smallest closed polyline around each TANK text
    tanks = []
    ents = a.geometry["entities"]
    closed = [(i, e) for i, e in enumerate(ents) if e["t"] == "P" and e.get("c") and a.region_of(e["p"][0], e["p"][1]) == rid]
    seen = set()
    for i, tx, r in a.texts:
        if r != rid or not TANK_RE.search(tx["s"]):
            continue
        px, py = tx["p"]
        best = None
        for k, e in closed:
            exs, eys = e["p"][0::2], e["p"][1::2]
            if not (min(exs) <= px <= max(exs) and min(eys) <= py <= max(eys)):
                continue
            ar = (max(exs) - min(exs)) * (max(eys) - min(eys))
            if ar * (a.to_m ** 2) > 400:
                continue
            if best is None or ar < best[0]:
                best = (ar, k, e)
        if best and best[1] not in seen:
            seen.add(best[1])
            tanks.append({"ent": best[1], "loop": best[2]["p"], "texts": [i], "text": " ".join(tx["s"].split())})
        elif best:
            next(t for t in tanks if t["ent"] == best[1])["texts"].append(i)
    tank_all = g.zeros()
    for tk in tanks:
        m = g.fill_loops([tk["loop"]]) & roofed & ~strip_mask
        tk["cells"] = int(m.sum())
        tk["bbox"] = g.bbox_of(m)
        tank_all |= m

    for li, L in enumerate(levels):
        sel = level_of == li
        L.cells = int(sel.sum())
        L.slab_corr = float((bar & out_d & sel).sum()) * 0.5
        L.tank_cells = int((tank_all & sel).sum())
        L.bbox = g.bbox_of(sel)
        L.strips = []
    for s_ in strips:
        cx, cy = ((s_.lo + s_.hi) / 2, s_.c) if s_.horizontal else (s_.c, (s_.lo + s_.hi) / 2)
        j, k = g.row(cy), g.col(cx)
        li = int(level_of[j, k]) if g.inside(j, k) else -1
        s_.level = li
        if li >= 0:
            levels[li].strips.append(s_)

    khurras = []
    for i, tx, r in a.texts:
        if r == rid and KHURRA_RE.search(tx["s"]):
            if all(math.dist(tx["p"], o["p"]) > 1000 * unit for _, o in khurras):
                khurras.append((i, tx))

    return RoofResult(rid, g, roofed, level_of, levels, strips, t_mm, h_mm, note, khurras, tanks, skipped, notes=extra_notes,
                      parapet_h_source=h_src, parapet_t_source=t_src)
