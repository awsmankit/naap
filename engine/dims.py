"""Written dimension text vs measured dimension geometry.

On Indian drawings the written dimension governs: a DIMENSION whose text was overridden ("4550"
typed over a 4500 line) or whose block text was never regenerated says one thing and measures
another. Every use of a dimension value in the engine goes through `apply_written_dims`, which
replaces the value by the written one (config `dimensions.use`: "written" (default) or "measured";
a resolved flag can choose per dimension) and records the mismatch so `check_dimensions` can raise
an error flag with both values and the drawing refs.

Tolerance: a difference above max(5 mm, 0.5 %) is a mismatch.
"""
from __future__ import annotations

import hashlib
import re

from .types import Flag

TOL_MM = 5.0
TOL_REL = 0.005
MAX_FLAGS = 40

_FMT = [
    (re.compile(r"\\[Pp]"), " "),                      # MTEXT paragraph
    (re.compile(r"\\[AaCcFfHhQqTtWw][^;\\]*;"), ""),   # \A1; \H2.5; \fArial|b0;
    (re.compile(r"\\[LlOoKk]"), ""),                   # underline / overline toggles
    (re.compile(r"[{}]"), ""),
    (re.compile(r"%%[cCdDpP]"), ""),                   # diameter / degree / plus-minus
]
NUM_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w.])")
# text that is only a number, optionally with a unit / brackets ("4550", "(4550)", "4550 mm", "4.55 M")
ONLY_NUM = re.compile(r"^\s*[\(\[]?\s*(\d+(?:[.,]\d+)?)\s*(?:mm|MM|m|M|cm|CM)?\.?\s*[\)\]]?\s*$")


def _fid(key: str) -> str:
    return f"dimension-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def clean_text(s: str) -> str:
    s = s or ""
    for rx, rep in _FMT:
        s = rx.sub(rep, s)
    return " ".join(s.split())


def written_value(text: str, measured: float | None) -> tuple[float | None, str]:
    """Value written in a dimension text. Returns (value, how):
    how = "auto" (empty or '<>' only: the measured value is shown), "override" (a number was typed),
    "words" (text without a usable number, e.g. 'as/schedule'), "mixed" ('<>' plus other text)."""
    raw = text or ""
    s = clean_text(raw)
    if not s or s == "<>":
        return measured, "auto"
    if "<>" in s:
        # '<> mm wide exp. gap' shows the measured value; '4550 (<>)' also writes another number
        nums = [float(x.replace(",", ".")) for x in NUM_RE.findall(s.replace("<>", " "))]
        nums = [v for v in nums if v >= 10 and (measured is None or abs(v - measured) > 0.5)]
        if len(nums) == 1:
            return nums[0], "override"
        return measured, "mixed"
    m = ONLY_NUM.match(s)
    if m:
        return float(m.group(1).replace(",", ".")), "override"
    return None, "words"


def _mm(a, v: float) -> float:
    return v * a.to_m * 1000


def apply_written_dims(a, children: dict | None = None) -> None:
    """Rewrite a.dims so that e["m"] is the governing value (drawing units) and record mismatches in
    a.dim_mismatches. children: {dimension index: [child text records]} (the DIMENSION's block text)."""
    cfg = a.config or {}
    use = str(((cfg.get("dimensions") or {}).get("use")) or "written").lower()
    saved = cfg.get("flags", {}) or {}
    a.dim_mismatches = []
    out = []
    for i, e, rid in a.dims:
        m = e.get("m")
        lf = float(e.get("lf") or 1.0)
        shown = m * lf if m is not None else None     # what '<>' displays
        wv, how = written_value(e.get("s", ""), shown)
        src = "text override"
        if how == "auto" and children:
            # no override: the DIMENSION block's own text (may be stale if the block was not regenerated)
            for t in children.get(i, []):
                v2, h2 = written_value(t.get("s", ""), shown)
                if h2 == "override" and v2 is not None:
                    wv, how, src = v2, "override", "dimension block text"
                    break
        info = {"i": i, "rid": rid, "measured": shown, "written": wv, "how": how, "text": clean_text(e.get("s", ""))}
        e2 = e
        if how == "override" and shown is not None and wv is not None:
            diff = abs(wv - shown)
            tol = max(TOL_MM / (a.to_m * 1000) * lf, TOL_REL * abs(shown))
            if diff > tol:
                fid = _fid(f"override:{e.get('handle') or i}")
                st = saved.get(fid) or {}
                choice = use
                val = None
                if st.get("status") == "resolved":
                    v = str(st.get("value") or "").strip().lower()
                    if v.startswith("meas") or v.startswith("drawn") or v.startswith("geom"):
                        choice = "measured"
                    elif v.startswith("writ") or v.startswith("text"):
                        choice = "written"
                    else:
                        try:
                            val, choice = float(v), "user"
                        except ValueError:
                            pass
                elif st.get("status") == "accepted":
                    choice = "written"
                gov = {"written": wv, "measured": shown, "user": val}.get(choice, wv)
                e2 = {**e, "m": gov / lf, "m_geom": m, "m_written": wv / lf, "dim_choice": choice}
                info.update({"flag_id": fid, "choice": choice, "value": gov, "src": src, "status": st.get("status", "open")})
                a.dim_mismatches.append(info)
        if how == "words":
            e2 = {**e, "dim_words": info["text"]}
        out.append((i, e2, rid))
    a.dims = out


def dim_row_fields(a, e) -> dict:
    """Extra DimRow fields (measured / written / status / remarks) for one (rewritten) dim record."""
    if e.get("dim_words"):
        return {"remarks": f"text {e['dim_words']!r} replaces the value; the drawn length is listed"}
    if "m_geom" not in e:
        return {}
    lf = float(e.get("lf") or 1.0)
    meas = e["m_geom"] * lf
    wr = e["m_written"] * lf
    ch = e.get("dim_choice", "written")
    if ch == "measured":
        rem = f"drawn {meas:.0f} used (written {wr:.0f}); chosen by user"
        status = "ok"
    elif ch == "user":
        rem = f"user value {e['m'] * lf:.0f} used (written {wr:.0f}, drawn {meas:.0f})"
        status = "ok"
    else:
        rem = f"written {wr:.0f} used, drawn length is {meas:.0f} (written dimensions govern)"
        status = "assumed"
    return {"measured": round(meas, 2), "written": round(wr, 2), "status": status, "remarks": rem}


def check_dimensions(a) -> list[Flag]:
    flags = []
    unit = 1 / (a.to_m * 1000)
    mism = getattr(a, "dim_mismatches", []) or []
    for k, d in enumerate(mism):
        e = a.geometry["entities"][d["i"]]
        pts = [e[q] for q in ("p1", "p2", "tp", "dp") if q in e]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bbox = [min(xs) - 300 * unit, min(ys) - 300 * unit, max(xs) + 300 * unit, max(ys) + 300 * unit] if pts else None
        handle = e.get("handle")
        layer = a.layer_names[e["l"]] if e.get("l") is not None and e["l"] < len(a.layer_names) else ""
        used = {"written": "the written value", "measured": "the drawn length", "user": "your value"}.get(d["choice"], "the written value")
        title = f"Dimension text {d['written']:.0f} differs from its drawn length {d['measured']:.0f}"
        if k >= MAX_FLAGS:
            # very many: one summary flag carries the rest
            if k == MAX_FLAGS:
                flags.append(Flag(id=_fid("override:summary"), severity="error", category="dimension",
                                  title=f"{len(mism) - MAX_FLAGS} more dimensions whose text differs from the drawn length",
                                  detail="Written values are used for all of them (written dimensions govern).",
                                  evidence=[], refs=["dimensions"]))
            flags[-1].evidence.append(f"{d['text']!r} vs {d['measured']:.0f} ({handle}, {layer}, {a.region_name(d['rid'])})")
            continue
        flags.append(Flag(
            id=d["flag_id"], severity="error", category="dimension", title=title,
            detail=(f"The {d['src']} says {d['written']:.0f} but the dimension measures {d['measured']:.0f} "
                    f"(difference {abs(d['written'] - d['measured']):.0f}; tolerance max({TOL_MM:.0f} mm, {TOL_REL * 100:.1f}%)). "
                    f"Written dimensions govern on Indian drawings, so the engine uses {used}; anything measured off "
                    "this part of the drawing may be off by the difference."),
            location={"bbox": bbox} if bbox else None,
            evidence=[f"text {d['text']!r} on layer {layer} in {a.region_name(d['rid'])}",
                      f"measured {d['measured']:.1f} (geometry), written {d['written']:.0f}",
                      f"DIMENSION handle {handle}"],
            suggested_fix="Check with the architect which value is right. Accept to use the written value, or enter "
                          "'measured' or a value in mm.",
            input={"type": "text", "label": "Use: 'written', 'measured' or a value (mm)", "placeholder": "written"},
            refs=[f"dim:{handle}", "dimensions"]))
    return flags
