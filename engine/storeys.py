"""Storeys: which floor(s) a plan represents, floor levels and storey heights.

Generic text parsing only (Indian drafting conventions):
  * plan titles / sheet names: "GROUND FLOOR PLAN", "FIRST FLOOR PLAN", "TYPICAL (3rd-5th) FLOOR PLAN",
    "TYPICAL FLOOR PLAN (3RD TO 5TH)", "3 TO 5", "6 TO 9", "TERRACE PLAN", "MUMTY"
  * level labels: "First Floor Lvl.+3450", "Ground Floor Lvl.+450", "Terrace |Lvl.+15600", "F.G.L. ±00"

Floor numbers: ground = 0, first = 1, ...; basement = -1. Terrace / mumty are kept as names.
"""
from __future__ import annotations

import re

ORD_WORDS = {
    "GROUND": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7,
    "EIGHTH": 8, "EIGHTTH": 8, "NINTH": 9, "TENTH": 10, "ELEVENTH": 11, "TWELFTH": 12, "TWELVTH": 12,
    "THIRTEENTH": 13, "FOURTEENTH": 14, "FIFTEENTH": 15, "SIXTEENTH": 16, "SEVENTEENTH": 17,
    "EIGHTEENTH": 18, "NINETEENTH": 19, "TWENTIETH": 20, "BASEMENT": -1, "STILT": 0,
}
_ORD = "|".join(sorted(ORD_WORDS, key=len, reverse=True))
_NUM = r"(\d{1,2})\s*(?:ST|ND|RD|TH)?"
RANGE_RE = re.compile(_NUM + r"\s*(?:TO|-|–|—|&|AND)\s*" + _NUM + r"(?![\d])", re.I)
WORD_RANGE_RE = re.compile(rf"\b({_ORD})\b\s*(?:FLOOR)?\s*(?:TO|-|–|&|AND)\s*\b({_ORD})\b", re.I)
WORD_RE = re.compile(rf"\b({_ORD})\b", re.I)
NUM_FLOOR_RE = re.compile(r"\b(\d{1,2})\s*(?:ST|ND|RD|TH)\s*(?:FLOOR|FLR)\b", re.I)
GF_RE = re.compile(r"\b(G\.?\s*F\.?|GF)\b", re.I)
TERRACE_RE = re.compile(r"\b(TERRACE|ROOF)\b", re.I)
MUMTY_RE = re.compile(r"\b(MUMTY|MUMTI|STAIR\s*ROOM|MACHINE\s*ROOM)\b", re.I)
LEVEL_RE = re.compile(r"(?:LVL|LEVEL|FFL|F\.F\.L|SFL)\.?\s*[:=]?\s*([+\-±]?)\s*(\d+(?:\.\d+)?)", re.I)


def parse_floors(text: str) -> tuple[list[int] | None, str | None]:
    """Floors a title/sheet name refers to: ([3, 4, 5], None), ([0], None), (None, "terrace") or (None, None)."""
    s = " ".join(str(text or "").replace("\n", " ").split())
    if not s:
        return None, None
    m = WORD_RANGE_RE.search(s)
    if m:
        a, b = ORD_WORDS[m.group(1).upper()], ORD_WORDS[m.group(2).upper()]
        if b > a:
            return list(range(a, b + 1)), None
    m = RANGE_RE.search(s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a < b <= 60:
            return list(range(a, b + 1)), None
    if MUMTY_RE.search(s):
        return None, "mumty"
    if TERRACE_RE.search(s) and not re.search(r"\bFLOOR\b", s, re.I):
        return None, "terrace"
    m = NUM_FLOOR_RE.search(s)
    if m:
        return [int(m.group(1))], None
    if re.search(r"\bFLOOR\b", s, re.I) or re.search(r"\bPLAN\b", s, re.I):
        m = WORD_RE.search(s)
        if m:
            return [ORD_WORDS[m.group(1).upper()]], None
    if GF_RE.fullmatch(s.strip()):
        return [0], None
    for k, v in (("FF", 1), ("SF", 2), ("TF", 3)):
        if s.strip().upper() == k:
            return [v], None
    return None, None


def floor_name(n: int) -> str:
    if n == 0:
        return "Ground floor"
    if n < 0:
        return "Basement" if n == -1 else f"Basement {-n}"
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf} floor"


def group_name(floors: list[int] | None, special: str | None = None) -> str:
    if special:
        return special.title()
    if not floors:
        return "Floor not identified"
    if len(floors) == 1:
        return floor_name(floors[0])
    return f"Typical floors {floors[0]}–{floors[-1]} (x{len(floors)})"


def parse_level(text: str) -> tuple[str, int | None, str | None, float] | None:
    """'First Floor Lvl.+3450' -> ('First Floor', 1, None, 3450.0); 'Terrace |Lvl.+15600' -> ('Terrace', None, 'terrace', 15600)."""
    s = " ".join(str(text or "").replace("|", " ").replace("\n", " ").split())
    m = LEVEL_RE.search(s)
    if not m:
        return None
    sign, val = m.group(1), float(m.group(2))
    if sign == "-":
        val = -val
    name = s[:m.start()].strip(" :-.")
    if not name:
        return None
    if re.search(r"F\.?\s*G\.?\s*L|GROUND\s*LEVEL|N\.?G\.?L|PLINTH|SILL|LINTEL|SLAB\b|BEAM|WINDOW|DOOR|CHAJJA", name, re.I):
        return None
    floors, special = parse_floors(name if re.search(r"FLOOR|TERRACE|MUMTY|ROOF|STAIR|MACHINE", name, re.I)
                                   else "")
    if floors is None and special is None:
        return None
    if floors is not None and len(floors) != 1:
        return None
    return name, (floors[0] if floors else None), special, val


def level_table(texts) -> dict:
    """Collect floor levels from texts [(idx, ent, region)]: {"floors": {n: mm}, "special": {name: mm},
    "sources": {key: [entity idx]}, "conflicts": [...]}."""
    seen: dict = {}
    src: dict = {}
    for i, t, _ in texts:
        p = parse_level(t["s"])
        if not p:
            continue
        _, n, special, val = p
        key = n if n is not None else special
        seen.setdefault(key, []).append(val)
        src.setdefault(key, []).append(i)
    floors, special, conflicts = {}, {}, []
    for key, vals in seen.items():
        vals_r = sorted({round(v) for v in vals})
        best = max(vals_r, key=lambda v: sum(1 for x in vals if round(x) == v))
        if len(vals_r) > 1:
            conflicts.append((key, vals_r))
        (floors if isinstance(key, int) else special)[key] = float(best)
    return {"floors": floors, "special": special, "sources": src, "conflicts": conflicts}


def storey_heights(levels: dict) -> dict[int, tuple[float, str]]:
    """Floor-to-floor height per floor number from the level table: next level up minus this level."""
    fl = levels["floors"]
    out = {}
    for n, v in fl.items():
        nxt = fl.get(n + 1)
        what = f"{floor_name(n + 1)} level"
        if nxt is None:
            higher = [k for k in fl if k > n]
            if higher:
                continue  # a gap in the level list: do not guess
            nxt = levels["special"].get("terrace")
            what = "terrace level"
        if nxt is not None and nxt > v:
            out[n] = (nxt - v, f"{what} {nxt:+.0f} - {floor_name(n)} level {v:+.0f}")
    return out


# ---------------------------------------------------------------- per-plan storey setup (engine side)
def _parse_floor_value(v) -> tuple[list[int] | None, int | None]:
    """User answer to a 'confirm floors' flag: '6-9', '6 TO 9', '3,4,5' -> floors; '5' -> multiplier."""
    s = str(v or "").strip()
    if not s:
        return None, None
    # an explicit multiplier after the floors: '6-9 x3' / '6-9 ×3' (fewer identical floors than the range)
    mx = re.search(r"\s*[x×*]\s*(\d{1,2})\s*$", s, re.I)
    if mx and s[:mx.start()].strip():
        fl, m = _parse_floor_value(s[:mx.start()])
        return fl, int(mx.group(1)) if int(mx.group(1)) >= 1 else m
    fl, _ = parse_floors(s)
    if fl:
        return fl, len(fl)
    nums = [int(x) for x in re.findall(r"\d+", s)]
    if len(nums) > 1:
        return sorted(set(nums)), len(set(nums))
    if len(nums) == 1:
        return None, nums[0]
    return None, None


def plan_storeys(a, cfg: dict, saved: dict, fid, Flag) -> tuple[dict, list]:
    """Floors, multiplier, floor group and storey height for every included plan region.

    Returns ({"multi": bool, "plans": {rid: info}, "levels": table, "depth": (mm|None, flag)}, flags).
    Values come from the region (cluster/layouts or the user's region_overrides), else the plan title;
    storey heights from the level labels. Nothing is invented: unknown values are flagged."""
    from .plan import floors_of
    levels = level_table(a.texts)
    ftf = storey_heights(levels)
    plans: dict = {}
    for r in a.regions:
        if not a.is_plan(r["id"]):
            continue
        floors = r.get("floors") if isinstance(r.get("floors"), list) else None
        special = r.get("special")
        src = r.get("floors_source") or ""
        if not floors and not special:
            floors, special = parse_floors(r.get("name", ""))
            src = f"plan title '{r.get('name', '')}'" if floors or special else ""
        m = r.get("multiplier")
        mult = int(m) if isinstance(m, (int, float)) and m >= 1 else (len(floors) if floors else floors_of(r))
        plans[r["id"]] = {"rid": r["id"], "name": r.get("name", ""), "floors": floors or [], "special": special,
                          "multiplier": max(int(mult or 1), 1), "source": src, "ftf": None, "ftf_src": "",
                          "ftf_user": False, "flag": None}
    multi = sum(1 for p in plans.values() if p["floors"]) >= 2
    flags = []
    if not multi:
        return {"multi": False, "plans": plans, "levels": levels, "ftf": ftf, "depth": (None, None)}, flags

    def mk(f):
        st = saved.get(f.id) or {}   # storey flags are unique; no group lookup needed
        if st.get("status") in ("resolved", "accepted"):
            f.status, f.value = st["status"], st.get("value")
        flags.append(f)
        return f

    unit = 1 / (a.to_m * 1000)
    for p in plans.values():
        reg = a.reg_by_id[p["rid"]]
        if p["special"]:
            p["group"], p["order"] = group_name(None, p["special"]), 1000 + (1 if p["special"] == "mumty" else 0)
            continue
        # confirm the floors of typical / unidentified plans
        if p["multiplier"] > 1 or not p["floors"]:
            what = (f"floors {p['floors'][0]}–{p['floors'][-1]} (x{p['multiplier']})" if len(p["floors"]) > 1
                    else (f"{floor_name(p['floors'][0])} (x{p['multiplier']})" if p["floors"] else "an unknown floor"))
            f = mk(Flag(
                id=fid("storeys", f"floors:{p['name']}"), severity="warning" if p["floors"] else "error", category="storeys",
                title=f"Plan '{p['name']}' assumed to represent {what}: confirm",
                detail="Every quantity measured on this plan is multiplied by the number of floors it stands for.",
                location={"bbox": reg["bbox"]},
                evidence=[f"read from {p['source']}" if p["source"] else "no floor range in the plan title or sheet name"]
                + ([f"sheet (layout) '{reg['layout']}'"] if reg.get("layout") else []),
                suggested_fix="Accept, or enter the floors this plan covers (e.g. '6-9') or the number of floors.",
                input={"type": "text", "label": "Floors (e.g. 6-9) or count", "placeholder": "6-9"},
                refs=[f"region:{p['rid']}", "storeys"]))
            p["flag"] = f
            if f.status == "resolved":
                fl, m = _parse_floor_value(f.value)
                if fl:
                    p["floors"] = fl
                if m:
                    p["multiplier"] = m
        p["group"] = group_name(p["floors"], None) if p["floors"] else p["name"]
        if p["floors"] and p["multiplier"] != len(p["floors"]):   # an explicit multiplier ('6-9 x3')
            p["group"] = re.sub(r"\s*\(x\d+\)$", "", p["group"]) + f" (x{p['multiplier']})"
        if not p["floors"] and p["multiplier"] > 1:
            p["group"] = f"{p['name']} (x{p['multiplier']})"
        p["order"] = float(p["floors"][0]) if p["floors"] else 500.0
        # storey height: user / region value first, else the level labels
        if reg.get("floor_to_floor_mm"):
            src = reg.get("floor_to_floor_source") or "user config (region floor_to_floor_mm)"
            p["ftf"], p["ftf_src"] = float(reg["floor_to_floor_mm"]), (src if src.startswith("user") else "level labels: " + src)
        else:
            hs = [ftf[n] for n in p["floors"] if n in ftf]
            vals = {round(h[0]) for h in hs}
            if p["floors"] and len(hs) == len(p["floors"]) and len(vals) == 1:
                p["ftf"], p["ftf_src"] = hs[0][0], "level labels: " + hs[0][1]
            elif len(vals) > 1:
                p["ftf_src"] = "floors in this group have different storey heights: " + ", ".join(h[1] for h in hs)
    # missing storey heights
    for p in plans.values():
        if p["special"] or p["ftf"]:
            continue
        f = mk(Flag(
            id=fid("storeys", f"ftf:{p['name']}"), severity="missing", category="storeys",
            title=f"Floor-to-floor height not found for {p['group']}",
            detail=(p["ftf_src"] or "No level labels ('First Floor Lvl.+3450') for these floors and the one above.")
                   + " Wall heights on this plan fall back to the default and are marked assumed.",
            location={"bbox": a.reg_by_id[p["rid"]]["bbox"]},
            suggested_fix="Enter the floor-to-floor height (mm) read from a section.",
            input={"type": "number", "label": "Floor-to-floor (mm)", "unit": "mm", "placeholder": "3000"},
            refs=[f"region:{p['rid']}", "storeys"]))
        if f.status == "resolved" and f.value:
            try:
                p["ftf"], p["ftf_src"], p["ftf_user"] = float(f.value), f"user ({f.id})", True
            except (TypeError, ValueError):
                pass
    # floors with a level but no plan, or claimed by two plans
    claimed: dict = {}
    for p in plans.values():
        for n in p["floors"]:
            claimed.setdefault(n, []).append(p["name"])
    lv_floors = sorted(levels["floors"])
    missing = [n for n in lv_floors if n not in claimed]
    dup = {n: v for n, v in claimed.items() if len(v) > 1}
    if missing:
        mk(Flag(id=fid("storeys", "no-plan:" + ",".join(map(str, missing))), severity="error", category="storeys",
                title=f"No plan found for {', '.join(floor_name(n) for n in missing)}",
                detail="These floors have a level label but no plan region covers them, so they are not measured.",
                evidence=[f"levels: " + ", ".join(f"{floor_name(n)} {levels['floors'][n]:+.0f}" for n in lv_floors)],
                suggested_fix="Set the floors of the plan that represents them (region overrides), then re-run.",
                refs=["storeys"]))
    if dup:
        mk(Flag(id=fid("storeys", "dup:" + ",".join(map(str, sorted(dup)))), severity="error", category="storeys",
                title=f"Floor(s) claimed by more than one plan: {', '.join(floor_name(n) for n in sorted(dup))}",
                detail="; ".join(f"{floor_name(n)}: {', '.join(v)}" for n, v in sorted(dup.items()))
                       + ". They would be measured twice.",
                suggested_fix="Correct the floors of one of the plans (confirm-floors flag or region overrides).",
                refs=["storeys"]))
    # storey table (one confirmation for all storey heights)
    rows = []
    for p in sorted(plans.values(), key=lambda p: p.get("order", 0)):
        if p["special"]:
            continue
        rows.append(f"{p['group']}: {p['ftf']:.0f} mm ({p['ftf_src']})" if p["ftf"] else f"{p['group']}: not found")
    top = max(levels["special"].values(), default=None) if levels["special"] else None
    n_floors = len(claimed)
    sources = [i for v in levels["sources"].values() for i in v]
    f = mk(Flag(
        id=fid("storeys", "table"), severity="warning", category="storeys",
        title=f"Storeys read from the drawing: {n_floors} floor(s)"
              + (f", top level {top:+.0f}" if top is not None else "") + ": confirm storey heights",
        detail="Floor-to-floor height per floor group; masonry and plaster heights are floor-to-floor less the "
               "slab/beam depth.",
        location={"bbox": _ents_bbox(a, sources, 1000 * unit)} if sources else None,
        evidence=rows + [f"levels: " + ", ".join(f"{floor_name(n)} {v:+.0f}" for n, v in sorted(levels["floors"].items()))
                         + "".join(f", {k} {v:+.0f}" for k, v in levels["special"].items())]
                 + [f"conflicting level labels: {c}" for c in levels["conflicts"]],
        suggested_fix="Accept if right; change a group's height with region_overrides {'floor_to_floor_mm': ...}.",
        refs=["storeys"]))
    # slab / beam depth to take off floor-to-floor
    depth, depth_flag = None, None
    for k in ("storey_deduction_mm", "slab_beam_depth_mm"):
        if cfg.get(k):
            depth = float(cfg[k])
    if depth is None:
        notes = [(i, t) for i, t, _ in a.texts if re.search(r"AS\s*/?\s*(PER\s*)?STRUC|AS PER STRUCTURAL", t["s"], re.I)]
        depth_flag = mk(Flag(
            id=fid("storeys", "slab-beam-depth"), severity="missing", category="storeys",
            title="Slab/beam depth not given: wall heights taken as full floor-to-floor",
            detail="Masonry should stop at the underside of the slab/beam, i.e. floor-to-floor less the slab/beam "
                   "depth. The drawing does not give it" + (" ('as per structural')." if notes else ".")
                   + " Until entered, wall heights are the full floor-to-floor and those rows are unresolved.",
            location={"bbox": _ents_bbox(a, [i for i, _ in notes[:20]], 500 * unit)} if notes else None,
            evidence=[f"note: {' '.join(t['s'].split())!r}" for _, t in notes[:2]],
            suggested_fix="Enter the depth to deduct (slab thickness, or beam depth where walls sit under beams).",
            input={"type": "number", "label": "Slab/beam depth (mm)", "unit": "mm", "placeholder": "150"},
            refs=["storey-depth"]))
        if depth_flag.status == "resolved":
            try:
                depth = float(depth_flag.value)
            except (TypeError, ValueError):
                depth = None
    return {"multi": True, "plans": plans, "levels": levels, "ftf": ftf, "depth": (depth, depth_flag),
            "table_flag": f}, flags


def _ents_bbox(a, idxs, pad=0.0):
    pts = []
    for i in idxs:
        e = a.geometry["entities"][i]
        if "p" in e and e["t"] == "T":
            pts.append(e["p"])
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
