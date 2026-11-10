"""Drawing checks: flag what is wrong or missing for an MB.

All checks are generic heuristics driven by the confirmed layer roles and view
regions. Each returns a list of Flag. `run_checks` applies the statuses/values
the user saved in user_config["flags"].
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from statistics import median

import numpy as np

from .analysis import Analysis, Segment, opening_kind, preferred_size, schedule_sizes
from .dims import check_dimensions
from .types import Flag

TOL_MM = 10.0


def _fid(category: str, key: str) -> str:
    return f"{category}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def group_id(f) -> str:
    """Id shared by flags that repeat the same finding (same category, severity and title), e.g. the
    same room-size mismatch in every flat of every typical floor. A status saved under this id applies
    to every occurrence."""
    return _fid(f.category, f"group:{f.severity}:{f.title}")


def saved_status(saved: dict, f) -> dict:
    return saved.get(f.id) or saved.get(group_id(f)) or {}


def group_flags(flags: list, plan_of=None, remap: dict | None = None) -> list:
    """Collapse repeated findings into one flag per (category, severity, title). The first occurrence
    keeps its location; every occurrence is listed in `instances` and its refs are merged, so a
    resolution applies to all of them."""
    groups: dict = {}
    order = []
    for f in flags:
        key = (f.category, f.severity, f.title)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(f)
    out = []
    for key in order:
        fs = groups[key]
        if len(fs) == 1:
            out.append(fs[0])
            continue
        g = fs[0]
        plans: dict = {}
        for f in fs:
            p = plan_of(f) if plan_of else ""
            plans[p] = plans.get(p, 0) + 1
        g.instances = [{"id": f.id, "location": f.location, "plan": plan_of(f) if plan_of else ""} for f in fs]
        gid = group_id(g)
        if remap is not None:
            for f in fs:
                remap[f.id] = gid
        g.id = gid
        g.refs = list(dict.fromkeys(r for f in fs for r in f.refs))
        where = ", ".join(f"{p or 'drawing'} x{n}" for p, n in plans.items())
        g.evidence = [f"{len(fs)} occurrences: {where}"] + g.evidence[:6]
        g.detail = (g.detail + " " if g.detail else "") + f"(Same finding at {len(fs)} places; one answer applies to all.)"
        st = {f.status for f in fs}
        if len(st) == 1:
            g.status = fs[0].status
        out.append(g)
    return out


def _f(v: float) -> str:
    return f"{v:.0f}"


# =========================================================== geometry utils
class SegArray:
    """Segments as numpy arrays so a ray cast is one vectorised pass (same result as the loop)."""

    def __init__(self, segs: list[Segment]):
        self.x1 = np.array([s.x1 for s in segs], dtype=float)
        self.y1 = np.array([s.y1 for s in segs], dtype=float)
        self.x2 = np.array([s.x2 for s in segs], dtype=float)
        self.y2 = np.array([s.y2 for s in segs], dtype=float)

    def cast(self, x: float, y: float, horizontal: bool) -> tuple[float, float]:
        if horizontal:
            a1, a2, b1, b2, p, q = self.y1, self.y2, self.x1, self.x2, y, x
        else:
            a1, a2, b1, b2, p, q = self.x1, self.x2, self.y1, self.y2, x, y
        m = (a1 != a2) & ((a1 - p) * (a2 - p) <= 0)
        if not m.any():
            return -math.inf, math.inf
        a1m, a2m, b1m, b2m = a1[m], a2[m], b1[m], b2[m]
        v = b1m + (p - a1m) * (b2m - b1m) / (a2m - a1m)
        up = v[v > q + 1e-6]
        dn = v[v < q - 1e-6]
        return (float(dn.max()) if len(dn) else -math.inf), (float(up.min()) if len(up) else math.inf)


def _cast(segs, x: float, y: float, horizontal: bool) -> tuple[float, float]:
    """Nearest boundary crossings either side of (x, y) along one axis."""
    if isinstance(segs, SegArray):
        return segs.cast(x, y, horizontal)
    lo, hi = -math.inf, math.inf
    for s in segs:
        if horizontal:
            a1, a2, b1, b2, p, q = s.y1, s.y2, s.x1, s.x2, y, x
        else:
            a1, a2, b1, b2, p, q = s.x1, s.x2, s.y1, s.y2, x, y
        if a1 == a2 or (a1 - p) * (a2 - p) > 0:
            continue
        v = b1 + (p - a1) * (b2 - b1) / (a2 - a1)
        if q + 1e-6 < v < hi:
            hi = v
        elif lo < v < q - 1e-6:
            lo = v
    return lo, hi


def _span(segs, x, y, horizontal, offset) -> tuple[float | None, tuple[float, float] | None]:
    vals, centre = [], None
    for k in (-1, 0, 1):
        ox, oy = (0, k * offset) if horizontal else (k * offset, 0)
        lo, hi = _cast(segs, x + ox, y + oy, horizontal)
        if math.isfinite(lo) and math.isfinite(hi):
            vals.append(hi - lo)
            if k == 0:
                centre = (lo, hi)
    if not vals:
        return None, None
    return median(vals), centre


def _proper_cross(s: Segment, t: Segment) -> tuple[float, float] | None:
    """Interior intersection point of two segments (not at/near either end)."""
    d = (s.x2 - s.x1) * (t.y2 - t.y1) - (s.y2 - s.y1) * (t.x2 - t.x1)
    if abs(d) < 1e-9:
        return None
    u = ((t.x1 - s.x1) * (t.y2 - t.y1) - (t.y1 - s.y1) * (t.x2 - t.x1)) / d
    v = ((t.x1 - s.x1) * (s.y2 - s.y1) - (t.y1 - s.y1) * (s.x2 - s.x1)) / d
    if 0.02 < u < 0.98 and 0.02 < v < 0.98:
        return s.x1 + u * (s.x2 - s.x1), s.y1 + u * (s.y2 - s.y1)
    return None


def _grid_pairs(segs: list[Segment], cell: float):
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, s in enumerate(segs):
        for gx in range(int(min(s.x1, s.x2) // cell), int(max(s.x1, s.x2) // cell) + 1):
            for gy in range(int(min(s.y1, s.y2) // cell), int(max(s.y1, s.y2) // cell) + 1):
                grid[(gx, gy)].append(i)
    seen = set()
    for ids in grid.values():
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                key = (ids[a], ids[b]) if ids[a] < ids[b] else (ids[b], ids[a])
                if key not in seen:
                    seen.add(key)
                    yield key


def _bbox(points) -> list[float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


# =========================================================== 1. room labels vs drawn
RAY_FRACTIONS = (-0.4, -0.25, -0.1, 0.0, 0.1, 0.25, 0.4)


def room_rays(segs: list[Segment], x: float, y: float, along_x_size: float, along_y_size: float):
    """Spans measured by a fan of parallel rays through the label.
    Returns (spans_x, spans_y, centre_box) with spans in drawing units."""
    sx, sy, box = [], [], [None, None, None, None]
    for k in RAY_FRACTIONS:
        lo, hi = _cast(segs, x, y + k * along_y_size, True)
        ok = math.isfinite(lo) and math.isfinite(hi)
        sx.append(hi - lo if ok else None)
        if ok and k == 0:
            box[0], box[2] = lo, hi
        lo, hi = _cast(segs, x + k * along_x_size, y, False)
        ok = math.isfinite(lo) and math.isfinite(hi)
        sy.append(hi - lo if ok else None)
        if ok and k == 0:
            box[1], box[3] = lo, hi
    return sx, sy, box


def _consensus(vals: list[float]) -> tuple[float | None, int]:
    if not vals:
        return None, 0
    best, n = None, 0
    for v in vals:
        c = sum(1 for w in vals if abs(w - v) <= TOL_MM)
        if c > n or (c == n and best is not None and v < best):
            best, n = v, c
    return best, n


def check_room_sizes(a: Analysis, skip_layers: set[int]) -> list[Flag]:
    unit = 1 / (a.to_m * 1000)
    by_region: dict = defaultdict(list)
    for s in a.bound_segments:
        if s.layer not in skip_layers:
            by_region[a.region_of(s.x1, s.y1)].append(s)
    arrays = {rid: SegArray(v) for rid, v in by_region.items()}
    empty = SegArray([])
    flags = []
    for r in a.rooms:
        segs = arrays.get(r.region, empty)
        best = None
        for lw, ll in ((r.w, r.l), (r.l, r.w)):  # label may be WxL in either orientation
            sx, sy, box = room_rays(segs, r.x, r.y, lw * unit, ll * unit)
            res = []
            for lab, spans in ((lw, sx), (ll, sy)):
                spans_mm = [a.mm(v) if v is not None else None for v in spans]
                ok = [v for v in spans_mm if v is not None and 0.4 * lab <= v <= 1.6 * lab]
                if not ok:
                    res.append(None)  # open bay / unrelated hit: cannot verify this axis
                    continue
                if any(abs(v - lab) <= TOL_MM for v in ok):
                    res.append((lab, lab, len(ok), 0.0))  # some ray agrees with the label
                    continue
                c, n = _consensus(ok)
                central = spans_mm[len(spans_mm) // 2]
                if n < 3 or (n < 4 and (central is None or abs(central - c) > TOL_MM)):
                    res.append(None)  # rays disagree among themselves: not reliable
                    continue
                res.append((lab, c, n, abs(c - lab)))
            known = [x for x in res if x]
            score = (len(known), -sum(x[3] for x in known))
            if known and (best is None or score > best[0]):
                best = (score, res, box, lw, ll)
        if best is None:
            continue
        _, res, box, lw, ll = best
        bad = [(ax, x) for ax, x in zip("xy", res) if x and x[3] > TOL_MM]
        if not bad:
            continue
        drawn = [x[1] if x else None for x in res]
        drawn_s = " x ".join(_f(v) if v else "?" for v in drawn)
        bbox = box if None not in box else None
        evidence = [f"label text: {r.text!r}",
                    f"wall faces by ray-cast from the label: {drawn_s} mm (x by y); "
                    + ", ".join(f"{n} of {len(RAY_FRACTIONS)} rays agree on {_f(c)}" for _, (lab, c, n, e) in bad)]
        confirmed = any(x[2] >= 5 for _, x in bad)
        for ax, (lab, meas, n, e) in bad:
            for _, d, rid in a.dims:
                m = d.get("m")
                if m and abs(a.mm(m) - meas) <= TOL_MM and rid == r.region and bbox and _near_bbox(d, bbox, 1500 * unit):
                    evidence.append(f"dimension {a.mm(m):.0f} on {a.layer_names[d['l']]} agrees with the drawn size")
                    confirmed = True
                    break
        what = ", ".join(f"{'x' if ax == 'x' else 'y'}: {_f(lab)} labelled vs {_f(meas)} drawn" for ax, (lab, meas, n, e) in bad)
        flags.append(Flag(
            id=_fid("room-size", f"{r.name}@{r.x:.0f},{r.y:.0f}"),
            severity="error" if confirmed else "warning", category="room-size",
            title=f"{r.name}: labelled {_f(r.w)}X{_f(r.l)} but drawn {drawn_s}",
            detail=f"{what} (tolerance {TOL_MM:.0f} mm). The label may be stale or cover a larger area than the room."
                   + ("" if confirmed else " Not confirmed by a dimension: check visually."),
            location={"bbox": bbox} if bbox else {"x": r.x, "y": r.y},
            evidence=evidence,
            suggested_fix="Confirm the correct room size with the architect; enter the size to use in the MB.",
            input={"type": "text", "label": "Use size (WxL, mm)",
                   "placeholder": f"{_f(drawn[0] or lw)}X{_f(drawn[1] or ll)}"},
            refs=[f"room:{r.name}@{r.x:.0f},{r.y:.0f}"],
        ))
    return flags


def check_label_overlaps(a: Analysis) -> list[Flag]:
    """A room label whose WxL, centred on the label, would contain other room labels
    probably measures a group of rooms (risk of double counting)."""
    unit = 1 / (a.to_m * 1000)
    flags = []
    for r in a.rooms:
        for lw, ll in ((r.w, r.l),):
            hw, hl = 0.4 * lw * unit, 0.4 * ll * unit
            inner = [o for o in a.rooms if o is not r and o.region == r.region
                     and abs(o.x - r.x) < hw and abs(o.y - r.y) < hl and o.w * o.l < r.w * r.l]
            if not inner:
                continue
            names = sorted({o.name for o in inner})
            flags.append(Flag(
                id=_fid("room-size", f"group:{r.name}@{r.x:.0f},{r.y:.0f}"), severity="warning", category="room-size",
                title=f"{r.name} ({_f(r.w)}X{_f(r.l)}) seems to include {', '.join(names)}",
                detail="Other labelled rooms fall inside this label's size. The label is probably the gross size of a "
                       "group of rooms, so measuring both would double count floor area.",
                location={"bbox": [r.x - lw * unit / 2, r.y - ll * unit / 2, r.x + lw * unit / 2, r.y + ll * unit / 2]},
                evidence=[f"label {r.text!r} at ({r.x:.0f}, {r.y:.0f})"] + [f"inside: {o.text!r}" for o in inner[:4]],
                suggested_fix="Enter the net size of this room (WxL), or accept if the gross size is intended.",
                input={"type": "text", "label": "Net size (WxL, mm)", "placeholder": f"{_f(r.w)}X{_f(r.l)}"},
                refs=[f"room:{r.name}@{r.x:.0f},{r.y:.0f}"],
            ))
    return flags


def check_overlay_layers(a: Analysis) -> tuple[list[Flag], set[int]]:
    """A wall layer whose outline covers some (not all) rooms of a plan but bounds none
    of them is probably another drawing overlaid on the plan."""
    unit = 1 / (a.to_m * 1000)
    flags, intruders = [], set()
    by_layer: dict[int, list[Segment]] = defaultdict(list)
    for s in a.wall_segments:
        by_layer[s.layer].append(s)
    hit_layers: set[int] = set()
    for r in a.rooms:
        for horizontal in (True, False):
            for k in (-0.25, 0.0, 0.25):
                ox, oy = (0, k * r.l * unit) if horizontal else (k * r.w * unit, 0)
                x, y = r.x + ox, r.y + oy
                lo_hits: dict[int, float] = {}
                hi_hits: dict[int, float] = {}
                for s in a.wall_segments:
                    a1, a2, b1, b2, p, q = (s.y1, s.y2, s.x1, s.x2, y, x) if horizontal else (s.x1, s.x2, s.y1, s.y2, x, y)
                    if a1 == a2 or (a1 - p) * (a2 - p) > 0:
                        continue
                    v = b1 + (p - a1) * (b2 - b1) / (a2 - a1)
                    side = hi_hits if v > q else lo_hits
                    d = abs(v - q)
                    if d < side.get(s.layer, math.inf):
                        side[s.layer] = d
                reach = (r.w if horizontal else r.l) * unit  # ignore hits far outside the room
                for side in (lo_hits, hi_hits):
                    if not side:
                        continue
                    m = min(side.values())
                    if m > reach:
                        continue
                    tied = [lay for lay, d in side.items() if d <= m + 5 * unit]
                    if len(tied) == 1:  # only credit a layer that bounds the room on its own
                        hit_layers.add(tied[0])
    for layer, segs in by_layer.items():
        if len(segs) < 4 or layer in hit_layers:
            continue
        bb = _bbox([(s.x1, s.y1) for s in segs] + [(s.x2, s.y2) for s in segs])
        rid = a.region_of(segs[0].x1, segs[0].y1)
        rooms = [r for r in a.rooms if r.region == rid]
        inside = [r for r in rooms if bb[0] < r.x < bb[2] and bb[1] < r.y < bb[3]]
        if not inside or len(inside) == len(rooms):
            continue
        intruders.add(layer)
        notes = [t["s"].replace("\n", " ") for _, t, trid in a.texts
                 if trid == rid and bb[0] <= t["p"][0] <= bb[2] and bb[1] <= t["p"][1] <= bb[3]
                 and re.search(r"HIGH|HT\.|PLATFORM|RAMP", t["s"], re.I)]
        flags.append(Flag(
            id=_fid("overlap", f"overlay:{a.layer_names[layer]}"), severity="warning", category="overlap",
            title=f"'{a.layer_names[layer]}' is drawn over {len(inside)} room(s) but bounds none of them",
            detail=f"Its outline covers {', '.join(sorted({r.name for r in inside})[:5])} without forming their walls: "
                   "probably a separate feature overlaid on the plan. Excluded from room/opening checks.",
            location={"bbox": bb},
            evidence=[f"{len(segs)} wall segments on {a.layer_names[layer]}"] + [f"text inside: {n!r}" for n in notes[:4]],
            suggested_fix=f"Confirm where this feature really is. If it is not part of the building, set "
                          f"'{a.layer_names[layer]}' to 'ignore'; otherwise accept.",
            refs=[f"layer:{a.layer_names[layer]}"],
        ))
    return flags, intruders


def _near_bbox(d: dict, bbox, margin) -> bool:
    for k in ("p1", "p2"):
        if k not in d:
            return False
        x, y = d[k]
        if not (bbox[0] - margin <= x <= bbox[2] + margin and bbox[1] - margin <= y <= bbox[3] + margin):
            return False
    return True


# =========================================================== 2. schedule consistency
def check_schedule(a: Analysis) -> list[Flag]:
    flags = []
    sizes = schedule_sizes(a)
    unit = 1 / (a.to_m * 1000)
    # (a) the same tag given different sizes in different places
    for tag, by_size in sizes.items():
        if len(by_size) < 2:
            continue
        parts = [f"{_f(w)}X{_f(h)} ({', '.join(sorted({e.source for e in es}))})" for (w, h), es in by_size.items()]
        first = next(iter(by_size.values()))[0]
        pw, ph = preferred_size(by_size)
        flags.append(Flag(
            id=_fid("opening-size", f"conflict:{tag}"), severity="error", category="opening-size",
            title=f"{tag}: conflicting sizes {' vs '.join(parts)}",
            detail=f"The schedule gives more than one size for {tag}. The MB currently uses {_f(pw)}X{_f(ph)}.",
            location={"x": first.x, "y": first.y},
            evidence=[f"{e.source} '{tag}' {_f(w)}X{_f(h)} at ({e.x:.0f}, {e.y:.0f})" for (w, h), es in by_size.items() for e in es],
            suggested_fix="Confirm the correct size; enter it to use in the MB.",
            input={"type": "text", "label": f"Size for {tag} (WxH, mm)", "placeholder": f"{_f(pw)}X{_f(ph)}"},
            refs=[f"tag:{tag}"],
        ))
    # (b) detail heading "TAG (WxH)" vs the dimensions drawn on that elevation detail
    for s in a.schedule:
        if s.source != "heading":
            continue
        hx, hy = s.x, s.y
        widths = []
        for _, d, rid in a.dims:
            if rid != s.region or "p1" not in d or "p2" not in d or not d.get("m"):
                continue
            (x1, y1), (x2, y2) = d["p1"], d["p2"]
            if abs(x2 - x1) <= abs(y2 - y1):
                continue  # vertical dimension
            cx = (x1 + x2) / 2
            ty = (d.get("tp") or d["p1"])[1]
            if abs(cx - hx) <= (s.w / 2 + 600) * unit and hy < ty < hy + 4500 * unit:
                widths.append(a.mm(d["m"]))
        if not widths:
            continue
        drawn = max(widths)
        if abs(drawn - s.w) > TOL_MM:
            flags.append(Flag(
                id=_fid("opening-size", f"detail:{s.tag}@{hx:.0f},{hy:.0f}"), severity="error", category="opening-size",
                title=f"{s.tag}: detail heading says {_f(s.w)} wide but the detail is dimensioned {_f(drawn)}",
                detail="The elevation detail under the heading carries a different overall width dimension.",
                location={"bbox": [hx - s.w / 2 * unit - 600 * unit, hy, hx + s.w / 2 * unit + 600 * unit, hy + 4000 * unit]},
                evidence=[f"heading '{s.tag} ({_f(s.w)}X{_f(s.h)})' at ({hx:.0f}, {hy:.0f})",
                          f"horizontal dims above it: {', '.join(_f(w) for w in sorted(set(widths)))}"],
                suggested_fix="Check the plan opening and confirm the width; enter it to use in the MB.",
                input={"type": "text", "label": f"Size for {s.tag} (WxH, mm)", "placeholder": f"{_f(s.w)}X{_f(s.h)}"},
                refs=[f"tag:{s.tag}"],
            ))
    # (c) tags defined but unused / used but undefined
    plan = defaultdict(list)
    for tag, x, y, _ in a.plan_tags:
        plan[tag].append((x, y))
    for tag in sorted(set(sizes) - set(plan)):
        e = next(iter(sizes[tag].values()))[0]
        flags.append(Flag(
            id=_fid("schedule", f"unused:{tag}"), severity="warning", category="schedule",
            title=f"{tag} is in the schedule but never tagged on the plan",
            detail="Either the type is not used, or the plan uses a different tag name for it.",
            location={"x": e.x, "y": e.y}, evidence=[f"{e.source} entry '{tag}' at ({e.x:.0f}, {e.y:.0f})"],
            suggested_fix="Check whether a plan tag refers to this type under another name; accept if unused.",
            refs=[f"tag:{tag}"],
        ))
    for tag in sorted(set(plan) - set(sizes)):
        pts = plan[tag]
        flags.append(Flag(
            id=_fid("schedule", f"undefined:{tag}"), severity="error", category="schedule",
            title=f"{tag} is tagged {len(pts)}x on the plan but has no size in the schedule",
            detail="Without a schedule size the opening cannot be measured or deducted.",
            location={"bbox": _bbox(pts)} if len(pts) > 1 else {"x": pts[0][0], "y": pts[0][1]},
            evidence=[f"plan tag at ({x:.0f}, {y:.0f})" for x, y in pts[:10]],
            suggested_fix="Enter the opening size (WxH, mm).",
            input={"type": "text", "label": f"Size for {tag} (WxH, mm)", "placeholder": "1000X2100"},
            refs=[f"tag:{tag}"],
        ))
    return flags


# =========================================================== 3. plan door width vs schedule
def check_plan_openings(a: Analysis) -> list[Flag]:
    flags = []
    sizes = schedule_sizes(a)
    unit = 1 / (a.to_m * 1000)
    bad: dict[str, list] = defaultdict(list)
    total: dict[str, int] = defaultdict(int)
    for tag, x, y, rid in a.plan_tags:
        if opening_kind(tag) != "door" or tag not in sizes:
            continue
        near = [d for d in a.doors if math.hypot(d.x - x, d.y - y) <= 1500 * unit]
        if not near:
            continue
        d = min(near, key=lambda d: math.hypot(d.x - x, d.y - y))
        total[tag] += 1
        sw = preferred_size(sizes[tag])[0]
        dw = a.mm(d.width)
        if abs(dw - sw) > TOL_MM:
            bad[tag].append((x, y, dw, sw, d))
    for tag, items in bad.items():
        sw = items[0][3]
        widths = sorted({_f(i[2]) for i in items})
        pts = [(i[0], i[1]) for i in items]
        flags.append(Flag(
            id=_fid("opening-size", f"plan:{tag}:" + ";".join(f"{x:.0f},{y:.0f}" for x, y in pts)),
            severity="error", category="opening-size",
            title=f"{tag}: {len(items)} of {total[tag]} drawn door(s) are {'/'.join(widths)} wide vs schedule {_f(sw)}",
            detail="Door width measured from the swing arc of the door symbol nearest to the tag.",
            location={"bbox": _bbox(pts)} if len(pts) > 1 else {"x": pts[0][0], "y": pts[0][1]},
            evidence=[f"tag {tag} at ({x:.0f}, {y:.0f}); door symbol on {a.layer_names[d.layer]} {dw:.0f} wide"
                      for x, y, dw, _, d in items],
            suggested_fix="Confirm whether the opening or the schedule is right; enter the width to use for these openings.",
            input={"type": "number", "label": "Width to use (mm)", "unit": "mm", "placeholder": widths[0]},
            refs=[f"tag:{tag}"],
        ))
    return flags


# =========================================================== 4. openings without a tag
def find_wall_gaps(a: Analysis, skip_layers: set[int]) -> list[dict]:
    """Openings = pairs of short 'jamb' lines facing each other across a wall gap.
    Parapet lines are left out: a break in a parapet is a step in the roof edge, not a door/window."""
    unit = 1 / (a.to_m * 1000)
    segs = [s for s in a.wall_segments if s.layer not in skip_layers and a.roles[s.layer] != "parapet"]
    ends: dict[tuple[int, int], list[Segment]] = defaultdict(list)
    key = lambda x, y: (round(x / (5 * unit)), round(y / (5 * unit)))  # noqa: E731
    for s in segs:
        ends[key(s.x1, s.y1)].append(s)
        ends[key(s.x2, s.y2)].append(s)

    jambs = []
    for s in segs:
        ln = a.mm(s.length)
        if not 60 <= ln <= 450:
            continue
        vertical = abs(s.x2 - s.x1) < 1 * unit
        horizontal = abs(s.y2 - s.y1) < 1 * unit
        if not (vertical or horizontal):
            continue
        # which side does the wall continue on?
        side = 0
        for px, py in ((s.x1, s.y1), (s.x2, s.y2)):
            for o in ends[key(px, py)]:
                if o is s:
                    continue
                ox, oy = (o.x2, o.y2) if key(o.x1, o.y1) == key(px, py) else (o.x1, o.y1)
                delta = (ox - px) if vertical else (oy - py)
                if abs(delta) > 1 * unit:
                    side += 1 if delta > 0 else -1
        if side == 0:
            continue
        jambs.append((s, vertical, -1 if side > 0 else 1))  # gap direction = away from the wall

    gaps, used = [], set()
    for i, (s, vert, gdir) in enumerate(jambs):
        if i in used:
            continue
        a_lo, a_hi = (min(s.y1, s.y2), max(s.y1, s.y2)) if vert else (min(s.x1, s.x2), max(s.x1, s.x2))
        pos = s.x1 if vert else s.y1
        best = None
        for j, (t, vt, tdir) in enumerate(jambs):
            if j == i or j in used or vt != vert or tdir != -gdir:
                continue
            b_lo, b_hi = (min(t.y1, t.y2), max(t.y1, t.y2)) if vert else (min(t.x1, t.x2), max(t.x1, t.x2))
            if abs(a_lo - b_lo) > 5 * unit or abs(a_hi - b_hi) > 5 * unit:
                continue
            tpos = t.x1 if vert else t.y1
            dist = (tpos - pos) * gdir
            if 450 * unit <= dist <= 6000 * unit and (best is None or dist < best[1]):
                best = (j, dist, tpos)
        if not best:
            continue
        j, dist, tpos = best
        mid = (pos + tpos) / 2
        # make sure no wall line runs across the gap
        blocked = False
        for o in segs:
            if vert and abs(o.y2 - o.y1) < 1 * unit and a_lo - 1 <= o.y1 <= a_hi + 1 and min(o.x1, o.x2) < mid < max(o.x1, o.x2):
                blocked = True
                break
            if not vert and abs(o.x2 - o.x1) < 1 * unit and a_lo - 1 <= o.x1 <= a_hi + 1 and min(o.y1, o.y2) < mid < max(o.y1, o.y2):
                blocked = True
                break
        if blocked:
            continue
        used.update((i, j))
        cx, cy = (mid, (a_lo + a_hi) / 2) if vert else ((a_lo + a_hi) / 2, mid)
        lo, hi = sorted((pos, tpos))
        bbox = [lo, a_lo, hi, a_hi] if vert else [a_lo, lo, a_hi, hi]
        gaps.append({"x": cx, "y": cy, "width_mm": a.mm(dist), "bbox": bbox, "wall_mm": a.mm(a_hi - a_lo),
                     "layer": a.layer_names[s.layer]})
    return gaps


def check_untagged_openings(a: Analysis, skip_layers: set[int]) -> list[Flag]:
    unit = 1 / (a.to_m * 1000)
    flags = []
    for g in find_wall_gaps(a, skip_layers):
        radius = max(1500, 0.75 * g["width_mm"]) * unit
        if any(math.hypot(x - g["x"], y - g["y"]) <= radius for _, x, y, _ in a.plan_tags):
            continue
        has_symbol = any(math.hypot(d.x - g["x"], d.y - g["y"]) <= g["width_mm"] * unit for d in a.doors)
        flags.append(Flag(
            id=_fid("untagged-opening", f"{g['x']:.0f},{g['y']:.0f}"), severity="warning", category="untagged-opening",
            title=f"{_f(g['width_mm'])} wide opening in a {_f(g['wall_mm'])} wall with no door/window tag",
            detail=("A door symbol is drawn but not tagged." if has_symbol else
                    "No door or window type is given: open passage, or a missing door/shutter?"),
            location={"bbox": g["bbox"]},
            evidence=[f"wall gap between jamb lines on {g['layer']} centred at ({g['x']:.0f}, {g['y']:.0f})"],
            suggested_fix="Confirm whether it is an open passage (accept) or enter the door/window type that goes here.",
            input={"type": "text", "label": "Door/window type (tag)", "placeholder": "open"},
        ))
    return flags


# =========================================================== 5. overlapping drawings
def check_overlaps(a: Analysis) -> tuple[list[Flag], set[int]]:
    unit = 1 / (a.to_m * 1000)
    segs = a.wall_segments
    per_layer = defaultdict(int)
    for s in segs:
        per_layer[s.layer] += 1
    crossings: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for i, j in _grid_pairs(segs, 3000 * unit):
        s, t = segs[i], segs[j]
        if s.layer == t.layer:
            continue
        p = _proper_cross(s, t)
        if p:
            k = (s.layer, t.layer) if per_layer[s.layer] <= per_layer[t.layer] else (t.layer, s.layer)
            crossings[k].append(p)
    flags, intruders = [], set()
    plans_of_layer = defaultdict(set)
    for s in segs:
        plans_of_layer[s.layer].add(a.region_of(s.x1, s.y1))
    for (small, big), pts in crossings.items():
        if len(pts) < 3:
            continue
        if len(plans_of_layer[small]) >= 2:
            # the layer carries walls on several plans (every floor of a multi-storey drawing): it is
            # the building's own wall layer, so the crossings are local drafting overlaps, not an overlay
            bb = _bbox(pts)
            flags.append(Flag(
                id=_fid("overlap", f"cross:{a.layer_names[small]}|{a.layer_names[big]}"), severity="warning", category="overlap",
                title=f"Walls on '{a.layer_names[small]}' cross walls on '{a.layer_names[big]}' at {len(pts)} points",
                detail="Wall lines of two layers cross each other (lines drawn through each other). Both layers are "
                       "used on several plans, so both are measured; check the crossing spots.",
                location={"bbox": [bb[0] - 500 * unit, bb[1] - 500 * unit, bb[2] + 500 * unit, bb[3] + 500 * unit]},
                evidence=[f"crossing at ({x:.0f}, {y:.0f}) in {a.region_name(a.region_of(x, y))}" for x, y in pts[:5]],
                suggested_fix="Accept if the lines only overlap in the drawing; fix the layers otherwise.",
                refs=[f"layer:{a.layer_names[small]}"]))
            continue
        intruders.add(small)
        bb = _bbox([(s.x1, s.y1) for s in segs if s.layer == small] + [(s.x2, s.y2) for s in segs if s.layer == small])
        notes = [t["s"].replace("\n", " ") for _, t, rid in a.texts
                 if bb[0] <= t["p"][0] <= bb[2] and bb[1] <= t["p"][1] <= bb[3]
                 and re.search(r"WALL|PLATFORM|RAMP|HIGH|HT\.", t["s"], re.I)]
        flags.append(Flag(
            id=_fid("overlap", f"{a.layer_names[small]}|{a.layer_names[big]}"), severity="warning", category="overlap",
            title=f"Walls on '{a.layer_names[small]}' cross {len(pts)} walls on '{a.layer_names[big]}'",
            detail="Two drawings appear to be overlaid (e.g. a feature drawn on top of the plan). "
                   "Its walls are excluded from room and opening checks until you confirm.",
            location={"bbox": bb},
            evidence=[f"{len(pts)} wall crossings; first at ({pts[0][0]:.0f}, {pts[0][1]:.0f})"] + [f"text nearby: {n!r}" for n in notes[:4]],
            suggested_fix=f"If '{a.layer_names[small]}' is not part of this building, set its role to 'ignore'. "
                          "Otherwise confirm its location and scope.",
            refs=[f"layer:{a.layer_names[small]}"],
        ))
    return flags, intruders


# =========================================================== 6. material plan vs section
def _section_cuts(a: Analysis) -> list[dict]:
    unit = 1 / (a.to_m * 1000)
    ents = a.geometry["entities"]
    letters = [(t, rid) for _, t, rid in a.texts if re.fullmatch(r"[A-Z]{1,2}\d?", t["s"].strip())]
    cuts = []
    for i, e in enumerate(ents):
        if e["t"] != "L" or e.get("v") or not SECTION_RE.search(a.layer_names[e["l"]]):
            continue
        x1, y1, x2, y2 = e["p"]
        rid = a.region_of(x1, y1)
        if not a.is_plan(rid) or math.hypot(x2 - x1, y2 - y1) < 3000 * unit:
            continue
        horizontal = abs(y2 - y1) < 1 * unit
        if not horizontal and abs(x2 - x1) >= 1 * unit:
            continue
        letter = None
        for t, _ in letters:
            if min(math.dist(t["p"], (x1, y1)), math.dist(t["p"], (x2, y2))) < 1000 * unit:
                letter = t["s"].strip()
                break
        cuts.append({"ent": i, "region": rid, "horizontal": horizontal, "c": y1 if horizontal else x1,
                     "lo": min(x1, x2) if horizontal else min(y1, y2), "hi": max(x1, x2) if horizontal else max(y1, y2),
                     "letter": letter})
    return cuts


SECTION_RE = re.compile(r"SEC", re.I)


def _loop_crossings(loop: list[float], c: float, horizontal: bool) -> list[float]:
    pts = list(zip(loop[0::2], loop[1::2]))
    out = []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        a1, a2, b1, b2 = (y1, y2, x1, x2) if horizontal else (x1, x2, y1, y2)
        if a1 == a2 or (a1 - c) * (a2 - c) > 0 or max(a1, a2) == c:
            continue
        out.append(b1 + (c - a1) * (b2 - b1) / (a2 - a1))
    return sorted(out)


def check_materials(a: Analysis) -> list[Flag]:
    legend = a.legend
    if len(set(legend.values())) < 2:
        return []
    unit = 1 / (a.to_m * 1000)
    ents = a.geometry["entities"]
    hatches = [(i, e) for i, e in enumerate(ents) if e["t"] == "H" and e.get("pn") in legend]
    flags = []
    for cut in _section_cuts(a):
        if not cut["letter"]:
            continue
        sec = [r for r in a.regions if r.get("kind") == "section"
               and re.search(rf"SECTION\s*[-–:]?\s*{re.escape(cut['letter'])}\b", r["name"], re.I)]
        if not sec:
            continue
        sec = sec[0]
        # plan: wall pieces cut by the section line
        plan_iv = []
        for i, e in hatches:
            for lp in e["p"]:
                xs = _loop_crossings(lp, cut["c"], cut["horizontal"])
                for k in range(0, len(xs) - 1, 2):
                    if xs[k + 1] - xs[k] > 20 * unit and cut["lo"] <= xs[k] <= cut["hi"]:
                        plan_iv.append((xs[k], xs[k + 1], legend[e["pn"]]))
        # section: thin vertical hatch loops (walls) inside the section view
        sb = sec["bbox"]
        sec_iv = []
        for i, e in hatches:
            for lp in e["p"]:
                bb = _bbox(list(zip(lp[0::2], lp[1::2])))
                if not (sb[0] <= bb[0] and bb[2] <= sb[2] and sb[1] <= bb[1] and bb[3] <= sb[3]):
                    continue
                if bb[2] - bb[0] <= 1000 * unit and bb[3] - bb[1] > bb[2] - bb[0]:
                    sec_iv.append((bb[0], bb[2], legend[e["pn"]]))
        if not plan_iv or not sec_iv:
            continue
        p_lo, p_hi = min(i[0] for i in plan_iv), max(i[1] for i in plan_iv)
        s_lo, s_hi = min(i[0] for i in sec_iv), max(i[1] for i in sec_iv)
        best = None
        for name, f in (("same", lambda v: v), ("shift", lambda v: v - p_lo + s_lo), ("mirror", lambda v: s_hi - (v - p_lo))):
            pairs = []
            for x0, x1, m in plan_iv:
                u0, u1 = sorted((f(x0), f(x1)))
                hits = [sm for s0, s1, sm in sec_iv if min(u1, s1) - max(u0, s0) >= 0.5 * (u1 - u0)]
                if hits:
                    pairs.append((x0, x1, m, hits))
            if best is None or len(pairs) > len(best[1]):
                best = (name, pairs)
        name, pairs = best
        if len(pairs) < max(2, len(plan_iv) // 3):
            continue
        conflicts = defaultdict(list)
        for x0, x1, m, hits in pairs:
            other = {h for h in hits if h != m}
            if other and m not in hits:
                conflicts[(m, sorted(other)[0])].append((x0, x1))
        for (mp, ms), ivs in conflicts.items():
            c = cut["c"]
            pts = [(v, c) for iv in ivs for v in iv] if cut["horizontal"] else [(c, v) for iv in ivs for v in iv]
            bb = _bbox(pts)
            pad = 300 * unit
            flags.append(Flag(
                id=_fid("material", f"{cut['letter']}:{mp}:{ms}:{len(ivs)}"), severity="warning", category="material",
                title=f"{len(ivs)} wall(s) are {mp} in plan but {ms} in {sec['name']}",
                detail=f"Wall hatches on the plan cut line {cut['letter']}-{cut['letter']} do not match the hatches "
                       f"in {sec['name']} (legend: " + ", ".join(f"{k} = {v}" for k, v in legend.items()) + ").",
                location={"bbox": [bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad]},
                evidence=[f"plan wall {a.mm(x1 - x0):.0f} thk at {x0:.0f}..{x1:.0f}" for x0, x1 in ivs[:6]]
                         + [f"section mapping: {name}"],
                suggested_fix="Confirm the wall material; the MB follows the plan unless you enter otherwise.",
                input={"type": "text", "label": "Material to use", "placeholder": mp},
                refs=[f"material:{cut['letter']}"],
            ))
    return flags


# =========================================================== 7. missing information
def _any_text(a: Analysis, pattern: str) -> list[tuple[dict, int | None]]:
    rx = re.compile(pattern, re.I)
    return [(t, rid) for _, t, rid in a.texts if rx.search(t["s"])]


def _wall_blocks(a: Analysis, skip_layers: set[int]) -> list[dict]:
    unit = 1 / (a.to_m * 1000)
    segs = [s for s in a.wall_segments if s.layer not in skip_layers]
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tol = 40 * unit
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    cell = 2000 * unit
    for i, s in enumerate(segs):
        for gx in range(int((min(s.x1, s.x2) - tol) // cell), int((max(s.x1, s.x2) + tol) // cell) + 1):
            for gy in range(int((min(s.y1, s.y2) - tol) // cell), int((max(s.y1, s.y2) + tol) // cell) + 1):
                grid[(gx, gy)].append(i)
    for ids in grid.values():
        for p in range(len(ids)):
            s = segs[ids[p]]
            for q in range(p + 1, len(ids)):
                t = segs[ids[q]]
                if (min(s.x1, s.x2) - tol <= max(t.x1, t.x2) and min(t.x1, t.x2) - tol <= max(s.x1, s.x2)
                        and min(s.y1, s.y2) - tol <= max(t.y1, t.y2) and min(t.y1, t.y2) - tol <= max(s.y1, s.y2)):
                    if _seg_dist(s, t) <= tol:
                        parent[find(ids[p])] = find(ids[q])
    groups = defaultdict(list)
    for i in range(len(segs)):
        groups[find(i)].append(segs[i])
    blocks = []
    for g in groups.values():
        length = a.mm(sum(s.length for s in g))
        if length < 20000:
            continue
        bb = _bbox([(s.x1, s.y1) for s in g] + [(s.x2, s.y2) for s in g])
        rooms = [r for r in a.rooms if bb[0] <= r.x <= bb[2] and bb[1] <= r.y <= bb[3]]
        blocks.append({"bbox": bb, "region": a.region_of(g[0].x1, g[0].y1), "rooms": rooms, "length_mm": length})
    return blocks


def _seg_dist(s: Segment, t: Segment) -> float:
    if _proper_cross(s, t):
        return 0.0
    def pd(px, py, u):
        dx, dy = u.x2 - u.x1, u.y2 - u.y1
        L2 = dx * dx + dy * dy
        k = 0 if L2 == 0 else max(0, min(1, ((px - u.x1) * dx + (py - u.y1) * dy) / L2))
        return math.hypot(px - u.x1 - k * dx, py - u.y1 - k * dy)
    return min(pd(s.x1, s.y1, t), pd(s.x2, s.y2, t), pd(t.x1, t.y1, s), pd(t.x2, t.y2, s))


def check_missing(a: Analysis, skip_layers: set[int]) -> list[Flag]:
    flags = []
    plan_box = None
    plans = [r for r in a.regions if a.is_plan(r["id"])]
    if plans:
        plan_box = {"bbox": plans[0]["bbox"]}

    # (a) wall height per building block: is there a section through it?
    cuts = _section_cuts(a)
    st = getattr(a, "storeys", None) or {}
    # multi-storey: storey heights come from the level labels; roof/terrace plans are not measured as floors
    known_h = {rid for rid, p in (st.get("plans") or {}).items() if st.get("multi") and (p.get("ftf") or p.get("special"))}
    for b in _wall_blocks(a, skip_layers):
        bb = b["bbox"]
        if not b["rooms"]:
            continue  # roofs/terraces/site features: heights come from parapet notes
        if b["region"] in known_h:
            continue  # multi-storey plan: wall height = storey height from the level labels
        crossed = any(c["region"] == b["region"] and (
            (c["horizontal"] and bb[1] < c["c"] < bb[3] and c["lo"] < bb[2] and c["hi"] > bb[0]) or
            (not c["horizontal"] and bb[0] < c["c"] < bb[2] and c["lo"] < bb[3] and c["hi"] > bb[1])) for c in cuts)
        if crossed:
            continue
        names = sorted({r.name for r in b["rooms"]})
        label = f"{', '.join(names[:4])}{'…' if len(names) > 4 else ''}" if names else f"block at ({bb[0]:.0f}, {bb[1]:.0f})"
        sizes = sorted({f"{r.w:.0f}X{r.l:.0f}" for r in b["rooms"]})
        flags.append(Flag(
            id=_fid("missing-data", f"wall-height:{bb[0]:.0f},{bb[1]:.0f}"), severity="missing", category="missing-data",
            title=f"Wall height missing for block: {label}",
            detail="No section line passes through this block, so its wall height cannot be read from the drawing."
                   + (f" Labels: {', '.join(sizes[:3])}." if sizes else ""),
            location={"bbox": bb},
            evidence=[f"{len(b['rooms'])} room label(s), {b['length_mm'] / 1000:.0f} m of wall lines; "
                      f"{len(cuts)} section line(s) found on the plan, none crossing"],
            suggested_fix="Enter the wall height (floor to underside of slab/beam) for this block.",
            input={"type": "number", "label": "Wall height (mm)", "unit": "mm", "placeholder": "3000"},
            refs=[f"region:{b['region']}", "wall-height"],
        ))

    # (b) slab / beam sizes
    str_notes = _any_text(a, r"AS\s*/?\s*(PER\s*)?STR|AS PER STRUCTURAL|STRUCTURAL DRG")
    slab_given = _any_text(a, r"\d{2,3}\s*(MM)?\s*TH(K|ICK)\.?\s*(RCC\s*)?SLAB|SLAB\s*(THK|THICKNESS)\s*[:=-]?\s*\d{2,3}")
    if not slab_given:
        loc = {"x": str_notes[0][0]["p"][0], "y": str_notes[0][0]["p"][1]} if str_notes else plan_box
        flags.append(Flag(
            id=_fid("missing-data", "slab-beam"), severity="missing", category="missing-data",
            title="Slab and beam sizes not given" + (" ('as per structural')" if str_notes else ""),
            detail="RCC slab thickness, beam and lintel sizes are needed for concrete, steel and masonry deductions.",
            location=loc, evidence=[f"note: {t['s'].replace(chr(10), ' ')!r}" for t, _ in str_notes[:3]],
            suggested_fix="Enter the slab thickness (beam sizes go in Assumptions).",
            input={"type": "number", "label": "Slab thickness (mm)", "unit": "mm", "placeholder": "150"},
            refs=["slab"],
        ))

    # (c) foundations / excavation / PCC
    has_foundation = any(re.search(r"FOUNDATION|FOOTING", r["name"], re.I) for r in a.regions) or \
        _any_text(a, r"FOOTING|FOUNDATION\s*(PLAN|DETAIL|DEPTH)|P\.?\s*C\.?\s*C\b|EXCAVATION")
    if not has_foundation:
        flags.append(Flag(
            id=_fid("missing-data", "foundation"), severity="missing", category="missing-data",
            title="No foundation plan, footing sizes, excavation depth or PCC",
            detail="Earthwork, PCC and foundation concrete/masonry cannot be measured.",
            location=plan_box, suggested_fix="Get the foundation drawing, or enter the depth/spec to assume.",
            input={"type": "text", "label": "Foundation depth / spec to assume", "placeholder": "e.g. 1500 deep, PCC 1:4:8 100 thk"},
            refs=["foundation"],
        ))

    # (d) lintels
    if not _any_text(a, r"LINTEL[^\n]*\d{2,4}\s*[xX×]\s*\d{2,4}|\d{2,4}\s*[xX×]\s*\d{2,4}[^\n]*LINTEL|LINTEL[^\n]*\d{2,3}\s*(MM)?\s*TH"):
        flags.append(Flag(
            id=_fid("missing-data", "lintel"), severity="missing", category="missing-data",
            title="Lintel sizes not given",
            detail="Lintel levels may be shown but section sizes are not, so lintel concrete and masonry deductions are open.",
            location=plan_box, suggested_fix="Enter the lintel section to assume (B x D, mm).",
            input={"type": "text", "label": "Lintel size (BxD mm)", "placeholder": "230X150"}, refs=["lintel"],
        ))

    # (e) skirting / dado
    if not _any_text(a, r"SKIRTING|DADO"):
        flags.append(Flag(
            id=_fid("missing-data", "skirting-dado"), severity="missing", category="missing-data",
            title="Skirting / dado not specified", detail="Height and material of skirting and toilet/kitchen dado are not given.",
            location=plan_box, suggested_fix="Enter the skirting/dado spec to assume.",
            input={"type": "text", "label": "Skirting / dado spec", "placeholder": "100 skirting; 2100 dado in toilets"},
            refs=["skirting"],
        ))

    # (f) floor finish per room
    finish = re.compile(r"FLOORING|FLOOR TILE|VITRIFIED|CERAMIC|KOTA|GRANITE|MARBLE|IPS|TREMIX|CC FLOOR|TILES", re.I)
    finish_texts = [(t, rid) for _, t, rid in a.texts if finish.search(t["s"])]
    unit = 1 / (a.to_m * 1000)
    no_finish = []
    for r in a.rooms:
        box = 0.5 * max(r.w, r.l) * unit
        if not any(rid == r.region and abs(t["p"][0] - r.x) < box and abs(t["p"][1] - r.y) < box for t, rid in finish_texts):
            no_finish.append(r)
    if no_finish:
        names = sorted({r.name for r in no_finish})
        elsewhere = sorted({t["s"].replace("\n", " ").strip() for t, rid in finish_texts if not a.is_plan(rid)})
        flags.append(Flag(
            id=_fid("missing-data", "floor-finish"), severity="missing", category="missing-data",
            title=f"Floor finish not specified on plan for {len(no_finish)} room(s)",
            detail=", ".join(names[:30]) + ("…" if len(names) > 30 else ""),
            location={"bbox": _bbox([(r.x, r.y) for r in no_finish])},
            evidence=[f"finish note elsewhere (sections/details): {s!r}" for s in elsewhere[:5]],
            suggested_fix="Enter the default floor finish; specific rooms can be listed in Assumptions.",
            input={"type": "text", "label": "Default floor finish", "placeholder": "40 thk CC flooring"},
            refs=["floor-finish"],
        ))

    # (g) tanks
    tanks = _any_text(a, r"\bTANK\b")
    tanks = [(t, rid) for t, rid in tanks if a.is_plan(rid)]
    if tanks:
        pts = [tuple(t["p"]) for t, _ in tanks]
        flags.append(Flag(
            id=_fid("missing-data", "tank"), severity="missing", category="missing-data",
            title=f"Tank wall/base details missing ({', '.join(sorted({t['s'].replace(chr(10), ' ').strip() for t, _ in tanks})[:3])})",
            detail="Tank walls, base slab and cover are not detailed (usually RCC per structural).",
            location={"bbox": _bbox(pts)} if len(pts) > 1 else {"x": pts[0][0], "y": pts[0][1]},
            suggested_fix="Enter the tank wall/base thickness to assume, or accept to leave tanks out.",
            input={"type": "text", "label": "Tank wall / base thk (mm)", "placeholder": "200 / 200"}, refs=["tank"],
        ))
    return flags


def check_ambiguous(a: Analysis) -> list[Flag]:
    flags = []
    rx = re.compile(r"\(\s*\d+\s*\+\s*\d+\s*\)|\d+\s*\+\s*\d+\s*\+\s*\(?\s*\d+")
    for _, t, rid in a.texts:
        s = t["s"].replace("\n", " ").replace("\t", " ")
        if rx.search(s) and not re.search(r"LVL|LEVEL|±|\+\s*\d+\s*$", s.split("+")[0], re.I):
            flags.append(Flag(
                id=_fid("ambiguous", f"{s}@{t['p'][0]:.0f},{t['p'][1]:.0f}"), severity="warning", category="ambiguous",
                title=f"Ambiguous height/size notation: {s.strip()!r}",
                detail="A sum of values is given without saying what each part is (e.g. water depth + freeboard?).",
                location={"x": t["p"][0], "y": t["p"][1]},
                evidence=[f"text on {a.layer_names[t['l']]} in {a.region_name(rid)}"],
                suggested_fix="Confirm what the notation means; enter the value to use.",
                input={"type": "text", "label": "Meaning / value to use", "placeholder": ""},
            ))
    return flags


# =========================================================== driver
def run_checks(a: Analysis) -> list[Flag]:
    overlap_flags, intruders = check_overlaps(a)
    overlay_flags, more = check_overlay_layers(a)
    overlap_flags += overlay_flags
    intruders |= more
    a.skip_layers = set(intruders)
    flags = []
    for fn in (lambda: check_room_sizes(a, intruders), lambda: check_label_overlaps(a), lambda: check_schedule(a), lambda: check_plan_openings(a),
               lambda: check_untagged_openings(a, intruders), lambda: overlap_flags, lambda: check_materials(a),
               lambda: check_missing(a, intruders), lambda: check_ambiguous(a), lambda: check_dimensions(a)):
        try:
            flags.extend(fn())
        except Exception as exc:  # one broken heuristic must not kill the MB
            flags.append(Flag(id=_fid("internal", repr(exc)), severity="warning", category="internal",
                              title=f"A check failed to run: {type(exc).__name__}", detail=str(exc)[:300]))
    if not any(r in ("wall-brick", "wall-AAC") for r in a.roles):
        flags.append(Flag(id="setup-no-walls", severity="error", category="setup",
                          title="No layer is assigned a wall role", suggested_fix="Assign wall layers in the Layers tab."))
    if not any(a.is_plan(r["id"]) for r in a.regions):
        flags.append(Flag(id="setup-no-plan", severity="error", category="setup",
                          title="No included region is marked as a plan", suggested_fix="Tick a plan region in the Regions tab."))
    # de-duplicate ids, apply saved statuses
    saved = a.config.get("flags", {}) or {}
    seen, out = set(), []
    for f in flags:
        if f.id in seen:
            continue
        seen.add(f.id)
        st = saved_status(saved, f)
        if st.get("status") in ("resolved", "accepted"):
            f.status = st["status"]
            f.value = st.get("value")
        out.append(f)
    order = {"error": 0, "warning": 1, "missing": 2}
    out.sort(key=lambda f: (f.status != "open", order.get(f.severity, 3), f.category))
    return out
