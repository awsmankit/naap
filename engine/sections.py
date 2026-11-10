"""Relate section views to the plan they are cut from.

A section line on the plan (layer name containing SEC, with a letter bubble) is
matched to the section view titled "SECTION - <letter>". The horizontal axis of
the view is related to the position along the cut by comparing wall-hatch
intervals (same / shifted / mirrored), as in checks.check_materials.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .checks import _bbox, _loop_crossings, _section_cuts


@dataclass
class SectionMap:
    letter: str
    cut: dict                  # from checks._section_cuts (region, horizontal, c, lo, hi, ent)
    region: dict               # section view region
    mode: str                  # same | shift | mirror
    to_plan: Callable[[float], float]   # section-view x -> position along the cut (plan coord)
    matched: int               # wall intervals that agree


def section_maps(a) -> list[SectionMap]:
    legend = a.legend
    unit = 1 / (a.to_m * 1000)
    ents = a.geometry["entities"]
    hatches = [(i, e) for i, e in enumerate(ents) if e["t"] == "H" and e.get("pn") in legend]
    out = []
    for cut in _section_cuts(a):
        if not cut["letter"] or not a.is_plan(cut["region"]):
            continue
        secs = [r for r in a.regions if r.get("kind") == "section"
                and re.search(rf"SECTION\s*[-–:]?\s*{re.escape(cut['letter'])}\b", r["name"], re.I)]
        if not secs:
            continue
        sec = secs[0]
        plan_iv = []
        for i, e in hatches:
            for lp in e["p"]:
                xs = _loop_crossings(lp, cut["c"], cut["horizontal"])
                for k in range(0, len(xs) - 1, 2):
                    if xs[k + 1] - xs[k] > 20 * unit and cut["lo"] <= xs[k] <= cut["hi"]:
                        plan_iv.append((xs[k], xs[k + 1]))
        sb = sec["bbox"]
        sec_iv = []
        for i, e in hatches:
            for lp in e["p"]:
                bb = _bbox(list(zip(lp[0::2], lp[1::2])))
                if sb[0] <= bb[0] and bb[2] <= sb[2] and sb[1] <= bb[1] and bb[3] <= sb[3] \
                        and bb[2] - bb[0] <= 1000 * unit and bb[3] - bb[1] > bb[2] - bb[0]:
                    sec_iv.append((bb[0], bb[2]))
        if not plan_iv or not sec_iv:
            continue
        # vote: every (plan wall, section wall) pair proposes an offset for each direction;
        # keep the (direction, offset) whose mapped plan walls best overlap section walls
        sec_iv.sort()
        best = None
        tried = set()
        for sgn in (1, -1):
            for p0, p1 in plan_iv:
                pc = (p0 + p1) / 2
                for s0, s1 in sec_iv:
                    if abs((s1 - s0) - (p1 - p0)) > 0.35 * max(p1 - p0, s1 - s0) + 20 * unit:
                        continue
                    off = (s0 + s1) / 2 - sgn * pc
                    key = (sgn, round(off / (50 * unit)))
                    if key in tried:
                        continue
                    tried.add(key)
                    score = 0.0
                    n = 0
                    for q0, q1 in plan_iv:
                        u0, u1 = sorted((sgn * q0 + off, sgn * q1 + off))
                        ov = max((min(u1, t1) - max(u0, t0)) / max(u1 - u0, t1 - t0) for t0, t1 in sec_iv)
                        if ov > 0.5:
                            score += ov
                            n += 1
                    if best is None or score > best[0]:
                        best = (score, n, sgn, off)
        if best is None or best[1] < max(2, len(plan_iv) // 3):
            continue
        _, nmatch, sgn, off = best
        mode = "same direction" if sgn == 1 else "mirrored"
        to_plan = (lambda o, sg: (lambda u: (u - o) / sg))(off, sgn)
        best = (mode, nmatch)
        inv = {mode: to_plan}
        out.append(SectionMap(cut["letter"], cut, sec, best[0], inv[best[0]], best[1]))
    return out
