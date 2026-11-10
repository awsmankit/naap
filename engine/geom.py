"""Geometry helpers shared by the app and the measurement engine.

Entity records are the compact dicts produced by app/extract.py (see the
docstring there for the full shape).
"""
from __future__ import annotations

import math

UNIT_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}


def entity_points(e: dict) -> list[tuple[float, float]]:
    """All representative points of an entity record (used for bboxes)."""
    t = e["t"]
    if t in ("L", "P", "A", "C"):
        p = e.get("p") or []
        if t in ("A", "C") and not p:
            cx, cy = e["c"]
            r = e["r"]
            return [(cx - r, cy - r), (cx + r, cy + r)]
        return list(zip(p[0::2], p[1::2]))
    if t == "H":
        return [pt for loop in e["p"] for pt in zip(loop[0::2], loop[1::2])]
    if t in ("T", "I"):
        x, y = e["p"]
        if t == "T":
            h = e.get("h", 0) or 0
            n = max((len(s) for s in e["s"].split("\n")), default=1)
            lines = e["s"].count("\n") + 1
            return [(x, y - h * lines * 1.4), (x + h * 0.7 * n, y + h)]
        return [(x, y)]
    if t == "D":
        return [tuple(e[k]) for k in ("p1", "p2", "tp") if k in e]
    return []


def entity_bbox(e: dict) -> tuple[float, float, float, float] | None:
    pts = entity_points(e)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def region_of_point(regions: list[dict], x: float, y: float) -> int | None:
    """Id of the smallest region whose bbox contains (x, y), or None."""
    best, area = None, math.inf
    for r in regions:
        b = r["bbox"]
        if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
            a = (b[2] - b[0]) * (b[3] - b[1])
            if a < area:
                best, area = r["id"], a
    return best


def polyline_length(flat: list[float], closed: bool = False) -> float:
    pts = list(zip(flat[0::2], flat[1::2]))
    if closed and len(pts) > 2:
        pts.append(pts[0])
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def entity_length(e: dict) -> float:
    if e["t"] == "L":
        x1, y1, x2, y2 = e["p"]
        return math.hypot(x2 - x1, y2 - y1)
    if e["t"] in ("P", "A", "C"):
        return polyline_length(e.get("p") or [], bool(e.get("c")))
    return 0.0
