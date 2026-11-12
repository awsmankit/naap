"""Schedules of an RCC plan set that are drawn as text laid out on a grid (no usable ruling lines):
the beam schedule (type, b, D, bottom / top / stirrup bars, legs, remarks) and the slab schedule
(type, thickness, main and distribution steel, remark). Columns are found from the header words and
the x positions of the data cells, rows from the label column, so no cell address is hard-coded."""
from __future__ import annotations

import re

from .sheet import Sheet, Txt, ref, words as sheet_words

TOKEN = re.compile(r"Nos\.?|Dia|spacing|[bD]\b", re.I)


def _split_tokens(t: Txt) -> list[Txt]:
    """'NosDia Nos' -> three tokens with the box shared out by character count."""
    s = t.s.replace(" ", "")
    if not re.fullmatch(r"(?:Nos\.?|Dia|spacing)+", s, re.I) or t.s.strip().lower() in ("nos", "dia", "nos.", "spacing"):
        return [t]
    toks = re.findall(r"Nos\.?|Dia|spacing", s, re.I)
    total = sum(len(x) for x in toks)
    x0, x1 = t.bb[0], t.bb[2]
    out, pos = [], x0
    for k in toks:
        w = (x1 - x0) * len(k) / total
        out.append(Txt(k, (pos, t.bb[1], pos + w, t.bb[3]), t.size, t.rot, t.layer, t.handle))
        pos += w
    return out


def _lines(ts: list[Txt], tol: float = 2.6) -> list[list[Txt]]:
    rows: list[list[Txt]] = []
    for t in sorted(ts, key=lambda t: (-t.cy, t.cx)):
        if rows and abs(rows[-1][0].cy - t.cy) <= tol:
            rows[-1].append(t)
        else:
            rows.append([t])
    return [sorted(r, key=lambda t: t.cx) for r in rows]


def n_dia(n: str, d: str) -> dict | None:
    """('3', '25') -> {"n": 3, "dia": 25}; '-' or blank -> None."""
    m1, m2 = re.search(r"\d+", n or ""), re.search(r"\d+", d or "")
    if not m1 or not m2:
        return None
    return {"n": int(m1.group()), "dia": int(m2.group())}


def parse_beam_schedule_pages(sh: Sheet) -> dict:
    """One page of a beam schedule -> {"rows": [rec], "grade": ..., "steel": ..., "heading": ..., "columns": [...]} or {}.
    Needs a header band with 'Nos'/'Dia' sub-headings and a first column of beam types."""
    ws = [t for ts in [_split_tokens(t) for t in sheet_words(sh)] for t in ts]
    nos = [t for t in ws if re.fullmatch(r"Nos", t.s, re.I)]
    if len(nos) < 4:
        return {}
    ys = {}
    for t in nos:
        ys.setdefault(round(t.cy / 3), []).append(t)
    hy = max(ys.values(), key=len)
    sub_y = sum(t.cy for t in hy) / len(hy)
    if len(hy) < 3:
        return {}
    data = [t for t in ws if t.cy < sub_y - 4]
    header = [t for t in ws if t.cy >= sub_y - 4]
    # sub header row words (b, D, Nos, Dia, spacing) at sub_y
    subs = [t for t in header if abs(t.cy - sub_y) <= 3.0]
    # column centres: x clusters of the data words
    lines = _lines(data)
    xs = sorted(t.cx for t in data if re.fullmatch(r"[\d\-.+]+[A-Za-z]?", t.s))
    cols: list[list[float]] = []
    for x in xs:
        if cols and x - cols[-1][-1] <= 7:
            cols[-1].append(x)
        else:
            cols.append([x])
    centres = [sum(c) / len(c) for c in cols if len(c) >= max(3, len(lines) // 4)]
    if len(centres) < 15:
        return {}
    # header path per column: every header word whose box covers the column centre (a little slack)
    def path(cx: float) -> list[Txt]:
        hs = [t for t in header if t.bb[0] - 2.5 <= cx <= t.bb[2] + 2.5 and t.cy > sub_y - 4]
        return sorted(hs, key=lambda t: -t.cy)

    names: list[str] = []
    for cx in centres:
        p = path(cx)
        txt = " ".join(t.s for t in p).lower()
        sub = next((t.s.lower() for t in p if abs(t.cy - sub_y) <= 3), "")
        if re.search(r"\btype\b", txt) or (not p and cx == centres[0]):
            nm = "type"
        elif "remark" in txt:
            nm = "remark"
        elif sub in ("b",) :
            nm = "b"
        elif sub == "d":
            nm = "d"
        elif "legs" in txt:
            nm = "legs"
        else:
            kind = {"nos": "n", "nos.": "n", "dia": "dia", "spacing": "s"}.get(sub, "")
            if "support" in txt:
                grp = "top_left" if "left" in txt else "top_right" if "right" in txt else "top_support"
            elif "straight" in txt:
                grp = "bot_straight"
            elif "anchor" in txt:
                grp = "top_anchor"
            elif "extra" in txt and "cent" in txt:
                grp = "bot_extra"
            elif "left" in txt:
                grp = "st_left"
            elif "right" in txt:
                grp = "st_right"
            elif "center" in txt or "centre" in txt:
                grp = "st_center"
            elif "dia" in txt:
                grp, kind = "st", "dia"
            else:
                grp = f"col{len(names)}"
            nm = f"{grp}.{kind}" if kind else grp
        names.append(nm)
    if "type" not in names:
        names[0] = "type"
    if "st.dia" in names:
        # stirrup columns after DIA come in the order left n, left s, centre s, right n, right s, legs
        i0 = names.index("st.dia")
        for nm, i in zip(("st_left.n", "st_left.s", "st_center.s", "st_right.n", "st_right.s", "legs.n"), range(i0 + 1, len(names))):
            if names[i] not in ("remark",):
                names[i] = nm
    need = {"b", "d", "bot_straight.n", "bot_straight.dia", "top_anchor.n", "st.dia"}
    if not need <= set(names):
        return {}
    ci = {n: i for i, n in enumerate(names)}
    c0 = centres[0]

    def col_of(t: Txt) -> int | None:
        k = min(range(len(centres)), key=lambda i: abs(centres[i] - t.cx))
        half = (centres[k] - centres[k - 1]) / 2 if k else (centres[1] - centres[0]) / 2
        half2 = (centres[k + 1] - centres[k]) / 2 if k + 1 < len(centres) else half
        if t.cx < centres[k] - half - 1 or t.cx > centres[k] + half2 + 1:
            return None
        return k

    label_x0 = c0 + 10
    labels = [t for ln in lines for t in ln if abs(t.cx - c0) <= 14 and re.fullmatch(r"[A-Za-z]{0,2}\d{0,3}[A-Za-z]?", t.s) and t.s]
    labels = [t for t in labels if not re.fullmatch(r"[A-Za-z]{3,}", t.s)]
    labels.sort(key=lambda t: -t.cy)
    rows: list[dict] = []
    ylines = _lines(data)
    anchor_cols = {ci.get("top_anchor.n"), ci.get("top_anchor.dia")}
    loose = []
    for ln in ylines:
        lab = next((t for t in ln if t in labels), None)
        cells: dict[int, list[Txt]] = {}
        for t in ln:
            k = col_of(t)
            if k is not None:
                cells.setdefault(k, []).append(t)
        if lab is not None:
            rows.append({"label": lab.s.strip(), "cells": cells, "y": lab.cy, "items": list(ln)})
        else:
            loose.append((ln, cells))
    for ln, cells in loose:
        above = [r for r in rows if r["y"] >= ln[0].cy]
        prev = min(above, key=lambda r: r["y"] - ln[0].cy) if above else None
        cont = (prev is not None and cells and set(cells) <= anchor_cols
                and re.search(r"\+\s*$", " ".join(t.s for t in prev["cells"].get(ci["top_anchor.dia"], []))))
        if cont:
            prev.setdefault("extra", []).append(cells)
            prev["items"] += ln
            continue
        near = min(rows, key=lambda r: abs(r["y"] - ln[0].cy), default=None)
        if near is not None and abs(near["y"] - ln[0].cy) <= 17:
            for k, v in cells.items():
                near["cells"].setdefault(k, []).extend(v)
            near["items"] += ln
    recs = []
    heading = " ".join(t.s for t in sorted([t for t in ws if t.cy > sub_y + 60], key=lambda t: (-t.cy, t.cx)))
    for r in rows:
        def cell(name):
            k = ci.get(name)
            if k is None:
                return ""
            ts = sorted(r["cells"].get(k, []), key=lambda t: (-t.cy, t.cx))
            return "".join(t.s for t in ts) if name == "d" else " ".join(t.s for t in ts)

        label = r["label"]
        full = " ".join(t.s for t in r["items"])
        rec = {"tag": label, "sheet": sh.n, "raw": " ".join(t.s for t in sorted(r["items"], key=lambda t: (-t.cy, t.cx)))}
        rec["not_used"] = bool(re.search(r"NOT\s*USED", full, re.I))
        rec["b"] = _num(cell("b"))
        dtxt = cell("d")
        rec["d_text"] = dtxt
        rec["d"] = _num(dtxt.split("/")[0]) if re.match(r"\d", dtxt or "") else None
        rec["d_end"] = _num(dtxt.split("/")[1]) if "/" in dtxt and re.match(r"\d+/\d+", dtxt) else None
        for g in ("bot_straight", "bot_extra", "top_anchor", "top_left", "top_right"):
            a = n_dia(cell(g + ".n"), cell(g + ".dia"))
            rec[g] = [a] if a else []
        for ex in r.get("extra") or []:
            a = n_dia(" ".join(t.s for t in ex.get(ci["top_anchor.n"], [])), " ".join(t.s for t in ex.get(ci["top_anchor.dia"], [])))
            if a:
                rec["top_anchor"].append(a)
        st = {"dia": _num(cell("st.dia"))}
        for z in ("left", "right"):
            st[z] = {"n": _num(cell(f"st_{z}.n")), "s": _num(cell(f"st_{z}.s"))}
        st["center_s"] = _num(cell("st_center.s"))
        rec["stirrup"] = st
        rec["legs"] = int(_num(cell("legs.n") or cell("legs")) or 2) if ("legs.n" in ci or "legs" in ci) else None
        rem = " ".join(t.s for t in sorted(r["cells"].get(ci.get("remark", -1), []), key=lambda t: (-t.cy, t.cx)))
        rec["remark"] = rem
        m = re.search(r"E\s*/\s*F\s*(\d+)\s*(?:%%[Cc]|[ØøΦ∅]|DIA)?\s*(\d{2})", rem + " " + full, re.I)
        rec["side_each_face"] = {"n": int(m.group(1)), "dia": int(m.group(2))} if m else None
        rec["legs4"] = bool(re.search(r"4\s*LEGG?", rem, re.I)) or rec["legs"] == 4
        rec["canti"] = bool(re.search(r"CANTI", rem, re.I))
        rec["inverted"] = bool(re.search(r"INV", rem, re.I))
        rec["crank"] = bool(re.search(r"CRANK", rem, re.I))
        rec["ref"] = ref("schedule-row", f"Beam schedule {label}", r["items"], sh.n)
        recs.append(rec)
    gm = re.search(r"\bM\s*[:\-]?\s*(\d{2})\b", heading)
    fm = re.search(r"\bFe\s*[:\-]?\s*(\d{3})", heading, re.I)
    return {"rows": recs, "grade": f"M{gm.group(1)}" if gm else None, "steel": f"Fe{fm.group(1)}" if fm else None,
            "heading": heading, "columns": names, "sheet": sh.n}


def _num(s) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(s or ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- slab schedule

BAR = re.compile(r"(?:%%[Cc]|[ØøΦ∅]|DIA\.?|#)?\s*(\d{1,2})\s*(?:%%[Cc]|[ØøΦ∅])?\s*[-@]+\s*(\d{2,3})\s*(?:C\s*/\s*C|CC|C/C)?", re.I)


def bar_spec(s: str) -> dict | None:
    """'%%C 8 @ 175 C/C ALT. BAR BENT UP' -> {"dia": 8, "s": 175, "alt": True, "bent": True, "both": False}."""
    m = BAR.search(s or "")
    if not m or not 6 <= int(m.group(1)) <= 40:
        return None
    u = s.upper()
    return {"dia": int(m.group(1)), "s": int(m.group(2)), "alt_bent": bool(re.search(r"ALT\.?\s*BAR\s*BENT", u)),
            "spring": "SPRING" in u, "top_and_bottom": "TOP AND BOTTOM" in u or "TOP & BOTTOM" in u,
            "extra_top": bool(re.search(r"EXTRA\s*@?\s*TOP", u)), "bothways": "BOTHWAYS" in u, "text": " ".join(s.split())}


def parse_slab_schedule(sh: Sheet) -> dict:
    """The schedule of slabs: a heading with TYPE / THICK / MAIN / DISTRIBUTION / REMARK columns and rows labelled S<n>."""
    ws = sh.texts
    hd = [t for t in ws if re.fullmatch(r"THICK(NESS)?|TYPE|MAIN\s*STEEL|DISTRIBUTION\s*STEEL|REMARKS?", t.s.strip(), re.I)]
    if not hd:
        return {}
    # the heading row: the y with most of these words
    byy: dict[int, list[Txt]] = {}
    for t in hd:
        byy.setdefault(round(t.cy / 6), []).append(t)
    row = max(byy.values(), key=len)
    if len(row) < 3:
        return {}
    hy = sum(t.cy for t in row) / len(row)
    col = {}
    for t in row:
        s = t.s.strip().upper()
        col["type" if s == "TYPE" else "thick" if s.startswith("THICK") else "main" if s.startswith("MAIN") else
            "dist" if s.startswith("DIST") else "remark"] = t
    if not {"type", "thick", "main"} <= set(col):
        return {}
    ex = col["type"].cx
    labels = sorted([t for t in ws if abs(t.cx - ex) < 14 and t.cy < hy - 4 and re.fullmatch(r"[A-Za-z]{1,2}\d{1,3}[A-Za-z]?", t.s.strip())],
                    key=lambda t: -t.cy)
    if labels:
        from collections import Counter
        pre = Counter(re.match(r"[A-Za-z]+", t.s.strip()).group().upper() for t in labels).most_common(1)[0][0]
        labels = [t for t in labels if t.s.strip().upper().startswith(pre)]
        gaps = [a.cy - b.cy for a, b in zip(labels, labels[1:])]
        med = sorted(gaps)[len(gaps) // 2] if gaps else 0
        keep = labels[:1]
        for a, b in zip(labels, labels[1:]):
            if med and a.cy - b.cy > 2.2 * med:
                break
            keep.append(b)
        labels = keep
    if not labels:
        return {}
    xs = {k: v.cx for k, v in col.items()}
    order = sorted(xs, key=xs.get)
    bound = {}
    for i, k in enumerate(order):
        lo = (xs[order[i - 1]] + xs[k]) / 2 if i else xs[k] - 40
        hi = (xs[k] + xs[order[i + 1]]) / 2 if i + 1 < len(order) else xs[k] + 80
        bound[k] = (lo, hi)
    # beam: "thick" column is narrow; steel columns take everything up to the next header
    bound["thick"] = (bound["thick"][0], bound["thick"][1])
    y_lo = labels[-1].cy - 14
    body = [t for t in ws if t.cy < hy - 4 and t.cy > y_lo and t.cx > ex - 20 and t.cx < max(b[1] for b in bound.values())]
    recs: dict[str, dict] = {}
    items: dict[str, list] = {t.s.strip(): [t] for t in labels}
    lab_by = {t.s.strip(): t for t in labels}
    cells: dict[str, dict[str, list[Txt]]] = {k: {} for k in lab_by}
    for t in body:
        if t in labels:
            continue
        c = next((k for k in order if bound[k][0] <= t.cx < bound[k][1]), None)
        if c is None or c == "type":
            continue
        u = t.s.upper()
        extra = bool(re.search(r"EXTRA|BOTHWAYS", u)) and c in ("main", "dist") and not re.match(r"\s*(%%C|[ØøΦ∅])?\s*\d{1,2}\s*@", t.s) or (
            c in ("main", "dist") and re.search(r"EXTRA\s*@?\s*TOP", u) and t.cx > bound["main"][0] and "ALT" not in u)
        above = [l for l in labels if l.cy >= t.cy - 3]
        below = [l for l in labels if l.cy < t.cy - 3]
        if extra and above:
            lab = min(above, key=lambda l: l.cy - t.cy)
        else:
            lab = min(labels, key=lambda l: abs(l.cy - t.cy))
        k = lab.s.strip()
        cells[k].setdefault("extra" if extra else c, []).append(t)
        items[k].append(t)
    for k, lab in lab_by.items():
        def txt(c):
            return " ".join(t.s for t in sorted(cells[k].get(c, []), key=lambda t: (-t.cy, t.cx)))
        th = re.search(r"\d{2,3}", txt("thick"))
        rem = txt("remark").upper()
        r = {"tag": k, "thk": int(th.group()) if th else None, "main": bar_spec(txt("main")), "dist": bar_spec(txt("dist")),
             "extra": bar_spec(txt("extra")), "remark": rem, "raw": {c: txt(c) for c in cells[k]}, "sheet": sh.n,
             "ref": ref("schedule-row", f"Slab schedule {k}", items[k], sh.n)}
        r["oneway"] = "ONEWAY" in rem.replace(" ", "")
        r["twoway"] = "TWOWAY" in rem.replace(" ", "")
        r["canti"] = "CANTI" in rem
        r["mesh"] = "MESH" in rem
        recs[k] = r
    heading = next((t.s for t in ws if re.search(r"SCHEDULE\s+OF\s+SLABS?", t.s, re.I)), "")
    gm = re.search(r"\bM\s*[:\-]?\s*(\d{2})\b", heading)
    fm = re.search(r"Fe\s*[:\-]?\s*(\d{3})", heading, re.I)
    return {"rows": recs, "grade": f"M{gm.group(1)}" if gm else None, "steel": f"Fe{fm.group(1)}" if fm else None,
            "heading": heading, "sheet": sh.n,
            "bbox": [min(t.bb[0] for ts in items.values() for t in ts), min(t.bb[1] for ts in items.values() for t in ts),
                     max(t.bb[2] for ts in items.values() for t in ts), max(t.bb[3] for ts in items.values() for t in ts)]}
