"""General notes -> facts the quantities depend on (covers, development lengths, grades,
pile cut-off). Only what is written is returned; callers flag what is absent."""
from __future__ import annotations

import re

from .sheet import Sheet, Txt, ref


def text_lines(ts: list[Txt], tol: float = 2.0) -> list[tuple[str, list[Txt]]]:
    rows: list[list[Txt]] = []
    for t in sorted(ts, key=lambda t: (-t.cy, t.cx)):
        if rows and abs(rows[-1][0].cy - t.cy) < tol:
            rows[-1].append(t)
        else:
            rows.append([t])
    return [(" ".join(x.s.strip() for x in sorted(r, key=lambda t: t.cx)), r) for r in rows]


def notes_block(sh: Sheet) -> list[tuple[str, list[Txt]]]:
    a = sh.full(r"NOTES\s*:?")
    if not a:
        return []
    a = max(a, key=lambda t: t.cy)
    ts = [t for t in sh.texts if a.bb[0] - 40 <= t.cx <= a.bb[0] + 420 and t.cy < a.cy and a.cy - t.cy < 900]
    return text_lines(ts)


COVER_EL = r"(COLUMNS?|FOOTINGS?|TIE\s*BEAMS?|PLINTH\s*BEAMS?|FLOOR\s*BEAMS?|BEAMS?|SLABS?|PILES?|RAFT|WALLS?|STAIRS?)"


GRADE_RE = re.compile(r"(?:CONCRETE\s+MIX|CONCRETE\s+GRADE|GRADE\s+OF\s+(?:RCC\s+)?CONCRETE|CONCRETE|RCC)\s*(?:SHALL\s+BE\s+|FOR\s+ALL\s+\w+\s+)?"
                      r"[.:=\-]?\s*M\s*-?\s*(\d{2})\b", re.I)


def concrete_grade(text: str) -> str | None:
    """'CONCRETE MIX M25', 'GRADE OF CONCRETE M25', 'CONCRETE GRADE: M-25', 'RCC M25'; not a lean/PCC mix."""
    for m in GRADE_RE.finditer(text):
        if re.search(r"(LEAN|PLAIN|PCC|P\.C\.C\.?)\s*$", text[max(0, m.start() - 12):m.start()], re.I):
            continue
        return f"M{m.group(1)}"
    return None


def parse_notes(sh: Sheet) -> dict:
    lines = notes_block(sh)
    out: dict = {"sheet": sh.n, "covers": {}, "dev_length": {}, "lap": "", "grade": None, "pcc_mix": None,
                 "pile_cutoff_below_gl": None, "lift_pile_note": "", "steel": "", "refs": {}, "text": [l for l, _ in lines]}
    full = " \n".join(l for l, _ in lines)
    out["grade"] = concrete_grade(full)
    m = re.search(r"LEAN\s+CONCRETE\s+(1\s*:\s*\d\s*:\s*\d+)", full, re.I)
    if m:
        out["pcc_mix"] = m.group(1).replace(" ", "")
    m = re.search(r"(FE|GRADE\s*FE)\s*-?\s*(\d{3})", full, re.I)
    if m:
        out["steel"] = f"Fe{m.group(2)}"
    in_dev = False
    for l, ts in lines:
        u = l.upper()
        m = re.match(r"^\s*" + COVER_EL + r"\s+(\d{2,3})\s*MM", u)
        if m:
            key = re.sub(r"\s+", " ", m.group(1)).rstrip("S") if not m.group(1).endswith("SS") else m.group(1)
            out["covers"][key] = int(m.group(2))
            out["refs"][f"cover:{key}"] = ref("note", f"cover {key}", ts, sh.n)
        if "DEVELOPMENT LENGTH" in u and "FOLLOW" in u:
            in_dev = True
            out["refs"]["dev_length"] = ref("note", "development lengths", ts, sh.n)
            continue
        if in_dev:
            nums = re.findall(r"\d+", u)
            if len(nums) == 3 and int(nums[0]) <= 40:
                out["dev_length"][int(nums[0])] = {"tension": int(nums[1]), "compression": int(nums[2])}
                out["refs"]["dev_length"]["handles"] += [t.handle for t in ts]
            elif out["dev_length"] and not nums and "BAR" not in u:
                in_dev = False
        if re.search(r"LAP\s+LENGTH", u):
            out["lap"] = l.strip()
        m = re.search(r"CUT\s*OF+\s*LVL\.?\s*OF\s*ALL\s*PILES\s*SHALL\s*BE\s*(\d{3,5})\s*MM", u)
        if m:
            out["pile_cutoff_below_gl"] = int(m.group(1))
            out["refs"]["pile_cutoff"] = ref("note", "pile cut-off", ts, sh.n)
        if re.search(r"LIFT\s+PILE", u):
            out["lift_pile_note"] = l.strip()
        m = re.search(r"DESIGNED\s+FOR\s+G\s*\+\s*(\d+)", u)
        if m:
            out["storeys_above_ground"] = int(m.group(1))
    return out
