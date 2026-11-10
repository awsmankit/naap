"""Index the geometry once so the MB builder and the checks can share it.

Everything here is generic: it relies on the layer roles and view regions the
user confirmed, plus common drafting conventions (room labels "NAME / WxL",
door/window tags like "D1", schedule entries "D1 (900X2100)" or table rows).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .geom import UNIT_TO_M, region_of_point

# ----------------------------------------------------------------- text patterns
DIM_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*$", re.I)
TAG = re.compile(r"^\s*([DWV][A-Z]{0,2}\d{0,3}'?)\s*$")
TAG_STOP = {"WC", "DN", "DW", "VD", "DWG", "WT"}
# any short code (e.g. "SD", "RS1") counts as a tag when it sits on a door/window-tag layer
LAYER_TAG = re.compile(r"^\s*([A-Z]{1,3}\d{0,3}'?)\s*$")
LAYER_TAG_STOP = TAG_STOP | {"UP", "LVL", "FFL", "SFL", "NO", "TO", "OF", "THE", "AND", "MM", "X", "Y", "N", "S", "E"}
SINGLE_LINE_ROOM = re.compile(r"^\s*(.*?[A-Za-z].*?)[\s:-]+(\d{3,5}(?:\.\d+)?)\s*[xX×*]\s*(\d{3,5}(?:\.\d+)?)\s*(?:mm)?\s*$", re.I)
SCHEDULE_INLINE = re.compile(r"^\s*([A-Z]{1,3}\d{0,3}'?)\s*-?\s*\(\s*(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*\)")
SIZE_CELL = re.compile(r"^\s*(\d{3,5})\s*[xX×]\s*(\d{3,5})\s*$")
SECTION_LAYER = re.compile(r"SEC", re.I)
WALL_ROLES = ("wall-brick", "wall-AAC", "parapet")
OPENING_ROLES = ("door", "window")


def parse_room_label(text: str) -> tuple[str, float, float] | None:
    """"BEDROOM-1\\n3600X3000" -> ("BEDROOM-1", 3600.0, 3000.0); name must precede the WxL line."""
    # a size broken over two lines ("1500X" / "2400") is one size
    text = re.sub(r"(\d)\s*([xX×*])\s*\n\s*(\d)", r"\1\2\3", text.replace("\r", ""))
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    for i, ln in enumerate(lines):
        m = DIM_LINE.match(ln)
        if not m or i == 0:
            continue
        name = _join_name(lines[:i])
        if not name or TAG.match(name):
            return None
        return name, float(m.group(1)), float(m.group(2))
    # "LIVING ROOM 4200 X 3600" on one line
    for i, ln in enumerate(lines):
        m = SINGLE_LINE_ROOM.match(ln)
        if m:
            name = _join_name(lines[:i] + [m.group(1)])
            if name and not TAG.match(name) and not re.search(r"\b(MM|THK|HIGH|HT|WIDE|LVL|KHURRA|TANK|SIZE)\b", name, re.I):
                return name, float(m.group(2)), float(m.group(3))
    return None


def _join_name(parts: list[str]) -> str:
    name = ""
    for part in parts:
        part = part.strip()
        name = name[:-1] + part if name.endswith("-") else f"{name} {part}".strip()
    name = re.sub(r"\s+", " ", name).strip()
    return re.sub(r"-\s+(?=\d)", "-", name)  # "STORE- 2" -> "STORE-2"


def parse_opening_tag(text: str) -> str | None:
    m = TAG.match(text.strip())
    if not m:
        return None
    tag = m.group(1).upper()
    return None if tag.rstrip("'") in TAG_STOP else tag


def parse_schedule_entry(text: str) -> tuple[str, float, float] | None:
    m = SCHEDULE_INLINE.match(text.strip())
    if not m:
        return None
    return m.group(1).upper(), float(m.group(2)), float(m.group(3))


def opening_kind(tag: str) -> str:
    t = tag.upper()
    if t.startswith("V"):
        return "ventilator"
    if t.startswith("W"):
        return "window"
    if t.startswith("D") or t.endswith("D"):
        return "door"
    return "other"


# ----------------------------------------------------------------- records
@dataclass
class RoomLabel:
    name: str
    w: float               # as labelled (mm)
    l: float
    x: float               # approx centre of the label
    y: float
    region: int | None
    text: str
    ent: int | None = None     # index of the label text entity (or the size text if stacked)


@dataclass
class ScheduleEntry:
    tag: str
    w: float
    h: float
    source: str            # "heading" (inline "D1 (WxH)") or "table"
    x: float
    y: float
    text_h: float
    region: int | None
    ent: int | None = None


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: int
    ent: int

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass
class DoorSymbol:
    ent: int
    x: float
    y: float
    width: float           # from swing-arc radius
    layer: int


@dataclass
class Analysis:
    geometry: dict
    config: dict
    to_m: float = 0.001
    layer_names: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    regions: list[dict] = field(default_factory=list)
    reg_by_id: dict = field(default_factory=dict)
    included: set = field(default_factory=set)
    texts: list[tuple[int, dict, int | None]] = field(default_factory=list)   # (idx, ent, region)
    rooms: list[RoomLabel] = field(default_factory=list)
    plan_tags: list[tuple[str, float, float, int | None]] = field(default_factory=list)
    plan_tag_ents: list[int] = field(default_factory=list)      # entity index per plan_tags item
    skip_layers: set = field(default_factory=set)              # overlaid layers found by the checks
    schedule: list[ScheduleEntry] = field(default_factory=list)
    wall_segments: list[Segment] = field(default_factory=list)
    bound_segments: list[Segment] = field(default_factory=list)  # walls + columns (room boundaries)
    doors: list[DoorSymbol] = field(default_factory=list)
    legend: dict[str, str] = field(default_factory=dict)   # hatch pattern -> material word from the legend
    dims: list[tuple[int, dict, int | None]] = field(default_factory=list)
    units_note: str = ""
    dim_mismatches: list = field(default_factory=list)       # written dimension text != drawn length (engine/dims.py)
    dedup: dict = field(default_factory=dict)                # same-place duplicates removed (engine/dedup.py)

    _rcache: dict = field(default_factory=dict)
    _by_region: dict | None = None

    # -- helpers --------------------------------------------------------
    def region_of(self, x: float, y: float) -> int | None:
        key = (x, y)
        r = self._rcache.get(key, -1)
        if r == -1:
            r = self._rcache[key] = region_of_point(self.regions, x, y)
        return r

    def region_ents(self, rid: int | None) -> list[int]:
        """Indices of entities whose first point lies in region rid (ascending)."""
        if self._by_region is None:
            by: dict = defaultdict(list)
            for i, e in enumerate(self.geometry["entities"]):
                p = e.get("p")
                if not p:
                    continue
                if isinstance(p[0], list):
                    p = p[0]
                    if not p:
                        continue
                by[self.region_of(p[0], p[1])].append(i)
            self._by_region = by
        return self._by_region.get(rid, [])

    def region_name(self, rid: int | None) -> str:
        return self.reg_by_id[rid]["name"] if rid is not None and rid in self.reg_by_id else "Unassigned"

    def kind(self, rid: int | None) -> str | None:
        return self.reg_by_id[rid].get("kind", "other") if rid is not None and rid in self.reg_by_id else None

    def is_plan(self, rid: int | None) -> bool:
        return rid in self.included and self.kind(rid) == "plan"

    def mm(self, v: float) -> float:
        return v * self.to_m * 1000


def _looks_like_mm(geometry: dict) -> bool:
    """Header units often lie: if dimension values / room-label sizes are typical millimetre
    building sizes (hundreds to tens of thousands), the drawing is in mm."""
    vals = sorted(e["m"] for e in geometry["entities"] if e["t"] == "D" and not e.get("v") and e.get("m"))
    labs = []
    for e in geometry["entities"]:
        if e["t"] == "T" and len(labs) < 500:
            p = parse_room_label(e["s"])
            if p:
                labs += [p[1], p[2]]
    if len(vals) < 20 and len(labs) < 4:
        return False
    med = vals[len(vals) // 2] if vals else 0
    lab_med = sorted(labs)[len(labs) // 2] if labs else 0
    return (not vals or 100 <= med <= 30000) and (not labs or 800 <= lab_med <= 20000)


def analyse(geometry: dict, config: dict) -> Analysis:
    a = Analysis(geometry, config)
    a.to_m = UNIT_TO_M.get(geometry.get("units", "mm"), 0.001)
    a.units_note = ""
    if a.to_m != 0.001 and _looks_like_mm(geometry):
        a.units_note = (f"$INSUNITS says '{geometry.get('units')}' but dimension values and room sizes read as "
                        "millimetres; mm assumed")
        a.to_m = 0.001
    a.layer_names = [l["name"] for l in geometry["layers"]]
    role_map = config.get("layer_roles", {})
    a.roles = [role_map.get(n, "ignore") for n in a.layer_names]
    a.regions = config.get("regions", [])
    a.reg_by_id = {r["id"]: r for r in a.regions}
    a.included = {r["id"] for r in a.regions if r.get("include")}
    ents = geometry["entities"]

    has_room_layers = "room text" in a.roles
    has_tag_layers = any(r in OPENING_ROLES for r in a.roles)
    a.legend = find_legend(a, ents)
    children: dict[int, list[dict]] = defaultdict(list)

    for i, e in enumerate(ents):
        t = e["t"]
        role = a.roles[e["l"]]
        if e.get("v") and e.get("pi") is not None:
            children[e["pi"]].append(e)
        if t == "T":
            rid = a.region_of(*e["p"])
            a.texts.append((i, e, rid))
        elif t == "D" and not e.get("v"):
            x, y = e.get("tp") or e.get("p1") or (0, 0)
            a.dims.append((i, e, a.region_of(x, y)))
        elif t in ("L", "P") and role in WALL_ROLES + ("column", "window"):
            p = e["p"]
            rid = a.region_of(p[0], p[1])
            if not a.is_plan(rid):
                continue
            pts = list(zip(p[0::2], p[1::2]))
            if t == "P" and e.get("c") and len(pts) > 2 and pts[0] != pts[-1]:
                pts.append(pts[0])
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                s = Segment(x1, y1, x2, y2, e["l"], i)
                a.bound_segments.append(s)  # window/sill lines close rooms across window openings
                if role in WALL_ROLES:
                    a.wall_segments.append(s)
        elif t == "H" and (e.get("pn") in a.legend or role in WALL_ROLES):
            # wall hatches bound rooms too (walls are often hatch-only at openings/junctions)
            for loop in e["p"]:
                if not a.is_plan(a.region_of(loop[0], loop[1])):
                    continue
                pts = list(zip(loop[0::2], loop[1::2]))
                for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                    if (x1, y1) != (x2, y2):
                        a.bound_segments.append(Segment(x1, y1, x2, y2, e["l"], i))

    # door symbols: top-level inserts on door layers; width = overall extent of the symbol
    for i, e in enumerate(ents):
        if e["t"] != "I" or e.get("v") or a.roles[e["l"]] != "door":
            continue
        kids = children.get(i, [])
        if not kids or any(k["t"] == "T" for k in kids):  # tag bubbles carry text
            continue
        if not a.is_plan(a.region_of(*e["p"])):
            continue
        pts = [p for k in kids if k["t"] in ("L", "P", "A") for p in zip(k["p"][0::2], k["p"][1::2])]
        if not pts:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        width = max(max(xs) - min(xs), max(ys) - min(ys))
        if 500 <= a.mm(width) <= 4000:
            a.doors.append(DoorSymbol(i, (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, width, e["l"]))

    _rooms(a, has_room_layers)
    _tags_and_schedule(a, has_tag_layers)
    from .dims import apply_written_dims
    apply_written_dims(a, {i: [k for k in children.get(i, []) if k["t"] == "T"] for i, _, _ in a.dims})
    return a


MATERIAL_WORDS = re.compile(r"\b(BRICK|AAC|FLY\s*ASH|STONE|CONCRETE BLOCK|BLOCK|RCC|CONCRETE)\b", re.I)


def find_legend(a: Analysis, ents: list[dict]) -> dict[str, str]:
    """Hatch pattern -> material, from small legend swatches with a material word to their right."""
    unit = 1 / (a.to_m * 1000)
    words = []
    for e in ents:
        if e["t"] == "T":
            m = MATERIAL_WORDS.search(e["s"])
            if m and not a.is_plan(a.region_of(*e["p"])):
                words.append((e, m.group(1).upper()))
    found: dict[str, set[str]] = defaultdict(set)
    for e in ents:
        if e["t"] != "H" or not e.get("pn") or e["pn"] == "SOLID":
            continue
        pts = [p for lp in e["p"] for p in zip(lp[0::2], lp[1::2])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bb = (min(xs), min(ys), max(xs), max(ys))
        if max(bb[2] - bb[0], bb[3] - bb[1]) > 5000 * unit:
            continue
        for t, word in words:
            tx, ty = t["p"]
            h = t.get("h", 0) or 0
            if bb[1] - h <= ty <= bb[3] + h and bb[2] <= tx <= bb[2] + 8000 * unit:
                found[e["pn"]].add(word)
    return {pn: next(iter(w)) for pn, w in found.items() if len(w) == 1}


def _text_centre(e: dict) -> tuple[float, float]:
    x, y = e["p"]
    ap = e.get("ap")
    if ap:  # MTEXT: the attachment point is the anchor; move to the middle of the text block
        h = e.get("h", 0) or 0
        lines = [ln for ln in e["s"].split("\n") if ln.strip()] or [""]
        wid, hei = 0.6 * h * max(len(ln) for ln in lines), 1.5 * h * len(lines)
        col, row = (ap - 1) % 3, (ap - 1) // 3
        return x + (0.5 - 0.5 * col) * wid, y - (0.5 - 0.5 * row) * hei
    h = e.get("h", 0) or 0
    return x + 0.45 * h * len(e["s"].split("\n")[0]), y + h / 2


def _rooms(a: Analysis, has_room_layers: bool) -> None:
    used: set[int] = set()
    cand = [(i, e, rid) for i, e, rid in a.texts
            if (not has_room_layers or a.roles[e["l"]] == "room text") and a.is_plan(rid)]
    for i, e, rid in cand:
        parsed = parse_room_label(e["s"])
        if parsed:
            x, y = _text_centre(e)
            a.rooms.append(RoomLabel(parsed[0], parsed[1], parsed[2], x, y, rid, e["s"], i))
            used.add(i)
    # separate "NAME" + "WxL" texts stacked on top of each other
    for i, e, rid in cand:
        if i in used:
            continue
        m = DIM_LINE.match(e["s"].strip())
        if not m:
            continue
        h = e.get("h", 0) or 1
        sx, sy = e["p"]
        best, bd = None, math.inf
        for j, f, rj in cand:
            if j == i or j in used or DIM_LINE.match(f["s"].strip()) or parse_opening_tag(f["s"]):
                continue
            fx, fy = f["p"]
            dy, dx = fy - sy, abs(fx - sx)
            if 0.3 * h < dy < 4 * h and dx < 8 * h and math.hypot(dx, dy) < bd:
                best, bd = (j, f), math.hypot(dx, dy)
        if best:
            j, f = best
            name = _join_name(f["s"].split("\n"))
            if name and not (TAG.match(name) and parse_opening_tag(name)):  # "WC" is a room, "D1" a tag
                cx, cy = _text_centre(e)
                a.rooms.append(RoomLabel(name, float(m.group(1)), float(m.group(2)), cx, cy, rid,
                                         f"{f['s']}\n{e['s']}", j))
                used.update((i, j))


def _tags_and_schedule(a: Analysis, has_tag_layers: bool) -> None:
    size_cells = []
    for i, e, rid in a.texts:
        s = e["s"]
        inline = parse_schedule_entry(s)
        if inline:
            a.schedule.append(ScheduleEntry(inline[0], inline[1], inline[2], "heading", e["p"][0], e["p"][1],
                                            e.get("h", 0) or 0, rid, i))
            continue
        m = SIZE_CELL.match(s)
        if m:
            size_cells.append((e, float(m.group(1)), float(m.group(2))))
    for i, e, rid in a.texts:
        tag = parse_opening_tag(e["s"])
        if not tag and has_tag_layers and a.roles[e["l"]] in OPENING_ROLES and a.is_plan(rid):
            m = LAYER_TAG.match(e["s"])
            if m and m.group(1).rstrip("'") not in LAYER_TAG_STOP:
                tag = m.group(1)
        if not tag:
            continue
        if a.kind(rid) in ("schedule", "detail") or rid not in a.included:
            # table row: a tag cell with a "W X H" cell to its right on the same row
            h = e.get("h", 0) or 1
            row = [(c, w, hh) for c, w, hh in size_cells
                   if abs(c["p"][1] - e["p"][1]) < 0.6 * h and 0 < c["p"][0] - e["p"][0] < 25 * h]
            if row:
                c, w, hh = min(row, key=lambda r: r[0]["p"][0] - e["p"][0])
                a.schedule.append(ScheduleEntry(tag, w, hh, "table", e["p"][0], e["p"][1], h, rid, i))
            continue
        if a.is_plan(rid) and (not has_tag_layers or a.roles[e["l"]] in OPENING_ROLES):
            a.plan_tags.append((tag, e["p"][0], e["p"][1], rid))
            a.plan_tag_ents.append(i)


def schedule_sizes(a: Analysis) -> dict[str, dict[tuple[float, float], list[ScheduleEntry]]]:
    out: dict[str, dict[tuple[float, float], list[ScheduleEntry]]] = defaultdict(lambda: defaultdict(list))
    for s in a.schedule:
        out[s.tag][(s.w, s.h)].append(s)
    return out


def preferred_size(entries: dict[tuple[float, float], list[ScheduleEntry]]) -> tuple[float, float]:
    """Prefer the schedule table over detail headings; then the most frequent size."""
    def score(item):
        size, es = item
        return (any(e.source == "table" for e in es), len(es))
    return max(entries.items(), key=score)[0]
