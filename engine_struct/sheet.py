"""Per-sheet index over the PDF geometry (app/pdf_extract.py): texts, straight segments,
circles and filled shapes, all in the shared y-up drawing space (PDF points)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


@dataclass
class Txt:
    s: str
    bb: tuple[float, float, float, float]
    size: float
    rot: float
    layer: str
    handle: str
    parts: list | None = None     # [[text, x0, y0, x1, y1], ...] original spans when several were merged

    @property
    def cx(self) -> float:
        return (self.bb[0] + self.bb[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bb[1] + self.bb[3]) / 2


@dataclass
class Seg:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str
    handle: str
    dash: bool = False

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def horiz(self) -> bool:
        return abs(self.y2 - self.y1) <= 0.02 * max(self.length, 1e-9)

    @property
    def vert(self) -> bool:
        return abs(self.x2 - self.x1) <= 0.02 * max(self.length, 1e-9)


@dataclass
class Circ:
    cx: float
    cy: float
    r: float
    layer: str
    handle: str
    filled: bool = False


@dataclass
class Fill:
    bb: tuple[float, float, float, float]
    layer: str
    handle: str
    rect: bool                    # axis-aligned 4-corner outline
    pts: list = field(default_factory=list)


def bbox_of(items) -> list[float]:
    xs, ys = [], []
    for it in items:
        b = it.bb if hasattr(it, "bb") else it
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    return [min(xs), min(ys), max(xs), max(ys)] if xs else [0, 0, 0, 0]


def ref(kind: str, label: str, items, sheet: int | None = None) -> dict:
    """A drawing reference {kind, label, bbox, handles} for MB rows and flags."""
    items = list(items)
    bbs = []
    for it in items:
        if isinstance(it, Seg):
            bbs.append((min(it.x1, it.x2), min(it.y1, it.y2), max(it.x1, it.x2), max(it.y1, it.y2)))
        elif isinstance(it, Circ):
            bbs.append((it.cx - it.r, it.cy - it.r, it.cx + it.r, it.cy + it.r))
        elif hasattr(it, "bb"):
            bbs.append(it.bb)
        elif isinstance(it, (list, tuple)) and len(it) == 4:
            bbs.append(tuple(it))
    hs = []
    for it in items:
        h = getattr(it, "handle", None)
        if h and h not in hs:
            hs.append(h)
    out = {"kind": kind, "label": label, "bbox": [round(v, 2) for v in bbox_of(bbs)], "handles": hs[:400]}
    if sheet is not None:
        out["sheet"] = sheet
    return out


class Sheet:
    def __init__(self, geo: dict, info: dict):
        self.n = info["sheet"]
        self.info = info
        self.bbox = info["bbox"]
        self.title = info.get("title_block") or {}
        lay = [x["name"] for x in geo["layers"]]
        self.texts: list[Txt] = []
        self.segs: list[Seg] = []
        self.circles: list[Circ] = []
        self.fills: list[Fill] = []
        for e in geo["entities"]:
            if e.get("sheet") != self.n:
                continue
            ln = lay[e["l"]]
            t = e["t"]
            if t == "T":
                self.texts.append(Txt(e["s"], tuple(e["bb"]), e.get("f", e.get("h", 0)), e.get("r", 0), ln, e["handle"], e.get("parts")))
            elif t == "L":
                p = e["p"]
                self.segs.append(Seg(p[0], p[1], p[2], p[3], ln, e["handle"], bool(e.get("dash"))))
            elif t == "P":
                p = e["p"]
                for i in range(0, len(p) - 2, 2):
                    self.segs.append(Seg(p[i], p[i + 1], p[i + 2], p[i + 3], ln, e["handle"], bool(e.get("dash"))))
            elif t == "C":
                self.circles.append(Circ(e["c"][0], e["c"][1], e["r"], ln, e["handle"]))
            elif t == "H":
                if "c" in e:
                    self.circles.append(Circ(e["c"][0], e["c"][1], e["r"], ln, e["handle"], filled=True))
                    continue
                for loop in e["p"]:
                    xs, ys = loop[0::2], loop[1::2]
                    bb = (min(xs), min(ys), max(xs), max(ys))
                    pts = list(zip(xs, ys))
                    uniq = {(round(x, 1), round(y, 1)) for x, y in pts}
                    rect = len(uniq) <= 5 and all(
                        min(abs(x - bb[0]), abs(x - bb[2])) < 0.3 and min(abs(y - bb[1]), abs(y - bb[3])) < 0.3
                        for x, y in pts)
                    self.fills.append(Fill(bb, ln, e["handle"], rect, pts))

    # ---- text queries
    def find(self, rx: str, flags=re.I) -> list[Txt]:
        r = re.compile(rx, flags)
        return [t for t in self.texts if r.search(t.s)]

    def full(self, rx: str, flags=re.I) -> list[Txt]:
        r = re.compile(rx, flags)
        return [t for t in self.texts if r.fullmatch(t.s.strip())]

    def texts_in(self, bb) -> list[Txt]:
        x0, y0, x1, y1 = bb
        return [t for t in self.texts if x0 <= t.cx <= x1 and y0 <= t.cy <= y1]

    def segs_in(self, bb, layer: str | None = None) -> list[Seg]:
        x0, y0, x1, y1 = bb
        return [s for s in self.segs if (layer is None or s.layer == layer)
                and min(s.x1, s.x2) >= x0 - 0.5 and max(s.x1, s.x2) <= x1 + 0.5
                and min(s.y1, s.y2) >= y0 - 0.5 and max(s.y1, s.y2) <= y1 + 0.5]

    def lines_near(self, bb, pad: float = 0) -> list[Seg]:
        x0, y0, x1, y1 = bb[0] - pad, bb[1] - pad, bb[2] + pad, bb[3] + pad
        return [s for s in self.segs if max(s.x1, s.x2) >= x0 and min(s.x1, s.x2) <= x1
                and max(s.y1, s.y2) >= y0 and min(s.y1, s.y2) <= y1]

    def local(self, x: float, y: float) -> tuple[float, float]:
        """Shared drawing space -> displayed page coordinates (pt, y down), for messages."""
        return x - self.bbox[0], self.bbox[3] - y


def sheets(geo: dict) -> list[Sheet]:
    return [Sheet(geo, s) for s in geo.get("sheets", []) if not s.get("scanned")]


def words(sh: "Sheet") -> list[Txt]:
    """Texts with merged spans split back into their original pieces (needed for table cells)."""
    out = []
    for t in sh.texts:
        if t.parts:
            for s, x0, y0, x1, y1 in t.parts:
                out.append(Txt(s, (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), t.size, t.rot, t.layer, t.handle))
        else:
            out.append(t)
    return out
