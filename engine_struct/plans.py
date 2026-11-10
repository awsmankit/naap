"""Plan reading for structural sheets: column blobs, the main plan region, piles, tags,
beam strips (from dashed double lines) and slab panels (flood fill between beams).

All lengths here are in PDF points; calibrate.py converts them to millimetres.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from .sheet import Circ, Seg, Sheet, Txt


@dataclass
class Blob:
    bb: tuple[float, float, float, float]
    handles: list[str]
    kind: str = "rect"            # "rect" | "circle"
    tag: str = ""                 # column type (C1...) when known
    tag_src: str = ""

    @property
    def w(self) -> float:
        return self.bb[2] - self.bb[0]

    @property
    def h(self) -> float:
        return self.bb[3] - self.bb[1]

    @property
    def cx(self) -> float:
        return (self.bb[0] + self.bb[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bb[1] + self.bb[3]) / 2


def _touch(a, b, tol) -> bool:
    return a[0] <= b[2] + tol and b[0] <= a[2] + tol and a[1] <= b[3] + tol and b[1] <= a[3] + tol


def _components(bbs: list, tol: float) -> list[list[int]]:
    """Connected groups of boxes that touch (within tol). Grid-bucketed union-find."""
    n = len(bbs)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = max(tol * 4, 20.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for i, b in enumerate(bbs):
        for gx in range(int((b[0] - tol) // cell), int((b[2] + tol) // cell) + 1):
            for gy in range(int((b[1] - tol) // cell), int((b[3] + tol) // cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
    for ids in grid.values():
        for a_i, i in enumerate(ids):
            for j in ids[a_i + 1:]:
                if find(i) != find(j) and _touch(bbs[i], bbs[j], tol):
                    parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def column_layers(sh: Sheet) -> set[str]:
    return {f.layer for f in sh.fills if re.search(r"COL", f.layer, re.I)} or {f.layer for f in sh.fills}


def column_blobs(sh: Sheet, region=None, min_side: float = 3.0) -> list[Blob]:
    """Solid-filled column sections (rectangles, possibly in several fill pieces) and
    stroked circles on the same layers (circular columns)."""
    lays = column_layers(sh)
    fills = [f for f in sh.fills if f.layer in lays and (region is None or _touch(f.bb, region, 0))]
    out = []
    for grp in _components([f.bb for f in fills], 0.3):
        xs = [fills[i].bb[0] for i in grp] + [fills[i].bb[2] for i in grp]
        ys = [fills[i].bb[1] for i in grp] + [fills[i].bb[3] for i in grp]
        bb = (min(xs), min(ys), max(xs), max(ys))
        if bb[2] - bb[0] < min_side or bb[3] - bb[1] < min_side:
            continue
        if max(bb[2] - bb[0], bb[3] - bb[1]) > 6 * min(bb[2] - bb[0], bb[3] - bb[1]):
            continue
        out.append(Blob(bb, [fills[i].handle for i in grp]))
    for c in sh.circles:
        if c.layer in lays and c.r * 2 >= min_side and (region is None or _touch((c.cx, c.cy, c.cx, c.cy), region, 0)):
            bb = (c.cx - c.r, c.cy - c.r, c.cx + c.r, c.cy + c.r)
            if not any(_touch(bb, b.bb, -0.5 * c.r) for b in out):
                out.append(Blob(bb, [c.handle], "circle"))
    # a filled circle drawn as a fill (circular column) is already a blob; mark it
    for c in sh.circles:
        if c.filled and c.layer in lays:
            for b in out:
                if abs(b.cx - c.cx) < 0.3 * c.r and abs(b.cy - c.cy) < 0.3 * c.r:
                    b.kind = "circle"
    return out


def main_cluster(items: list, link: float) -> list[int]:
    """Indices of the largest group of boxes linked within `link` (the main plan)."""
    if not items:
        return []
    bbs = [it.bb if hasattr(it, "bb") else it for it in items]
    groups = _components(bbs, link)
    return max(groups, key=len)


def plan_region(sh: Sheet, blobs: list[Blob] | None = None, link: float = 160.0) -> tuple | None:
    blobs = blobs if blobs is not None else column_blobs(sh)
    if len(blobs) < 6:
        return None
    idx = main_cluster(blobs, link)
    if len(idx) < 6:
        return None
    xs = [blobs[i].bb[0] for i in idx] + [blobs[i].bb[2] for i in idx]
    ys = [blobs[i].bb[1] for i in idx] + [blobs[i].bb[3] for i in idx]
    return (min(xs), min(ys), max(xs), max(ys))


def grow(bb, d):
    return (bb[0] - d, bb[1] - d, bb[2] + d, bb[3] + d)


def inside(x, y, bb) -> bool:
    return bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]


# ------------------------------------------------------------------ piles

def pile_circles(sh: Sheet, region, exclude_layers: set[str] | None = None, min_count: int = 10) -> tuple[list[Circ], float]:
    """The biggest family of equal circles in the plan region (pile marks). Duplicates
    (same centre) are dropped. Returns (circles, radius)."""
    cs = [c for c in sh.circles if not c.filled and inside(c.cx, c.cy, region)
          and (not exclude_layers or c.layer not in exclude_layers)]
    if not cs:
        return [], 0.0
    fam = Counter((c.layer, round(c.r, 1)) for c in cs)
    (lay, r), n = fam.most_common(1)[0]
    if n < min_count:
        return [], 0.0
    pick = [c for c in cs if c.layer == lay and abs(c.r - r) <= 0.15]
    seen: dict[tuple[int, int], Circ] = {}
    for c in pick:
        k = (round(c.cx / 0.5), round(c.cy / 0.5))
        seen.setdefault(k, c)
    return list(seen.values()), r


# ------------------------------------------------------------------ tags

def tags(sh: Sheet, rx: str, region=None) -> list[Txt]:
    return [t for t in sh.full(rx) if region is None or inside(t.cx, t.cy, region)]


# ------------------------------------------------------------------ beams

@dataclass
class Line:
    horiz: bool
    c: float                   # y for horizontal, x for vertical
    a: float                   # start along the axis
    b: float                   # end
    handles: list[str] = field(default_factory=list)


@dataclass
class Strip:
    horiz: bool
    c: float                   # centreline coordinate
    a: float
    b: float
    width_pt: float
    handles: list[str] = field(default_factory=list)
    tag: str = ""
    tag_src: str = ""          # "tag" | "inherited" | ""
    tag_ref: Txt | None = None
    id: int = 0

    @property
    def length(self) -> float:
        return self.b - self.a

    @property
    def bb(self):
        w = self.width_pt / 2
        return (self.a, self.c - w, self.b, self.c + w) if self.horiz else (self.c - w, self.a, self.c + w, self.b)

    def dist(self, x, y) -> float:
        u, v = (x, y) if self.horiz else (y, x)
        du = 0.0 if self.a <= u <= self.b else min(abs(u - self.a), abs(u - self.b))
        return math.hypot(du, max(0.0, abs(v - self.c) - self.width_pt / 2))


def beam_layers(sh: Sheet) -> set[str]:
    lays = {s.layer for s in sh.segs if re.search(r"BEAM", s.layer, re.I)}
    if lays:
        return lays
    return {s.layer for s in sh.segs if s.dash}


def merge_lines(segs: list[Seg], gap: float | None = None) -> tuple[list[Line], float]:
    """Collinear axis-aligned dash pieces -> continuous lines. The merge gap is learnt
    from the most common dash gap unless given."""
    rows: dict[tuple[bool, float], list[tuple[float, float, str]]] = {}
    for s in segs:
        if s.length < 0.3:
            continue
        if s.horiz:
            rows.setdefault((True, round((s.y1 + s.y2) / 2, 1)), []).append((min(s.x1, s.x2), max(s.x1, s.x2), s.handle))
        elif s.vert:
            rows.setdefault((False, round((s.x1 + s.x2) / 2, 1)), []).append((min(s.y1, s.y2), max(s.y1, s.y2), s.handle))
    if gap is None:
        gaps = Counter()
        for pcs in rows.values():
            pcs.sort()
            for p, q in zip(pcs, pcs[1:]):
                g = q[0] - p[1]
                if 0.3 < g < 12:
                    gaps[round(g, 1)] += 1
        gap = (gaps.most_common(1)[0][0] * 1.25) if gaps else 3.0
    # rows within 0.15pt of each other are the same line
    keys = sorted(rows)
    merged_rows: dict[tuple[bool, float], list] = {}
    for k in keys:
        prev = next((m for m in merged_rows if m[0] == k[0] and abs(m[1] - k[1]) <= 0.15), None)
        merged_rows.setdefault(prev or k, []).extend(rows[k])
    out = []
    for (hz, c), pcs in merged_rows.items():
        pcs.sort()
        cur = None
        for a, b, h in pcs:
            if cur and a - cur.b <= gap:
                cur.b = max(cur.b, b)
                cur.handles.append(h)
            else:
                if cur:
                    out.append(cur)
                cur = Line(hz, c, a, b, [h])
        if cur:
            out.append(cur)
    return out, gap


def _subtract(a: float, b: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = [(a, b)]
    for c0, c1 in sorted(cuts):
        nxt = []
        for x0, x1 in out:
            if c1 <= x0 or c0 >= x1:
                nxt.append((x0, x1))
                continue
            if c0 > x0:
                nxt.append((x0, c0))
            if c1 < x1:
                nxt.append((c1, x1))
        out = nxt
    return out


def beam_strips(lines: list[Line], blobs: list[Blob], wmin: float, wmax: float, min_len: float) -> list[Strip]:
    """Pair parallel lines wmin..wmax apart into beam strips, then cut them at columns
    (so lengths are clear spans between column faces)."""
    strips: list[Strip] = []
    for hz in (True, False):
        ls = sorted([l for l in lines if l.horiz == hz and l.b - l.a >= min_len], key=lambda l: l.c)
        used_pairs = set()
        for i, l1 in enumerate(ls):
            for j in range(i + 1, len(ls)):
                l2 = ls[j]
                d = l2.c - l1.c
                if d > wmax:
                    break
                if d < wmin:
                    continue
                a, b = max(l1.a, l2.a), min(l1.b, l2.b)
                if b - a < min_len:
                    continue
                # a nearer partner on either side means l1-l2 are not one beam
                if any(wmin <= l3.c - l1.c < d and min(l3.b, l1.b) - max(l3.a, l1.a) >= 0.8 * (b - a)
                       for l3 in ls[i + 1:j]):
                    continue
                used_pairs.add((i, j))
                strips.append(Strip(hz, (l1.c + l2.c) / 2, a, b, d, l1.handles + l2.handles))
    # cut at columns
    out: list[Strip] = []
    for s in strips:
        cuts = []
        for bl in blobs:
            lo, hi = (bl.bb[1], bl.bb[3]) if s.horiz else (bl.bb[0], bl.bb[2])
            if lo - 0.5 <= s.c <= hi + 0.5:
                cuts.append((bl.bb[0], bl.bb[2]) if s.horiz else (bl.bb[1], bl.bb[3]))
        for a, b in _subtract(s.a, s.b, cuts):
            if b - a >= min_len:
                out.append(Strip(s.horiz, s.c, a, b, s.width_pt, s.handles))
    # drop strips that duplicate a longer one (same axis, overlapping heavily)
    out.sort(key=lambda s: -s.length)
    keep: list[Strip] = []
    for s in out:
        if any(k.horiz == s.horiz and abs(k.c - s.c) < 0.5 * s.width_pt and min(k.b, s.b) - max(k.a, s.a) > 0.6 * s.length
               for k in keep):
            continue
        keep.append(s)
    for i, s in enumerate(keep):
        s.id = i
    return keep


def assign_beam_tags(strips: list[Strip], tag_txts: list[Txt], max_d: float) -> list[Txt]:
    """Each tag labels the nearest strip (within max_d). Strips left without a tag take the
    tag of the nearest tagged strip on the same line (continuous beam). Returns tags that
    matched no strip."""
    orphan = []
    for t in tag_txts:
        best = min(strips, key=lambda s: s.dist(t.cx, t.cy), default=None)
        if best is None or best.dist(t.cx, t.cy) > max_d:
            orphan.append(t)
            continue
        if best.tag and best.tag != t.s.strip():
            # two different tags on one strip: keep the nearer
            if best.tag_ref and best.dist(best.tag_ref.cx, best.tag_ref.cy) <= best.dist(t.cx, t.cy):
                continue
        best.tag, best.tag_src, best.tag_ref = t.s.strip(), "tag", t
    for s in strips:
        if s.tag:
            continue
        same = [o for o in strips if o.tag_src == "tag" and o.horiz == s.horiz and abs(o.c - s.c) < 0.6 * s.width_pt]
        if same:
            o = min(same, key=lambda o: min(abs(o.a - s.b), abs(s.a - o.b), abs(o.a - s.a)))
            s.tag, s.tag_src, s.tag_ref = o.tag, "inherited", o.tag_ref
    return orphan


# ------------------------------------------------------------------ slab panels

@dataclass
class Panel:
    tag: str
    area_pt2: float
    bb: tuple
    tag_txt: Txt
    sunken: bool = False
    leaked: bool = False
    shared: list[str] = field(default_factory=list)   # other tags in the same panel
    label: int = 0


SHRINK_PX = 1.5


def slab_panels(region, lines: list[Line], blobs: list[Blob], s_tags: list[Txt], sunken_txts: list[Txt],
                px_per_pt: float = 2.0, max_area_pt2: float | None = None) -> list[Panel]:
    """Flood-fill the space between beam lines and columns from each slab tag."""
    x0, y0, x1, y1 = grow(region, 6)
    W = int((x1 - x0) * px_per_pt) + 3
    H = int((y1 - y0) * px_per_pt) + 3
    wall = np.zeros((H, W), dtype=bool)

    def px(x, y):
        return int(round((x - x0) * px_per_pt)), int(round((y1 - y) * px_per_pt))

    for l in lines:
        if l.horiz:
            a, r = px(l.a, l.c)
            b, _ = px(l.b, l.c)
            if 0 <= r < H:
                wall[max(r - 0, 0):r + 1, max(min(a, b), 0):min(max(a, b) + 1, W)] = True
        else:
            c, ra = px(l.c, l.a)
            _, rb = px(l.c, l.b)
            if 0 <= c < W:
                wall[max(min(ra, rb), 0):min(max(ra, rb) + 1, H), c:c + 1] = True
    for bl in blobs:
        a, r1 = px(bl.bb[0], bl.bb[1])
        b, r0 = px(bl.bb[2], bl.bb[3])
        wall[max(r0, 0):min(r1 + 1, H), max(a, 0):min(b + 1, W)] = True
    wall = ndimage.binary_dilation(wall, iterations=1)
    lab, n = ndimage.label(~wall)
    sizes = ndimage.sum(np.ones_like(lab), lab, index=np.arange(n + 1))
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])).tolist())
    panels: list[Panel] = []
    by_label: dict[int, Panel] = {}
    sunk_labels = set()
    for t in sunken_txts:
        c, r = px(t.cx, t.cy)
        if 0 <= r < H and 0 <= c < W and lab[r, c]:
            sunk_labels.add(int(lab[r, c]))
    for t in s_tags:
        c, r = px(t.cx, t.cy)
        if not (0 <= r < H and 0 <= c < W):
            continue
        k = int(lab[r, c])
        if k == 0:  # tag text sits on a line: probe a small neighbourhood
            win = lab[max(r - 4, 0):r + 5, max(c - 4, 0):c + 5]
            vals = [v for v in np.unique(win) if v]
            k = int(max(vals, key=lambda v: sizes[v])) if vals else 0
        if k == 0:
            continue
        if k in by_label:
            by_label[k].shared.append(t.s.strip())
            continue
        rows, cols = np.nonzero(lab == k)
        # walls are drawn 1 px wide on the line centre and dilated by 1 px, so the free
        # region stops 1.5 px short of each bounding line: add that strip back
        sh_ = SHRINK_PX
        bb = (x0 + (cols.min() - sh_) / px_per_pt, y1 - (rows.max() + sh_) / px_per_pt,
              x0 + (cols.max() + sh_) / px_per_pt, y1 - (rows.min() - sh_) / px_per_pt)
        area = (sizes[k] + sh_ * _perimeter(lab == k) + 4 * sh_ * sh_) / (px_per_pt ** 2)
        leaked = k in border or (max_area_pt2 is not None and area > max_area_pt2)
        p = Panel(t.s.strip(), area, bb, t, sunken=k in sunk_labels, leaked=leaked, label=k)
        by_label[k] = p
        panels.append(p)
    return panels


def _perimeter(mask) -> float:
    er = ndimage.binary_erosion(mask)
    return float((mask & ~er).sum())
