"""Scale calibration for NOT-TO-SCALE sheets.

On NOT-TO-SCALE sheets only written dimensions count. Where a quantity still needs a length
measured off the plan (beam spans, slab panels, raft outline) we need points-per-mm for
that plan. Evidence, all from written values:

  * dimension text next to a parallel dimension line ("1800" over a 37.3 pt line);
  * elements whose size is written in a schedule (pile Ø from the pile detail, column
    sizes from the column schedule) compared with their drawn size;
  * the user's own two points + known length (config "calibration").

The consensus is the median of all candidate ratios; the spread is the inter-quartile
range relative to the median, reported per sheet so the MB can say how far to trust it.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field

from .plans import Blob, inside
from .sheet import Seg, Sheet, Txt


@dataclass
class Calib:
    sheet: int
    pt_per_mm: float | None
    n: int = 0
    spread: float = 0.0                       # IQR / median
    sources: dict = field(default_factory=dict)  # source -> count
    samples: list[dict] = field(default_factory=list)
    method: str = ""                          # "dimensions" | "element sizes" | "user" | "transferred"
    outliers: list[dict] = field(default_factory=list)  # written dims whose scaled length disagrees (see consensus)

    def mm(self, pt: float) -> float:
        return pt / self.pt_per_mm

    def to_dict(self) -> dict:
        return {"sheet": self.sheet, "pt_per_mm": self.pt_per_mm, "scale_1_to": self.scale_1_to,
                "n": self.n, "spread_pct": round(self.spread * 100, 2), "sources": self.sources, "method": self.method,
                "samples": self.samples[:60], "outliers": self.outliers[:40]}

    @property
    def scale_1_to(self) -> float | None:
        # 1 pt = 25.4/72 mm of paper, so paper mm per real mm = pt_per_mm * 25.4/72
        return round(1 / (self.pt_per_mm * 25.4 / 72), 1) if self.pt_per_mm else None


def dim_samples(sh: Sheet, region) -> list[dict]:
    """Written dimension values matched to the dimension line they sit on."""
    out = []
    lines = [s for s in sh.segs if (s.horiz or s.vert) and s.length > 2]
    for t in sh.texts:
        m = re.fullmatch(r"\(?(\d{2,6})\)?", t.s.strip())
        if not m or not inside(t.cx, t.cy, region):
            continue
        val = float(m.group(1))
        if not 50 <= val <= 100000:
            continue
        rot = (round(t.rot) % 180)
        horiz_text = rot in (0, 180) or abs(rot) < 5
        vert_text = abs(rot - 90) < 5
        if not (horiz_text or vert_text):
            continue
        th = (t.bb[3] - t.bb[1]) if horiz_text else (t.bb[2] - t.bb[0])
        tw = (t.bb[2] - t.bb[0]) if horiz_text else (t.bb[3] - t.bb[1])
        best, bd = None, None
        for s in lines:
            if horiz_text and not s.horiz or vert_text and not s.vert:
                continue
            if s.length < tw * 0.9:
                continue
            if horiz_text:
                a, b = sorted((s.x1, s.x2))
                if not a <= t.cx <= b:
                    continue
                d = abs((s.y1 + s.y2) / 2 - t.cy)
            else:
                a, b = sorted((s.y1, s.y2))
                if not a <= t.cy <= b:
                    continue
                d = abs((s.x1 + s.x2) / 2 - t.cx)
            if d > 1.6 * th:
                continue
            if bd is None or d < bd:
                best, bd = s, d
        if best is None:
            continue
        L = _between_extension_lines(best, lines)
        out.append({"source": "dimension", "value_mm": val, "len_pt": round(L, 3), "orient": "h" if best.horiz else "v",
                    "ratio": L / val, "text": t.s, "handles": [t.handle, best.handle],
                    "at": [round(t.cx, 1), round(t.cy, 1)]})
    return out


def _between_extension_lines(d: Seg, lines: list[Seg]) -> float:
    """Distance between the extension lines that cross a dimension line. CAD dimension
    lines often run past the extension lines (DIMDLE), so the raw line length is too long."""
    if d.horiz:
        y = (d.y1 + d.y2) / 2
        a, b = sorted((d.x1, d.x2))
        xs = [s.x1 for s in lines if s.vert and s is not d and a - 0.2 <= s.x1 <= b + 0.2
              and min(s.y1, s.y2) - 0.8 <= y <= max(s.y1, s.y2) + 0.8 and s.length < 40 * max(1.0, d.length / 40)]
    else:
        x = (d.x1 + d.x2) / 2
        a, b = sorted((d.y1, d.y2))
        xs = [s.y1 for s in lines if s.horiz and s is not d and a - 0.2 <= s.y1 <= b + 0.2
              and min(s.x1, s.x2) - 0.8 <= x <= max(s.x1, s.x2) + 0.8 and s.length < 40 * max(1.0, d.length / 40)]
    if len(xs) >= 2 and max(xs) - min(xs) >= 0.6 * d.length:
        return max(xs) - min(xs)
    return d.length


def element_samples(blobs: list[Blob], sizes: dict[str, tuple[float, float]]) -> list[dict]:
    """Tagged column blobs against their schedule size (short side and long side)."""
    out = []
    for b in blobs:
        if not b.tag or b.tag not in sizes:
            continue
        s0, s1 = sorted(sizes[b.tag])
        d0, d1 = sorted((b.w, b.h))
        for d, s in ((d0, s0), (d1, s1)):
            out.append({"source": f"column {b.tag}", "value_mm": s, "len_pt": round(d, 3), "ratio": d / s,
                        "handles": b.handles[:2], "at": [round(b.cx, 1), round(b.cy, 1)]})
    return out


def vote_column_scale(blobs: list[Blob], sizes: list[tuple[float, float]], tol: float = 0.04) -> list[dict]:
    """Untagged plans: the ratio that makes most column blobs match a scheduled size."""
    cands = []
    for b in blobs:
        d1 = max(b.w, b.h)
        for s in sizes:
            cands.append(d1 / max(s))
    best, score = None, -1
    for r in cands:
        n = 0
        for b in blobs:
            d0, d1 = sorted((b.w / r, b.h / r))
            if any(abs(d0 - min(s)) <= tol * min(s) and abs(d1 - max(s)) <= tol * max(s) for s in sizes):
                n += 1
        if n > score:
            best, score = r, n
    if best is None:
        return []
    out = []
    for b in blobs:
        d0, d1 = sorted((b.w, b.h))
        for s in sizes:
            if abs(d0 / best - min(s)) <= tol * min(s) and abs(d1 / best - max(s)) <= tol * max(s):
                out.append({"source": "column size (schedule)", "value_mm": max(s), "len_pt": round(d1, 3),
                            "ratio": d1 / max(s), "handles": b.handles[:2], "at": [round(b.cx, 1), round(b.cy, 1)]})
                out.append({"source": "column size (schedule)", "value_mm": min(s), "len_pt": round(d0, 3),
                            "ratio": d0 / min(s), "handles": b.handles[:2], "at": [round(b.cx, 1), round(b.cy, 1)]})
                break
    return out


def consensus(sheet: int, samples: list[dict], method: str) -> Calib:
    if not samples:
        return Calib(sheet, None, method=method)
    rs = sorted(x["ratio"] for x in samples)
    med = statistics.median(rs)
    # drop gross mismatches (a dimension matched to the wrong line) before the final figure
    keep = [x for x in samples if abs(x["ratio"] / med - 1) <= 0.15]
    rs = sorted(x["ratio"] for x in keep) or rs
    med = statistics.median(rs)
    if len(rs) >= 4:
        q = statistics.quantiles(rs, n=4)
        spread = (q[2] - q[0]) / med
    else:
        spread = (max(rs) - min(rs)) / med if len(rs) > 1 else 0.0
    src: dict = {}
    for x in keep:
        k = x["source"].split(" ")[0] if x["source"].startswith("column ") else x["source"]
        src[k] = src.get(k, 0) + 1
    out = Calib(sheet, med, len(keep), spread, src, keep, method)
    out.rejected = len(samples) - len(keep)
    out.outliers = dim_outliers(samples, med)
    return out


OUTLIER_REL = 0.03       # written dimension vs scaled length: more than 3 % apart ...
OUTLIER_PT = 1.0         # ... and more than 1 pt on paper (beyond plotting precision)


def dim_outliers(samples: list[dict], pt_per_mm: float | None) -> list[dict]:
    """Written dimension values whose drawn (scaled) length disagrees with the consensus scale: the
    PDF counterpart of an overridden DIMENSION text. The written value governs; the flag says where."""
    if not pt_per_mm:
        return []
    out = []
    for x in samples:
        if x.get("source") != "dimension":
            continue
        implied = x["value_mm"] * pt_per_mm
        dev = x["len_pt"] / implied - 1 if implied else 0.0
        if abs(dev) > OUTLIER_REL and abs(x["len_pt"] - implied) > OUTLIER_PT:
            out.append({"text": x.get("text"), "value_mm": x["value_mm"], "scaled_mm": round(x["len_pt"] / pt_per_mm),
                        "dev_pct": round(dev * 100, 1), "handles": x.get("handles", []), "at": x.get("at")})
    out.sort(key=lambda o: -abs(o["dev_pct"]))
    return out


def user_calib(sheet: int, spec: dict) -> Calib | None:
    """cfg["calibration"][str(sheet)] = {"p1": [x, y], "p2": [x, y], "mm": L} (geometry
    coordinates) or {"pt_per_mm": r} or {"scale": 100} (1:100 on the sheet)."""
    if not spec:
        return None
    if spec.get("pt_per_mm"):
        r = float(spec["pt_per_mm"])
    elif spec.get("scale"):
        r = 72 / 25.4 / float(spec["scale"])
    elif spec.get("p1") and spec.get("p2") and spec.get("mm"):
        r = math.dist(spec["p1"], spec["p2"]) / float(spec["mm"])
    else:
        return None
    return Calib(sheet, r, 1, 0.0, {"user": 1}, [{"source": "user", "ratio": r, **spec}], "user")
