"""Typical details read from text and their written dimensions: the pile detail, the
footing section depth, the raft thickness/section chain, PCC thickness."""
from __future__ import annotations

import math
import re

from . import calibrate as C
from .sheet import Sheet, ref
from .tables import bar_sets, spacing


def _near(sh: Sheet, t, dx: float, dy_up: float, dy_down: float):
    return [u for u in sh.texts if abs(u.cx - t.cx) <= dx and t.cy - dy_down <= u.cy <= t.cy + dy_up]


def parse_pile_detail(sh: Sheet) -> dict:
    """'DETAIL OF 300 Ø SINGLE UNDER REAMED PILE (P1)' and the text/dims around it."""
    heads = sh.find(r"PILE\s*\(?P\d|UNDER\s*-?\s*REAMED\s*PILE|DETAIL\s+OF\s+\d+\s*[Ø∅Φ]?\s*(DIA)?.*PILE")
    if not heads:
        return {}
    # title may span two lines: use the upper line for diameter/type
    top = max(heads, key=lambda t: t.cy)
    title = " ".join(t.s for t in sorted(_near(sh, top, 200, 30, 40), key=lambda t: -t.cy)
                     if re.search(r"PILE|DETAIL|REAMED", t.s, re.I))
    out: dict = {"title": title, "sheet": sh.n, "refs": [ref("detail", "pile detail", [top], sh.n)]}
    m = re.search(r"(\d{3,4})\s*(?:MM)?\s*[Ø∅Φ]|[Ø∅Φ]\s*(\d{3,4})|(\d{3,4})\s*DIA", title, re.I)
    out["dia"] = int(next(g for g in m.groups() if g)) if m else None
    out["bulbs"] = 2 if re.search(r"DOUBLE", title, re.I) else 1 if re.search(r"SINGLE|UNDER\s*-?\s*REAMED", title, re.I) else None
    m = re.search(r"\((P\d+)\)", title)
    out["tag"] = m.group(1) if m else "P"
    region = (top.cx - 260, top.cy - 20, top.cx + 260, top.cy + 1100)
    ts = [t for t in sh.texts if region[0] <= t.cx <= region[2] and region[1] <= t.cy <= region[3]]
    for t in ts:
        s = t.s.upper()
        if re.search(r"RING|SPIRAL|TIES?", s) and spacing(s):
            out["rings"] = spacing(s)
            out["refs"].append(ref("detail", "pile rings", [t], sh.n))
        elif bar_sets(s) and "@" not in s:
            out["long"] = bar_sets(s)
            out["refs"].append(ref("detail", "pile bars", [t], sh.n))
        m = re.search(r"CUT\s*OFF.*?\(?(\d{3,5})\)?\s*(?:MM)?\s*BELOW", s)
        if m:
            out["cutoff_below_gl"] = int(m.group(1))
    # written dimensions inside the detail
    dims = C.dim_samples(sh, region)
    vert = [d for d in dims if d["value_mm"] >= 3000]
    if vert:
        d = max(vert, key=lambda d: d["value_mm"])
        out["length"] = int(d["value_mm"])
        out["refs"].append(ref("dimension", f"pile length {d['text']}", [tuple(d["at"]) * 2], sh.n) | {"handles": d["handles"]})
    small = sorted({int(d["value_mm"]) for d in dims if 200 <= d["value_mm"] <= 2500})
    if out.get("dia") and len(small) >= 1:
        bigger = [v for v in small if v > out["dia"] * 1.5 and v <= out["dia"] * 3.5]
        # horizontal dims below the toe: shaft (= dia) and bulb (largest within 1.5..3.5 x dia)
        hz = [d for d in dims if d.get("orient") == "h" and int(d["value_mm"]) in bigger]
        if hz:
            out["bulb_dia"] = int(max(d["value_mm"] for d in hz))
            local = [d for d in dims if int(d["value_mm"]) in (out["dia"], out["bulb_dia"])]
            out["_detail_scale"] = sum(d["ratio"] for d in local) / len(local)
    m = [d for d in dims if d.get("orient") == "v" and 600 <= d["value_mm"] < 3000]
    if m:
        out["bulb_spacing"] = int(m[0]["value_mm"])
    out["_region"] = region
    return out


def bulb_shape(sh: Sheet, pd: dict) -> dict | None:
    """Heights of the bulb cones measured on the detail, scaled with the detail's own
    written shaft/bulb widths. Returns {"h_top", "h_bot"} in mm (scaled -> assumed)."""
    r = pd.get("_detail_scale")
    if not r or not pd.get("bulb_dia") or not pd.get("dia"):
        return None
    x0, y0, x1, y1 = pd["_region"]
    diag = [s for s in sh.segs if x0 <= s.x1 <= x1 and y0 <= s.y1 <= y1 and not s.horiz and not s.vert and s.length > 3]
    if not diag:
        return None
    # half-widths in pt
    dr = (pd["bulb_dia"] - pd["dia"]) / 2 * r
    cand = [s for s in diag if abs(abs(s.x2 - s.x1) - dr) <= 0.2 * dr]
    if len(cand) < 2:
        return None
    ups, downs = [], []
    for s in cand:
        dy = abs(s.y2 - s.y1)
        # the outer end of the segment is the bulb's widest point; the cone above it rises
        # towards the shaft (shaft end higher) and the one below falls to the shaft
        (xa, ya), (xb, yb) = (s.x1, s.y1), (s.x2, s.y2)
        cx = (x0 + x1) / 2
        outer_y = ya if abs(xa - cx) > abs(xb - cx) else yb
        inner_y = yb if outer_y == ya else ya
        (ups if inner_y > outer_y else downs).append(dy / r)
    if not ups or not downs:
        return None
    ups.sort()
    downs.sort()
    return {"h_top": round(ups[len(ups) // 2]), "h_bot": round(downs[len(downs) // 2]), "n_segments": len(cand)}


def bulb_volume_m3(dia: float, bulb: float, h_top: float, h_bot: float) -> float:
    """Extra concrete of one bulb beyond the shaft: two frusta minus the shaft cylinder."""
    R, r = bulb / 2000, dia / 2000
    fr = lambda h: math.pi * h / 3 * (R * R + R * r + r * r)
    ht, hb = h_top / 1000, h_bot / 1000
    return fr(ht) + fr(hb) - math.pi * r * r * (ht + hb)


def vertical_chain(sh: Sheet, region) -> list[dict]:
    """Stacked vertical dimensions in a section (e.g. 2000 / 900 / 100). Dimensions that
    sit inside a longer one are dropped; the rest are returned top to bottom."""
    dims = [d for d in C.dim_samples(sh, region)]
    out = []
    for d in dims:
        h = [x for x in sh.segs if d["handles"][1] == x.handle]
        if not h:
            continue
        s = h[0]
        if not s.vert:
            continue
        lo, hi = sorted((s.y1, s.y2))
        out.append({**d, "lo": lo, "hi": hi})
    keep = []
    for d in out:
        if any(o is not d and o["lo"] - 1 <= d["lo"] and d["hi"] <= o["hi"] + 1 and o["value_mm"] > d["value_mm"] for o in out):
            continue
        keep.append(d)
    keep.sort(key=lambda d: -d["hi"])
    return keep


def find_value_near(sh: Sheet, rx: str, value_rx: str = r"(\d{2,4})", max_d: float = 60) -> tuple[int, object] | None:
    """A number written near a label (e.g. the 100 next to 'P.C.C.')."""
    for lab in sh.find(rx):
        best = None
        for t in sh.texts:
            m = re.fullmatch(value_rx, t.s.strip())
            if not m or t is lab:
                continue
            d = math.hypot(t.cx - lab.cx, t.cy - lab.cy)
            if d <= max_d and (best is None or d < best[0]):
                best = (d, int(m.group(1)), t)
        if best:
            return best[1], best[2]
    return None
