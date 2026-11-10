"""Group drawing geometry into view regions (plans, sections, elevations, schedules).

Approach: rasterise every top-level entity bbox (grown by half the gap) onto a
coarse occupancy grid whose cell size equals the gap, then take 8-connected
components. Very large entities (sheet frames, stray far-away objects) are
excluded so they do not glue unrelated views together.
"""
from __future__ import annotations

import math
import re
from collections import deque

from engine.geom import entity_bbox, region_of_point  # noqa: F401

TITLE_RE = re.compile(r"\b(PLAN|SECTION|ELEVATION|SCHEDULE|DETAIL|LAYOUT|ROOF)\b", re.I)
KINDS = ["plan", "section", "elevation", "schedule", "detail", "other"]
MAX_GRID = 1200


def _top_level_bboxes(entities: list[dict]) -> dict[int, list[float]]:
    """bbox per top-level entity; INSERT/DIMENSION bboxes include their exploded children."""
    boxes: dict[int, list[float]] = {}
    for i, e in enumerate(entities):
        bb = entity_bbox(e)
        if bb is None:
            continue
        key = e["pi"] if e.get("v") else i
        if key is None:
            key = i
        cur = boxes.get(key)
        if cur is None:
            boxes[key] = list(bb)
        else:
            cur[0] = min(cur[0], bb[0]); cur[1] = min(cur[1], bb[1])
            cur[2] = max(cur[2], bb[2]); cur[3] = max(cur[3], bb[3])
    return boxes


def _robust_extent(boxes: list[list[float]]) -> tuple[float, float, float, float]:
    xs = sorted((b[0] + b[2]) / 2 for b in boxes)
    ys = sorted((b[1] + b[3]) / 2 for b in boxes)
    n = len(xs)
    lo, hi = int(n * 0.02), max(int(n * 0.98) - 1, 0)
    return xs[lo], ys[lo], xs[hi], ys[hi]


def default_gap(entities: list[dict]) -> float:
    boxes = list(_top_level_bboxes(entities).values())
    if not boxes:
        return 1.0
    x0, y0, x1, y1 = _robust_extent(boxes)
    diag = math.hypot(x1 - x0, y1 - y0) or 1.0
    return round(max(diag * 0.004, 1.0), -1 if diag > 1000 else 0)


def _components(boxes: dict[int, list[float]], gap: float, min_entities: int,
                limit_extent: bool = True) -> list[tuple[list[float], set[int]]]:
    """Connected components of entity bboxes (grown by gap/2) on a coarse occupancy grid."""
    if not boxes:
        return []
    x0, y0, x1, y1 = _robust_extent(list(boxes.values()))
    rw, rh = max(x1 - x0, gap), max(y1 - y0, gap)
    # Ignore things that are huge relative to the bulk of the drawing (frames)
    # or that lie far outside it (strays); they stay visible but unassigned.
    max_w, max_h = (rw * 0.5, rh * 0.5) if limit_extent else (math.inf, math.inf)
    margin = max(rw, rh)
    gx0, gy0 = x0 - margin, y0 - margin
    cell = max(gap, (rw + 2 * margin) / MAX_GRID, (rh + 2 * margin) / MAX_GRID)
    nx = int((rw + 2 * margin) / cell) + 2
    ny = int((rh + 2 * margin) / cell) + 2

    grid: dict[tuple[int, int], list[int]] = {}
    half = gap / 2
    for k, b in boxes.items():
        if (b[2] - b[0]) > max_w or (b[3] - b[1]) > max_h:
            continue
        if b[2] < gx0 or b[0] > gx0 + nx * cell or b[3] < gy0 or b[1] > gy0 + ny * cell:
            continue
        i0 = max(int((b[0] - half - gx0) / cell), 0)
        i1 = min(int((b[2] + half - gx0) / cell), nx - 1)
        j0 = max(int((b[1] - half - gy0) / cell), 0)
        j1 = min(int((b[3] + half - gy0) / cell), ny - 1)
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                grid.setdefault((i, j), []).append(k)

    comp_of_cell: dict[tuple[int, int], int] = {}
    ncomp = 0
    for start in grid:
        if start in comp_of_cell:
            continue
        comp_of_cell[start] = ncomp
        q = deque([start])
        while q:
            ci, cj = q.popleft()
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (ci + di, cj + dj)
                    if nb in grid and nb not in comp_of_cell:
                        comp_of_cell[nb] = ncomp
                        q.append(nb)
        ncomp += 1

    members: dict[int, set[int]] = {}
    for c, ks in grid.items():
        comp = comp_of_cell[c]
        members.setdefault(comp, set()).update(ks)

    raw = []
    for comp, ks in members.items():
        if len(ks) < min_entities:
            continue
        raw.append((_union(boxes, ks), ks))
    return raw


def _union(boxes, ks) -> list[float]:
    bb = [math.inf, math.inf, -math.inf, -math.inf]
    for k in ks:
        b = boxes[k]
        bb = [min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])]
    return bb


def cluster(entities: list[dict], gap: float | None = None, min_entities: int = 8,
            layouts: list[dict] | None = None) -> dict:
    """Return {"gap": g, "regions": [...], "assign": {top_level_index: region_id}}.

    Each region: {"id", "bbox": [minx, miny, maxx, maxy], "count", "name", "kind",
                  "include": bool, "title_guess"}.
    With `layouts` (geometry["layouts"]), a raster region that holds the model windows of two or
    more paper-space viewports (e.g. stacked floor plans drawn close together) is split along
    those windows, and every region shown by a viewport gets its sheet name, floors and multiplier
    (see engine/storeys.py and split_by_layouts).
    """
    boxes = _top_level_bboxes(entities)
    if not boxes:
        return {"gap": gap or 1.0, "regions": [], "assign": {}}
    if not gap or gap <= 0:
        gap = default_gap(entities)
    raw = [(bb, ks, None) for bb, ks in _components(boxes, gap, min_entities)]
    if layouts:
        raw = split_by_layouts(entities, boxes, raw, layouts, gap, min_entities)

    # Order regions top-to-bottom, left-to-right like reading a sheet.
    raw.sort(key=lambda r: (-round(r[0][3] / (gap * 10)), r[0][0]))
    regions, assign = [], {}
    for rid, (bb, ks, view) in enumerate(raw):
        title, kind = _guess_title(entities, ks, bb)
        reg = {
            "id": rid,
            "bbox": [round(v, 1) for v in bb],
            "count": len(ks),
            "name": title or f"Region {rid + 1}",
            "title_guess": title,
            "kind": kind,
            "include": kind in ("plan", "schedule"),
            "level": "",
        }
        if view:
            _annotate(reg, view, title)
        elif layouts and KEY_PLAN_RE.search(reg["name"]):
            reg["kind"], reg["include"] = "other", False
        regions.append(reg)
        for k in ks:
            assign[k] = rid
    if layouts and any(r.get("floors") for r in regions):
        _storey_heights(entities, regions)
    return {"gap": gap, "regions": regions, "assign": assign}


def _storey_heights(entities, regions) -> None:
    """floor_to_floor_mm per plan region from the level labels (sections / elevations)."""
    from engine.storeys import level_table, storey_heights
    texts = [(i, e, None) for i, e in enumerate(entities) if e["t"] == "T"]
    lv = level_table(texts)
    ftf = storey_heights(lv)
    for r in regions:
        fl = r.get("floors") or []
        if r.get("kind") != "plan" or not fl:
            continue
        hs = {ftf[n][0] for n in fl if n in ftf}
        if len(hs) == 1 and all(n in ftf for n in fl):
            r["floor_to_floor_mm"] = hs.pop()
            r["floor_to_floor_source"] = "; ".join(dict.fromkeys(ftf[n][1] for n in fl[:1]))
        if fl[0] in lv["floors"]:
            r["level"] = f"{lv['floors'][fl[0]]:+.0f}"


# ---------------------------------------------------------------- paper-space layouts
LAYOUT_KIND = [(re.compile(r"\bELE(V|VATION)?\b|\bELE\.", re.I), "elevation"),
               (re.compile(r"\bSEC(TION)?\b", re.I), "section"),
               (re.compile(r"SCHEDULE", re.I), "schedule"),
               (re.compile(r"DETAIL|\bDW\b|DOOR|WINDOW|TOILET|STAIR|KITCHEN|RAILING", re.I), "detail")]


KEY_PLAN_RE = re.compile(r"\b(KEY|LOCATION|INDEX)\s*PLAN\b", re.I)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _windows(layouts: list[dict]) -> list[dict]:
    """Distinct viewport windows. A window shown on 3+ sheets is a key plan / legend, not a view."""
    out: list[dict] = []
    for lay in layouts:
        for vp in lay.get("viewports", []):
            w = vp["window"]
            same = next((o for o in out if _iou(o["window"], w) > 0.9), None)
            if same:
                if lay["name"] not in same["layouts"]:
                    same["layouts"].append(lay["name"])
                continue
            out.append({"window": list(w), "layouts": [lay["name"]], "layout": lay, "vp": vp})
    return [o for o in out if len(o["layouts"]) < 3]


def _paper_title(lay: dict, vp: dict) -> str:
    """The sheet's biggest title text (drawing title, not the project/sheet-number strip)."""
    best, bh = "", 0.0
    for t in lay.get("titles", []):
        s = t["s"]
        if re.search(r"/|\bAR-\d|^TYPE\b|PROJECT|CLIENT|SCALE|DATE|DRAWN", s, re.I) and not TITLE_RE.search(s):
            continue
        h = float(t.get("h") or 0) + (1000 if TITLE_RE.search(s) else 0)
        if h > bh:
            best, bh = s, h
    return best


def split_by_layouts(entities, boxes, raw, layouts, gap, min_entities):
    """Split raster regions that hold several viewport windows; tag single-window regions."""
    wins = _windows(layouts)
    if not wins:
        return raw
    for w in wins:
        x0, y0, x1, y1 = w["window"]
        ks = set()
        for k, b in boxes.items():
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1 and (b[2] - b[0]) <= (x1 - x0) and (b[3] - b[1]) <= (y1 - y0):
                ks.add(k)
        w["keys"] = ks
    wins = [w for w in wins if len(w["keys"]) >= min_entities]
    out = []
    for bb, ks, _ in raw:
        inside = [w for w in wins if len(w["keys"] & ks) >= 0.6 * len(w["keys"])]
        # distinct views only (two viewports onto the same view do not split it)
        distinct: list[dict] = []
        for w in sorted(inside, key=lambda w: -len(w["keys"] & ks)):
            if all(len(w["keys"] & d["keys"]) < 0.5 * min(len(w["keys"]), len(d["keys"])) for d in distinct):
                distinct.append(w)
        if len(distinct) < 2:
            out.append((bb, ks, distinct[0] if distinct and len(distinct[0]["keys"] & ks) >= 0.5 * len(ks) else None))
            continue
        # smallest window first claims the entities whose centre lies in it
        taken: set[int] = set()
        for w in sorted(distinct, key=lambda w: len(w["keys"])):
            mine = (w["keys"] & ks) - taken
            comps = _components({k: boxes[k] for k in mine}, gap, 1, limit_extent=False)
            if not comps:
                continue
            big = max(len(c[1]) for c in comps)
            keep = set()
            for cbb, cks in comps:
                if len(cks) >= max(min_entities, 0.05 * big):
                    keep |= cks
            if len(keep) < min_entities:
                continue
            taken |= keep
            out.append((_union(boxes, keep), keep, w))
        rest = {k: boxes[k] for k in ks - taken}
        for cbb, cks in _components(rest, gap, min_entities, limit_extent=False):
            out.append((cbb, cks, None))
    return out


def _annotate(reg: dict, view: dict, model_title: str) -> None:
    from engine.storeys import group_name, parse_floors
    lay = view["layout"]
    paper = _paper_title(lay, view["vp"])
    reg["layout"] = lay["name"]
    reg["sheet_title"] = paper
    if not model_title:
        reg["name"] = paper or lay["name"]
        reg["title_guess"] = reg["name"]
    sheet_kind = next((kind for rx, kind in LAYOUT_KIND if rx.search(lay["name"]) or rx.search(paper or "")), None)
    if sheet_kind and reg["kind"] in ("other", "plan"):
        # a plan drawn on a detail/section/elevation sheet (staircase plan, toilet layout) is a detail
        reg["kind"] = sheet_kind if reg["kind"] == "other" or sheet_kind == "detail" else reg["kind"]
    elif reg["kind"] == "other" and re.search(r"PLAN|FLOOR|\bGF\b|\bFF\b|\bSF\b|TERRACE|\d+\s*TO\s*\d+",
                                              f"{lay['name']} {paper}", re.I):
        reg["kind"] = "plan"
    reg["include"] = reg["kind"] in ("plan", "schedule")
    if reg["kind"] != "plan":
        return
    # floors: model title first, then the sheet title, then the sheet (layout) name
    src = None
    for text, where in ((model_title, "plan title"), (paper, "sheet title"), (lay["name"], "sheet name")):
        floors, special = parse_floors(text)
        if floors or special:
            src = (floors, special, f"{where} '{text}'")
            break
    if src:
        floors, special, why = src
        reg["floors"] = floors or []
        reg["special"] = special
        reg["multiplier"] = len(floors) if floors else 1
        reg["floors_source"] = why
        reg["group"] = group_name(floors, special)


ROOM_DIM_RE = re.compile(r"^\s*\d+(?:\.\d+)?\s*[xX×]\s*\d+(?:\.\d+)?\s*$", re.M)
OPENING_SIZE_RE = re.compile(r"^\s*[A-Z]{1,3}\d{0,3}'?\s*-?\s*\(\s*\d+\s*[xX×]\s*\d+\s*\)", re.I)


def _guess_title(entities, keys, bb) -> tuple[str, str]:
    best, best_h = "", 0.0
    room_like = schedule_like = 0
    for k in keys:
        e = entities[k]
        if e["t"] != "T":
            continue
        s = e["s"].strip()
        if ROOM_DIM_RE.search(s):
            room_like += 1
        if OPENING_SIZE_RE.search(s):
            schedule_like += 1
        if TITLE_RE.search(s) and len(s) < 80:
            h = e.get("h", 0) or 0
            if h > best_h:
                best, best_h = " ".join(s.split()), h
    kind = "other"
    if best:
        m = TITLE_RE.search(best)
        word = m.group(1).upper() if m else ""
        kind = {"PLAN": "plan", "LAYOUT": "plan", "ROOF": "plan", "SECTION": "section",
                "ELEVATION": "elevation", "SCHEDULE": "schedule", "DETAIL": "detail"}.get(word, "other")
    if schedule_like >= 3 and kind in ("other", "detail"):
        kind = "schedule"
        best = best or "Door / window schedule"
    if room_like >= 2 and kind in ("other", "detail"):
        kind = "plan"
    return best, kind
