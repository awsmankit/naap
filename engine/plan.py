"""Floor-plan measurement on a raster.

Everything is derived from the confirmed layer roles, view regions and the
drawing's own legend; values that must come from the user (wall heights, finishes,
conflicting sizes) arrive through user_config and resolved flags.

Pipeline for one floor-plan region:
  1. masonry  = wall hatches whose pattern is in the legend (material from the legend)
                + walls drawn only as parallel wall-role lines (material from the layer role)
  2. columns  = closed polylines / solid hatches on column-role layers
  3. openings = each plan tag is matched to a gap in the masonry of about the scheduled
                width near the tag; the gap is filled (gross wall), then deducted
  4. roofed   = roof-plan outline moved onto this plan (offset from matching section
                lines), else the area enclosed by walls
  5. spaces   = connected roofed free cells; room labels are allocated to them
  6. faces    = solid/free cell transitions -> internal / external plaster
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .analysis import opening_kind
from .pairs import strips_from_segments
from .raster import Grid, run_bounds, run_length, touches_border

WALL_ROLES = ("wall-brick", "wall-AAC", "parapet")
LOW_WALL_RE = re.compile(r"(\d{3,4})\s*MM\s*(?:HT|HIGH|HEIGHT)\.?\s*WALL", re.I)
# "200 mm high rcc wall": a concrete upstand drawn with wall lines (balcony edges) - not masonry
RCC_UPSTAND_RE = re.compile(r"(\d{3,4})\s*MM\s*(?:HT|HIGH|HEIGHT)\.?\s*(?:R\.?\s*C\.?\s*C\.?|CONCRETE)\s*WALL", re.I)
FINISH_RE = re.compile(r"FLOORING|FLOOR\s*TILE|TILES?\b|VITRIFIED|CERAMIC|KOTA|GRANITE|MARBLE|IPS|TREMIX|"
                       r"CC\s*FLOOR|TERRAZZO|MOSAIC|WOODEN\s*FLOOR|LAMINATE", re.I)
NOT_FINISH_RE = re.compile(r"COBA|WATER\s*PROOF|ROOF|SKIRTING|DADO|WALL\s*TILE", re.I)
MATERIAL_CODES = ["BRICK", "AAC"]   # extended at runtime with other legend words


def norm_finish(s: str) -> str:
    return " ".join(s.split()).strip(" .")


@dataclass
class Placed:
    """An opening (tag) located in a wall gap."""
    idx: int
    tag: str
    ent: int
    x: float
    y: float
    w_mm: float | None
    h_mm: float | None
    kind: str
    size_note: str = ""
    gap: dict | None = None        # {bbox, horizontal, t_mm, width_mm, mat, sides: [...]}


@dataclass
class Space:
    id: int
    name: str
    labels: list = field(default_factory=list)     # RoomLabel objects allocated here
    cells: int = 0
    bbox: list | None = None
    comp: int = 0
    note: str = ""
    finish: str | None = None
    finish_src: str = ""
    finish_ref: dict | None = None
    text_ents: list = field(default_factory=list)
    adj_hatches: set = field(default_factory=set)

    @property
    def labelled(self) -> bool:
        return bool(self.labels)


@dataclass
class FloorPlan:
    rid: int
    name: str
    floors: int
    grid: Grid
    cell_m: float
    masonry: np.ndarray
    mat: np.ndarray            # int8 material code per masonry/fill cell (-1 none)
    hid: np.ndarray            # int32 source hatch entity (-1 none)
    linewall: np.ndarray       # int16 strip index (-1 none)
    columns: np.ndarray
    fill: np.ndarray           # int16 opening index (-1 none)
    roofed: np.ndarray
    tos: np.ndarray            # float32 T.O.S. (mm) under each cell, nan where unknown
    height: np.ndarray         # float32 wall height (mm) per cell
    zone: np.ndarray           # int16 zone index per cell
    owner: np.ndarray          # int32 space id per free roofed cell (-1 none)
    zones: list
    spaces: list
    placed: list
    strips: list               # line-only walls
    roof_offset: tuple | None
    roof_note: str
    notes: list = field(default_factory=list)
    unplaced: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers
def _seg_items(a, rid, roles, skip):
    ents = a.geometry["entities"]
    for i in a.region_ents(rid):
        e = ents[i]
        if e["t"] not in ("L", "P") or a.roles[e["l"]] not in roles or e["l"] in skip:
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


def floors_of(region: dict) -> int:
    """Number of identical floors a plan represents: explicit 'floors', else a title like '3 TO 5'."""
    for v in (region.get("multiplier"), region.get("floors")):
        if isinstance(v, (list, tuple)):
            if v:
                return len(v)
            continue
        try:
            if v and int(v) >= 1:
                return int(v)
        except (TypeError, ValueError):
            pass
    m = re.search(r"(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*(?:TO|-|–)\s*(\d{1,2})\s*(?:ST|ND|RD|TH)?\b", region.get("name", ""), re.I)
    if m and int(m.group(2)) > int(m.group(1)):
        return int(m.group(2)) - int(m.group(1)) + 1
    return 1


def roof_offset(a, plan_rid, roof_rid) -> tuple[tuple[float, float] | None, str]:
    """Offset (dx, dy) so that plan point + offset = roof-plan point, from section lines with
    the same letter on both plans."""
    from .checks import _section_cuts
    cuts = _section_cuts(a)
    dx = dy = None
    used = []
    for c in cuts:
        if c["region"] != plan_rid or not c["letter"]:
            continue
        for d in cuts:
            if d["region"] == roof_rid and d["letter"] == c["letter"] and d["horizontal"] == c["horizontal"]:
                if c["horizontal"] and dy is None:
                    dy = d["c"] - c["c"]
                    used.append(f"{c['letter']}-{c['letter']} (y {c['c']:.0f} -> {d['c']:.0f})")
                elif not c["horizontal"] and dx is None:
                    dx = d["c"] - c["c"]
                    used.append(f"{c['letter']}-{c['letter']} (x {c['c']:.0f} -> {d['c']:.0f})")
    if dx is not None and dy is not None:
        return (dx, dy), "roof plan aligned on section lines " + ", ".join(used)
    return None, "roof plan could not be aligned (needs one horizontal and one vertical section line with the same letters on both plans)"


# ---------------------------------------------------------------- build
def build_floor(a, rid: int, cfg: dict, roof=None, flag_zones=(), mat_overrides=(),
                close_gaps_mm: float | None = None) -> FloorPlan | None:
    unit = 1 / (a.to_m * 1000)
    ents = a.geometry["entities"]
    skip = a.skip_layers
    legend = a.legend
    mat_codes = list(MATERIAL_CODES)
    for w in legend.values():
        if w not in mat_codes:
            mat_codes.append(w)

    hatches = []
    for i in a.region_ents(rid):
        e = ents[i]
        if e["t"] != "H" or e["l"] in skip or not e["p"]:
            continue
        role = a.roles[e["l"]]
        if not (e.get("pn") in legend or (role in WALL_ROLES and e.get("pn") != "SOLID")):
            continue
        if a.region_of(e["p"][0][0], e["p"][0][1]) != rid:
            continue
        m = legend.get(e.get("pn")) or ("AAC" if role == "wall-AAC" else "BRICK")
        hatches.append((i, e, mat_codes.index(m)))
    wall_items = list(_seg_items(a, rid, WALL_ROLES, skip))
    if not hatches and not wall_items:
        return None

    xs, ys = [], []
    for _, e, _ in hatches:
        for lp in e["p"]:
            xs += lp[0::2]
            ys += lp[1::2]
    for x1, y1, x2, y2, _ in wall_items:
        xs += [x1, x2]
        ys += [y1, y2]
    off0 = roof_offset(a, rid, roof.region)[0] if roof is not None else None
    if off0 is not None:
        rb = roof.grid
        rx0, ry0 = rb.x0 - off0[0], rb.y0 - off0[1]
        xs += [rx0, rx0 + rb.nx * rb.cell]
        ys += [ry0, ry0 + rb.ny * rb.cell]
    for r in a.rooms:
        if r.region == rid:
            xs.append(r.x)
            ys.append(r.y)
    # keep to the building: drop far-away stragglers (robust 1-99% extent, then pad)
    bbox = (min(xs), min(ys), max(xs), max(ys))
    cell = 20 * unit
    while ((bbox[2] - bbox[0]) / cell) * ((bbox[3] - bbox[1]) / cell) > 3.0e7:
        cell *= 1.5
    g = Grid(bbox, cell, pad=1500 * unit)
    cell_m = cell * a.to_m
    cell_mm = cell_m * 1000

    # 1. masonry from hatches
    hid = np.full(g.shape, -1, dtype=np.int32)
    hmat = {}
    for i, e, mc in hatches:
        g.fill_loops(e["p"], out=hid, value=i)
        hmat[i] = mc
    masonry = hid >= 0
    mat = np.full(g.shape, -1, dtype=np.int8)
    if hmat:
        lut_ids = np.array(sorted(hmat))
        lut_mat = np.array([hmat[k] for k in lut_ids], dtype=np.int8)
        pos = np.searchsorted(lut_ids, hid[masonry])
        mat[masonry] = lut_mat[pos]

    # 2. walls drawn only as lines (no hatch)
    strips, _ = strips_from_segments(wall_items, 75 * unit, 450 * unit, 2 * unit, 300 * unit)
    linewall = np.full(g.shape, -1, dtype=np.int16)
    line_strips = []
    low_texts = [(i, t, float(LOW_WALL_RE.search(t["s"].replace("\n", " ")).group(1)))
                 for i, t, r in a.texts if r == rid and LOW_WALL_RE.search(t["s"].replace("\n", " "))]
    # voids inside hatches (columns, hollow piers/boxes): lines drawn there are not walls
    col_pre = g.zeros()
    for i in a.region_ents(rid):
        e = ents[i]
        if a.roles[e["l"]] == "column" and e["l"] not in skip and e["t"] in ("P", "H") and e.get("p"):
            p0 = e["p"][0] if e["t"] == "H" else e["p"]
            if a.region_of(p0[0], p0[1]) == rid:
                if e["t"] == "P" and e.get("c"):
                    g.fill_loops([e["p"]], out=col_pre, value=True)
                elif e["t"] == "H":
                    g.fill_loops(e["p"], out=col_pre, value=True)
    holes = ndimage.binary_fill_holes(masonry | col_pre) & ~masonry
    # each RCC-upstand note claims the nearest line-only wall strip (within 1.5 m): not masonry
    rcc_strip = {}
    for i, t, r in a.texts:
        if r != rid or not RCC_UPSTAND_RE.search(t["s"].replace("\n", " ")):
            continue
        tx, ty = t["p"]
        best = None
        for k, s in enumerate(strips):
            q = s.rect()
            dd = math.hypot(max(q[0] - tx, 0, tx - q[2]), max(q[1] - ty, 0, ty - q[3]))
            if dd <= 1500 * unit and (best is None or dd < best[0]):
                best = (dd, k)
        if best:
            rcc_strip[best[1]] = i
    rcc_notes = []
    for si, s in enumerate(strips):
        if si in rcc_strip:
            rcc_notes.append((rcc_strip[si], s))
            continue
        sl = g.rect_slices(s.rect())   # the strip's cells (fill_rect), as a window
        n = (sl[0].stop - sl[0].start) * (sl[1].stop - sl[1].start)
        if not n or masonry[sl].sum() > 0.2 * n or holes[sl].sum() > 0.5 * n:
            continue
        roles = [a.roles[ents[k]["l"]] for k in s.ents]
        mc = mat_codes.index("AAC") if roles.count("wall-AAC") > len(roles) / 2 else mat_codes.index("BRICK")
        r = s.rect()
        low = None
        for ti, t, hmm in low_texts:
            tx, ty = t["p"]
            dxr = max(r[0] - tx, 0, tx - r[2])
            dyr = max(r[1] - ty, 0, ty - r[3])
            if math.hypot(dxr, dyr) <= 3000 * unit:
                low = (ti, hmm)
                break
        k = len(line_strips)
        line_strips.append({"strip": s, "mat": mc, "low": low, "roles": sorted(set(roles))})
        sel = ~masonry[sl] & (linewall[sl] < 0)
        linewall[sl][sel] = k
        mat[sl][sel] = mc

    # 3. columns
    columns = g.zeros()
    for i in a.region_ents(rid):
        e = ents[i]
        if a.roles[e["l"]] != "column" or e["l"] in skip:
            continue
        if e["t"] == "P" and e.get("c"):
            p = e["p"]
            if a.region_of(p[0], p[1]) != rid:
                continue
            w, h = max(p[0::2]) - min(p[0::2]), max(p[1::2]) - min(p[1::2])
            if 0 < w * a.to_m <= 2.0 and 0 < h * a.to_m <= 2.0:
                g.fill_loops([p], out=columns, value=True)
        elif e["t"] == "H" and e["p"] and a.region_of(e["p"][0][0], e["p"][0][1]) == rid:
            g.fill_loops(e["p"], out=columns, value=True)
    # IS 1200: column footprints that lie ON the masonry (hatch not voided / wall lines run through the
    # column) are RCC inside the wall; outlines drawn as separate lines are closed up here too
    col_fp = columns.copy()
    col_lines = g.zeros()
    for x1, y1, x2, y2, _ in _seg_items(a, rid, ("column",), skip):
        g.draw_segment(col_lines, x1, y1, x2, y2)
    if col_lines.any():
        filled = ndimage.binary_fill_holes(col_lines)
        lab_c, _ = ndimage.label(filled & ~col_lines)
        maxc = 2000 * unit / g.cell
        for k, sl in enumerate(ndimage.find_objects(lab_c), 1):
            if sl is not None and (sl[0].stop - sl[0].start) <= maxc and (sl[1].stop - sl[1].start) <= maxc:
                # interior of the outline, grown by one cell (the outline itself is half in, half out)
                sub = ndimage.binary_dilation(lab_c[sl] == k) if min(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) > 2 \
                    else lab_c[sl] == k
                col_fp[sl] |= sub & filled[sl]
    col_in_wall = col_fp & (masonry | (linewall >= 0))
    columns &= ~masonry & (linewall < 0)

    mw = masonry | (linewall >= 0)
    solid0 = mw | columns

    # 4. openings
    placed, unplaced = _place_openings(a, rid, g, solid0, mw, mat, cfg, unit)
    fill = np.full(g.shape, -1, dtype=np.int16)
    for op in placed:
        if not op.gap:
            continue
        j0, j1, i0, i1 = op.gap["ij"]
        sel = np.zeros(g.shape, dtype=bool)
        sel[j0:j1, i0:i1] = True
        sel &= ~solid0
        fill[sel] = op.idx
        mat[sel] = op.gap["mat"]
    solid = solid0 | (fill >= 0)

    # material overrides from resolved material flags (bbox -> whole connected wall pieces)
    if mat_overrides:
        lab, _ = ndimage.label(mw | (fill >= 0))
        for bbox_o, word in mat_overrides:
            if word not in mat_codes:
                mat_codes.append(word)
            sl = g.slices(bbox_o)
            ids = np.unique(lab[sl])
            ids = ids[ids > 0]
            if len(ids):
                mat[np.isin(lab, ids)] = mat_codes.index(word)

    # 5. barriers + roofed area
    barrier = g.zeros()
    for x1, y1, x2, y2, _ in _seg_items(a, rid, WALL_ROLES + ("window", "column"), skip):
        g.draw_segment(barrier, x1, y1, x2, y2)
    barrier &= ~ndimage.binary_dilation(solid)

    off, roof_note = (None, "no roof plan")
    fp_lv = None
    tos = np.full(g.shape, np.nan, dtype=np.float32)
    if roof is not None:
        off, roof_note = roof_offset(a, rid, roof.region)
    if off is not None:
        rg = roof.grid
        ci = np.floor((g.xc(np.arange(g.nx)) + off[0] - rg.x0) / rg.cell).astype(int)
        rj = np.floor((g.yc(np.arange(g.ny)) + off[1] - rg.y0) / rg.cell).astype(int)
        okc = (ci >= 0) & (ci < rg.nx)
        okr = (rj >= 0) & (rj < rg.ny)
        lv = np.full(g.shape, -1, dtype=np.int16)
        sub = roof.level_of[np.ix_(np.clip(rj, 0, rg.ny - 1), np.clip(ci, 0, rg.nx - 1))]
        lv[:] = np.where(okr[:, None] & okc[None, :], sub, -1)
        roofed = lv >= 0
        fp_lv = lv
        lut = np.array([L.tos_mm if L.tos_mm is not None else np.nan for L in roof.levels] + [np.nan], dtype=np.float32)
        tos = lut[np.where(lv >= 0, lv, len(roof.levels))]
    else:
        blk = solid | barrier
        it = int(round(close_gaps_mm / cell_mm / 2)) if close_gaps_mm else 0
        if it:
            # multi-storey floor: the slab above covers corridors/lobbies open at their ends, so
            # openings narrower than close_gaps_mm count as closed (outside is grown back afterwards)
            sq = np.ones((3, 3), dtype=bool)   # square element: keeps right-angled corners square
            lab, n = ndimage.label(~ndimage.binary_dilation(blk, sq, iterations=it))
            outside = ndimage.binary_dilation(np.isin(lab, list(touches_border(lab))), sq, iterations=it) & ~blk
            roof_note = f"roofed = enclosed by walls (floor above), openings < {close_gaps_mm:.0f} mm closed"
        else:
            lab, n = ndimage.label(~blk)
            outside = np.isin(lab, list(touches_border(lab)))
        roofed = ~outside

    # 6. zones and wall heights
    walls_cfg = cfg.get("walls", {})
    default_h = float((walls_cfg.get("wall-brick") or {}).get("height_mm") or 3000)
    zones = [{"name": "Default (no zone)", "bbox": None, "height_mm": default_h, "source": "ASSUMED default wall height",
              "assumed": True, "flag": None}]
    height = np.full(g.shape, default_h, dtype=np.float32)
    zone = np.zeros(g.shape, dtype=np.int16)
    if off is not None:
        for li, L in enumerate(roof.levels):
            tos_s = f"T.O.S. {L.tos_mm:+.0f}" if L.tos_mm is not None else "level unknown"
            zi = len(zones)
            zones.append({"name": f"Roof {tos_s}", "bbox": None, "height_mm": None, "source": "roof outline",
                          "assumed": True, "flag": None, "level": li, "auto": True})
            zone[fp_lv == li] = zi
    cand = []
    for z in flag_zones:
        if z.get("region") is not None and z.get("region") != rid:
            continue
        cand.append(dict(z))
    for z in cfg.get("zones") or []:
        if not z.get("bbox"):
            continue
        if z.get("region") is not None and z.get("region") != rid:
            continue
        cand.append({"name": z.get("name") or "Zone", "bbox": z["bbox"], "height_mm": z.get("wall_height_mm"),
                     "source": "user config (zones)", "assumed": bool(z.get("assumed")), "flag": None,
                     "to_beam_soffit": bool(z.get("to_beam_soffit"))})
    # per-level flags apply to that roof level's cells; boxes: bigger first so smaller (more specific) win
    hsrc = np.zeros(g.shape, dtype=np.int16)
    for z in [z for z in cand if z.get("level") is not None and fp_lv is not None]:
        zi = len(zones)
        zones.append(z)
        m = fp_lv == z["level"]
        hsrc[m] = zi
        height[m] = float(z["height_mm"])
    cand = [z for z in cand if z.get("level") is None]
    cand.sort(key=lambda z: -((z["bbox"][2] - z["bbox"][0]) * (z["bbox"][3] - z["bbox"][1])))
    for z in cand:
        zi = len(zones)
        zones.append(z)
        tmp = g.zeros()
        g.fill_rect(tmp, z["bbox"])
        zone[tmp] = zi
        if z.get("height_mm"):
            height[tmp] = float(z["height_mm"])
            hsrc[tmp] = zi

    wall_h = height.copy()
    low_wall = g.zeros()
    for k, ls in enumerate(line_strips):
        if ls["low"]:
            wall_h[linewall == k] = ls["low"][1]
            low_wall |= linewall == k
    # zones whose heights were read to the underside of a beam band get no further beam deduction
    to_beam = g.zeros()
    for zi, z in enumerate(zones):
        if z.get("to_beam_soffit"):
            to_beam |= zone == zi

    # IS 1200: beams over walls (beam projection lines on beam layers of this plan)
    from .is1200 import beam_layer_ids, brick_cfg
    blay = beam_layer_ids(a)
    beam_items = []
    if blay:
        for i in a.region_ents(rid):
            e = ents[i]
            if e["t"] not in ("L", "P") or e["l"] not in blay or e["l"] in skip:
                continue
            p = e["p"]
            if a.region_of(p[0], p[1]) != rid:
                continue
            pts = list(zip(p[0::2], p[1::2]))
            if e["t"] == "P" and e.get("c") and len(pts) > 2:
                pts.append(pts[0])
            src = e["pi"] if e.get("v") and e.get("pi") is not None else i
            beam_items += [(x1, y1, x2, y2, src) for (x1, y1), (x2, y2) in zip(pts, pts[1:])]
    beam_mask = g.zeros()
    beam_refs = []
    if beam_items:
        bstrips, _ = strips_from_segments(beam_items, 100 * unit, 900 * unit, 2 * unit, 300 * unit)
        used = set()
        for s_ in bstrips:
            g.fill_rect(beam_mask, s_.rect())
            used |= s_.ents
            beam_refs.append((s_.rect(), sorted(s_.ents)))
        half_w = float(brick_cfg(cfg).get("beam_width_mm") or 230) / 2 * unit
        for x1, y1, x2, y2, src in beam_items:
            if src in used:
                continue
            if abs(y2 - y1) <= abs(x2 - x1):     # single centre line: a band of the beam width
                rect = [min(x1, x2), (y1 + y2) / 2 - half_w, max(x1, x2), (y1 + y2) / 2 + half_w]
            else:
                rect = [(x1 + x2) / 2 - half_w, min(y1, y2), (x1 + x2) / 2 + half_w, max(y1, y2)]
            g.fill_rect(beam_mask, rect)
            beam_refs.append((rect, [src]))
        beam_mask = ndimage.binary_dilation(beam_mask)

    # 7. spaces
    free = ~(solid | barrier) & roofed
    lab, ncomp = ndimage.label(free)
    owner = np.full(g.shape, -1, dtype=np.int32)
    spaces = _allocate_spaces(a, rid, g, free, lab, ncomp, owner, cfg, unit)

    fp = FloorPlan(rid, a.region_name(rid), floors_of(a.reg_by_id.get(rid, {})), g, cell_m, masonry, mat, hid,
                   linewall, columns, fill, roofed, tos, height, zone, owner, zones, spaces, placed, line_strips,
                   off, roof_note, unplaced=unplaced)
    fp.wall_h = wall_h
    fp.rcc_upstands = rcc_notes
    fp.hsrc = hsrc
    fp.mat_codes = mat_codes
    fp.cell_mm = cell_mm
    fp.barrier = barrier
    fp.solid = solid
    fp.free = free
    fp.col_in_wall = col_in_wall
    fp.low_wall = low_wall
    fp.to_beam = to_beam
    fp.beam_mask = beam_mask
    fp.beam_lines = len(beam_items)
    fp.beam_refs = beam_refs
    fp.brick_dh = np.zeros(g.shape, dtype=np.float32)     # IS 1200 beam deduction per cell (mm), set by the MB builder
    return fp


# ---------------------------------------------------------------- openings
def _gap_candidates(g, solid, mw, bounds, px, py, R, w_lo, w_hi, cell_mm):
    """Gaps in walls near (px, py): empty runs bounded by solid cells on both ends whose
    jambs overlap across the wall thickness."""
    out = []
    j0, j1 = max(g.row(py - R), 0), min(g.row(py + R) + 1, g.ny)
    i0, i1 = max(g.col(px - R), 0), min(g.col(px + R) + 1, g.nx)
    if j1 <= j0 or i1 <= i0:
        return out
    seen = set()
    for horizontal in (True, False):
        sub = solid[j0:j1, i0:i1] if horizontal else solid[j0:j1, i0:i1].T
        # jamb thickness from masonry alone where the jamb is masonry (columns flush with
        # the wall would otherwise widen it), else from all solid cells
        (bs, be), (ms, me) = (bounds["v"], bounds["mv"]) if horizontal else (bounds["h"], bounds["mh"])
        # empty runs bounded by solid on both ends, all rows at once (row-major order)
        d = np.diff(sub.astype(np.int8), axis=1)
        W = sub.shape[1] + 1
        sr, sc = np.nonzero(d == -1)               # first empty = sc + 1
        er, ec = np.nonzero(d == 1)                # first solid after empty = ec + 1
        if not len(sr) or not len(er):
            continue
        kend = er * W + ec + 1
        k = np.searchsorted(kend, sr * W + sc + 1, side="right")
        ok = k < len(kend)
        k = np.where(ok, k, 0)
        ok &= er[k] == sr
        widths = (ec[k] + 1 - (sc + 1)) * cell_mm
        ok &= (widths >= w_lo) & (widths <= w_hi)
        for r, s, e, width in zip(sr[ok].tolist(), (sc[ok] + 1).tolist(), (ec[k[ok]] + 1).tolist(), widths[ok].tolist()):
            if horizontal:
                jr, al, ar = j0 + r, i0 + s - 1, i0 + e
                cL, cR = (jr, al), (jr, ar)
            else:
                jr, al, ar = i0 + r, j0 + s - 1, j0 + e
                cL, cR = (al, jr), (ar, jr)
            sL, eL = _jamb_bounds(cL, -1, horizontal, solid, mw, bs, be, ms, me)
            sR, eR = _jamb_bounds(cR, +1, horizontal, solid, mw, bs, be, ms, me)
            ov0, ov1 = max(sL, sR), min(eL, eR)
            t = (ov1 - ov0) * cell_mm
            if t < 60 or t > 900:
                continue
            key = (horizontal, int(al), int(ar), int(ov0), int(ov1))
            if key in seen:
                continue
            seen.add(key)
            if horizontal:
                blk = solid[ov0:ov1, al + 1:ar]
                ij = (int(ov0), int(ov1), int(al + 1), int(ar))
                cx, cy = float(g.xc((al + 1 + ar) / 2 - 0.5)), float(g.yc((ov0 + ov1) / 2 - 0.5))
            else:
                blk = solid[al + 1:ar, ov0:ov1]
                ij = (int(al + 1), int(ar), int(ov0), int(ov1))
                cx, cy = float(g.xc((ov0 + ov1) / 2 - 0.5)), float(g.yc((al + 1 + ar) / 2 - 0.5))
            if blk.any():
                continue
            out.append({"horizontal": horizontal, "ij": ij, "x": cx, "y": cy, "width_mm": width, "t_mm": t,
                        "jambs": ((jr, al), (jr, ar)) if horizontal else ((al, jr), (ar, jr))})
    return out


def _jamb_bounds(c, step, horizontal, solid, mw, bs, be, ms, me, max_cells=30):
    """Thickness-axis bounds of the wall at a jamb: the most common masonry run over the
    first cells into the jamb (columns are skipped, voids in hatches are filled), so a wall
    joining exactly at the jamb row or a flush column does not widen it."""
    j, i = c
    seen = {}
    first = None
    for _ in range(max_cells):
        if not (0 <= j < solid.shape[0] and 0 <= i < solid.shape[1]) or not solid[j, i]:
            break
        if mw[j, i]:
            key = (int(ms[j, i]), int(me[j, i]))
            seen[key] = seen.get(key, 0) + 1
            first = first or key
        if horizontal:
            i += step
        else:
            j += step
    if seen:
        return max(seen.items(), key=lambda kv: (kv[1], kv[0] == first))[0]
    return bs[c], be[c]


def _place_openings(a, rid, g, solid, mw, mat, cfg, unit):
    from .mb_sizes import opening_size  # resolved sizes (user > schedule)
    cell_mm = g.cell * a.to_m * 1000
    mwf = ndimage.binary_fill_holes(mw | solid) & (mw | ~solid)  # hollow piers / boxes (and cased columns) count as wall
    bounds = {"v": run_bounds(solid, axis=0), "h": run_bounds(solid, axis=1),
              "mv": run_bounds(mwf, axis=0), "mh": run_bounds(mwf, axis=1)}
    R = 2500 * unit
    placed: list[Placed] = []
    cands = []
    tags = [(k, t) for k, t in enumerate(a.plan_tags) if t[3] == rid]
    for k, (tag, x, y, _) in tags:
        w, h, note = opening_size(a, tag)
        op = Placed(len(placed), tag, a.plan_tag_ents[k], x, y, w, h, opening_kind(tag), note)
        placed.append(op)
        lo, hi = (0.6 * w, 1.45 * w + 150) if w else (450, 3600)
        for c in _gap_candidates(g, solid, mwf, bounds, x, y, R, lo, hi, cell_mm):
            d = math.hypot(c["x"] - x, c["y"] - y) / R
            sc = d + (abs(c["width_mm"] - w) / w if w else 0.5)
            cands.append((sc, op.idx, c))
    cands.sort(key=lambda t: t[0])
    taken = []
    for sc, idx, c in cands:
        op = placed[idx]
        if op.gap:
            continue
        j0, j1, i0, i1 = c["ij"]
        if any(not (j1 <= b[0] or b[1] <= j0 or i1 <= b[2] or b[3] <= i0) for b in taken):
            continue
        taken.append(c["ij"])
        (ja, ia), (jb, ib) = c["jambs"]
        m = mat[ja, ia] if mw[ja, ia] else mat[jb, ib]
        if m < 0:
            m = 0
        c["mat"] = int(m)
        c["bbox"] = [float(g.x0 + i0 * g.cell), float(g.y0 + j0 * g.cell), float(g.x0 + i1 * g.cell), float(g.y0 + j1 * g.cell)]
        op.gap = c
    unplaced = [op for op in placed if not op.gap]
    return placed, unplaced


# ---------------------------------------------------------------- spaces
def _free_box(free, j, i, cell_mm):
    s, e = run_bounds(free[j:j + 1, :], axis=1)
    w = (e[0, i] - s[0, i]) * cell_mm
    s2, e2 = run_bounds(free[:, i:i + 1], axis=0)
    ln = (e2[j, 0] - s2[j, 0]) * cell_mm
    return w, ln, (int(s2[j, 0]), int(e2[j, 0]), int(s[0, i]), int(e[0, i]))


def label_centre(a, r):
    """Centre of a room label text block (MTEXT attachment point aware)."""
    if r.ent is None:
        return r.x, r.y
    e = a.geometry["entities"][r.ent]
    x, y = e["p"]
    h = e.get("h", 0) or 0
    lines = [ln for ln in e["s"].split("\n") if ln.strip()] or [""]
    wid = 0.6 * h * max(len(ln) for ln in lines)
    hei = 1.5 * h * len(lines)
    ap = e.get("ap")
    if ap:
        col, row = (ap - 1) % 3, (ap - 1) // 3
        return x + (0.5 - 0.5 * col) * wid, y - (0.5 - 0.5 * row) * hei
    return x + wid / 2, y + h / 2


def _label_rect(free, g, ji, rw, rl, cell_mm, unit):
    """Room rectangle for a label: along each axis, a free run (from a fan of offsets across
    the room) that matches the labelled size; otherwise the labelled size centred on the label."""
    j, i = ji
    close = lambda v, ref: abs(v - ref) <= max(30, 0.02 * ref)  # noqa: E731
    best = None
    for wx, wy in ((rw, rl), (rl, rw)):
        xb = yb = None
        for f in (0, -0.1, 0.1, -0.2, 0.2, -0.3, 0.3, -0.4, 0.4):
            jj = j + int(round(f * wy * unit / g.cell))
            if xb is None and 0 <= jj < g.ny and free[jj, i]:
                s, e = run_bounds(free[jj:jj + 1, :], axis=1)
                if close((e[0, i] - s[0, i]) * cell_mm, wx):
                    xb = (int(s[0, i]), int(e[0, i]))
            ii = i + int(round(f * wx * unit / g.cell))
            if yb is None and 0 <= ii < g.nx and free[j, ii]:
                s, e = run_bounds(free[:, ii:ii + 1], axis=0)
                if close((e[j, 0] - s[j, 0]) * cell_mm, wy):
                    yb = (int(s[j, 0]), int(e[j, 0]))
        score = (xb is not None) + (yb is not None)
        if best is None or score > best[0]:
            best = (score, wx, wy, xb, yb)
    _, wx, wy, xb, yb = best

    def anchored(run, size_cells, c, n):
        """Label-sized span: anchored to a run end if that keeps the label near its middle, else centred."""
        lo, hi = run
        cands = [(lo, lo + size_cells), (hi - size_cells, hi)]
        best_c = min(cands, key=lambda ab: abs((ab[0] + ab[1]) / 2 - c))
        if best_c[0] <= c <= best_c[1]:  # the label lies inside the anchored span
            a_, b_ = best_c
        else:
            a_, b_ = c + 0.5 - size_cells / 2, c + 0.5 + size_cells / 2
        return max(int(round(a_)), 0), min(int(round(b_)), n)

    if xb is None:
        s, e = run_bounds(free[j:j + 1, :], axis=1)
        xb = anchored((int(s[0, i]), int(e[0, i])), wx * unit / g.cell, i, g.nx)
    if yb is None:
        s, e = run_bounds(free[:, i:i + 1], axis=0)
        yb = anchored((int(s[j, 0]), int(e[j, 0])), wy * unit / g.cell, j, g.ny)
    return best[3] is not None, best[4] is not None, (yb[0], yb[1], xb[0], xb[1]), (wx, wy)


def _snap(free, g, x, y, unit, radius_mm=400):
    j, i = g.row(y), g.col(x)
    if g.inside(j, i) and free[j, i]:
        return j, i
    r = int(radius_mm * unit / g.cell) + 1
    j0, j1, i0, i1 = max(j - r, 0), min(j + r + 1, g.ny), max(i - r, 0), min(i + r + 1, g.nx)
    sub = free[j0:j1, i0:i1]
    if not sub.any():
        return None
    jj, ii = np.nonzero(sub)
    k = np.argmin((jj + j0 - j) ** 2 + (ii + i0 - i) ** 2)
    return int(jj[k] + j0), int(ii[k] + i0)


def _allocate_spaces(a, rid, g, free, lab, ncomp, owner, cfg, unit):
    cell_mm = g.cell * a.to_m * 1000
    c2 = (g.cell * a.to_m) ** 2
    by_comp = defaultdict(list)
    for r in a.rooms:
        if r.region != rid:
            continue
        cx, cy = label_centre(a, r)
        ji = _snap(free, g, cx, cy, unit) or _snap(free, g, r.x, r.y, unit, 800)
        if ji is None:
            continue
        comp = int(lab[ji])
        x_ok, y_ok, box, orient = _label_rect(free, g, ji, r.w, r.l, cell_mm, unit)
        by_comp[comp].append({"room": r, "ji": ji, "box": box, "ok": x_ok and y_ok, "x_ok": x_ok, "y_ok": y_ok,
                              "orient": orient, "w": (box[3] - box[2]) * cell_mm, "l": (box[1] - box[0]) * cell_mm})

    sizes = np.bincount(lab.ravel(), minlength=ncomp + 1)
    spaces: list[Space] = []
    plan_texts = [(i, t) for i, t, r in a.texts if r == rid]
    min_cells = int(0.25 / c2)
    narrow = 450 * unit / g.cell
    objs = ndimage.find_objects(lab)
    for comp in range(1, ncomp + 1):
        if sizes[comp] < min_cells:
            continue
        sl = objs[comp - 1]
        if sl is not None and min(sl[0].stop - sl[0].start, sl[1].stop - sl[1].start) < narrow:
            continue  # slivers (window-box pockets, gaps between parallel lines) are not rooms
        items = by_comp.get(comp, [])
        cmask = None
        lone_void = (len(items) == 1 and _is_void(items[0]["room"].name)
                     and sizes[comp] * c2 > 2 * items[0]["room"].w * items[0]["room"].l / 1e6)
        if len(items) == 1 and not lone_void:
            # (a lift/shaft label in a much bigger open space, e.g. a lift well open to the lobby,
            #  only claims its own rectangle below)
            sp = Space(len(spaces), items[0]["room"].name, [items[0]["room"]], comp=comp)
            spaces.append(sp)
            owner[lab == comp] = sp.id
            continue
        if not items:
            sp = Space(len(spaces), "", [], comp=comp)
            spaces.append(sp)
            owner[lab == comp] = sp.id
            continue
        cmask = lab == comp
        label_area = sum(it["room"].w * it["room"].l for it in items) / 1e6
        comp_area = sizes[comp] * c2
        consistent = [it for it in items if it["ok"]]
        others = [it for it in items if not it["ok"]]
        if not consistent and abs(comp_area - label_area) <= 0.05 * label_area:
            # one open hall holding several labelled bays: share it by label area
            sp = Space(len(spaces), " + ".join(sorted({it["room"].name for it in items}, key=_natkey)),
                       [it["room"] for it in items], comp=comp,
                       note=f"{len(items)} labels in one open space; enclosed area {comp_area:.2f} m2 vs labels {label_area:.2f} m2")
            spaces.append(sp)
            owner[cmask] = sp.id
            continue
        # each label claims its room rectangle: the free run where it matches the label size,
        # else the label size centred on the label (smallest rooms first)
        claimed = np.zeros_like(cmask)
        for it in sorted(items, key=lambda t: (t["room"].w * t["room"].l, not t["ok"])):
            r = it["room"]
            j0, j1, i0, i1 = it["box"]
            box = np.zeros_like(cmask)
            box[j0:j1, i0:i1] = True
            sel = box & cmask & ~claimed
            if not sel.any():
                continue
            note = "" if it["ok"] else ("label-sized rectangle around the label (room is open to "
                                         "a larger space on " + ("x" if not it["x_ok"] else "") + ("y" if not it["y_ok"] else "") + ")")
            sp = Space(len(spaces), r.name, [r], comp=comp, note=note)
            spaces.append(sp)
            owner[sel] = sp.id
            claimed |= sel
        rest = cmask & ~claimed
        if rest.sum() >= min_cells:
            sub_lab, n = ndimage.label(rest)
            for k in range(1, n + 1):
                sel = sub_lab == k
                if sel.sum() < min_cells:
                    continue
                sp = Space(len(spaces), "", [], comp=comp, note="unlabelled part of an open space")
                spaces.append(sp)
                owner[sel] = sp.id

    # bboxes, cell counts, names of unlabelled spaces
    objs = ndimage.find_objects(owner + 1)
    counts = np.bincount((owner + 1).ravel(), minlength=len(spaces) + 1)
    for sp in spaces:
        sl = objs[sp.id] if sp.id < len(objs) else None
        sp.cells = int(counts[sp.id + 1])
        if sl is not None:
            sp.bbox = [float(g.x0 + sl[1].start * g.cell), float(g.y0 + sl[0].start * g.cell),
                       float(g.x0 + sl[1].stop * g.cell), float(g.y0 + sl[0].stop * g.cell)]
        sp.text_ents = [r.ent for r in sp.labels if r.ent is not None]
        if not sp.name:
            names = []
            for i, t in plan_texts:
                j, k = g.row(t["p"][1]), g.col(t["p"][0])
                if g.inside(j, k) and owner[j, k] == sp.id:
                    s = " ".join(t["s"].split())
                    if re.search(r"[A-Z]{2,}", s) and not re.match(r"^(LVL|FFL|UP|DN)\b", s, re.I) and len(s) < 40:
                        names.append(s)
                        sp.text_ents.append(i)
            if sp.bbox:
                wb, lb = (sp.bbox[2] - sp.bbox[0]) * a.to_m * 1000, (sp.bbox[3] - sp.bbox[1]) * a.to_m * 1000
                size = f"{wb:.0f}x{lb:.0f}"
            else:
                size = "?"
            sp.name = (names[0] if names else "Unlabelled space") + f" ({size} box)"
    return [sp for sp in spaces if sp.cells > 0]


def _is_void(name: str) -> bool:
    from .quantities import is_void
    return is_void(name)


def _natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def local_thickness(mask, cell_mm):
    return np.minimum(run_length(mask, 1), run_length(mask, 0)) * cell_mm
