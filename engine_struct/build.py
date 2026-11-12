"""Structural measurement book from a vector PDF set (pile/raft, columns, plinth beams,
floor beams and slabs).

build_struct_mb(geometry, user_config) -> MBResult (engine.types), with
meta["steel"], meta["schedules"], meta["calibration"], meta["sheets"].

Rules kept throughout:
  * values come from written text (schedules, notes, details, written dimensions);
  * lengths scaled off a NOT-TO-SCALE plan are used only with status "assumed" and the
    remark SCALED (written dimensions govern);
  * anything not written is a "missing" flag with an input; it is never invented. Where a
    standard detailing convention is needed for steel (hooks, laps, curtailment) it is listed
    in Assumptions and flagged.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np
from scipy.spatial import cKDTree

from engine.rowid import assign_stable_ids
from engine.types import AbstractRow, Flag, MBResult, MBRow

from . import calibrate as C
from . import details as D
from . import plans as P
from . import tables as T
from .notes import parse_notes
from .sheet import Sheet, ref, sheets as all_sheets

SCALED = "scaled from drawing – verify (only written dimensions govern)"
COL_SIZES_DEFAULT: list[tuple[float, float]] = []


def kg_per_m(d: float) -> float:
    return d * d / 162.0


# ====================================================================== accumulator

class Acc:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.rows: list[MBRow] = []
        self.flags: dict[str, Flag] = {}
        self.assumptions: list[str] = []
        self.steel: list[dict] = []
        self.answers = cfg.get("flags") or {}

    # ---- flags
    def flag(self, id: str, severity: str, category: str, title: str, detail: str = "", refs=None,
             input: dict | None = None, suggested_fix: str = "", evidence=None, affects=None) -> Flag:
        if id in self.flags:
            return self.flags[id]
        loc = None
        refs = [r for r in (refs or []) if r]
        if refs:
            bb = refs[0].get("bbox")
            if bb:
                loc = {"bbox": bb, "sheet": refs[0].get("sheet")}
        f = Flag(id=id, severity=severity, category=category, title=title, detail=detail, location=loc,
                 evidence=list(evidence or []), suggested_fix=suggested_fix, input=input,
                 refs=list(affects or []))
        f.instances = [{"id": id, "location": {"bbox": r.get("bbox"), "sheet": r.get("sheet")}, "plan": r.get("label", "")}
                       for r in refs[:50]]
        a = self.answers.get(id)
        if a:
            f.status = a.get("status", "open")
            f.value = a.get("value")
        self.flags[id] = f
        return f

    def answer(self, id: str):
        a = self.answers.get(id) or {}
        if a.get("status") == "resolved" and a.get("value") not in (None, ""):
            return a["value"]
        return None

    def assume(self, text: str):
        if text not in self.assumptions:
            self.assumptions.append(text)

    # ---- rows
    def row(self, trade: str, item: str, location: str, nos, l, b, h, unit: str, remarks: str = "",
            status: str = "ok", refs=None, flag_ids=None, floor_group: str = "", floor_order: float = 0,
            plan: str = "", qty: float | None = None) -> MBRow:
        vals = [v for v in (nos, l, b, h) if v is not None]
        q = qty if qty is not None else (math.prod(vals) if vals else 0.0)
        r = MBRow(item=item, location=location, nos=nos, l=_r3(l), b=_r3(b), h=_r3(h), qty=round(q, 3), unit=unit,
                  remarks=remarks, trade=trade, status=status, flag_ids=list(flag_ids or []),
                  refs=[x for x in (refs or []) if x][:60], floor_group=floor_group, floor_order=floor_order, plan=plan)
        if qty is None:
            # keep the exporter's =PRODUCT() valid after rounding of the factors
            vals = [v for v in (r.nos, r.l, r.b, r.h) if v is not None]
            r.qty = round(math.prod(vals), 3) if vals else 0.0
        r.id = f"r{len(self.rows) + 1}"
        self.rows.append(r)
        return r

    def bars(self, element: str, location: str, dia: int, nos: float, length_m: float, status: str, remarks: str,
             refs=None, floor_group: str = "", floor_order: float = 0, flag_ids=None, plan: str = ""):
        if not nos or not length_m:
            return
        w = kg_per_m(dia)
        r = self.row("STEEL", f"Steel reinforcement (TMT) – {element}", f"{location} – #{dia}", round(nos, 2),
                     length_m, round(w, 4), None, "kg", remarks, status, refs, flag_ids, floor_group, floor_order, plan)
        self.steel.append({"element": element, "dia": dia, "kg": r.qty, "row": r.id, "status": status,
                           "floor_group": floor_group})


def _bars_area(self, element: str, location: str, dia: int, area_m2: float, kg_per_m2: float, status: str, remarks: str,
               refs=None, floor_group: str = "", floor_order: float = 0, plan: str = ""):
    """Steel from an area: qty (kg) = area x kg/m² (slabs, walls)."""
    if not area_m2 or not kg_per_m2:
        return
    r = self.row("STEEL", f"Steel reinforcement (TMT) – {element}", f"{location} – #{dia}", None, round(area_m2, 3), round(kg_per_m2, 4), None, "kg", remarks,
                 status, refs, None, floor_group, floor_order, plan)
    self.steel.append({"element": element, "dia": dia, "kg": r.qty, "row": r.id, "status": status, "floor_group": floor_group})


Acc.bars_area = _bars_area


def _r3(v):
    return None if v is None else round(float(v), 3)


# ====================================================================== sheet roles

def classify(sh: Sheet) -> str:
    t = (sh.title.get("title") or "").upper()
    if "PILE" in t:
        return "pile"
    if "RAFT" in t:
        return "raft"
    if "FOOTING" in t or "FOUNDATION" in t:
        return "footing"
    if "COLUMN" in t:
        return "column"
    if "PLINTH" in t:
        return "plinth"
    if "LIFT" in t:
        return "lift"
    if "STAIR" in t:
        return "stair"
    if "SLAB" in t or "BEAM" in t:
        return "floor"
    return "other"


def sheet_label(sh: Sheet) -> str:
    return sh.title.get("drawing_no") or f"Sheet {sh.n}"


# ====================================================================== main

def build_struct_mb(geo: dict, cfg: dict | None = None) -> MBResult:
    cfg = cfg or {}
    A = Acc(cfg)
    shs = all_sheets(geo)
    roles = {sh.n: classify(sh) for sh in shs}
    from . import rcc as RCC
    rcc_found = RCC.find_plan_sheet(shs, skip=lambda sh: bool(T.parse_slab_schedule(sh)) and roles[sh.n] == "floor")
    if rcc_found:
        roles[rcc_found[0].n] = "rcc"
    by_role: dict[str, list[Sheet]] = defaultdict(list)
    for sh in shs:
        by_role[roles[sh.n]].append(sh)
    meta: dict = {"source": "pdf", "sheets": [], "schedules": {}, "calibration": {}, "counts": {}}
    for sh in shs:
        meta["sheets"].append({"sheet": sh.n, "file": sh.info.get("file"), "page": sh.info.get("file_page"),
                               "drawing_no": sheet_label(sh), "title": sh.title.get("title"),
                               "role": roles[sh.n], "scale": sh.title.get("scale"),
                               "floor_scope": sh.title.get("floor_scope")})
    if geo.get("scanned_pages"):
        A.flag("struct:scanned-pages", "warning", "input", f"{len(geo['scanned_pages'])} page(s) are scanned images",
               f"Pages {geo['scanned_pages']} have no vector content and were skipped.")

    pdf_text_flags(A, geo, shs)

    # ---------------- notes
    notes = {}
    notes_sheet = None
    for sh in shs:
        n = parse_notes(sh)
        if n["dev_length"] or n["covers"]:
            notes = n
            notes_sheet = sh.n
            break
    meta["notes"] = {k: v for k, v in notes.items() if k not in ("refs", "text")}
    nref = notes.get("refs", {})
    if not notes.get("dev_length") and not rcc_found:
        A.flag("struct:notes:dev-length", "missing", "missing-data", "Development lengths not found in the notes",
               "Anchorage and laps of beam/column/pile bars need Ld per bar size.",
               input={"type": "number", "label": "Ld in tension, in bar diameters", "unit": "×d", "placeholder": "e.g. 47"})
    grade = notes.get("grade") or "M25"
    if not notes.get("grade") and not rcc_found:
        A.flag("struct:notes:grade", "missing", "missing-data", "Concrete grade not written",
               input={"type": "text", "label": "Concrete grade", "placeholder": "e.g. M25"})
    pcc_mix = notes.get("pcc_mix")
    covers = notes.get("covers", {})

    def Ld(d: int) -> tuple[float, str]:
        dl = notes.get("dev_length", {}).get(d)
        if dl:
            return dl["tension"] / 1000, "Ld from note 25"
        v = A.answer("struct:notes:dev-length")
        if v:
            return float(v) * d / 1000, "Ld from your answer"
        return None, "Ld missing"

    def cover(key: str, *alts: str) -> tuple[float | None, str]:
        for k in (key,) + alts:
            if k in covers:
                return covers[k], f"cover {covers[k]} mm ({k.lower()}, notes)"
        return None, ""

    rcc_out = None
    if rcc_found:
        rcc_out = RCC.build_rcc(A, shs, cfg, meta, sheet_label)
    # ---------------- schedules
    sched: dict = {"beam": {}, "slab": {}}
    floor_sheets = sorted(by_role.get("floor", []), key=lambda s: s.n)
    for sh in floor_sheets:
        sched["beam"][sh.n] = T.parse_beam_schedule(sh)
        sched["slab"][sh.n] = T.parse_slab_schedule(sh)
    footing = {}
    column = {}
    plinth = {}
    pile = {}
    for sh in shs:
        if not footing:
            footing = T.parse_footing_schedule(sh)
        if not column and roles[sh.n] == "column":
            column = T.parse_column_schedule(sh)
        if not plinth and roles[sh.n] == "plinth":
            plinth = T.parse_plinth_schedule(sh)
        if not pile and roles[sh.n] in ("pile", "footing", "raft"):
            pile = D.parse_pile_detail(sh)
    if not column:
        for sh in shs:
            column = T.parse_column_schedule(sh)
            if column:
                break
    tabs = []
    for k, d in (("footing", footing), ("column", column), ("plinth", plinth)):
        if d.get("_table"):
            tabs.append(d["_table"].to_dict())
    for n, d in sched["beam"].items():
        if d.get("_table"):
            tabs.append(d["_table"].to_dict())
    for n, d in sched["slab"].items():
        if d.get("_table"):
            tabs.append(d["_table"].to_dict())
    if rcc_out and rcc_out.get("tables"):
        tabs = tabs + rcc_out["tables"]
    meta["schedules"] = {"tables": tabs,
                         "pile": {k: v for k, v in pile.items() if not k.startswith("_") and k != "refs"}}
    _schedule_consistency(A, sched, floor_sheets)

    col_sizes = {k: (v["size"] if v.get("size") else (v["dia"], v["dia"]) if v.get("dia") else None)
                 for k, v in column.items() if not k.startswith("_")}
    col_sizes = {k: v for k, v in col_sizes.items() if v}
    rect_sizes = sorted({tuple(sorted(v)) for v in col_sizes.values()})

    # ---------------- calibration + columns per sheet
    calib: dict[int, C.Calib] = {}
    blobs: dict[int, list[P.Blob]] = {}
    region: dict[int, tuple] = {}
    for sh in shs:
        if roles[sh.n] not in ("pile", "raft", "footing", "plinth", "floor"):
            continue
        bl = P.column_blobs(sh)
        reg = P.plan_region(sh, bl)
        if not reg:
            continue
        region[sh.n] = reg
        blobs[sh.n] = [b for b in bl if P.inside(b.cx, b.cy, P.grow(reg, 2))]
        _tag_columns(sh, blobs[sh.n], reg)
        samples = C.dim_samples(sh, P.grow(reg, 80))
        method = "dimensions"
        samples += C.element_samples(blobs[sh.n], col_sizes)
        if not samples and rect_sizes:
            samples = C.vote_column_scale([b for b in blobs[sh.n]], rect_sizes)
            method = "column sizes (schedule)"
        elif any(x["source"].startswith("column") for x in samples):
            method = "dimensions + tagged column sizes"
        cal = C.consensus(sh.n, samples, method)
        u = C.user_calib(sh.n, (cfg.get("calibration") or {}).get(str(sh.n)) or {})
        if u:
            cal = u
        calib[sh.n] = cal
        meta["calibration"][sh.n] = cal.to_dict() | {"drawing_no": sheet_label(sh)}
        fid = f"struct:calibration:{sheet_label(sh)}"
        if cal.pt_per_mm is None:
            A.flag(fid, "missing", "calibration", f"{sheet_label(sh)}: no scale found",
                   "No written dimension or scheduled element size could be matched on this plan, so no length can be scaled.",
                   [ref("plan", sheet_label(sh), [reg], sh.n)],
                   input={"type": "text", "label": "Scale as 1:N, or two points and a length in config 'calibration'", "placeholder": "1:100"})
        elif cal.spread > 0.03 or cal.n < 3:
            A.flag(fid, "warning", "calibration", f"{sheet_label(sh)}: scale calibration is weak "
                   f"(spread {cal.spread * 100:.1f}%, {cal.n} samples)",
                   f"Consensus {cal.pt_per_mm:.5f} pt/mm (≈1:{cal.scale_1_to}) from {cal.sources}. Scaled lengths on this sheet may be off by about the spread.",
                   [ref("plan", sheet_label(sh), [reg], sh.n)],
                   input={"type": "text", "label": "Scale as 1:N (overrides)", "placeholder": "1:137"})
        if cal.outliers:
            ol = cal.outliers
            A.flag(f"struct:calibration-outliers:{sheet_label(sh)}", "warning", "calibration",
                   f"{sheet_label(sh)}: {len(ol)} written dimension(s) disagree with the scaled drawing",
                   f"Against the consensus scale (≈1:{cal.scale_1_to}) these written dimensions scale to a different "
                   f"length (by more than {C.OUTLIER_REL * 100:.0f}%). The written value governs; "
                   "lengths scaled near them are suspect, and they were not trusted for the scale.",
                   [ref("dimension", f"{o['text']} (scales to {o['scaled_mm']})", [[o['at'][0] - 6, o['at'][1] - 6, o['at'][0] + 6,
                                                                               o['at'][1] + 6]], sh.n) for o in ol if o.get("at")],
                   evidence=[f"written {o['value_mm']:.0f}, scales to {o['scaled_mm']:.0f} ({o['dev_pct']:+.1f}%)" for o in ol[:12]],
                   suggested_fix="Accept (written values govern), or check the dimension with the engineer.")
        ans = A.answer(fid)
        if ans:
            m = re.search(r"1\s*:\s*(\d+(?:\.\d+)?)", str(ans))
            if m:
                calib[sh.n] = C.user_calib(sh.n, {"scale": float(m.group(1))})
                meta["calibration"][sh.n]["user_override"] = str(ans)
    for n, c in calib.items():
        if c.pt_per_mm:
            A.assume(f"{meta['calibration'][n]['drawing_no']}: plan scale {c.pt_per_mm:.5f} pt/mm (≈1:{c.scale_1_to}) "
                     f"by {c.method}, {c.n} samples, spread {c.spread * 100:.2f}%.")

    if rcc_out:
        region[rcc_out["sheet"].n] = rcc_out["region"]
    # ---------------- foundations
    raft_sh = next(iter(by_role.get("raft", [])), None)
    pile_sh = next(iter(by_role.get("pile", [])), None)
    mode = _foundation_mode(A, pile_sh, raft_sh, region, calib, notes)
    if pile_sh and mode in ("piled_raft", "piles_only"):
        _piles(A, pile_sh, pile, notes, region, calib, Ld, cover, meta)
    if footing and (pile_sh or raft_sh):
        _footings(A, pile_sh or raft_sh, footing, region, Ld, cover, pcc_mix, meta)
    if raft_sh and mode in ("piled_raft", "raft_only"):
        _raft(A, raft_sh, region, calib, pcc_mix, meta)

    # ---------------- columns
    base_sh = raft_sh or pile_sh
    if not rcc_out:
        _columns(A, base_sh, floor_sheets, blobs, region, calib, column, Ld, cover, meta, notes)
    _footing_column_check(A, footing, column, meta, base_sh)

    # ---------------- plinth beams
    for sh in by_role.get("plinth", []):
        if sh.n in region and plinth:
            _beams(A, sh, plinth, blobs[sh.n], region[sh.n], calib.get(sh.n), Ld, cover, meta,
                   kind="plinth", floor_group="Plinth", floor_order=-1, mult=1)

    # ---------------- floors
    _floor_scope_checks(A, floor_sheets, notes)
    for k, sh in enumerate(floor_sheets):
        fs = sh.title.get("floor_scope") or {}
        mult = int(fs.get("multiplier") or 1)
        fg = _floor_group(sh)
        fo = float(fs.get("from", k))
        if sh.n not in region:
            A.flag(f"struct:plan-missing:{sheet_label(sh)}", "warning", "missing-data",
                   f"{sheet_label(sh)}: no plan with columns found", "Beams and slabs on this sheet were not measured.")
            continue
        bs = sched["beam"].get(sh.n) or {}
        ss = sched["slab"].get(sh.n) or {}
        _beams(A, sh, bs, blobs[sh.n], region[sh.n], calib.get(sh.n), Ld, cover, meta,
               kind="floor", floor_group=fg, floor_order=fo, mult=mult, slab_sched=ss)
        _slabs(A, sh, ss, blobs[sh.n], region[sh.n], calib.get(sh.n), cover, meta, fg, fo, mult)

    # ---------------- lift, stair: not measurable without written overall sizes/heights
    for role, what in (("lift", "Lift walls"), ("stair", "Staircase")):
        for sh in by_role.get(role, []):
            fid = f"struct:not-measured:{role}"
            v = A.answer(fid)
            A.flag(fid, "missing", "missing-data", f"{what} ({sheet_label(sh)}) not measured",
                   f"{sheet_label(sh)} gives sections and bar marks but not the overall lengths/levels needed for "
                   f"a volume (and the sheet is NOT TO SCALE). Enter the RCC volume to include it.",
                   [ref("sheet", sheet_label(sh), [sh.bbox], sh.n)],
                   input={"type": "number", "label": f"{what}: RCC volume (all floors)", "unit": "m³"})
            if v:
                A.row("RCC", f"RCC {grade} in {what.lower()}", sheet_label(sh), 1, None, None, None, "m³",
                      "volume entered by user", "ok", [ref("sheet", sheet_label(sh), [sh.bbox], sh.n)], [fid],
                      qty=float(v))

    # ---------------- assumptions common to steel
    A.assume("Bar weight d²/162 kg/m (d in mm).")
    A.assume("Measurement convention: beams full depth between column faces, slabs clear between beam faces, "
             "columns floor to floor. Same total volume as IS 1200 (beams below slab, slab over beams).")
    A.assume("Hooks: 135° stirrup/tie/ring hooks 10d each end (IS 2502 / SP 34); no allowance for wastage or chairs.")
    A.assume("Laps: one lap of Ld (tension, note 25) per 12 m of continuous bar; bars anchored Ld into supports.")
    A.assume("Curtailment per the typical details: support extras max(Ld, 0.25 L) + Ld; mid-span extra 0.75 L; "
             "beam end zones 2D at S1, middle at S2; slab top bars 0.3 × short span from each edge.")

    remap = assign_stable_ids(A.rows)
    for st_ in A.steel:
        st_["row"] = remap.get(st_["row"], st_["row"])
    res = MBResult(mb=A.rows, abstract=_abstract(A.rows), rooms=[], openings=[], dimensions=[],
                   assumptions=A.assumptions, meta=meta, flags=list(A.flags.values()))
    meta["steel"] = steel_summary(A.steel)
    meta["foundation_mode"] = mode
    try:
        meta["review"] = struct_review(shs, roles, region, tabs, notes_sheet, mode, A, cfg)
    except Exception as exc:  # noqa: BLE001  (the review summary must never stop a build)
        meta["review"] = {"kind": "structural", "error": f"{type(exc).__name__}: {exc}"}
    return res


# ====================================================================== helpers

TEXT_AS_GEOMETRY = ("text plotted as geometry — labels, tags and schedules can't be read; re-export with TrueType "
                    "fonts or 'SHX text as comments'")


def pdf_text_flags(A: Acc, geo: dict, shs: list[Sheet]) -> None:
    """Sheets whose text was plotted as strokes (AutoCAD SHX fonts) with no comments to recover it from."""
    for sh in shs:
        info = sh.info
        if info.get("shx_texts"):
            A.assume(f"{sheet_label(sh)}: {info['shx_texts']} text(s) drawn in SHX fonts were read from the PDF's "
                     "'AutoCAD SHX Text' comments.")
        if not info.get("text_as_geometry"):
            continue
        stats = info.get("stroke_stats") or {}
        A.flag(f"struct:text-as-geometry:{sh.n}", "error", "input", f"{sheet_label(sh)}: {TEXT_AS_GEOMETRY}",
               f"Sheet {sh.n} has {info.get('texts', 0)} text run(s) but {stats.get('short_strokes', 0)} short strokes, "
               f"{stats.get('clustered', 0) * 100:.0f}% of them in glyph-sized clusters: the text was plotted as lines "
               "(AutoCAD SHX fonts) and the PDF has no 'AutoCAD SHX Text' comments to read it back from. Nothing written "
               "on this sheet (tags, sizes, schedules, notes) is used.",
               [ref("sheet", sheet_label(sh), [sh.bbox], sh.n)],
               suggested_fix="Re-plot from AutoCAD with TrueType fonts, or set PDFSHX=1 ('SHX text as comments') in the "
                             "PDF export, then upload again.")

def _floor_group(sh: Sheet) -> str:
    fs = sh.title.get("floor_scope") or {}
    if not fs:
        return sheet_label(sh)
    lab = fs.get("label", "")
    return f"{lab} slab ({sheet_label(sh)})" + (f" ×{fs['multiplier']}" if fs.get("multiplier", 1) > 1 else "")


def _tag_columns(sh: Sheet, bl: list[P.Blob], reg):
    """Column type tags (C1..) written next to the columns on this plan."""
    tg = P.tags(sh, r"C\d{1,2}[A-Z]?", P.grow(reg, 40))
    if not tg or not bl:
        return
    pts = np.array([(b.cx, b.cy) for b in bl])
    tree = cKDTree(pts)
    for t in tg:
        d, i = tree.query((t.cx, t.cy))
        b = bl[i]
        reach = max(b.w, b.h) * 0.5 + 25
        if d <= reach and (not b.tag or d < getattr(b, "_td", 1e9)):
            b.tag, b.tag_src = t.s.strip(), "tag"
            b._td = d


def _schedule_consistency(A: Acc, sched: dict, floor_sheets: list[Sheet]):
    def norm(d):
        return {k: {kk: vv for kk, vv in v.items() if kk not in ("ref", "raw", "sheet")} for k, v in d.items()
                if not k.startswith("_")}
    for kind in ("beam", "slab"):
        base = None
        for sh in floor_sheets:
            d = norm(sched[kind].get(sh.n) or {})
            if not d:
                A.flag(f"struct:{kind}-schedule-missing:{sheet_label(sh)}", "missing", "schedule",
                       f"{sheet_label(sh)}: {kind} schedule not found", "Sizes/bars for this floor are unknown.")
                continue
            if base is None:
                base = (sh, d)
                continue
            diff = [k for k in set(d) | set(base[1]) if d.get(k) != base[1].get(k)]
            if diff:
                A.flag(f"struct:{kind}-schedule-differs:{sheet_label(sh)}", "warning", "schedule",
                       f"{sheet_label(sh)}: {kind} schedule differs from {sheet_label(base[0])} for {', '.join(sorted(diff))}",
                       "Each floor is measured with its own sheet's schedule.")


def _foundation_mode(A: Acc, pile_sh, raft_sh, region, calib, notes) -> str:
    if not (pile_sh and raft_sh):
        return "piles_only" if pile_sh else "raft_only" if raft_sh else "none"
    fid = "struct:foundation:pile-vs-raft"
    piles_under = bool(raft_sh.full(r"PILES?"))
    detail = (f"{sheet_label(pile_sh)} ({pile_sh.title.get('title')}) shows a pile layout over the building footprint; "
              f"{sheet_label(raft_sh)} ({raft_sh.title.get('title')}) shows a raft over the same footprint. ")
    if piles_under:
        detail += (f"{sheet_label(raft_sh)}'s raft section labels PILE under the raft, so the set reads as a piled raft "
                   "and both piles and raft are measured. ")
    cut = notes.get("pile_cutoff_below_gl")
    if cut:
        detail += f"Note 20: pile cut-off {cut} mm below GL. "
    A.flag(fid, "warning", "ambiguous", f"Pile plan ({sheet_label(pile_sh)}) vs raft ({sheet_label(raft_sh)}): which applies where?",
           detail + "Confirm with the structural engineer. Answer 'piled_raft', 'piles_only' or 'raft_only'.",
           [ref("plan", sheet_label(pile_sh), [region.get(pile_sh.n, pile_sh.bbox)], pile_sh.n),
            ref("plan", sheet_label(raft_sh), [region.get(raft_sh.n, raft_sh.bbox)], raft_sh.n)],
           input={"type": "text", "label": "Foundation system", "placeholder": "piled_raft | piles_only | raft_only"},
           affects=["foundation"])
    v = A.answer(fid)
    if v in ("piled_raft", "piles_only", "raft_only"):
        return v
    return "piled_raft" if piles_under else "piled_raft"


# ---------------------------------------------------------------------- piles

def _piles(A: Acc, sh: Sheet, pd: dict, notes: dict, region, calib, Ld, cover, meta):
    reg = region.get(sh.n)
    if not reg:
        return
    col_layers = {c.layer for c in sh.circles if c.layer in P.column_layers(sh)} - {None}
    circles, r = P.pile_circles(sh, P.grow(reg, 20))
    lab = sheet_label(sh)
    if not circles:
        A.flag("struct:piles:none", "missing", "missing-data", f"{lab}: no pile marks found on the plan",
               input={"type": "number", "label": "Number of piles", "unit": "nos"})
        return
    n = len(circles)
    meta["counts"]["piles"] = {"sheet": lab, "count": n, "radius_pt": r}
    cal = calib.get(sh.n)
    if cal and cal.pt_per_mm and pd.get("dia"):
        drawn = 2 * r / cal.pt_per_mm
        meta["counts"]["piles"]["drawn_dia_mm"] = round(drawn)
        if abs(drawn - pd["dia"]) > 0.1 * pd["dia"]:
            A.flag("struct:piles:drawn-dia", "warning", "consistency",
                   f"Pile marks scale to Ø{drawn:.0f} mm, detail says Ø{pd['dia']}",
                   "The circles counted may not be the piles.")
    pref = [ref("pile", f"P {i + 1}", [c], sh.n) for i, c in enumerate(circles)]
    allref = ref("piles", f"{n} piles", circles, sh.n)
    dia = pd.get("dia")
    L = pd.get("length")
    cut = pd.get("cutoff_below_gl") or notes.get("pile_cutoff_below_gl")
    tag = pd.get("tag", "P")
    missing = []
    for key, label in (("dia", "Pile diameter"), ("length", "Pile length below cut-off")):
        if not pd.get(key):
            fid = f"struct:piles:{key}"
            v = A.answer(fid)
            A.flag(fid, "missing", "missing-data", f"{label} not found in the pile detail",
                   input={"type": "number", "label": label, "unit": "mm"})
            if v:
                pd[key] = float(v)
            else:
                missing.append(key)
    dia, L = pd.get("dia"), pd.get("length")
    if missing:
        return
    if notes.get("lift_pile_note"):
        A.flag("struct:piles:lift-cutoff", "warning", "ambiguous", "Lift-area piles have a different cut-off",
               f"Note: '{notes['lift_pile_note']}'. The lift-well level is not written, so lift piles are measured "
               "like the others (same cut-off and length as the pile detail).", [allref],
               input={"type": "number", "label": "Number of piles under the lift well", "unit": "nos"})
    rem = f"{tag} Ø{dia:.0f}, {L / 1000:g} m below cut-off ({lab} detail); {n} pile marks counted on {lab} plan"
    trade = "EARTHWORK"
    if cut:
        A.row(trade, f"Boring for {dia:.0f} mm dia bored cast-in-situ under-reamed piles (GL to toe)", f"{lab} pile plan",
              n, (cut + L) / 1000, None, None, "m",
              f"{rem}; GL to toe = cut-off {cut} + {L:.0f} mm", "ok", [allref] + pref[:40], plan=lab)
    else:
        A.flag("struct:piles:cutoff", "missing", "missing-data", "Pile cut-off level not written",
               input={"type": "number", "label": "Cut-off depth below GL", "unit": "mm"})
    area = math.pi / 4 * (dia / 1000) ** 2
    A.row("RCC", f"RCC M25 in piles", f"{tag} shaft, {lab}", n, L / 1000, round(area, 5), None, "m³",
          f"{rem}; B = π/4·d²", "ok", [allref], plan=lab)
    nb = pd.get("bulbs")
    bd = pd.get("bulb_dia")
    shape = D.bulb_shape(sh, pd) if bd else None
    fid = "struct:piles:bulb"
    if nb and bd and shape:
        v1 = D.bulb_volume_m3(dia, bd, shape["h_top"], shape["h_bot"])
        uv = A.answer(fid)
        st = "assumed"
        if uv:
            v1, st = float(uv), "ok"
        A.flag(fid, "warning", "assumed", f"Under-ream bulb shape scaled from the pile detail",
               f"Bulb Ø{bd} and shaft Ø{dia:.0f} are written; the cone heights ({shape['h_top']} mm above, "
               f"{shape['h_bot']} mm below the widest point) are scaled from the detail using those two widths. "
               f"Extra concrete per bulb {v1:.4f} m³.", pd.get("refs"),
               input={"type": "number", "label": "Extra concrete per bulb", "unit": "m³"})
        A.row("RCC", f"RCC M25 in piles", f"{tag} under-ream bulbs ({nb} per pile, Ø{bd})", n * nb, None, None, None, "m³",
              f"{v1:.4f} m³ per bulb; " + (SCALED if st == "assumed" else "per your answer"), st, [allref],
              [fid], plan=lab, qty=n * nb * v1)
    else:
        A.flag(fid, "missing", "missing-data", "Under-ream bulb size not found",
               "Bulb diameter/number not written in the pile detail; bulbs are not included.", pd.get("refs"),
               input={"type": "number", "label": "Extra concrete per bulb", "unit": "m³"})
    # steel
    c, cnote = cover("PILE", "FOOTING")
    if "PILE" not in (notes.get("covers") or {}):
        A.flag("struct:piles:cover", "warning", "assumed", "Pile cover not in the notes",
               f"Ring length uses the footing cover ({c} mm) from the notes.",
               input={"type": "number", "label": "Pile cover", "unit": "mm"})
        v = A.answer("struct:piles:cover")
        if v:
            c = float(v)
    for bs in pd.get("long", []):
        ld, ln = Ld(bs["dia"])
        st = "assumed"
        length = L / 1000 + (ld or 0)
        A.bars("Piles", f"{tag} longitudinal {bs['n']} nos × {n} piles", bs["dia"], bs["n"] * n, length, st,
               f"{L / 1000:g} m + anchorage into raft/cap {ln}" + ("" if ld else " (missing, not added)"),
               [allref], plan=lab)
    rg = pd.get("rings")
    if rg and c is not None:
        nr = math.floor(L / rg["s"]) + 1
        dcore = dia - 2 * c - rg["dia"]
        lr = (math.pi * dcore + 2 * 10 * rg["dia"]) / 1000
        A.bars("Piles", f"{tag} rings @{rg['s']} ({nr} per pile × {n})", rg["dia"], nr * n, lr, "assumed",
               f"ring Ø{dcore:.0f} c/l + 2×10d hooks; {cnote}", [allref], plan=lab)


# ---------------------------------------------------------------------- footings

def _footings(A: Acc, sh: Sheet, fs: dict, region, Ld, cover, pcc_mix, meta):
    lab = sheet_label(sh)
    c, cnote = cover("FOOTING")
    dmin = None
    m = sh.find(r"D\s*=\s*\d{3,5}\s*(MM)?\s*MIN")
    if m:
        dmin = int(re.search(r"(\d{3,5})", m[0].s).group(1))
    pcc = (D.find_value_near(sh, r"P\.?\s*C\.?\s*C\.?\s*\(M-?\d+\)", r"(\d{2,3})", 90)
           or D.find_value_near(sh, r"P\.?C\.?C\.?", r"(\d{2,3})", 90))
    pcc_t = pcc[0] if pcc and 50 <= pcc[0] <= 200 else None
    if m and pcc_t is None:
        A.flag("struct:footing:pcc", "missing", "missing-data", "PCC thickness under footings not found",
               input={"type": "number", "label": "PCC thickness", "unit": "mm"})
    if sh.find(r"P\.?C\.?C\.?\s*\(M-?10\)") and pcc_mix:
        A.flag("struct:footing:pcc-mix", "warning", "consistency", "PCC mix: footing section says M-10, notes say "
               f"lean concrete {pcc_mix}", "Measured under the notes' mix; check which applies.")
    for tag, f in fs.items():
        if tag.startswith("_"):
            continue
        tg = [t for t in P.tags(sh, re.escape(tag)) if not P.inside(t.cx, t.cy, P.grow(tuple(fs["_table"].bbox), 2))]
        n = len(tg)
        fr = [ref("footing", tag, [t], sh.n) for t in tg]
        meta["counts"][tag] = n
        if n == 0:
            A.flag(f"struct:footing:{tag}:count", "missing", "schedule", f"{tag} in schedule but not found on the plan",
                   input={"type": "number", "label": f"Number of {tag}", "unit": "nos"}, refs=[f["ref"]])
            v = A.answer(f"struct:footing:{tag}:count")
            if not v:
                continue
            n = int(v)
        if not f.get("size"):
            continue
        a, b = f["size"]
        d1, d2 = f.get("d1"), f.get("d2")
        rem = f"{tag} {a}×{b} (footing schedule, {lab})"
        if dmin:
            A.row("EARTHWORK", "Earthwork in excavation for foundations", f"{tag} ×{n}", n, b / 1000, a / 1000,
                  dmin / 1000, "m³", f"{rem}; depth D={dmin} MIN to bottom of PCC (section B-B); no working space added",
                  "assumed", fr, plan=lab)
        else:
            A.flag(f"struct:footing:{tag}:depth", "missing", "missing-data", f"{tag}: founding depth not written",
                   input={"type": "number", "label": "Depth below GL to bottom of PCC", "unit": "mm"})
        if pcc_t:
            A.row("PCC", f"PCC {pcc_mix or ''} below footings and raft".replace("  ", " "), f"{tag} ×{n}", n, b / 1000,
                  a / 1000, pcc_t / 1000, "m³", f"{rem}; PCC {pcc_t} thick, same plan size as footing", "ok", fr, plan=lab)
        if d1:
            A.row("RCC", "RCC M25 in footings", f"{tag} base (D1) ×{n}", n, b / 1000, a / 1000, d1 / 1000, "m³",
                  f"{rem}; D1 = {d1:.0f}", "ok", fr, plan=lab)
        if d1 and d2 and f.get("pedestal"):
            pa, pb = f["pedestal"]
            A1, A2 = a * b / 1e6, pa * pb / 1e6
            v = d2 / 1000 / 3 * (A1 + A2 + math.sqrt(A1 * A2))
            A.row("RCC", "RCC M25 in footings", f"{tag} sloped part (D2, frustum to pedestal {pa}×{pb}) ×{n}", n, None,
                  None, None, "m³", f"frustum: D2/3·(A1+A2+√(A1A2)), D2 = {d2:.0f}", "ok", fr, plan=lab, qty=n * v)
            if dmin and pcc_t:
                hp = dmin - (pcc_t or 0) - d1 - d2
                A.row("RCC", "RCC M25 in pedestals", f"{tag} pedestal {pa}×{pb} ×{n}", n, pb / 1000, pa / 1000, hp / 1000,
                      "m³", f"height = D {dmin} MIN − PCC − D1 − D2 = {hp:.0f} (top taken at GL)", "assumed", fr, plan=lab)
                A.flag(f"struct:footing:{tag}:pedestal-top", "warning", "assumed",
                       f"{tag}: pedestal top level not written", "Taken at ground level (section B-B shows it at GL).",
                       fr[:1], input={"type": "number", "label": "Pedestal height", "unit": "mm"})
        # mesh
        for key, along, across in (("ast1", b, a), ("ast2", a, b)):
            sp = f.get(key)
            if not sp or c is None or not d1:
                continue
            nb = math.floor((across - 2 * c) / sp["s"]) + 1
            lb = (along - 2 * c + 2 * (d1 - 2 * c)) / 1000
            A.bars("Footings", f"{tag} {key.upper()} #{sp['dia']}@{sp['s']} ({nb} bars × {n})", sp["dia"], nb * n, lb, "assumed",
                   f"bar = {along}−2c + 2 end bends (D1−2c); {cnote}", fr, plan=lab)


# ---------------------------------------------------------------------- raft

def _raft(A: Acc, sh: Sheet, region, calib, pcc_mix, meta):
    lab = sheet_label(sh)
    thk_t = sh.find(r"RAFT\s*TH(?:ICK|K)?\.?\s*(?:NESS)?\s*\d{3,4}")
    if not thk_t:
        A.flag("struct:raft:thk", "missing", "missing-data", "Raft thickness not written",
               input={"type": "number", "label": "Raft thickness", "unit": "mm"})
        return
    thk = int(re.search(r"(\d{3,4})", thk_t[0].s).group(1))
    reg = region.get(sh.n)
    cal = calib.get(sh.n)
    area, outline = _outline_area(sh, reg) if reg else (None, None)
    if not area or not cal or not cal.pt_per_mm:
        A.flag("struct:raft:area", "missing", "missing-data", "Raft outline/area not measurable",
               input={"type": "number", "label": "Raft plan area", "unit": "m²"})
        v = A.answer("struct:raft:area")
        if not v:
            return
        a_m2, st = float(v), "ok"
    else:
        a_m2 = area / cal.pt_per_mm ** 2 / 1e6
        st = "assumed"
    rr = [ref("raft", "raft outline", [outline or reg], sh.n), ref("note", "raft thickness", thk_t[:1], sh.n)]
    meta["counts"]["raft_area_m2"] = round(a_m2, 2)
    rem = f"plan area {a_m2:.2f} m² " + (SCALED if st == "assumed" else "(your value)")
    # section chain (depth to PCC bottom, PCC thickness)
    sec = sh.find(r"SECTION.*RAFT|RAFT.*SECTION|SECTION\s+AT")
    depth = pcc_t = None
    if sec:
        s0 = max(sec, key=lambda t: t.cy)
        regn = (sh.bbox[0], s0.cy, sh.bbox[2] - 0.2 * (sh.bbox[2] - sh.bbox[0]), s0.cy + 0.45 * (sh.bbox[3] - sh.bbox[1]))
        chain = D.vertical_chain(sh, regn)
        vals = [int(d["value_mm"]) for d in chain]
        allv = [int(d["value_mm"]) for d in C.dim_samples(sh, regn)]
        if 100 in allv or any(v <= 150 for v in allv if v != 75):
            pcc_t = min(v for v in allv if 50 <= v <= 150 and v != 75)
        if vals:
            depth = sum(vals)
            meta["counts"]["raft_section_chain"] = vals
    A.row("RCC", "RCC M25 in raft", f"Raft {thk} thick ({lab})", 1, None, round(a_m2, 3), thk / 1000, "m³",
          f"{rem}; thickness written ('{thk_t[0].s}')", st, rr, plan=lab)
    if pcc_t:
        A.row("PCC", f"PCC {pcc_mix or ''} below footings and raft".replace("  ", " "), f"under raft ({lab})", 1, None,
              round(a_m2, 3), pcc_t / 1000, "m³", f"{rem}; PCC {pcc_t} (section)", st, rr, plan=lab)
    else:
        A.flag("struct:raft:pcc", "missing", "missing-data", "PCC thickness under the raft not found",
               input={"type": "number", "label": "PCC thickness", "unit": "mm"})
    if depth:
        A.row("EARTHWORK", "Earthwork in excavation for foundations", f"Raft area ({lab})", 1, None, round(a_m2, 3),
              depth / 1000, "m³", f"{rem}; depth {depth} below GL to PCC bottom (section dims {meta['counts'].get('raft_section_chain')}); "
              "no working space/slopes", st, rr, plan=lab)
    else:
        A.flag("struct:raft:depth", "missing", "missing-data", "Raft founding depth not written",
               input={"type": "number", "label": "Excavation depth", "unit": "mm"})
    A.flag("struct:raft:steel", "missing", "missing-data", "Raft reinforcement not measured",
           "The raft section may give bar marks (top and bottom mesh) but not "
           "extra bars/laps over the whole raft; enter steel if needed.", rr,
           input={"type": "number", "label": "Raft steel", "unit": "kg"})
    v = A.answer("struct:raft:steel")
    if v:
        A.row("STEEL", "Steel reinforcement (TMT) – Raft", "raft (your value)", 1, None, None, None, "kg", "", "ok", rr,
              qty=float(v))


def _outline_area(sh: Sheet, reg):
    """Area enclosed by the plan's outer boundary: the layer carrying the longest lines
    around the column cluster, rasterised and filled."""
    big = P.grow(reg, 120)
    segs = [s for s in sh.segs if P.inside(s.x1, s.y1, big) and P.inside(s.x2, s.y2, big) and (s.horiz or s.vert)]
    if not segs:
        return None, None
    tot = Counter()
    for s in segs:
        if s.length > 0.25 * (reg[2] - reg[0]):
            tot[s.layer] += s.length
    if not tot:
        return None, None
    lay = tot.most_common(1)[0][0]
    ls = [s for s in segs if s.layer == lay and s.length > 3]
    from scipy import ndimage
    x0, y0, x1, y1 = P.grow(reg, 125)
    k = 1.0
    W, H = int((x1 - x0) * k) + 2, int((y1 - y0) * k) + 2
    wall = np.zeros((H, W), bool)
    for s in ls:
        n = int(max(abs(s.x2 - s.x1), abs(s.y2 - s.y1)) * k * 2) + 2
        xs = np.linspace(s.x1, s.x2, n)
        ys = np.linspace(s.y1, s.y2, n)
        wall[np.clip(((y1 - ys) * k).astype(int), 0, H - 1), np.clip(((xs - x0) * k).astype(int), 0, W - 1)] = True
    wall = ndimage.binary_dilation(wall, iterations=1)
    lab_, _ = ndimage.label(~wall)
    outside = set(np.unique(np.concatenate([lab_[0], lab_[-1], lab_[:, 0], lab_[:, -1]])).tolist())
    inside = ~np.isin(lab_, list(outside))
    area_px = inside.sum() - 0.5 * wall.sum() * 0  # boundary pixels counted inside (line centre ≈ edge)
    if area_px < 0.2 * (reg[2] - reg[0]) * (reg[3] - reg[1]) * k * k:
        return None, None
    rows, cols = np.nonzero(inside)
    bb = (x0 + cols.min() / k, y1 - rows.max() / k, x0 + cols.max() / k, y1 - rows.min() / k)
    return float(area_px) / k / k, bb


# ---------------------------------------------------------------------- columns

def _register(src: list[P.Blob], dst: list[P.Blob], s: float, tol: float) -> dict[int, int]:
    """Match columns of a tagged plan (src) to another plan (dst): scale s known, translation
    found by voting. Returns dst index -> src index."""
    if not src or not dst:
        return {}
    S = np.array([(b.cx, b.cy) for b in src]) * s
    Dp = np.array([(b.cx, b.cy) for b in dst])
    tree = cKDTree(S)
    order = np.argsort(Dp[:, 0] + Dp[:, 1])[:6]
    best, score = None, -1
    for i in order:
        for j in range(len(S)):
            t = Dp[i] - S[j]
            d, _ = tree.query(Dp - t)
            sc = int((d <= tol).sum())
            if sc > score:
                best, score = t, sc
    d, idx = tree.query(Dp - best)
    return {i: int(idx[i]) for i in range(len(dst)) if d[i] <= tol}


def _columns(A: Acc, base_sh, floor_sheets, blobs, region, calib, column, Ld, cover, meta, notes):
    if not column:
        A.flag("struct:columns:schedule", "missing", "schedule", "Column schedule not found")
        return
    base = blobs.get(base_sh.n) if base_sh else None
    tagged = [b for b in (base or []) if b.tag]
    meta["counts"]["columns_by_tag"] = dict(Counter(b.tag for b in tagged))
    if base_sh and tagged:
        on_plan = set(b.tag for b in tagged)
        sch = {k for k in column if not k.startswith("_")}
        for t in sorted(on_plan - sch):
            A.flag(f"struct:columns:tag-not-in-schedule:{t}", "error", "schedule", f"Column {t} on plan but not in schedule",
                   refs=[ref("column", t, [b for b in tagged if b.tag == t], base_sh.n)])
        for t in sorted(sch - on_plan):
            A.flag(f"struct:columns:schedule-not-on-plan:{t}", "warning", "schedule", f"Column {t} in schedule but not tagged on plan",
                   refs=[column[t]["ref"]])
    c, cnote = cover("COLUMN")
    for sh in floor_sheets:
        if sh.n not in blobs:
            continue
        fs = sh.title.get("floor_scope") or {}
        mult = int(fs.get("multiplier") or 1)
        fg, fo = _floor_group(sh), float(fs.get("from", 0))
        lab = sheet_label(sh)
        dst = blobs[sh.n]
        n_tagged = 0
        if tagged and calib.get(sh.n) and calib.get(base_sh.n) and calib[sh.n].pt_per_mm and calib[base_sh.n].pt_per_mm:
            s = calib[sh.n].pt_per_mm / calib[base_sh.n].pt_per_mm
            tol = 300 * calib[sh.n].pt_per_mm
            mp = _register(tagged, dst, s, tol)
            for i, j in mp.items():
                dst[i].tag, dst[i].tag_src = tagged[j].tag, f"position on {sheet_label(base_sh)}"
            n_tagged = len(mp)
        # size check / fallback by size
        cal = calib.get(sh.n)
        for b in dst:
            if cal and cal.pt_per_mm:
                dims = sorted((round(b.w / cal.pt_per_mm), round(b.h / cal.pt_per_mm)))
                b._mm = dims
                if not b.tag:
                    hits = [k for k, v in column.items() if not k.startswith("_")
                            and _col_size_match(v, dims)]
                    if len(hits) == 1:
                        b.tag, b.tag_src = hits[0], "size"
                elif b.tag in column and not _col_size_match(column[b.tag], dims):
                    A.flag(f"struct:columns:size:{lab}:{b.tag}", "warning", "consistency",
                           f"{lab}: column matched to {b.tag} is drawn {dims[0]}×{dims[1]}",
                           refs=[ref("column", b.tag, [b], sh.n)])
        cnt = Counter(b.tag or "?" for b in dst)
        meta["counts"].setdefault("columns_per_floor", {})[lab] = dict(cnt)
        if base and len(dst) != len(base):
            A.flag(f"struct:columns:count:{lab}", "warning", "consistency",
                   f"{lab}: {len(dst)} columns on plan vs {len(base)} on {sheet_label(base_sh)}",
                   "Columns per floor are counted on each floor's own plan.",
                   [ref("plan", lab, [region[sh.n]], sh.n)])
        if cnt.get("?"):
            A.flag(f"struct:columns:untyped:{lab}", "missing", "schedule",
                   f"{lab}: {cnt['?']} columns could not be typed (no tag, size not unique)",
                   refs=[ref("column", "?", [b for b in dst if not b.tag], sh.n)],
                   input={"type": "text", "label": "Column type for these", "placeholder": "C5"})
        # height
        fid = f"struct:columns:height:{lab}"
        h = A.answer(fid) or (A.cfg.get("floor_heights_mm") or {}).get(lab)
        A.flag(fid, "missing", "missing-data", f"{lab}: column height (floor-to-floor below this slab) not written",
               "No levels are written on the structural set, so column volumes and bars need this height.",
               [ref("plan", lab, [region[sh.n]], sh.n)],
               input={"type": "number", "label": f"Column height below {fs.get('label', lab)} slab", "unit": "mm"},
               affects=[f"columns:{lab}"])
        for tag in sorted(t for t in cnt if t != "?"):
            spec = column.get(tag)
            bs = [b for b in dst if b.tag == tag]
            rf = [ref("column", f"{tag} ×{len(bs)}", bs, sh.n)]
            src = Counter(b.tag_src for b in bs)
            if not spec:
                continue
            a, bb_ = (spec["size"] or (spec["dia"], spec["dia"]))
            area = (math.pi / 4 * (spec["dia"] / 1000) ** 2) if spec.get("dia") else a * bb_ / 1e6
            rem = f"{tag} {('Ø' + str(spec['dia'])) if spec.get('dia') else f'{a}×{bb_}'}; {len(bs)} on {lab} plan, typed by {dict(src)}"
            if not h:
                A.row("RCC", "RCC M25 in columns", f"{tag} – {fs.get('label', lab)}", len(bs) * mult, None, round(area, 4),
                      None, "m³", rem + "; HEIGHT MISSING – qty 0 until answered", "unresolved", rf, [fid], fg, fo, lab, qty=0.0)
                continue
            hm = float(h) / 1000
            A.row("RCC", "RCC M25 in columns", f"{tag} – {fs.get('label', lab)}", len(bs) * mult, None, round(area, 4), hm,
                  "m³", rem + "; height from your answer", "ok", rf, [fid], fg, fo, lab)
            for st_ in spec.get("long", []):
                ld, ln = Ld(st_["dia"])
                A.bars("Columns", f"{tag} longitudinal {st_['n']} nos × {len(bs) * mult}", st_["dia"], st_["n"] * len(bs) * mult,
                       hm + (ld or 0), "assumed", f"height + one lap Ld ({ln}, note 11)", rf, fg, fo, [fid], lab)
            te, tm = spec.get("ties_end"), spec.get("ties_mid")
            if te and tm and c is not None:
                lo = max(max(a, bb_), float(h) / 6, 450)
                ne = 2 * (math.floor(lo / te["s"]) + 1)
                nm = max(0, math.ceil((float(h) - 2 * lo) / tm["s"]))
                if spec.get("dia"):
                    lt = math.pi * (spec["dia"] - 2 * c - te["dia"]) + 20 * te["dia"]
                else:
                    lt = 2 * ((a - 2 * c) + (bb_ - 2 * c)) + 20 * te["dia"]
                A.bars("Columns", f"{tag} ties {ne}@{te['s']} end + {nm}@{tm['s']} mid × {len(bs) * mult}", te["dia"],
                       (ne + nm) * len(bs) * mult, lt / 1000, "assumed",
                       f"end region max(D, H/6, 450) = {lo:.0f} each end (IS 13920); outer tie only, cross-ties not counted; {cnote}",
                       rf, fg, fo, [fid], lab)
    if any(r.get("text") == "-" for v in column.values() if isinstance(v, dict) for r in v.get("long_raw", [])[1:]):
        A.flag("struct:columns:upper-bars", "warning", "ambiguous",
               "Column bars '2ND FLOOR SLAB TO TERRACE' are '-' in the schedule",
               "Read as 'same as below' (bars from the first range used for all floors). Confirm.",
               [next(v["ref"] for k, v in column.items() if not k.startswith("_"))])


def _footing_column_check(A: Acc, footing: dict, column: dict, meta: dict, base_sh):
    """Footing schedule 'n X (a X b)' columns per footing vs columns of that size tagged on the plan."""
    tagged = meta["counts"].get("columns_by_tag") or {}
    for tag, f in footing.items():
        if tag.startswith("_") or not f.get("columns", {}).get("size"):
            continue
        n_f = meta["counts"].get(tag) or 0
        want = n_f * f["columns"]["n"]
        types = [k for k, v in column.items() if not k.startswith("_") and v.get("size")
                 and sorted(v["size"]) == sorted(f["columns"]["size"])]
        have = sum(tagged.get(t, 0) for t in types)
        meta["counts"].setdefault("footing_columns", {})[tag] = {"expected": want, "tagged": have, "types": types}
        if types and want != have:
            A.flag(f"struct:footing:{tag}:columns", "warning", "consistency",
                   f"{n_f} × {tag} carry {want} columns of {f['columns']['size']}, plan tags show {have} ({', '.join(types)})",
                   refs=[f["ref"]])


def _col_size_match(spec: dict, dims: list[int], tol: float = 0.06) -> bool:
    if spec.get("dia"):
        return all(abs(d - spec["dia"]) <= tol * spec["dia"] + 15 for d in dims)
    if not spec.get("size"):
        return False
    a, b = sorted(spec["size"])
    return abs(dims[0] - a) <= tol * a + 15 and abs(dims[1] - b) <= tol * b + 15


# ---------------------------------------------------------------------- beams

def _beams(A: Acc, sh: Sheet, sched: dict, bl, reg, cal, Ld, cover, meta, kind: str, floor_group: str,
           floor_order: float, mult: int, slab_sched: dict | None = None):
    lab = sheet_label(sh)
    if not cal or not cal.pt_per_mm:
        return
    r = cal.pt_per_mm
    specs = {k: v for k, v in sched.items() if not k.startswith("_")}
    widths = [v["b"] for v in specs.values() if v.get("b")] or [230, 300]
    lays = P.beam_layers(sh)
    segs = [s for s in sh.segs if s.layer in lays and P.inside(s.x1, s.y1, P.grow(reg, 60))]
    lines, gap = P.merge_lines(segs)
    strips = P.beam_strips(lines, bl, 0.75 * min(widths) * r, 1.25 * max(widths) * r, 150 * r)
    tg = P.tags(sh, r"B\d{1,2}[A-Z]?'?", P.grow(reg, 60))
    orphans = P.assign_beam_tags(strips, tg, 900 * r)
    meta.setdefault("beams", {})[lab] = {"strips": len(strips), "tags": len(tg), "orphan_tags": len(orphans),
                                         "merge_gap_pt": round(gap, 2)}
    trade_item = "RCC M25 in plinth beams" if kind == "plinth" else "RCC M25 in beams"
    cov, cnote = cover("TIE BEAM", "PLINTH BEAM", "FLOOR BEAM", "BEAM") if kind == "plinth" else cover("FLOOR BEAM", "BEAM")
    on_plan = {s.tag for s in strips if s.tag} | {t.s.strip() for t in tg}
    for t in sorted(on_plan - set(specs)):
        A.flag(f"struct:beams:not-in-schedule:{lab}:{t}", "error", "schedule", f"{lab}: beam {t} on plan but not in the schedule",
               refs=[ref("tag", t, [x for x in tg if x.s.strip() == t], sh.n)],
               input={"type": "text", "label": f"{t} size b×D", "unit": "mm", "placeholder": "230x450"})
    for t in sorted(set(specs) - on_plan):
        A.flag(f"struct:beams:not-on-plan:{lab}:{t}", "warning", "schedule", f"{lab}: beam {t} in the schedule but not tagged on the plan",
               refs=[specs[t]["ref"]])
    for t in orphans:
        A.flag(f"struct:beams:orphan-tag:{lab}:{t.s.strip()}:{round(t.cx)}", "warning", "schedule",
               f"{lab}: tag {t.s.strip()} is not next to any beam line", refs=[ref("tag", t.s, [t], sh.n)])
    untagged = [s for s in strips if not s.tag]
    if untagged:
        tot = sum(s.length for s in untagged) / r / 1000
        fid = f"struct:beams:untagged:{lab}"
        A.flag(fid, "missing", "schedule", f"{lab}: {len(untagged)} beam spans ({tot:.1f} m, scaled) carry no tag",
               "No tag on the span and none on the same beam line, so the size is unknown; not measured.",
               [ref("beam", f"untagged {i + 1}", [s.bb], sh.n) for i, s in enumerate(untagged)],
               input={"type": "text", "label": "Size b×D for these spans", "unit": "mm", "placeholder": "230x450"})
        v = A.answer(fid)
        if v and T.size2(str(v)):
            b_, d_ = T.size2(str(v))
            for s in untagged:
                s.tag, s.tag_src = "untagged", "answer"
            specs = dict(specs)
            specs["untagged"] = {"b": b_, "d": d_, "tag": "untagged", "ref": None}
    inh = [s for s in strips if s.tag_src == "inherited"]
    if inh:
        A.flag(f"struct:beams:inherited:{lab}", "warning", "assumed",
               f"{lab}: {len(inh)} beam spans take the tag of the next span on the same line",
               "Only one span of a continuous beam line is tagged; the others are assumed to be the same beam.",
               [ref("beam", f"{s.tag} (inherited)", [s.bb], sh.n) for s in inh])
    by_tag: dict[str, list[P.Strip]] = defaultdict(list)
    for s in strips:
        if s.tag and s.tag in specs:
            by_tag[s.tag].append(s)
    for tag in sorted(by_tag, key=lambda t: (len(t), t)):
        spec = specs[tag]
        ss = by_tag[tag]
        b_, d_ = spec.get("b"), spec.get("d")
        if not b_ or not d_:
            continue
        L = sum(s.length for s in ss) / r / 1000
        rf = [ref("beam", f"{tag} span {i + 1}: {s.length / r / 1000:.2f} m", [s.bb], sh.n) for i, s in enumerate(ss)]
        # drawn width check
        wd = np.median([s.width_pt for s in ss]) / r
        if abs(wd - b_) > 0.2 * b_:
            A.flag(f"struct:beams:width:{lab}:{tag}", "warning", "consistency",
                   f"{lab}: {tag} drawn {wd:.0f} wide, schedule says {b_:.0f}", refs=rf[:5])
        src = Counter(s.tag_src for s in ss)
        A.row("RCC", trade_item, f"{tag} {b_:.0f}×{d_:.0f} – {len(ss)} spans ({lab})", mult, round(L, 3), b_ / 1000, d_ / 1000,
              "m³", f"L = Σ clear spans {SCALED}; tags {dict(src)}", "assumed", rf, [], floor_group, floor_order, lab)
        _beam_steel(A, tag, spec, ss, r, mult, Ld, cov, cnote, kind, floor_group, floor_order, lab, rf)


def _beam_steel(A, tag, spec, ss, r, mult, Ld, cov, cnote, kind, fg, fo, lab, rf):
    if cov is None:
        return
    b_, d_ = spec["b"], spec["d"]
    spans = [s.length / r for s in ss]                 # mm
    if kind == "plinth":
        thru = [("bottom thru", spec.get("BST1")), ("top thru", spec.get("TST1"))]
        sup = [("top extra at supports", spec.get("TST2"))]
        mid = [("bottom extra", spec.get("BST2"))]
    else:
        thru = [("top thru", spec.get("T1")), ("bottom thru", spec.get("B1"))]
        sup = [("top extra L 1st layer", spec.get("T2")), ("top extra R 1st layer", spec.get("T3")),
               ("top extra L 2nd layer", spec.get("T4")), ("top extra R 2nd layer", spec.get("T5"))]
        mid = [("bottom extra mid-span", spec.get("B2"))]
    for name, sets in thru:
        for bs in sets or []:
            ld, ln = Ld(bs["dia"])
            if ld is None:
                continue
            tot = sum(L / 1000 + 2 * ld + math.floor(L / 12000) * ld for L in spans)
            A.bars("Plinth beams" if kind == "plinth" else "Beams", f"{tag} {name} {bs['n']}×#{bs['dia']}", bs["dia"],
                   bs["n"] * mult, tot, "assumed", f"Σ(span + 2Ld + laps), Ld {ld * 1000:.0f} ({ln}); {SCALED}", rf, fg, fo, plan=lab)
    for name, sets in sup:
        for bs in sets or []:
            ld, ln = Ld(bs["dia"])
            if ld is None:
                continue
            per_end = lambda L: max(ld, 0.25 * L / 1000) + ld
            ends = 2 if kind == "plinth" else 1          # floor schedule lists L and R separately
            tot = sum(ends * per_end(L) for L in spans)
            A.bars("Plinth beams" if kind == "plinth" else "Beams", f"{tag} {name} {bs['n']}×#{bs['dia']}", bs["dia"],
                   bs["n"] * mult, tot, "assumed", f"per support max(Ld, 0.25L) + Ld; {SCALED}", rf, fg, fo, plan=lab)
    for name, sets in mid:
        for bs in sets or []:
            tot = sum(0.75 * L / 1000 for L in spans)
            A.bars("Plinth beams" if kind == "plinth" else "Beams", f"{tag} {name} {bs['n']}×#{bs['dia']}", bs["dia"],
                   bs["n"] * mult, tot, "assumed", f"0.75 L per span; {SCALED}", rf, fg, fo, plan=lab)
    s1, s2 = spec.get("S1"), spec.get("S2")
    if s1 and s2:
        n = 0
        for L in spans:
            ez = min(2 * d_, L / 2)
            n += 2 * (math.floor(ez / s1["s"]) + 1) + max(0, math.ceil((L - 2 * ez) / s2["s"]) - 1)
        ls = (2 * ((b_ - 2 * cov) + (d_ - 2 * cov)) + 20 * s1["dia"]) / 1000
        A.bars("Plinth beams" if kind == "plinth" else "Beams", f"{tag} stirrups #{s1['dia']}@{s1['s']}/{s2['s']}",
               s1["dia"], n * mult, ls, "assumed", f"end zones 2D @S1, middle @S2; {cnote}; {SCALED}", rf, fg, fo, plan=lab)


# ---------------------------------------------------------------------- slabs

def _slabs(A: Acc, sh: Sheet, sched: dict, bl, reg, cal, cover, meta, fg, fo, mult):
    lab = sheet_label(sh)
    if not cal or not cal.pt_per_mm or not sched:
        return
    r = cal.pt_per_mm
    specs = {k: v for k, v in sched.items() if not k.startswith("_")}
    lays = P.beam_layers(sh)
    segs = [s for s in sh.segs if s.layer in lays and P.inside(s.x1, s.y1, P.grow(reg, 60))]
    lines, _ = P.merge_lines(segs)
    st = P.tags(sh, r"S\d{1,2}[A-Z]?", P.grow(reg, 30))
    sk = [t for t in sh.find(r"SUNKEN") if P.inside(t.cx, t.cy, P.grow(reg, 30))]
    panels = P.slab_panels(reg, lines, bl, st, sk, max_area_pt2=150e6 * r * r)
    meta.setdefault("slabs", {})[lab] = {"tags": len(st), "panels": len(panels)}
    on_plan = {p.tag for p in panels} | {t.s.strip() for t in st}
    for t in sorted(on_plan - set(specs)):
        A.flag(f"struct:slabs:not-in-schedule:{lab}:{t}", "error", "schedule", f"{lab}: slab {t} on plan but not in the schedule",
               refs=[ref("tag", t, [x for x in st if x.s.strip() == t], sh.n)])
    for t in sorted(set(specs) - on_plan):
        A.flag(f"struct:slabs:not-on-plan:{lab}:{t}", "warning", "schedule", f"{lab}: slab {t} in the schedule but not on the plan",
               refs=[specs[t]["ref"]])
    leaks = [p for p in panels if p.leaked]
    if leaks:
        A.flag(f"struct:slabs:open-panel:{lab}", "error", "geometry",
               f"{lab}: {len(leaks)} slab panels are not closed by beams", "Excluded; the outline could not be closed.",
               [ref("slab", p.tag, [p.tag_txt], sh.n) for p in leaks],
               input={"type": "number", "label": "Total area of these panels", "unit": "m²"})
    dup = [p for p in panels if p.shared and len(set(p.shared + [p.tag])) > 1]
    for p in dup:
        A.flag(f"struct:slabs:two-tags:{lab}:{round(p.tag_txt.cx)}", "warning", "consistency",
               f"{lab}: one panel carries tags {sorted(set(p.shared + [p.tag]))}", refs=[ref("slab", p.tag, [p.tag_txt], sh.n)])
    if any(p.sunken for p in panels):
        A.flag(f"struct:slabs:sunken-depth", "missing", "missing-data", "Sunken slab depth not written",
               "Sunken panels are measured at the scheduled thickness; the drop and its side walls are not measured.",
               [ref("slab", "SUNKEN", sk, sh.n)], input={"type": "number", "label": "Sunken depth", "unit": "mm"})
    c, cnote = cover("SLAB")
    for p in panels:
        if p.leaked:
            continue
        spec = specs.get(p.tag)
        if not spec or not spec.get("thk"):
            continue
        area = p.area_pt2 / r / r / 1e6
        bw, bh = (p.bb[2] - p.bb[0]) / r / 1000, (p.bb[3] - p.bb[1]) / r / 1000
        item = "RCC M25 in sunken slabs" if p.sunken else "RCC M25 in slabs"
        rf = [ref("slab", f"{p.tag} {area:.2f} m²", [p.bb, p.tag_txt], sh.n)]
        rect = area >= 0.97 * bw * bh
        A.row("RCC", item, f"{p.tag} {spec['thk']:.0f} thk – panel {round(bw, 2)}×{round(bh, 2)} ({lab})", mult,
              round(bw, 3) if rect else None, round(bh, 3) if rect else round(area, 3), spec["thk"] / 1000, "m³",
              f"clear panel {area:.2f} m² {SCALED}" + ("" if rect else "; irregular: B = area"), "assumed", rf, [], fg, fo, lab,
              qty=None if rect else mult * area * spec["thk"] / 1000)
        if c is None:
            continue
        lx, ly = sorted((bw * 1000, bh * 1000))       # short, long (mm)
        # bottom: A parallel to short span (spaced along long), B parallel to long span
        for key, bar_len, spread in (("A", lx, ly), ("B", ly, lx)):
            sp = spec.get(key)
            if sp:
                n = math.floor(spread / sp["s"]) + 1
                A.bars("Slabs", f"{p.tag} bottom {key} #{sp['dia']}@{sp['s']}", sp["dia"], n * mult,
                       (bar_len + 2 * 125) / 1000, "assumed", f"clear span + 2×125 into supports; {SCALED}", rf, fg, fo, plan=lab)
        for key, along, edges in (("C", ly, 2), ("D", lx, 2)):
            sp = spec.get(key)
            if sp:
                n = edges * (math.floor(along / sp["s"]) + 1)
                A.bars("Slabs", f"{p.tag} top {key} #{sp['dia']}@{sp['s']}", sp["dia"], n * mult,
                       (0.3 * lx + 250) / 1000, "assumed", f"0.3·Lx from edge + 250 anchorage; {SCALED}", rf, fg, fo, plan=lab)
    if panels:
        A.assume("Slab distribution bars and chairs are not counted (not scheduled).")


def _floor_scope_checks(A: Acc, floor_sheets, notes):
    seen: dict[int, str] = {}
    for sh in floor_sheets:
        fs = sh.title.get("floor_scope")
        lab = sheet_label(sh)
        if not fs:
            A.flag(f"struct:floor-scope:{lab}", "missing", "missing-data", f"{lab}: which floor(s) this sheet covers is not clear",
                   input={"type": "number", "label": "Number of identical floors", "unit": "nos"})
            continue
        for f in range(fs["from"], fs["to"] + 1):
            if f in seen:
                A.flag(f"struct:floor-scope:overlap:{f}", "error", "consistency",
                       f"Floor {f} is covered by both {seen[f]} and {lab}")
            seen[f] = lab
        # plan titles inside the sheet that disagree with the title block
        for t in sh.texts:
            if t.size < 14:
                continue
            m = re.search(r"(\d+)\s*(?:ST|ND|RD|TH)\s*TO\s*(\d+)\s*(?:ST|ND|RD|TH)\s*FLOOR", t.s, re.I)
            if m and (int(m.group(1)), int(m.group(2))) != (fs["from"], fs["to"]):
                A.flag(f"struct:floor-scope:plan-title:{lab}", "warning", "ambiguous",
                       f"{lab}: plan title says '{t.s.strip()}' but the title block says '{sh.title.get('title')}'",
                       f"Multiplier ×{fs['multiplier']} taken from the title block; the plan title would give "
                       f"×{int(m.group(2)) - int(m.group(1)) + 1} and double-count floors covered by other sheets.",
                       [ref("title", t.s, [t], sh.n)])
    top = notes.get("storeys_above_ground")
    if top is not None and seen:
        want = set(range(0, top + 1))
        miss = sorted(want - set(seen))
        if miss:
            A.flag("struct:floor-scope:gaps", "warning", "missing-data",
                   f"No slab sheet for floor(s) {miss} (note: G+{top})")


# ---------------------------------------------------------------------- abstract / steel

def _abstract(rows: list[MBRow]) -> list[AbstractRow]:
    order = ["EARTHWORK", "PCC", "RCC", "SHUTTERING", "STEEL"]
    groups: dict[tuple, list[MBRow]] = {}
    for r in rows:
        groups.setdefault((r.trade, r.item, r.unit), []).append(r)
    out = []
    for (trade, item, unit), rs in sorted(groups.items(), key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order else 9)):
        q = round(sum(r.qty for r in rs), 3)
        st = Counter(r.status for r in rs)
        rem = ", ".join(f"{v} {k}" for k, v in st.items() if k != "ok")
        refs = []
        for r in rs:
            refs += r.refs[:2]
        out.append(AbstractRow(item=item, qty=q, unit=unit, trade=trade, remarks=rem, row_ids=[r.id for r in rs],
                               refs=refs[:40]))
    return out


def steel_summary(steel: list[dict]) -> dict:
    by_dia: dict[int, float] = defaultdict(float)
    by_el: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    assumed = 0.0
    for s in steel:
        by_dia[s["dia"]] += s["kg"]
        by_el[s["element"]][s["dia"]] += s["kg"]
        if s["status"] != "ok":
            assumed += s["kg"]
    tot = sum(by_dia.values())
    return {"by_dia_kg": {d: round(v, 1) for d, v in sorted(by_dia.items())},
            "by_element_kg": {e: {d: round(v, 1) for d, v in sorted(m.items())} for e, m in by_el.items()},
            "total_kg": round(tot, 1), "assumed_kg": round(assumed, 1)}


# ====================================================================== review step
ROLE_REASON = {"pile": "Pile layout: piles counted and measured", "raft": "Raft: area and thickness measured",
               "footing": "Footings measured", "plinth": "Plinth beams measured", "floor": "Beams and slabs measured",
               "rcc": "RCC plan: beams, slabs, columns, walls, stubs measured (concrete, shuttering, steel)"}


def struct_review(shs, roles, region, tabs, notes_sheet, mode, A: Acc, cfg: dict) -> dict:
    """What the Review step shows for a PDF set (see engine/review.py for the architectural one)."""
    from engine.review import LABEL, flag_q
    tab_sheets: dict = defaultdict(list)
    for t in tabs:
        tab_sheets[t.get("sheet")].append(t.get("name") or "schedule")
    views, plans = [], []
    for k, sh in enumerate(shs):
        role, lab = roles[sh.n], sheet_label(sh)
        fs = sh.title.get("floor_scope") or {}
        v = {"id": k, "sheet": sh.n, "kind": role, "purpose": "unused", "reason": "", "feeds": [], "overlaps": [], "warn": ""}
        if sh.info.get("text_as_geometry"):
            v["reason"] = "Text plotted as lines: nothing written on it can be read"
        elif role in ROLE_REASON and sh.n in region:
            v["purpose"] = "measured"
            v["reason"] = ROLE_REASON[role]
            if role == "pile" and mode == "raft_only" or role == "raft" and mode == "piles_only":
                v["purpose"], v["reason"] = "unused", f"Not measured: foundation answered as {mode.replace('_', ' ')}"
            if tab_sheets.get(sh.n):
                v["reason"] += "; schedules: " + ", ".join(dict.fromkeys(tab_sheets[sh.n])).lower()
            if role in ("floor", "rcc"):
                mult = int(fs.get("multiplier") or 1)
                plans.append({"id": k, "sheet": sh.n, "name": lab, "group": fs.get("label") or lab, "multiplier": mult,
                              "floors": list(range(int(fs["from"]), int(fs["to"]) + 1)) if fs.get("from") is not None and fs.get("to") is not None else [],
                              "source": "title block" if fs else ""})
        elif role in ROLE_REASON:
            v["reason"] = "No plan with columns found on it, so nothing was measured"
        elif tab_sheets.get(sh.n) or role == "column":
            v["purpose"] = "sizes"
            v["reason"] = ("Schedules: " + ", ".join(dict.fromkeys(tab_sheets[sh.n])).lower()) if tab_sheets.get(sh.n) \
                else "Column schedule"
        elif notes_sheet == sh.n:
            v["purpose"] = "sizes"
            v["reason"] = "General notes: grade, covers and development lengths"
        elif role in ("lift", "stair"):
            v["reason"] = f"{'Lift' if role == 'lift' else 'Staircase'}: not measured (enter its RCC volume below)"
        else:
            v["reason"] = "Nothing on it is measured"
        v["label"] = LABEL[v["purpose"]]
        views.append(v)
    flags = list(A.flags.values())
    col = [f for f in flags if f.id.startswith("struct:columns:height:")]
    fh = cfg.get("floor_heights_mm") or {}
    rows = []
    for f in col:
        lab = f.id.split(":", 3)[3]
        val = f.value if f.status == "resolved" else fh.get(lab)
        rows.append({"key": f.id, "kind": "column", "name": (f.input or {}).get("label") or lab, "plan": lab,
                     "height_mm": float(val) if val not in (None, "") else None,
                     "source": "your answer" if f.status == "resolved" else ("your config" if lab in fh else
                                                                             "not written: column rows are 0 until entered"),
                     "assumed": f.status != "resolved" and lab not in fh, "confirmed": f.status != "open", "flag_ids": [f.id]})
    fnd = A.flags.get("struct:foundation:pile-vs-raft")
    sunk = A.flags.get("struct:slabs:sunken-depth")
    heights = {"multi": True, "rows": rows,
               "foundation": {"value": mode, "options": ["piled_raft", "piles_only", "raft_only"],
                              "source": "your answer" if fnd is not None and fnd.status == "resolved" else
                              ("assumed – please confirm" if fnd is not None else "only one foundation sheet"),
                              "assumed": fnd is not None and fnd.status == "open",
                              "flag_ids": [fnd.id] if fnd is not None else []},
               "sunken": {"value": sunk.value if sunk is not None and sunk.status == "resolved" else None,
                          "source": "your answer" if sunk is not None and sunk.status == "resolved" else
                          ("not written – please enter" if sunk is not None else "no sunken slabs found"),
                          "assumed": sunk is not None and sunk.status == "open", "flag_ids": [sunk.id] if sunk is not None else []}}
    want = ("struct:columns:height:", "struct:foundation:pile-vs-raft", "struct:slabs:sunken-depth", "struct:notes:grade",
            "struct:notes:dev-length", "struct:notes:cover", "struct:slabs:sunken")
    qs = []
    for f in flags:
        cal = f.id.startswith("struct:calibration:") and f.severity == "missing"
        if not (f.id.startswith(want) or cal):
            continue
        q = flag_q(f, lambda f: f.id)
        q["topic"] = "heights" if f.id.startswith("struct:columns:height:") else (
            "choice" if "foundation" in f.id else "structure")
        qs.append(q)
    qs.sort(key=lambda q: ({"choice": 0, "heights": 1, "structure": 2}[q["topic"]], q["id"]))
    n_floors = sum(p["multiplier"] for p in plans)
    building = {"multi": True, "storeys": n_floors, "typical_groups": sum(1 for p in plans if p["multiplier"] > 1),
                "text": (f"{len(plans)} floor slab sheet{'s' if len(plans) != 1 else ''}" if plans
                         else "No floor slab sheet found (sheets are recognised by their drawing title)")
                        + (f" for {n_floors} floors" if n_floors != len(plans) else "")
                        + (f", foundation: {mode.replace('_', ' ')}" if mode and mode != "none" else "")}
    return {"kind": "structural", "views": views, "plans": plans, "building": building, "heights": heights,
            "questions": qs}
