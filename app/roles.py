"""Layer role vocabulary and name-based auto-guessing."""
from __future__ import annotations

import re

ROLES = ["wall-brick", "wall-AAC", "parapet", "column", "door", "window", "room text", "dimension",
         "groove", "railing", "ignore"]

# Order matters: first match wins.
_RULES: list[tuple[str, str]] = [
    (r"GROOVE", "groove"),
    (r"RAILING|HAND ?RAIL|\bRAIL\b", "railing"),
    (r"HATCH|PATT|ELEV|SECTION|GRID TEXT|STOPPER|HANDLE|LOCK|FLUSHBOLT|SPRING|VISION", "ignore"),
    (r"DIM", "dimension"),
    (r"PARAPET|PRHT", "parapet"),
    (r"AAC|BLOCK ?WALL|WALL ?(100|150|200)\b|\b(100|150|200) ?WALL", "wall-AAC"),
    (r"WALL", "wall-brick"),
    (r"COLUMN|COLLOM|\bCOL\b|\bCOL\.", "column"),
    (r"\bDW\b|DOOR ?WINDOW|D/W", "door"),
    (r"DOOR", "door"),
    (r"WINDOW|\bWIN\b|GLAZ|VENT|\bC?SILL\b|\bCILL\b", "window"),
    (r"ROOM|TEXT ?DETAIL|AREA ?TAG|ROOM ?TAG", "room text"),
]


def guess_role(layer_name: str) -> str:
    name = layer_name.upper().replace("_", " ").replace("-", " ")
    name = name.split("$")[-1]  # xref-bound names like "XREF$0$A-WALL"
    for pattern, role in _RULES:
        if re.search(pattern, name):
            return role
    return "ignore"


def guess_roles(layers: list[dict]) -> dict[str, str]:
    return {l["name"]: guess_role(l["name"]) for l in layers}
