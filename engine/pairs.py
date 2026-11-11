"""Pair parallel wall-face lines into wall strips (centre line + thickness).

Used for parapets on roof plans and for walls drawn only as lines (no hatch).
Only axis-aligned segments are paired; diagonal walls are reported by the caller.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Strip:
    horizontal: bool       # True: runs along x, thickness along y
    c: float               # centre-line coordinate (y if horizontal else x)
    lo: float              # extent along the run
    hi: float
    t: float               # thickness (drawing units)
    ents: set = field(default_factory=set)

    @property
    def length(self) -> float:
        return self.hi - self.lo

    def rect(self) -> list[float]:
        h = self.t / 2
        if self.horizontal:
            return [self.lo, self.c - h, self.hi, self.c + h]
        return [self.c - h, self.lo, self.c + h, self.hi]


def axis_segments(items, tol: float):
    """items: iterable of (x1, y1, x2, y2, ent). Returns (horizontal, vertical) lists of
    (pos, lo, hi, ent); others are counted as skipped."""
    hs, vs, skipped = [], [], 0
    for x1, y1, x2, y2, ent in items:
        ln = max(abs(x2 - x1), abs(y2 - y1))
        tl = max(tol, 0.002 * ln)  # nearly axis-aligned (drafting slop) still counts
        if abs(y2 - y1) <= tl and abs(x2 - x1) > tl:
            hs.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2), ent))
        elif abs(x2 - x1) <= tl and abs(y2 - y1) > tl:
            vs.append(((x1 + x2) / 2, min(y1, y2), max(y1, y2), ent))
        elif abs(x2 - x1) > tol or abs(y2 - y1) > tol:
            skipped += 1
    return hs, vs, skipped


def _subtract(intervals, lo, hi):
    """Parts of [lo, hi] not covered by the (sorted, disjoint) intervals."""
    out, cur = [], lo
    for a, b in intervals:
        if b <= cur or a >= hi:
            continue
        if a > cur:
            out.append((cur, min(a, hi)))
        cur = max(cur, b)
        if cur >= hi:
            break
    if cur < hi:
        out.append((cur, hi))
    return out


def _add(intervals, lo, hi):
    intervals.append((lo, hi))
    intervals.sort()
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    intervals[:] = merged


def pair_lines(segs, horizontal: bool, tmin: float, tmax: float, min_len: float) -> list[Strip]:
    """For each line, the nearest parallel line on its + side (overlapping) forms a strip
    when their spacing is within [tmin, tmax]."""
    segs = sorted(segs)
    out: list[Strip] = []
    for k, (pa, lo, hi, ea) in enumerate(segs):
        covered: list[tuple[float, float]] = []
        for pb, lo2, hi2, eb in segs[k + 1:]:
            d = pb - pa
            if d <= 1e-6:
                continue
            if d > tmax:
                break
            a, b = max(lo, lo2), min(hi, hi2)
            if b - a <= 0:
                continue
            for u, v in _subtract(covered, a, b):
                if d >= tmin and v - u >= min_len:
                    out.append(Strip(horizontal, pa + d / 2, u, v, d, {ea, eb}))
            _add(covered, a, b)
            if covered and covered[0][0] <= lo and covered[0][1] >= hi:
                break
    return _merge(out)


def _merge(strips: list[Strip]) -> list[Strip]:
    """Join collinear touching strips of the same thickness."""
    groups = defaultdict(list)
    for s in strips:
        groups[(s.horizontal, round(s.c), round(s.t))].append(s)
    out = []
    for ss in groups.values():
        ss.sort(key=lambda s: s.lo)
        cur = None
        for s in ss:
            if cur and s.lo <= cur.hi + 1e-6:
                cur.hi = max(cur.hi, s.hi)
                cur.ents |= s.ents
            else:
                cur = Strip(s.horizontal, s.c, s.lo, s.hi, s.t, set(s.ents))
                out.append(cur)
    return out


def join_corners(strips: list[Strip]) -> None:
    """Extend strip ends to the centre line of a perpendicular strip they meet
    (L corners and T junctions), so centre-line lengths are measured corner to corner.

    Only perpendicular strips whose centre line lies near an end can move it, so they are looked up
    by centre line (sorted, bisect) instead of testing every pair; they are visited in list order as
    before, and the window grows if an end moves, so the result is the same as the all-pairs loop."""
    from bisect import bisect_left, bisect_right
    by_c = {h: sorted((o.c, k) for k, o in enumerate(strips) if o.horizontal == h) for h in (True, False)}
    cs = {h: [c for c, _ in v] for h, v in by_c.items()}
    R = max((o.t for o in strips), default=0.0)
    for s in strips:
        others, oc = by_c[not s.horizontal], cs[not s.horizontal]
        lo0, hi0 = s.lo, s.hi
        wlo, whi = lo0 - R, hi0 + R
        while True:
            s.lo, s.hi = lo0, hi0
            for k in sorted(k for _, k in others[bisect_left(oc, wlo):bisect_right(oc, whi)]):
                o = strips[k]
                reach = max(s.t, o.t)
                # o crosses s's centre line within its (slightly extended) extent?
                if not (o.lo - reach <= s.c <= o.hi + reach):
                    continue
                if abs(o.c - s.lo) <= reach and o.c < s.lo:
                    s.lo = o.c
                elif abs(o.c - s.hi) <= reach and o.c > s.hi:
                    s.hi = o.c
            if s.lo - R >= wlo and s.hi + R <= whi:
                break
            wlo, whi = min(wlo, s.lo - R), max(whi, s.hi + R)


def strips_from_segments(items, tmin, tmax, tol, min_len) -> tuple[list[Strip], int]:
    hs, vs, skipped = axis_segments(items, tol)
    strips = pair_lines(hs, True, tmin, tmax, min_len) + pair_lines(vs, False, tmin, tmax, min_len)
    join_corners(strips)
    return strips, skipped
