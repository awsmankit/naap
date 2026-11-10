"""Data shapes returned by the measurement engine (see engine/mb.py)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MBRow:
    """One measurement line. qty should equal nos*l*b*h over the fields that are set
    (the exporter then writes it as an Excel formula); otherwise the value is written."""
    item: str                      # item description; rows with the same item are grouped
    location: str = ""
    nos: float | None = 1
    l: float | None = None         # metres
    b: float | None = None         # metres
    h: float | None = None         # metres (H/D)
    qty: float = 0.0
    unit: str = ""
    remarks: str = ""
    code: str = ""                 # optional DSR/CPWD item code
    id: str = ""                   # stable within a run, e.g. "r12"
    trade: str = ""                # e.g. "B. MASONRY"
    status: str = "ok"             # "ok" | "assumed" | "unresolved"
    flag_ids: list[str] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)   # [{bbox, handles, kind, label}] drawing evidence
    floor_group: str = ""          # e.g. "Ground floor", "Typical floors 3–5 (x3)", "Terrace" (multi-storey MBs)
    floor_order: float = 0.0       # sort key of the floor group (lowest floor first)
    plan: str = ""                 # plan region the row was measured from
    desc: str = ""                 # sub-line text, e.g. "Gross wall", "Deduct W1 (1500x1200)", "Deduct RCC columns"


@dataclass
class AbstractRow:
    item: str
    qty: float
    unit: str
    code: str = ""
    remarks: str = ""
    trade: str = ""
    row_ids: list[str] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)


@dataclass
class Room:
    name: str
    location: str                  # region name
    width_mm: float
    length_mm: float
    area_m2: float
    x: float
    y: float
    source_text: str
    measured_l_mm: float | None = None   # drawn size (free box) where it could be measured
    measured_b_mm: float | None = None
    check: str = ""                # "OK" | "MISMATCH ..." | "open space" ...
    remarks: str = ""
    refs: list[dict] = field(default_factory=list)


@dataclass
class Opening:
    tag: str                       # e.g. "D1", "W2'"
    kind: str                      # "door" | "window" | "ventilator" | "other"
    width_mm: float | None
    height_mm: float | None
    count: int
    locations: dict[str, int] = field(default_factory=dict)   # region name -> count
    remarks: str = ""
    refs: list[dict] = field(default_factory=list)


@dataclass
class DimRow:
    location: str
    layer: str
    measurement: float | None
    text: str
    x: float
    y: float
    refs: list[dict] = field(default_factory=list)
    measured: float | None = None  # value from the dimension geometry (x DIMLFAC) when it differs from the text
    written: float | None = None   # value written in the dimension text (override / block text), if any
    status: str = "ok"             # "ok" | "assumed" (written value differs from geometry; written used)
    remarks: str = ""


@dataclass
class Flag:
    """Something missing or wrong in the drawing that affects the MB."""
    id: str                        # stable across runs (used to persist status in user_config["flags"])
    severity: str                  # "error" | "warning" | "missing"
    category: str                  # e.g. "room-size", "opening-size", "schedule", "untagged-opening",
                                   #      "overlap", "material", "missing-data", "ambiguous"
    title: str
    detail: str = ""
    location: dict | None = None   # {"x", "y"} or {"bbox": [minx, miny, maxx, maxy]} in drawing units
    evidence: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    status: str = "open"           # "open" | "resolved" | "accepted"
    input: dict | None = None      # what the user can enter: {"type": "number"|"text", "label", "unit", "placeholder"}
    value: Any = None              # user-entered value when resolved
    refs: list[str] = field(default_factory=list)  # what it affects: "room:<name>", "tag:<D1>", "region:<id>", "slab", ...
    instances: list[dict] = field(default_factory=list)  # grouped flag: every occurrence [{id, location, plan}]


@dataclass
class MBResult:
    mb: list[MBRow]
    abstract: list[AbstractRow]
    rooms: list[Room]
    openings: list[Opening]
    dimensions: list[DimRow]
    assumptions: list[str]
    meta: dict = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


