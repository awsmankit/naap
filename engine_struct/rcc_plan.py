"""Plan reading for an RCC slab-and-beam plan whose beams are drawn as pairs of parallel lines and whose
members are named by text tags: beam tags + (b X D) size labels, slab tags + 'NNN MM THICK' boxes, column tags.

Everything is found by structure: the scale from the written beam widths against the drawn line spacing,
beams from line pairs of that spacing, slab panels by flood fill between beam lines and columns."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from . import plans as P
from .sheet import Sheet, Txt

BEAM_TAG = re.compile(r"^([Bb])\s?(\d{1,3}[A-Z]?)(\s*INV\.?)?$")
SIZE_TAG = re.compile(r"^\(?\s*(\d{2,4})\s*[Xx×]\s*(\d{1,4}|D)\s*(?:/\s*(\d{2,4}))?\s*\)?$")
THICK_TAG = re.compile(r"^(\d{2,3})\s*MM$", re.I)
COL_TAG = re.compile(r"^(N?C\d{1,3}[A-Z]?)$")
COL_SIZE = re.compile(r"^(\d{3,4})\s*[Xx×]\s*(\d{3,4})$")
STUB_TAG = re.compile(r"^(H\d{1,2}|SP\d{1,2}|STUB\s?\d{0,2})$")


@dataclass
class Strip:
    horiz: bool
    c: float
    a: float
    b: float
    w: float
    handles: list = field(default_factory=list)
    tag: Txt | None = None
    size: Txt | None = None
    id: int = 0

    @property
    def length(self) -> float:
        return self.b - self.a

    @property
    def bb(self):
        h = self.w / 2
        return (self.a, self.c - h, self.b, self.c + h) if self.horiz else (self.c - h, self.a, self.c + h, self.b)

    def dist(self, x, y) -> float:
        u, v = (x, y) if self.horiz else (y, x)
        du = 0.0 if self.a <= u <= self.b else min(abs(u - self.a), abs(u - self.b))
        return math.hypot(du, max(0.0, abs(v - self.c) - self.w / 2))


def parse_size(s: str):
    m = SIZE_TAG.match(s.strip())
    if not m:
        return None
    return {"b": int(m.group(1)), "d": None if m.group(2) == "D" else int(m.group(2)),
            "d_end": int(m.group(3)) if m.group(3) else None, "d_is_slab": m.group(2) == "D"}


def classify_texts(sh: Sheet, region) -> dict:
    out = {k: [] for k in ('beam_tag', 'size', 'col_tag', 'col_size', 'thick', 'stub')}
    for t in sh.texts:
        if not P.inside(t.cx, t.cy, region):
            continue
        s = t.s.strip()
        if BEAM_TAG.match(s):
            out["beam_tag"].append(t)
        elif parse_size(s) and "(" in s:
            out["size"].append(t)
        elif COL_TAG.match(s):
            out["col_tag"].append(t)
        elif COL_SIZE.match(s):
            out["col_size"].append(t)
        elif THICK_TAG.match(s):
            if any(u.s.strip().upper() == "THICK" and abs(u.cx - t.cx) < 30 and 0 <= t.cy - u.cy < 24 for u in sh.texts):
                out["thick"].append(t)
        elif STUB_TAG.match(s):
            out["stub"].append(t)
    return out


# ---------------------------------------------------------------- scale and strips

def beam_lines(sh: Sheet, layer_rx: str = r"beam"):
    lays = {s.layer for s in sh.segs if re.search(layer_rx, s.layer, re.I)} or {s.layer for s in sh.segs}
    segs = [s for s in sh.segs if s.layer in lays and s.length >= 6]
    lines, gap = P.merge_lines(segs)
    return [l for l in lines if l.b - l.a >= 20], lays


def spacing_hist(lines, wmax: float = 45.0, min_ov: float = 20.0) -> Counter:
    cnt: Counter = Counter()
    for hz in (True, False):
        L = sorted([l for l in lines if l.horiz == hz], key=lambda l: l.c)
        for i, l1 in enumerate(L):
            for l2 in L[i + 1:]:
                d = l2.c - l1.c
                if d > wmax:
                    break
                if d > 4 and min(l1.b, l2.b) - max(l1.a, l2.a) >= min_ov:
                    cnt[round(d, 1)] += 1
    return cnt


def scale_from_widths(hist: Counter, widths: Counter, tol: float = 0.045) -> tuple[float | None, dict]:
    """pt per mm such that the drawn line spacings agree with the beam widths written on the plan.
    The score weighs each of the three most common widths by how often it is written."""
    top = [w for w, _ in widths.most_common(3) if widths[w] >= 2]
    if len(top) < 1 or not hist:
        return None, {}
    best, bs = None, 0
    for r in np.arange(0.01, 0.2, 0.0002):
        s = 0.0
        for w in top:
            tgt = r * w
            s += widths[w] * sum(c for d, c in hist.items() if abs(d - tgt) <= tol * tgt + 0.25)
        if s > bs:
            best, bs = r, s
    if best is None or bs < 2 * sum(widths.values()):
        return None, {}
    # refine: mean ratio of the matched spacings
    ratios, per_w = [], {}
    for w in top:
        tgt = best * w
        m = [(d, c) for d, c in hist.items() if abs(d - tgt) <= tol * tgt + 0.25]
        for d, c in m:
            ratios += [d / w] * c
        per_w[w] = sum(c for _, c in m)
    return float(np.median(ratios)), {"matches_by_width": per_w, "n": len(ratios), "ratios": ratios}


def find_strips(lines, pt_per_mm: float, widths: set[int], tol: float = 0.05, min_len: float = 6.0) -> list[Strip]:
    out: list[Strip] = []
    for hz in (True, False):
        ls = sorted([l for l in lines if l.horiz == hz], key=lambda l: l.c)
        for i, l1 in enumerate(ls):
            for l2 in ls[i + 1:]:
                d = l2.c - l1.c
                if d > max(widths) * pt_per_mm * 1.1:
                    break
                w = next((w for w in widths if abs(d - w * pt_per_mm) <= tol * w * pt_per_mm + 0.25), None)
                if w is None:
                    continue
                a, b = max(l1.a, l2.a), min(l1.b, l2.b)
                if b - a < min_len:
                    continue
                out.append(Strip(hz, (l1.c + l2.c) / 2, a, b, d, l1.handles + l2.handles))
    return out


def cut_strips(strips: list[Strip], blobs: list, min_len: float) -> list[Strip]:
    res = []
    for s in strips:
        cuts = []
        for bl in blobs:
            lo, hi = (bl.bb[1], bl.bb[3]) if s.horiz else (bl.bb[0], bl.bb[2])
            if lo - 0.5 <= s.c <= hi + 0.5 or abs(s.c - (lo + hi) / 2) < s.w / 2 + 1:
                cuts.append((bl.bb[0], bl.bb[2]) if s.horiz else (bl.bb[1], bl.bb[3]))
        for a, b in P._subtract(s.a, s.b, cuts):
            if b - a >= min_len:
                res.append(Strip(s.horiz, s.c, a, b, s.w, s.handles))
    res.sort(key=lambda s: -s.length)
    keep: list[Strip] = []
    for s in res:
        if any(k.horiz == s.horiz and abs(k.c - s.c) < 0.5 * s.w and min(k.b, s.b) - max(k.a, s.a) > 0.6 * s.length for k in keep):
            continue
        keep.append(s)
    for i, s in enumerate(keep):
        s.id = i
    return keep


def assign_tags(strips: list[Strip], tags: list[Txt], sizes: list[Txt], max_d: float) -> tuple[list[Txt], list[Txt]]:
    """Each beam tag labels the nearest strip it is written beside (text running along the strip)."""
    orphan = []
    for t in tags:
        along = abs(t.rot) % 180 < 45
        cand = [s for s in strips if s.horiz == along]
        best = min(cand, key=lambda s: s.dist(t.cx, t.cy), default=None)
        if best is None or best.dist(t.cx, t.cy) > max_d:
            orphan.append(t)
            continue
        if best.tag is None or best.dist(t.cx, t.cy) < best.dist(best.tag.cx, best.tag.cy):
            best.tag = t
    orph_s = []
    for t in sizes:
        along = abs(t.rot) % 180 < 45
        cand = [s for s in strips if s.horiz == along]
        best = min(cand, key=lambda s: s.dist(t.cx, t.cy), default=None)
        if best is None or best.dist(t.cx, t.cy) > max_d:
            orph_s.append(t)
            continue
        if best.size is None or best.dist(t.cx, t.cy) < best.dist(best.size.cx, best.size.cy):
            best.size = t
    return orphan, orph_s


# ---------------------------------------------------------------- slab panels

@dataclass
class Panel:
    tag: str
    area_pt2: float
    bb: tuple
    tag_txt: Txt
    label: int
    leaked: bool = False
    thick_txt: Txt | None = None
    shared: list = field(default_factory=list)
    beam_edge_px: float = 0.0
    perim_px: float = 0.0


def raster_panels(region, line_segs, strips: list[Strip], blobs, slab_tags: list[Txt], px_per_pt: float = 2.0,
                  extra_wall_boxes=(), seal: int = 1) -> tuple[list[Panel], dict]:
    """Flood fill between the beam lines (all of them, so strips and junction faces close the panels) and the columns."""
    x0, y0, x1, y1 = P.grow(region, 6)
    W = int((x1 - x0) * px_per_pt) + 3
    H = int((y1 - y0) * px_per_pt) + 3
    wall = np.zeros((H, W), dtype=bool)

    def px(x, y):
        return int(round((x - x0) * px_per_pt)), int(round((y1 - y) * px_per_pt))

    def line(ax, ay, bx, by):
        a, r = px(ax, ay)
        b, r2 = px(bx, by)
        if ax == bx or abs(bx - ax) < 1e-6:
            wall[max(min(r, r2), 0):min(max(r, r2) + 1, H), max(min(max(a, 0), W - 1), 0)] = True if 0 <= a < W else False
        elif abs(by - ay) < 1e-6:
            if 0 <= r < H:
                wall[r, max(min(a, b), 0):min(max(a, b) + 1, W)] = True
        else:
            n = int(max(abs(b - a), abs(r2 - r))) + 1
            for k in range(n + 1):
                cx = int(round(a + (b - a) * k / n))
                cy = int(round(r + (r2 - r) * k / n))
                if 0 <= cx < W and 0 <= cy < H:
                    wall[cy, cx] = True

    for s in line_segs:
        if s.horiz or s.vert:
            line(s.x1, s.y1, s.x2, s.y2)
    beam_only = np.zeros_like(wall)
    for st in strips:
        a, r1 = px(st.bb[0], st.bb[1])
        b, r0 = px(st.bb[2], st.bb[3])
        beam_only[max(r0, 0):min(r1 + 1, H), max(a, 0):min(b + 1, W)] = True
    for bl in blobs:
        a, r1 = px(bl.bb[0], bl.bb[1])
        b, r0 = px(bl.bb[2], bl.bb[3])
        beam_only[max(r0, 0):min(r1 + 1, H), max(a, 0):min(b + 1, W)] = True
    for bl in blobs:
        a, r1 = px(bl.bb[0], bl.bb[1])
        b, r0 = px(bl.bb[2], bl.bb[3])
        wall[max(r0, 0):min(r1 + 1, H), max(a, 0):min(b + 1, W)] = True
    for bb in extra_wall_boxes:
        a, r1 = px(bb[0], bb[1])
        b, r0 = px(bb[2], bb[3])
        wall[max(r0, 0):min(r1 + 1, H), max(a, 0):min(b + 1, W)] = True
    wall = ndimage.binary_dilation(wall, iterations=seal)
    lab, n = ndimage.label(~wall)
    sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(n + 1))
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])).tolist())
    panels: dict[int, Panel] = {}
    out: list[Panel] = []
    sh_ = seal + 0.5
    for t in slab_tags:
        c, r = px(t.cx, t.cy)
        if not (0 <= r < H and 0 <= c < W):
            continue
        k = int(lab[r, c])
        if k == 0:
            win = lab[max(r - 5, 0):r + 6, max(c - 5, 0):c + 6]
            vals = [v for v in np.unique(win) if v]
            k = int(max(vals, key=lambda v: sizes[v])) if vals else 0
        if k == 0:
            continue
        if k in panels:
            panels[k].shared.append(t)
            continue
        m = lab == k
        rows, cols = np.nonzero(m)
        bb = (x0 + (cols.min() - sh_) / px_per_pt, y1 - (rows.max() + sh_) / px_per_pt,
              x0 + (cols.max() + sh_) / px_per_pt, y1 - (rows.min() - sh_) / px_per_pt)
        er = ndimage.binary_erosion(m)
        edge = m & ~er
        per = float(edge.sum())
        area = (sizes[k] + sh_ * per + 4 * sh_ * sh_) / (px_per_pt ** 2)
        near_beam = ndimage.binary_dilation(beam_only, iterations=4)
        p = Panel(t.s.strip(), area, bb, t, k, leaked=k in border, perim_px=per, beam_edge_px=float((edge & near_beam).sum()))
        panels[k] = p
        out.append(p)
    return out, {"lab": lab, "px": px, "x0": x0, "y1": y1, "ppp": px_per_pt, "shape": (H, W)}


# ---------------------------------------------------------------- columns

def column_blobs(sh: Sheet, region, layer_rx: str = r"col|wall") -> list[P.Blob]:
    fills = [f for f in sh.fills if re.search(layer_rx, f.layer, re.I) and P._touch(f.bb, region, 0)]
    if not fills:      # no column / wall layer: any solid rectangle that is compact
        fills = [f for f in sh.fills if f.rect and P._touch(f.bb, region, 0)]
    out = []
    for grp in P._components([f.bb for f in fills], 0.3):
        xs = [fills[i].bb[0] for i in grp] + [fills[i].bb[2] for i in grp]
        ys = [fills[i].bb[1] for i in grp] + [fills[i].bb[3] for i in grp]
        bb = (min(xs), min(ys), max(xs), max(ys))
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if min(w, h) < 3.0 or max(w, h) > 8 * min(w, h):
            continue
        out.append(P.Blob(bb, [fills[i].handle for i in grp]))
    return out
