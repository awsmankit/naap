"""Stable MB row ids.

A row id is derived from what the row *is* (trade, item, location, sub-line text, sign, floor group
and the drawing evidence it was measured from), not from its position in the list, so re-running the
engine after a config change keeps the ids of rows that still exist. The review UI keeps its selection
across a recalculation by id.

Rows that would get the same id (e.g. two identical deduction lines with the same evidence) are told
apart by their order of occurrence among those identical rows only.
"""
from __future__ import annotations

import hashlib
import json


def _ref_key(refs) -> list:
    out = []
    for r in refs or []:
        if not r:
            continue
        hs = sorted(str(h) for h in (r.get("handles") or []) if h)
        if hs:
            out.append(["h", r.get("kind") or "", hs])
        else:
            bb = r.get("bbox")
            # evidence without handles (raster faces, gaps): its bbox, coarsely rounded so a tiny
            # numerical change does not change the id
            out.append(["b", r.get("kind") or "", [round(float(v) / 50) for v in bb] if bb else None, r.get("label") or ""])
    out.sort(key=lambda x: json.dumps(x, sort_keys=True))
    return out


def row_key(row) -> str:
    nos = row.nos if row.nos is not None else 1
    sign = "-" if (nos or 0) < 0 else "+"
    payload = [row.trade, row.item, row.location, getattr(row, "desc", "") or "", sign, row.floor_group or "",
               row.plan or "", row.unit, _ref_key(row.refs)]
    return json.dumps(payload, sort_keys=True, default=str)


def assign_stable_ids(rows, prefix: str = "r") -> dict[str, str]:
    """Give every row a deterministic id. Returns {old id: new id}."""
    seen: dict[str, int] = {}
    remap = {}
    for row in rows:
        h = hashlib.sha1(row_key(row).encode()).hexdigest()[:10]
        k = seen.get(h, 0)
        seen[h] = k + 1
        new = f"{prefix}{h}" + (f"-{k + 1}" if k else "")
        remap[row.id] = new
        row.id = new
    return remap
