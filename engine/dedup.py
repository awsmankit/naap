"""Duplicate and stale geometry.

1. Same-place duplicates (`dedupe_geometry`): an entity drawn twice in the same place (copy-paste in
   place, a block inserted twice, a hatch laid twice) is removed before measuring. Rasterised areas
   would survive it, but tags, room labels, railings and dimensions would be counted twice. The
   duplicate record is replaced by an inert {"t": "X"} record so entity indices (and "pi" links) do
   not move; a warning flag lists what was removed.

2. Whole-plan copies in different places (`plan_copies`): two plan regions whose wall geometry is the
   same up to a translation. Typical floors of a multi-storey drawing that carry different floor
   numbers are expected and only noted; anything else is flagged so the user can say whether the copy is
   a separate block / typical floor (keep) or a stale copy (exclude). Nothing is dropped silently.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

from .types import Flag

MEASURED_ROLES = ("wall-brick", "wall-AAC", "parapet", "column", "door", "window", "room text", "groove", "railing",
                  "beam")
WALL_ROLES = ("wall-brick", "wall-AAC", "parapet")
COPY_MIN_ITEMS = 12        # plans with fewer wall items are not compared
COPY_SIM = 0.85            # share of wall items that coincide after the translation (of the smaller plan)


def _fid(category: str, key: str) -> str:
    return f"{category}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def _q(v: float, q: float, off: float) -> int:
    return int(math.floor((v + off) / q + 0.5))


def _key(e: dict, role: str, q: float, off: float):
    t = e["t"]
    if t == "L":
        p = e["p"]
        a, b = (_q(p[0], q, off), _q(p[1], q, off)), (_q(p[2], q, off), _q(p[3], q, off))
        return ("L", role) + tuple(sorted((a, b)))
    if t == "P":
        p = e["p"]
        pts = [(_q(x, q, off), _q(y, q, off)) for x, y in zip(p[0::2], p[1::2])]
        if e.get("c") and len(pts) > 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return ("P", role, bool(e.get("c")), len(pts), tuple(sorted(pts)))
    if t == "H":
        pts = tuple(sorted((_q(x, q, off), _q(y, q, off)) for lp in e["p"] for x, y in zip(lp[0::2], lp[1::2])))
        return ("H", role, e.get("pn", ""), pts)
    if t == "T":
        return ("T", role, " ".join(str(e.get("s", "")).split()).upper(), _q(e["p"][0], q, off), _q(e["p"][1], q, off),
                round(float(e.get("h") or 0), 0))
    if t in ("C", "A"):
        c = e.get("c") or [0, 0]
        k = (t, role, _q(c[0], q, off), _q(c[1], q, off), _q(float(e.get("r") or 0), q, off))
        return k + (tuple(round(v) for v in e.get("a", [])),) if t == "A" else k
    if t == "I":
        return ("I", role, e.get("n"), _q(e["p"][0], q, off), _q(e["p"][1], q, off), round(float(e.get("r") or 0), 1),
                tuple(e.get("s") or ()))
    if t == "D":
        pts = tuple(sorted(tuple(_q(v, q, off) for v in e[k]) for k in ("p1", "p2") if k in e))
        return ("D", pts, round(float(e.get("m") or 0), 1), e.get("s", ""))
    return None


def dedupe_geometry(geometry: dict, layer_roles: dict, to_m: float, tol_mm: float = 1.0) -> tuple[dict, dict]:
    """Return (geometry with same-place duplicates made inert, info). The input is not modified."""
    ents = geometry["entities"]
    names = [l["name"] for l in geometry.get("layers", [])]
    roles = [layer_roles.get(n, "ignore") for n in names]
    q = tol_mm / (to_m * 1000)
    seen = ({}, {})
    dup: dict[int, int] = {}
    dim_parent = set()
    for i, e in enumerate(ents):
        if e["t"] == "D" and not e.get("v"):
            dim_parent.add(i)
    for i, e in enumerate(ents):
        t = e["t"]
        if e.get("v"):
            pi = e.get("pi")
            if pi in dup:                       # child of a duplicated insert
                dup[i] = pi
                continue
            if pi in dim_parent:
                continue                        # dimension graphics: compared via the DIMENSION record
        role = roles[e["l"]] if e.get("l") is not None and e["l"] < len(roles) else "ignore"
        if t in ("L", "P", "C", "A") and role not in MEASURED_ROLES:
            continue
        if t == "I" and e.get("v"):
            continue                            # nested inserts: their exploded content is compared instead
        if t not in ("L", "P", "H", "T", "C", "A", "I", "D"):
            continue
        try:
            k1, k2 = _key(e, role, q, 0.0), _key(e, role, q, q / 2)
        except (KeyError, IndexError, TypeError):
            continue
        if k1 is None:
            continue
        first = seen[0].get(k1)
        if first is None:
            first = seen[1].get(k2)
        if first is not None and first != i:
            dup[i] = first
            continue
        seen[0].setdefault(k1, i)
        seen[1].setdefault(k2, i)
    if not dup:
        return geometry, {"removed": 0}
    new = list(ents)
    by = Counter()
    handles = []
    pts_by = []
    for i, k in dup.items():
        e = ents[i]
        role = roles[e["l"]] if e.get("l") is not None and e["l"] < len(roles) else "ignore"
        new[i] = {"t": "X", "l": e.get("l", 0), "handle": e.get("handle"), "dup_of": ents[k].get("handle"),
                  **({"v": 1, "pi": e.get("pi")} if e.get("v") else {}), "was": e["t"]}
        kind = {"L": "line", "P": "polyline", "H": "hatch", "T": "text", "C": "circle", "A": "arc", "I": "block insert",
                "D": "dimension"}.get(e["t"], e["t"])
        if not (e.get("v") and e.get("pi") in dup):     # count top-level duplicates only
            by[(role, kind)] += 1
            if e.get("handle") and e.get("handle") not in handles:
                handles.append(e.get("handle"))
            p = e.get("p") or e.get("p1")
            if p:
                p0 = p[0] if isinstance(p[0], list) else p
                pts_by.append((p0[0], p0[1], role, kind, e.get("s") if e["t"] == "T" else None))
    g2 = {**geometry, "entities": new}
    return g2, {"removed": len(dup), "by": dict(by), "handles": handles, "points": pts_by}


def dedup_flags(a, info: dict, saved: dict) -> list[Flag]:
    """One warning flag per region holding removed duplicates."""
    if not info.get("removed"):
        return []
    unit = 1 / (a.to_m * 1000)
    per = defaultdict(list)
    for x, y, role, kind, s in info["points"]:
        per[a.region_of(x, y)].append((x, y, role, kind, s))
    flags = []
    for rid, items in per.items():
        if rid is not None and a.kind(rid) not in ("plan", None) and not a.is_plan(rid):
            continue          # duplicates in sections / elevations / details do not affect the MB
        c = Counter((role, kind) for _, _, role, kind, _ in items)
        texts = sorted({s for *_, s in items if s})
        xs, ys = [p[0] for p in items], [p[1] for p in items]
        name = a.region_name(rid)
        f = Flag(
            id=_fid("duplicate", f"same-place:{name}"), severity="warning", category="duplicate",
            title="Entities drawn twice in the same place: duplicates removed before measuring",
            detail=f"{len(items)} identical (within 1 mm) copies in {name} on layers that are measured. Only one of each "
                   "is used; otherwise tags, room labels, railings and dimensions would be counted twice.",
            location={"bbox": [min(xs) - 500 * unit, min(ys) - 500 * unit, max(xs) + 500 * unit, max(ys) + 500 * unit]},
            evidence=[f"{name}: " + ", ".join(f"{n} {kind}(s) on {role} layers" for (role, kind), n in c.most_common())]
            + ([f"duplicated texts: {', '.join(texts[:20])}"] if texts else []),
            suggested_fix="Accept (nothing to enter), and delete the duplicates in CAD (OVERKILL) before the next issue.",
            refs=["duplicates", f"region:{rid}"])
        st = saved.get(f.id) or {}
        if st.get("status") in ("resolved", "accepted"):
            f.status, f.value = st["status"], st.get("value")
        flags.append(f)
    return flags


# ---------------------------------------------------------------- whole-plan copies
def _wall_items(a, rid, q):
    """Translation-invariant keys of a plan's wall/column geometry: (key, anchor cell)."""
    ents = a.geometry["entities"]
    out = []
    for i in a.region_ents(rid):
        e = ents[i]
        role = a.roles[e["l"]] if e.get("l") is not None and e["l"] < len(a.roles) else "ignore"
        t = e["t"]
        if t in ("L", "P") and role in WALL_ROLES + ("column",):
            p = e["p"]
            pts = list(zip(p[0::2], p[1::2]))
            if t == "P" and e.get("c") and len(pts) > 2:
                pts.append(pts[0])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if (x2, y2) < (x1, y1):
                    x1, y1, x2, y2 = x2, y2, x1, y1
                dx, dy = round((x2 - x1) / q), round((y2 - y1) / q)
                if dx == 0 and dy == 0:
                    continue
                out.append(((role, dx, dy), (x1, y1)))
        elif t == "H" and (e.get("pn") in a.legend or role in WALL_ROLES):
            for lp in e["p"]:
                pts = list(zip(lp[0::2], lp[1::2]))
                x0, y0 = min(pts)
                shape = tuple(sorted((round((x - x0) / q), round((y - y0) / q)) for x, y in pts))
                out.append((("H", e.get("pn", ""), shape), (x0, y0)))
    return out


def _match(A, B, q):
    """Best translation B - A and the number of A items found in B after it."""
    ia = defaultdict(list)
    for k, p in A:
        ia[k].append(p)
    ib = defaultdict(list)
    for k, p in B:
        ib[k].append(p)
    votes = Counter()
    for k, pa in ia.items():
        pb = ib.get(k)
        if not pb:
            continue
        for x1, y1 in pa[:8]:
            for x2, y2 in pb[:8]:
                votes[(round((x2 - x1) / (10 * q)), round((y2 - y1) / (10 * q)))] += 1
    if not votes:
        return None, 0
    (tx, ty), _ = votes.most_common(1)[0]
    tx, ty = tx * 10 * q, ty * 10 * q
    cells = defaultdict(set)
    for k, (x, y) in B:
        cells[k].add((round(x / (10 * q)), round(y / (10 * q))))
    n = 0
    for k, (x, y) in A:
        s = cells.get(k)
        if not s:
            continue
        cx, cy = round((x + tx) / (10 * q)), round((y + ty) / (10 * q))
        if any((cx + dx, cy + dy) in s for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            n += 1
    return (tx, ty), n


def plan_copies(a) -> list[dict]:
    """Pairs of included plan regions whose wall geometry coincides after a translation."""
    q = 1 / (a.to_m * 1000)        # 1 mm in drawing units
    plans = [r for r in a.regions if a.is_plan(r["id"])]
    items = {r["id"]: _wall_items(a, r["id"], q) for r in plans}
    out = []
    for k, ra in enumerate(plans):
        for rb in plans[k + 1:]:
            A, B = items[ra["id"]], items[rb["id"]]
            if min(len(A), len(B)) < COPY_MIN_ITEMS:
                continue
            small, big = (A, B) if len(A) <= len(B) else (B, A)
            t, n = _match(small, big, q)
            if t is None:
                continue
            sim = n / len(small)
            if sim < COPY_SIM or len(small) < 0.7 * len(big):
                continue
            if t[0] * t[0] + t[1] * t[1] < (500 * q) ** 2:
                continue        # same place: handled by the same-place de-duplication
            out.append({"a": ra["id"], "b": rb["id"], "sim": round(sim, 3), "offset": t, "n": n,
                        "size": (len(A), len(B)), "identical": sim >= 0.995 and len(A) == len(B)})
    return out


_STALE = re.compile(r"\b(stale|old|exclude|drop|ignore|superseded|delete|remove)\b", re.I)


def copy_flags(a, copies: list[dict], storeys: dict, saved: dict) -> tuple[list[Flag], list[str], set]:
    """Flags for whole-plan copies; returns (flags, assumption notes, region ids the user called stale)."""
    flags, notes, stale = [], [], set()
    plans = (storeys or {}).get("plans", {})
    for c in copies:
        ra, rb = a.reg_by_id[c["a"]], a.reg_by_id[c["b"]]
        fa = set((plans.get(c["a"]) or {}).get("floors") or [])
        fb = set((plans.get(c["b"]) or {}).get("floors") or [])
        dx, dy = c["offset"]
        what = "identical" if c["identical"] else f"{c['sim'] * 100:.0f}% of the wall geometry coincides"
        if fa and fb and not (fa & fb):
            notes.append(f"Plans '{ra['name']}' and '{rb['name']}' are copies ({what}, offset {dx:+.0f}, {dy:+.0f}); "
                         "they carry different floors, so each is measured for its own floors (typical floors).")
            continue
        newer = rb if c["b"] > c["a"] else ra
        f = Flag(
            id=_fid("duplicate", f"plan-copy:{ra['name']}|{rb['name']}"), severity="warning", category="duplicate",
            title=f"Plan '{rb['name']}' is a copy of plan '{ra['name']}' ({what}): separate block, typical floor or stale copy?",
            detail=(f"The wall geometry of the two plans matches after moving by ({dx:+.0f}, {dy:+.0f}) drawing units "
                    f"({c['n']} of {min(c['size'])} wall items). Both are measured. If one is an old revision left "
                    "beside the current plan, say which one so it is excluded."),
            location={"bbox": [min(ra["bbox"][0], rb["bbox"][0]), min(ra["bbox"][1], rb["bbox"][1]),
                               max(ra["bbox"][2], rb["bbox"][2]), max(ra["bbox"][3], rb["bbox"][3])]},
            evidence=[f"'{ra['name']}' bbox {[round(v) for v in ra['bbox']]}", f"'{rb['name']}' bbox {[round(v) for v in rb['bbox']]}"],
            suggested_fix=f"Accept if both are real (separate blocks / floors). If one is stale, enter 'stale: {newer['name']}' "
                          "(or the name of the stale plan).",
            input={"type": "text", "label": "Keep both, or 'stale: <plan name>'", "placeholder": f"stale: {newer['name']}"},
            refs=[f"region:{c['a']}", f"region:{c['b']}", "plan-copy"])
        st = saved.get(f.id) or {}
        if st.get("status") in ("resolved", "accepted"):
            f.status, f.value = st["status"], st.get("value")
        if f.status == "resolved" and f.value and _STALE.search(str(f.value)):
            v = str(f.value).strip().lower()
            named = v.split(":", 1)[1].strip() if ":" in v else ""
            exact = [r for r in (ra, rb) if r["name"].lower() == named]
            hit = exact or sorted([r for r in (ra, rb) if r["name"].lower() in v], key=lambda r: -len(r["name"]))[:1]
            # 'stale' alone: the later (higher id) region is the stale one
            for r in (hit or [newer]):
                stale.add(r["id"])
        flags.append(f)
    return flags, notes, stale
