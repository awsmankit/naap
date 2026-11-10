"""Schedules drawn as CAD tables (ruling lines + text) -> structured rows.

grid_table() rebuilds one table from its ruling lines: each data row is found from its
label ("B1", "S2", "CF1", "C3"...) under a header anchor, the row band from the horizontal
rules around the label, and the cells from the vertical rules crossing the band. Header
text above each cell names the column. The per-schedule parsers then map columns by
header keywords, so nothing depends on one drawing's exact layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sheet import Sheet, Txt, ref

# ------------------------------------------------------------------ bar notation

BARSET = re.compile(r"(#)?\s*(\d{1,2})\s*(?:MM\s*)?(?:Ø|DIA\.?)?\s*-\s*(\d{1,3})\s*(NOS?\.?)?", re.I)
DIAS = {6, 8, 10, 12, 16, 20, 25, 28, 32, 36, 40}
SPACING = re.compile(r"#?\s*(\d{1,2})\s*(?:MM\s*)?#?\s*(?:Ø|DIA\.?)?\s*@\s*(\d{2,3})", re.I)
SIZE = re.compile(r"(\d{2,5})\s*[X×x*]\s*(\d{2,5})")


def bar_sets(s: str) -> list[dict]:
    """'#25-4 Nos. + #20-8 Nos.' -> [{"dia": 25, "n": 4}, {"dia": 20, "n": 8}]; '-' -> [].
    With '#' or 'NOS' the bar size comes first (#16-2NOS = 2 bars of 16). Bare 'a-b' (beam
    schedules: 4-12, 2-16) is read count-diameter when only b is a standard bar size; if both
    could be sizes the set is marked "ambiguous"."""
    out = []
    for hsh, a, b, nos in BARSET.findall(s or ""):
        a, b = int(a), int(b)
        if hsh or nos:
            d, n, amb = a, b, False
        elif b in DIAS and a not in DIAS:
            d, n, amb = b, a, False
        elif a in DIAS and b not in DIAS:
            d, n, amb = a, b, False
        else:
            d, n, amb = b, a, True
        if d in DIAS:
            out.append({"dia": d, "n": n} | ({"ambiguous": True} if amb else {}))
    return out


def spacing(s: str) -> dict | None:
    """'8@150C/C' / '8#@100C/C' / '#12@100C/C' -> {"dia": 8, "s": 150}."""
    m = SPACING.search(s or "")
    if m and 6 <= int(m.group(1)) <= 40:
        return {"dia": int(m.group(1)), "s": int(m.group(2))}
    return None


def size2(s: str) -> tuple[int, int] | None:
    m = SIZE.search(s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def num(s: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:MM\.?)?\s*", s or "", re.I)
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ generic grid

@dataclass
class Table:
    name: str
    sheet: int
    columns: list[str]                       # header text per cell column
    rows: list[dict] = field(default_factory=list)   # {"label", "cells": [str], "ref"}
    bbox: list[float] = field(default_factory=list)
    anchor: str = ""

    def col(self, *keys: str, last: bool = False) -> int | None:
        """Index of the first (or last) column whose header contains all keys (regex, case-insensitive)."""
        hits = [i for i, c in enumerate(self.columns) if all(re.search(k, c, re.I) for k in keys)]
        if not hits:
            return None
        return hits[-1] if last else hits[0]

    def to_dict(self) -> dict:
        return {"name": self.name, "sheet": self.sheet, "columns": self.columns, "bbox": self.bbox,
                "rows": [{"label": r["label"], "cells": r["cells"]} for r in self.rows]}


def _hrules(sh: Sheet, bb, min_len=8.0):
    return [s for s in sh.lines_near(bb, 2) if s.horiz and s.length >= min_len]


def _vrules(sh: Sheet, bb, min_len=4.0):
    return [s for s in sh.lines_near(bb, 2) if s.vert and s.length >= min_len]


def _merge(vals: list[float], tol: float) -> list[float]:
    out: list[float] = []
    for v in sorted(vals):
        if out and v - out[-1] <= tol:
            continue
        out.append(v)
    return out


def _cell_text(ts: list[Txt]) -> str:
    rows: list[list[Txt]] = []
    for t in sorted(ts, key=lambda t: -t.cy):
        if rows and abs(rows[-1][0].cy - t.cy) < max(2.0, 0.4 * (t.bb[3] - t.bb[1])):
            rows[-1].append(t)
        else:
            rows.append([t])
    return " ".join(" ".join(x.s.strip() for x in sorted(r, key=lambda t: t.cx)) for r in rows).strip()


def grid_table(sh: Sheet, name: str, anchor_rx: str, label_rx: str, max_down: float = 1400,
               max_side: float = 80) -> Table | None:
    """Rebuild the table whose label column sits under the anchor text (e.g. 'B\\.?\\s*NO')."""
    anchors = sh.find(anchor_rx)
    best: Table | None = None
    for a in anchors:
        labels = [t for t in sh.full(label_rx)
                  if t.cy < a.cy and a.cy - t.cy < max_down and abs(t.cx - a.cx) < max_side]
        if not labels:
            continue
        # keep the vertically aligned group nearest the anchor
        labels.sort(key=lambda t: -t.cy)
        col_x = labels[0].cx
        labels = [t for t in labels if abs(t.cx - col_x) < 6]
        tab = _rows_from_labels(sh, name, labels, a)
        if tab and (best is None or len(tab.rows) > len(best.rows)):
            best = tab
    return best


def _rows_from_labels(sh: Sheet, name: str, labels: list[Txt], anchor: Txt) -> Table | None:
    x_lo, x_hi = labels[0].bb[0] - 60, labels[0].bb[2] + 1200
    region = (x_lo, min(t.bb[1] for t in labels) - 300, x_hi, anchor.bb[3] + 60)
    H = _hrules(sh, region)
    V = _vrules(sh, region)
    rows = []
    table_right = None
    for lab in labels:
        above = [s for s in H if min(s.x1, s.x2) - 1 <= lab.cx <= max(s.x1, s.x2) + 1 and s.y1 > lab.cy]
        below = [s for s in H if min(s.x1, s.x2) - 1 <= lab.cx <= max(s.x1, s.x2) + 1 and s.y1 < lab.cy]
        if not above or not below:
            continue
        top = min(s.y1 for s in above)
        bot = max(s.y1 for s in below)
        if top - bot > 260:
            continue
        mid = (top + bot) / 2
        # table width: the chain of horizontal rule pieces on the top line
        if table_right is None:
            pieces = sorted([s for s in H if abs(s.y1 - top) < 0.6], key=lambda s: min(s.x1, s.x2))
            right = None
            for s in pieces:
                a0, a1 = min(s.x1, s.x2), max(s.x1, s.x2)
                if right is None:
                    if a0 <= lab.cx <= a1 + 1:
                        right, left = a1, a0
                elif a0 <= right + 1.5:
                    right = max(right, a1)
            table_right = right if right is not None else x_hi
            table_left = left if right is not None else x_lo
        # cell boundaries: vertical rules covering (nearly) the whole row band; shorter
        # verticals inside a cell belong to sketches drawn in it
        span = top - bot
        xs = _merge([s.x1 for s in V if min(s.y1, s.y2) <= bot + 0.1 * span and max(s.y1, s.y2) >= top - 0.1 * span
                     and table_left - 1 <= s.x1 <= table_right + 1], 1.0)
        if len(xs) < 3:
            continue
        cells, items = [], []
        for x0, x1 in zip(xs, xs[1:]):
            ts = [t for t in sh.texts if x0 <= t.cx <= x1 and bot <= t.cy <= top]
            cells.append(_cell_text(ts))
            items += ts
        rows.append({"label": lab.s.strip(), "cells": cells, "xs": xs, "top": top, "bot": bot,
                     "ref": ref("schedule-row", f"{name} {lab.s.strip()}", [(xs[0], bot, xs[-1], top)] + items, sh.n)})
    if not rows:
        return None
    xs = rows[0]["xs"]
    head_bot = rows[0]["top"]
    head_top = anchor.bb[3] + 2
    cols = []
    for x0, x1 in zip(xs, xs[1:]):
        ts = [t for t in sh.texts if x0 <= t.cx <= x1 and head_bot <= t.cy <= head_top]
        cols.append(_cell_text(ts))
    bb = [xs[0], rows[-1]["bot"], xs[-1], max(head_top, rows[0]["top"])]
    return Table(name, sh.n, cols, rows, bb, anchor.s)


# ------------------------------------------------------------------ specific schedules

def _codes_row(sh: Sheet, tab: Table) -> list[str]:
    """The short code line just above the first data row (B.NO. B D T1 ... S2), by cell."""
    r0 = tab.rows[0]
    xs = r0["xs"]
    band = [t for t in sh.texts if r0["top"] <= t.cy <= r0["top"] + 18]
    out = []
    for x0, x1 in zip(xs, xs[1:]):
        out.append(_cell_text([t for t in band if x0 <= t.cx <= x1]))
    return out


def parse_beam_schedule(sh: Sheet) -> dict:
    """Floor beam schedule (B.NO | B | D | T1..T5 | B1 B2 | S1 S2 | side face)."""
    tab = grid_table(sh, "Beam schedule", r"^B\.?\s*NO\.?$|^BEAM\s*NO", r"B\d{1,2}[A-Z]?'?")
    if not tab:
        return {}
    codes = [c.upper().replace(" ", "") for c in _codes_row(sh, tab)]
    out = {}
    for r in tab.rows:
        c = r["cells"]
        get = {}
        for i, code in enumerate(codes):
            if i < len(c):
                get[code] = c[i]
        sf = None
        for i, h in enumerate(tab.columns):
            if re.search(r"SIDE", h, re.I) and i < len(c):
                sf = c[i]
        rec = {"tag": r["label"], "b": num(get.get("B", "")), "d": num(get.get("D", "")),
               "raw": dict(zip(codes, c)), "ref": r["ref"], "sheet": sh.n}
        for k in ("T1", "T2", "T3", "T4", "T5", "B1", "B2"):
            rec[k] = bar_sets(get.get(k, ""))
        for k in ("S1", "S2"):
            rec[k] = spacing(get.get(k, ""))
        rec["side_face"] = bar_sets(sf or "") or spacing(sf or "")
        out[rec["tag"]] = rec
    out["_table"] = tab
    return out


def parse_slab_schedule(sh: Sheet) -> dict:
    tab = grid_table(sh, "Slab schedule", r"^SLAB$|SLAB\s*MARK|SLAB\s*TYPE", r"S\d{1,2}[A-Z]?")
    if not tab:
        return {}
    ti = tab.col("THICK")
    letters = {}
    for i, h in enumerate(tab.columns):
        m = re.search(r"\b([A-D])\s*$", h)
        if m:
            letters[m.group(1)] = i
    out = {}
    for r in tab.rows:
        c = r["cells"]
        rec = {"tag": r["label"], "thk": num(c[ti]) if ti is not None and ti < len(c) else None,
               "ref": r["ref"], "sheet": sh.n, "raw": c}
        for L, i in letters.items():
            rec[L] = spacing(c[i]) if i < len(c) else None
            rec[L + "_head"] = tab.columns[i]
        out[rec["tag"]] = rec
    out["_table"] = tab
    return out


def parse_footing_schedule(sh: Sheet) -> dict:
    tab = grid_table(sh, "Footing schedule", r"^FOOTING$|FOOTING\s*TYPE|FOOTING\s*MARK", r"[CI]?F\d{1,2}[A-Z]?")
    if not tab:
        return {}
    out = {}
    cs = {"size": tab.col("FOOTING", "SIZE"), "ped": tab.col("PA?E?DESTAL"), "col": tab.col("COLUMN"),
          "d1": tab.col(r"\bD1\b"), "d2": tab.col(r"\bD2\b"), "ast1": tab.col("AST\\s*1"), "ast2": tab.col("AST\\s*2")}
    for r in tab.rows:
        c = r["cells"]
        g = {k: (c[i] if i is not None and i < len(c) else "") for k, i in cs.items()}
        colsz = g["col"]
        m = re.match(r"\s*(\d+)\s*[X×]\s*\(", colsz or "")
        out[r["label"]] = {"tag": r["label"], "size": size2(g["size"]), "pedestal": size2(g["ped"]),
                           "columns": {"n": int(m.group(1)) if m else 1, "size": size2(colsz)},
                           "d1": num(g["d1"]), "d2": num(g["d2"]), "ast1": spacing(g["ast1"]),
                           "ast2": spacing(g["ast2"]), "raw": g, "ref": r["ref"], "sheet": sh.n}
    out["_table"] = tab
    return out


def parse_column_schedule(sh: Sheet) -> dict:
    tab = grid_table(sh, "Column schedule", r"^COLUMN$|COLUMN\s*TYPE|COLUMN\s*MARK", r"C\d{1,2}[A-Z]?")
    if not tab:
        return {}
    i_size = tab.col("COLUMN", "SIZE") if tab.col("COLUMN", "SIZE") is not None else tab.col("SIZE")
    i_mix = tab.col("CONC|MIX|GRADE")
    longs = [i for i, h in enumerate(tab.columns) if re.search(r"FOUNDATION|FLOOR|TERRACE|LONGITUDINAL", h, re.I)
             and not re.search(r"PLACEMENT", h, re.I)]
    i_end = tab.col("END")
    i_mid = tab.col("MID")
    out = {}
    for r in tab.rows:
        c = r["cells"]
        sz = c[i_size] if i_size is not None else ""
        circ = re.search(r"[Ø∅Φ]\s*(\d{2,4})|(\d{2,4})\s*[Ø∅Φ]|(\d{2,4})\s*DIA", sz, re.I)
        rec = {"tag": r["label"], "size": size2(sz), "dia": None, "mix": c[i_mix] if i_mix is not None else "",
               "long": [], "long_raw": [], "ties_end": spacing(c[i_end]) if i_end is not None else None,
               "ties_mid": spacing(c[i_mid]) if i_mid is not None else None, "ref": r["ref"], "sheet": sh.n,
               "raw": c}
        if circ and not rec["size"]:
            rec["dia"] = int(next(g for g in circ.groups() if g))
        for i in longs:
            rec["long_raw"].append({"range": tab.columns[i], "text": c[i]})
            if bar_sets(c[i]) and not rec["long"]:
                rec["long"] = bar_sets(c[i])
        out[r["label"]] = rec
    out["_table"] = tab
    return out


def parse_plinth_schedule(sh: Sheet) -> dict:
    tab = grid_table(sh, "Plinth beam schedule", r"^BEAM\s*NO\.?$|^B\.?\s*NO\.?$", r"B\d{1,2}[A-Z]?")
    if not tab:
        return {}
    i_size = tab.col("SIZE")
    out = {}
    codes = [c.upper().replace(" ", "") for c in _codes_row(sh, tab)]
    for r in tab.rows:
        c = r["cells"]
        g = dict(zip(codes, c))
        rec = {"tag": r["label"], "size": size2(c[i_size]) if i_size is not None else None, "raw": g,
               "ref": r["ref"], "sheet": sh.n}
        for k in ("BST1", "BST2", "TST1", "TST2"):
            rec[k] = bar_sets(g.get(k, ""))
        for k in ("S1", "S2"):
            rec[k] = spacing(g.get(k, ""))
        rec["b"], rec["d"] = (rec["size"] or (None, None))
        out[r["label"]] = rec
    out["_table"] = tab
    return out
