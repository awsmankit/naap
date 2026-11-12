"""RCC quantities (concrete, shuttering, steel) for a slab-and-beam plan set: one plan sheet with
tagged beams / slabs / columns, a slab schedule on the plan, a beam schedule on other sheets.

build_rcc(A, shs, cfg, meta) reads the set, adds rows / flags / assumptions to the accumulator `A`
(engine_struct.build.Acc) and returns the data the review step needs, or None if the set is not of this kind.

Rules kept: written values come from the schedules and notes; every length taken off the drawing is scaled
(status "assumed", the standard remark); detailing facts the sheets do not give (cover, development and lap
length, storey height) are flagged "missing" with visibly marked IS 456 defaults that can be overridden.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter, defaultdict

from . import calibrate as C
from . import plans as P
from . import rcc_plan as RP
from . import rcc_tables as RT
from .sheet import Sheet, Txt, ref

SCALED = "scaled from drawing – verify (only written dimensions govern)"

FORMWORK_DEFAULTS = {
    "slab_soffit": True,               # panel net area between beam faces
    "beam_soffit": True,               # b x L
    "beam_sides": True,                # both faces, (D - adjacent slab thickness) where the slab is cast with the beam
    "beam_sides_net_of_slab": True,    # False: full D on both faces
    "column_sides": True,              # perimeter x storey height (needs the height)
    "wall_both_faces": True,
    "slab_edges": True,                # free edges of cantilever slabs, thickness x length
    "sunk_sides": True,                # perimeter x sunk depth
    "opening_sides": True,             # perimeter x slab thickness of framed openings inside a panel
}
STEEL_DEFAULTS = {
    "cover_beam_mm": 25, "cover_slab_mm": 20, "cover_column_mm": 40,
    "ld_tension_x_d": 45,              # IS 456 cl. 26.2.1, M30 + Fe500 deformed bars: 0.87 fy d / (4 x 1.6 x 1.5)
    "anchor_factor": 1.0,              # embedment of thru / support bars into each support = factor x Ld
    "lap_every_m": 12.0, "lap_factor": 1.0,
    "hook_d": 10.0,                    # 135 degree stirrup hooks, each end
    "slab_anchor_mm": 150,             # slab bars extend into each support
    "support_extra_x_L": 0.30, "center_extra_x_L": 0.60,   # replaced by the plan's bar curtailment diagram if it is written
}


def kg_per_m(d: float) -> float:
    return d * d / 162.0


def merged(defaults: dict, over: dict | None) -> dict:
    out = dict(defaults)
    out.update({k: v for k, v in (over or {}).items() if k in defaults})
    return out


# ------------------------------------------------------------------ detection

def find_plan_sheet(shs: list[Sheet], skip=lambda sh: False) -> tuple[Sheet, dict] | None:
    """The sheet that carries a slab schedule and many beam tags."""
    best = None
    for sh in shs:
        if sh.info.get("text_as_geometry") or skip(sh):
            continue
        ss = RT.parse_slab_schedule(sh)
        if not ss or len(ss.get("rows") or {}) < 2:
            continue
        nb = sum(1 for t in sh.texts if RP.BEAM_TAG.match(t.s.strip()))
        if nb >= 3 and (best is None or nb > best[2]):
            best = (sh, ss, nb)
    return (best[0], best[1]) if best else None


def schedule_key(tag: str) -> list[str]:
    """Plan tag 'B110' / 'B2A' / 'b1' -> keys to try in the schedule ('110', '2A', 'b1')."""
    m = RP.BEAM_TAG.match(tag.strip())
    if not m:
        return [tag]
    return [m.group(1) + m.group(2), m.group(2), (m.group(1) + m.group(2)).upper(), (m.group(1) + m.group(2)).lower()]


def lookup(sched: dict, tag: str):
    for k in schedule_key(tag):
        if k in sched:
            return sched[k]
    return None


# ------------------------------------------------------------------ notes

STUB_DEF = re.compile(r"(STUB\d*|SP\d+|H\d+)\s*-\s*(?:HANGER\s*-?\s*)?(\d{2,4})\s*X\s*(\d{2,4})\s*-\s*(\d+)\s*%%[Cc]\s*(\d+)\s*-\s*"
                      r"(?:%%[Cc]\s*)?(\d{1,2})\s*(?:%%[Cc]|@)\s*(\d{2,3})(?:\s*C/?C)?\s*-?\s*(?:UPTO|UP TO)\s*([A-Z ]+?)\.?\s*$", re.I)
WALL_DEF = re.compile(r"(RCC\s*WALL\d*)\s*-\s*(\d{2,3})\s*MM\s*THICK\s*-\s*%%[Cc]\s*(\d{1,2})\s*@\s*(\d{2,3})", re.I)


def read_notes(sh: Sheet) -> dict:
    """Stub / hanger / column-stub (SP) definitions, RCC wall definitions and the curtailment fractions."""
    lines = {}
    for t in sh.texts:
        lines[t.handle] = t
    out = {"stubs": {}, "walls": {}, "curtail": {}, "text": []}
    for t in sh.texts:
        s = t.s.strip().lstrip("*").strip()
        m = STUB_DEF.search(s)
        if m:
            k = re.sub(r"\s+", "", m.group(1)).upper()
            out["stubs"][k] = {"tag": k, "b": int(m.group(2)), "d": int(m.group(3)), "n": int(m.group(4)), "bar": int(m.group(5)),
                               "tie": {"dia": int(m.group(6)), "s": int(m.group(7))}, "upto": m.group(8).strip().upper(),
                               "hanger": k.startswith("H") and not k.startswith("HB"), "text": s, "ref": ref("note", f"{k} definition", [t], sh.n)}
            continue
        m = WALL_DEF.search(s)
        if m:
            k = re.sub(r"\s+", "", m.group(1)).upper()
            out["walls"][k] = {"tag": k, "t": int(m.group(2)), "dia": int(m.group(3)), "s": int(m.group(4)), "text": s,
                               "ref": ref("note", f"{k} definition", [t], sh.n)}
    # the bar curtailment diagram: 0.30L at the supports, 0.60L at the centre
    head = next((t for t in sh.texts if re.search(r"BAR\s*CURTAIL", t.s, re.I)), None)
    if head:
        near = [t for t in sh.texts if abs(t.cx - head.cx) < 330 and 0 < t.cy - head.cy < 200 and re.search(r"\b0\.\d{2}\s*L", t.s)]
        vals = [float(re.search(r"0\.\d{2}", t.s).group()) for t in near]
        if vals:
            out["curtail"] = {"support": min(vals), "center": max(vals), "ref": ref("note", "bar curtailment", near, sh.n)}
    return out


# ------------------------------------------------------------------ main

def build_rcc(A, shs: list[Sheet], cfg: dict, meta: dict, label) -> dict | None:
    found = find_plan_sheet(shs)
    if not found:
        return None
    sh, slab_sched = found
    drg = label(sh)
    steel_cfg = merged(STEEL_DEFAULTS, cfg.get("steel"))
    fw = merged(FORMWORK_DEFAULTS, cfg.get("formwork"))
    n = sh.n

    # ---- beam schedule from every sheet of the set
    beam_rows: dict[str, dict] = {}
    beam_sheets = []
    for s in shs:
        if s is sh:
            continue
        r = RT.parse_beam_schedule_pages(s)
        if r and r.get("rows"):
            beam_sheets.append(s.n)
            for rec in r["rows"]:
                if rec["tag"] in beam_rows:
                    A.flag(f"struct:beam-sched-dup:{rec['tag']}", "warning", "schedule", f"Beam type {rec['tag']} appears twice in the beam schedule",
                           "The first row is used.", [rec["ref"]])
                    continue
                rec["grade"] = r.get("grade")
                beam_rows[rec["tag"]] = rec
    sched = slab_sched["rows"]
    grade = slab_sched.get("grade") or next((r.get("grade") for r in beam_rows.values() if r.get("grade")), None)
    fe = slab_sched.get("steel")
    if not grade:
        A.flag("struct:notes:grade", "missing", "missing-data", "Concrete grade not written",
               input={"type": "text", "label": "Concrete grade", "placeholder": "e.g. M25"})
    ans_g = A.answer("struct:notes:grade")
    grade = ans_g or grade or "M25"
    meta["notes"] = {"grade": grade, "steel": fe, "sheet": n, "grade_source": "schedule heading" if slab_sched.get("grade") else "answer/default"}
    notes = read_notes(sh)
    cur = notes["curtail"]
    sup_x = cur.get("support", steel_cfg["support_extra_x_L"])
    cen_x = cur.get("center", steel_cfg["center_extra_x_L"])

    for k_, rec_ in beam_rows.items():
        if rec_["not_used"]:
            continue
        miss = [nm for nm, ok in (("size", rec_.get("b") and (rec_.get("d") or rec_.get("d_text", "").upper() == "D")), ("bottom bars", rec_["bot_straight"]),
                                  ("top bars", rec_["top_anchor"]), ("stirrup dia/spacing", rec_["stirrup"]["dia"] and rec_["stirrup"]["center_s"])) if not ok]
        if miss:
            A.flag(f"struct:beam-sched-unreadable:{k_}", "warning", "schedule", f"Beam schedule row {k_}: {', '.join(miss)} not readable",
                   "Its steel is partly or fully missing from the totals.", [rec_["ref"]])
    # ---- the missing detailing facts, with visible defaults
    def default_flag(fid, title, detail, label_, unit, default, ph):
        A.flag(fid, "missing", "missing-data", title, detail, input={"type": "number", "label": label_, "unit": unit, "placeholder": ph},
               suggested_fix=f"Default used until answered: {default}")
        v = A.answer(fid)
        return (float(v), "your answer") if v else (default, f"DEFAULT – not on these sheets")

    ld_x, ld_src = default_flag("struct:notes:dev-length", "Development / lap length not on these sheets (the notes refer to general sheet 01)",
                                "Beam anchorage, extras into supports and laps use Ld; the results flagged below depend on it.",
                                "Ld in tension, in bar diameters", "×d", steel_cfg["ld_tension_x_d"],
                                f"default {steel_cfg['ld_tension_x_d']} (IS 456 cl. 26.2.1, {grade}, Fe500)")
    cov_b, cov_src = default_flag("struct:notes:cover", "Clear cover not on these sheets", "Stirrup perimeters and bar lengths use the cover.",
                                  "Clear cover to beam / column stirrups", "mm", steel_cfg["cover_beam_mm"], "default 25 (IS 456 table 16, moderate)")
    cov_s, cov_s_src = default_flag("struct:notes:cover-slab", "Slab cover not on these sheets", "Used for slab bar lengths where it matters.",
                                    "Clear cover to slab bars", "mm", steel_cfg["cover_slab_mm"], "default 20")
    ld_note = f"Ld = {ld_x:g}d ({ld_src})"
    ld_status_dep = "assumed"

    def Ld(d):
        return ld_x * d / 1000.0

    A.assume(f"Development / lap length: Ld = {ld_x:g} x bar diameter ({ld_src}); lap = {steel_cfg['lap_factor']:g} x Ld at {steel_cfg['lap_every_m']:g} m "
             "spacing along a bar; thru and support bars are anchored "
             f"{steel_cfg['anchor_factor']:g} x Ld into each support. Results that use it are marked 'Ld' in the remarks.")
    A.assume(f"Clear cover: beams and columns {cov_b:g} mm ({cov_src}), slabs {cov_s:g} mm ({cov_s_src}).")
    A.assume(f"Bar curtailment (from {'the plan' if cur else 'default'}): extra bars at supports {sup_x:g} L each side of the support plus Ld embedment; extra bars at centre {cen_x:g} L.")
    A.assume("Stirrups: perimeter 2((b-2c)+(D-2c)) + two 135 degree hooks of 10d; 4-legged beams add an inner stirrup of half the width; "
             "stirrup count from the zone lengths (nos x spacing) at the ends and the middle spacing between; no wastage.")
    A.assume("Slab steel: area x (kg/m)/spacing for main and for distribution steel, bars extended 150 mm into each support; 'alt. bar bent up' "
             "is counted as straight bars (the bend adds under 2%); 'top and bottom' doubles the steel; extra top steel is taken over the whole panel (extent not drawn).")

    # ---- plan texts, scale, beams
    reg0 = (sh.bbox[0], sh.bbox[1], sh.bbox[2], sh.bbox[3])
    T0 = RP.classify_texts(sh, reg0)
    widths = Counter(RP.parse_size(t.s)["b"] for t in T0["size"])
    lines, lays = RP.beam_lines(sh)
    pt_per_mm, sinfo = RP.scale_from_widths(RP.spacing_hist(lines), widths)
    blobs_all = RP.column_blobs(sh, reg0)
    calib = None
    if pt_per_mm is None:
        A.flag(f"struct:calibration:{drg}", "missing", "calibration", f"{drg}: no scale found",
               "The drawn beam widths could not be matched to the written widths.",
               input={"type": "text", "label": "Scale as 1:N", "placeholder": "1:75"})
        u = A.answer(f"struct:calibration:{drg}")
        m = re.search(r"1\s*:\s*(\d+(?:\.\d+)?)", str(u or ""))
        if not m:
            return {"sheet": sh, "region": reg0, "handled": True, "slab_schedule": slab_sched, "beam_rows": beam_rows, "rows_made": False}
        pt_per_mm = 72 / 25.4 / float(m.group(1))
    strips = RP.find_strips(lines, pt_per_mm, set(widths))
    strips = RP.cut_strips(strips, blobs_all, 6)
    idx = P.main_cluster([s.bb for s in strips], 200)
    strips = [strips[i] for i in idx]
    xs = [v for s in strips for v in (s.bb[0], s.bb[2])]
    ys = [v for s in strips for v in (s.bb[1], s.bb[3])]
    reg = (min(xs) - 30, min(ys) - 30, max(xs) + 30, max(ys) + 30)
    T = RP.classify_texts(sh, reg)
    sbb = slab_sched.get("bbox") or [0, 0, 0, 0]
    in_table = lambda t: sbb[0] - 5 <= t.cx <= sbb[2] + 5 and sbb[1] - 5 <= t.cy <= sbb[3] + 5
    T = {k: [t for t in v if not in_table(t)] for k, v in T.items()}
    blobs = [b for b in blobs_all if P.inside(b.cx, b.cy, reg)]
    gap = 12 * pt_per_mm * 0  # unused
    orph_tag, orph_size = RP.assign_tags(strips, T["beam_tag"], T["size"], max_d=max(14.0, 40 * pt_per_mm))

    # ---- calibration samples: written widths against drawn spacing, written column sizes against column blobs
    samples = []
    for s in strips:
        if s.size:
            sz = RP.parse_size(s.size.s)
            samples.append({"source": "beam width", "value_mm": float(sz["b"]), "len_pt": round(s.w, 3), "ratio": s.w / sz["b"],
                            "handles": [s.size.handle] + s.handles[:1], "at": [round((s.bb[0] + s.bb[2]) / 2, 1), round((s.bb[1] + s.bb[3]) / 2, 1)]})
    for ct in T["col_size"]:
        m = RP.COL_SIZE.match(ct.s.strip())
        near = min(blobs, key=lambda b: math.hypot(b.cx - ct.cx, b.cy - ct.cy), default=None)
        if near is not None and math.hypot(near.cx - ct.cx, near.cy - ct.cy) < 70:
            a, b_ = sorted((near.w, near.h))
            v0, v1 = sorted((int(m.group(1)), int(m.group(2))))
            for dpt, v in ((a, v0), (b_, v1)):
                samples.append({"source": "column size", "value_mm": float(v), "len_pt": round(dpt, 3), "ratio": dpt / v,
                                "handles": [ct.handle] + near.handles[:1], "at": [round(near.cx, 1), round(near.cy, 1)]})
    calib = C.consensus(n, samples, "beam widths + written column sizes")
    u = C.user_calib(n, (cfg.get("calibration") or {}).get(str(n)) or {})
    if u:
        calib = u
    if calib.pt_per_mm is None:
        calib = C.Calib(n, pt_per_mm, 0, 0.0, {"widths": 0}, [], "beam widths")
    ppm = calib.pt_per_mm
    # left/right/top/bottom halves agree?
    halves = {}
    if len(samples) >= 8:
        mx = (reg[0] + reg[2]) / 2
        my = (reg[1] + reg[3]) / 2
        for nm, pred in (("left", lambda a: a[0] < mx), ("right", lambda a: a[0] >= mx), ("top", lambda a: a[1] >= my), ("bottom", lambda a: a[1] < my)):
            rr = [x["ratio"] for x in samples if pred(x["at"])]
            if len(rr) >= 3:
                halves[nm] = {"pt_per_mm": round(statistics.median(rr), 5), "n": len(rr)}
    meta["calibration"][n] = calib.to_dict() | {"drawing_no": drg, "halves": halves, "voted_from": "written beam widths"}
    A.assume(f"{drg}: scale {ppm:.5f} pt/mm (about 1:{calib.scale_1_to}) from {calib.n} written beam widths and column sizes, spread {calib.spread * 100:.2f}% "
             f"(IQR/median); the sheet says it must not be scaled, so every length taken off it is 'assumed'.")
    fid_c = f"struct:calibration:{drg}"
    if calib.spread > 0.03 or calib.n < 3:
        A.flag(fid_c, "warning", "calibration", f"{drg}: scale calibration is weak (spread {calib.spread * 100:.1f}%, {calib.n} samples)",
               f"Consensus {ppm:.5f} pt/mm. Scaled lengths may be off by about the spread.", [ref("plan", drg, [reg], n)],
               input={"type": "text", "label": "Scale as 1:N (overrides)", "placeholder": "1:75"})
    else:
        A.flag(f"struct:scale-info:{drg}", "info", "calibration", f"{drg}: lengths are scaled from the drawing (spread {calib.spread * 100:.1f}%)",
               "The sheet says 'should not be scaled off; only written dimensions should be followed'. Beam lengths, slab areas, column and wall sizes come from the "
               "drawn geometry at the calibrated scale, so they are marked 'assumed – verify'. Written beam widths / thicknesses / sizes govern.", [ref("plan", drg, [reg], n)])
    if calib.outliers:
        A.flag(f"struct:calibration-outliers:{drg}", "warning", "calibration", f"{drg}: {len(calib.outliers)} written dimension(s) disagree with the scaled drawing", "",
               [ref("dimension", str(o.get("text")), [[o["at"][0] - 6, o["at"][1] - 6, o["at"][0] + 6, o["at"][1] + 6]], n) for o in calib.outliers if o.get("at")])

    mm = lambda pt: pt / ppm                     # points -> mm
    m_ = lambda pt: pt / ppm / 1000.0            # points -> m
    fg, fo = drg, 0.0
    mult = 1

    # ---- slab panels
    s_rx = re.compile(r"^" + re.escape(re.match(r"[A-Za-z]+", next(iter(sched))).group()) + r"\d{1,3}[A-Z]?$")
    slab_tags = [t for t in sh.texts if s_rx.match(t.s.strip()) and P.inside(t.cx, t.cy, reg) and not in_table(t)]
    good = [l for l in lays if sum(1 for s in sh.segs if s.layer == l and s.length >= 15) >= 8]
    segs = [s for s in sh.segs if (s.layer in good and s.length >= 1.5) or
            (re.search(r"col|wall", s.layer, re.I) and s.length >= 12 and (s.horiz or s.vert))]
    panels, rs = RP.raster_panels(reg, segs, strips, blobs, slab_tags, seal=1)
    meta["rcc_raster"] = {"px_per_pt": rs["ppp"]}
    ppp = rs["ppp"]

    thick_by_panel: dict[int, int] = {}
    thick_boxes = []
    for tt in T["thick"]:
        v = int(re.match(r"\d+", tt.s).group())
        thick_boxes.append((tt, v))
    unused_tags = 0
    for p in panels:
        cand = [(math.hypot(tt.cx - p.tag_txt.cx, tt.cy - p.tag_txt.cy), v, tt) for tt, v in thick_boxes]
        cand = [c for c in cand if c[0] < 80]
        if cand:
            p.thick_txt = min(cand, key=lambda c: c[0])[2]
            thick_by_panel[p.label] = min(cand, key=lambda c: c[0])[1]

    # ---- build the slab rows
    slab_tot = defaultdict(lambda: defaultdict(float))        # type -> thk -> m3
    slab_area = defaultdict(float)
    panel_rows = []
    for p in panels:
        types = [p.tag] + [t.s.strip() for t in p.shared]
        types = list(dict.fromkeys(types))
        rec = lookup(sched, p.tag)
        if p.leaked:
            A.flag(f"struct:slab-open:{p.tag}:{int(p.bb[0])}:{int(p.bb[1])}", "warning", "slab", f"Slab {p.tag}: panel is not closed by beam lines, area not measured",
                   "The flood fill leaked outside the plan; the panel has no area.", [ref("slab", p.tag, [p.bb], n)])
            continue
        if len(types) > 1:
            A.flag(f"struct:slab-shared:{p.tag}:{int(p.bb[0])}:{int(p.bb[1])}", "warning", "slab",
                   f"One closed panel carries several slab tags: {', '.join(types)}", "The area is split equally between the tags; check the panel boundaries.",
                   [ref("slab", p.tag, [p.bb], n)])
        area = p.area_pt2 / ppm / ppm / 1e6 / len(types)      # m2 each
        for ty in types:
            r = lookup(sched, ty) if ty in sched else None
            if r is None:
                A.flag(f"struct:slab-not-scheduled:{ty}", "warning", "schedule", f"Slab type {ty} is on the plan but not in the slab schedule", "No thickness or steel.",
                       [ref("slab", ty, [p.bb], n)])
                continue
            thk_box = thick_by_panel.get(p.label)
            thk = r["thk"] or thk_box
            if thk_box and r["thk"] and thk_box != r["thk"]:
                A.flag(f"struct:slab-thk-mismatch:{ty}:{thk_box}", "warning", "schedule", f"Slab {ty}: plan says {thk_box} mm, schedule says {r['thk']} mm",
                       "The schedule value is used.", [ref("slab", ty, [p.bb, p.thick_txt.bb if p.thick_txt else p.bb], n), r["ref"]])
            panel_rows.append({"panel": p, "type": ty, "rec": r, "thk": thk, "area": area, "perim": p.perim_px / ppp / ppm / 1000.0 / len(types),
                               "free_edge": max(0.0, p.perim_px - p.beam_edge_px) / ppp / ppm / 1000.0 / len(types)})
    # sunk areas: a SUNK label inside a panel
    sunk_txt = [t for t in sh.texts if re.search(r"SUNK", t.s, re.I) and P.inside(t.cx, t.cy, reg)]
    sunk_depths = {}
    for pr in panel_rows:
        p = pr["panel"]
        hit = [t for t in sunk_txt if p.bb[0] <= t.cx <= p.bb[2] and p.bb[1] <= t.cy <= p.bb[3]]
        if hit:
            near = [t for t in sh.texts if re.fullmatch(r"\d{2,3}\s*MM", t.s.strip()) and any(math.hypot(t.cx - h.cx, t.cy - h.cy) < 45 for h in hit)
                    and not RP.THICK_TAG.match("x") ]
            dep = next((int(re.match(r"\d+", t.s).group()) for t in near if int(re.match(r"\d+", t.s).group()) in (100, 150, 200, 250, 300, 350, 400, 450)), None)
            pr["sunk"] = {"depth": dep, "refs": hit}
            sunk_depths[id(pr)] = dep

    slab_el = f"RCC {grade} in slabs"
    for pr in panel_rows:
        p, r, thk = pr["panel"], pr["rec"], pr["thk"]
        if not thk:
            continue
        rf = ref("slab", f"{pr['type']} panel", [p.bb, p.tag_txt] + ([p.thick_txt] if p.thick_txt else []), n)
        loc = f"{pr['type']} ({thk} mm)" + (" – sunken portion" if pr.get("sunk") else "") + (" – cantilever" if r["canti"] else "")
        row = A.row("RCC", f"{slab_el}, {thk} mm thick", loc, 1, pr["area"], thk / 1000.0, None, "m³", f"net of beam faces; area {SCALED}; thickness {'schedule' if r['thk'] else 'plan'}",
                    "assumed", [rf, r["ref"]], None, fg, fo, drg)
        pr["row"] = row
        slab_tot[pr["type"]][thk] += row.qty
        slab_area[pr["type"]] += pr["area"]
        if fw["slab_soffit"]:
            A.row("SHUTTERING", "Shuttering – slab soffit", loc, 1, pr["area"], None, None, "m²", f"clear panel area, {SCALED}", "assumed", [rf], None, fg, fo, drg)
        if r["canti"] and fw["slab_edges"]:
            A.row("SHUTTERING", "Shuttering – slab edges (cantilever free edges)", loc, 1, round(pr["free_edge"], 3), thk / 1000.0, None, "m²",
                  f"free edge = panel perimeter less the edge on beams/columns; {SCALED}", "assumed", [rf], None, fg, fo, drg)
        if pr.get("sunk") and fw["sunk_sides"]:
            dep = pr["sunk"]["depth"]
            if dep:
                A.row("SHUTTERING", "Shuttering – sunken slab sides", loc, 1, round(pr["perim"], 3), dep / 1000.0, None, "m²",
                      f"perimeter x sunk depth {dep} mm (label on plan); {SCALED}", "assumed", [rf], None, fg, fo, drg)
            else:
                A.flag(f"struct:slabs:sunken-depth", "missing", "missing-data", "Sunk depth not readable on the plan",
                       "Sunken portions are measured as slab; their side shuttering needs the depth.",
                       [ref("slab", "sunken", [p.bb], n)], input={"type": "number", "label": "Sunk depth", "unit": "mm"})
        # steel
        slab_steel(A, pr, r, steel_cfg, cov_s, ppm, rf, fg, fo, drg)
        for key in ("main", "dist"):
            pass
    sunk_n = sum(1 for pr in panel_rows if pr.get("sunk"))
    if sunk_n:
        A.assume("Sunken portions are measured as slab of the scheduled thickness; the light-weight sunk filling is not RCC. Side shuttering = perimeter x the depth written on the plan.")
    slab_types_on_plan = {pr["type"] for pr in panel_rows}
    for ty in sched:
        if ty not in slab_types_on_plan and not any(t.s.strip() == ty for t in slab_tags):
            A.flag(f"struct:slab-not-on-plan:{ty}", "info", "schedule", f"Slab type {ty} is in the schedule but not on the plan", "", [sched[ty]["ref"]])
        elif ty not in slab_types_on_plan:
            A.flag(f"struct:slab-not-measured:{ty}", "warning", "slab", f"Slab type {ty} is tagged on the plan but no closed panel was measured",
                   "Its tag sits in a panel that leaked or is shared; its area is missing from the totals.", [ref("slab", ty, [t.bb for t in slab_tags if t.s.strip() == ty][:1], n)])

    # ---- beams
    bm_el = f"RCC {grade} in beams"
    panel_lab = rs["lab"]
    px = rs["px"]
    beam_out = []
    seen_tags = Counter()
    sched_used = set()
    for s in sorted(strips, key=lambda s: (-(s.bb[3]), s.bb[0])):
        if s.tag is None:
            continue
        tag = s.tag.s.strip()
        seen_tags[tag] += 1
        rec = lookup(beam_rows, tag)
        sz = RP.parse_size(s.size.s) if s.size else None
        L = m_(s.length)
        rf = ref("beam", tag, [s.bb, s.tag] + ([s.size] if s.size else []), n)
        rf["handles"] = list(dict.fromkeys(rf["handles"] + s.handles))[:60]
        if rec is None:
            A.flag(f"struct:beam-not-scheduled:{tag}", "warning", "schedule", f"Beam {tag} is on the plan but not in the beam schedule",
                   "Concrete is measured from the plan size; its steel is not measured.", [rf])
            if not sz:
                continue
        else:
            sched_used.add(rec["tag"])
            if rec["not_used"]:
                A.flag(f"struct:beam-not-used:{tag}", "warning", "schedule", f"Beam {tag} is marked NOT USED in the schedule but is drawn on the plan",
                       "Measured from the plan size; its steel is not measured (no schedule data).", [rf, rec["ref"]])
        b_mm = (rec or {}).get("b") or (sz and sz["b"])
        d_mm = (rec or {}).get("d") or (sz and sz["d"])
        if rec and sz and rec.get("b") and (rec["b"] != sz["b"] or (sz["d"] and rec.get("d") and rec["d"] != sz["d"])):
            A.flag(f"struct:beam-size-mismatch:{tag}", "warning", "schedule",
                   f"Beam {tag}: plan size {sz['b']}x{sz['d'] or 'D'}, schedule {int(rec['b'])}x{rec.get('d_text') or '?'}", "The schedule size is used.", [rf, rec["ref"]])
        hidden = (sz and sz["d_is_slab"]) or (rec and rec.get("d") is None and rec.get("d_text", "").upper() == "D")
        if rec and rec.get("not_used") and not sz:
            continue
        if not b_mm or not d_mm:
            if hidden:
                A.flag(f"struct:beam-hidden:{tag}", "info", "beam", f"{tag}: depth D = slab depth, no projection below the slab", "Not counted as a beam.", [rf])
            else:
                A.flag(f"struct:beam-size-missing:{tag}", "warning", "beam", f"Beam {tag}: size not readable", "", [rf])
            continue
        # slab thickness on each side (net of slab where cast monolithic)
        sides = []
        for sign in (1, -1):
            off = (s.w / 2 + 2.5) * sign
            ptx = (s.c + off, (s.a + s.b) / 2) if not s.horiz else ((s.a + s.b) / 2, s.c + off)
            if not s.horiz:
                cxy = px(ptx[0], ptx[1])
            else:
                cxy = px(ptx[0], ptx[1])
            k = 0
            try:
                k = int(panel_lab[cxy[1], cxy[0]])
            except Exception:
                k = 0
            sides.append(thick_by_panel.get(k) or next((pr["thk"] for pr in panel_rows if pr["panel"].label == k and pr["thk"]), 0))
        t_side = [x or 0 for x in sides]
        d_side = [d_mm - (t if fw["beam_sides_net_of_slab"] else 0) for t in t_side]
        status, rem = "assumed", f"L = clear span between supports, {SCALED}"
        if rec and rec.get("inverted"):
            rem += "; inverted beam (INV) – upstand, depth as written"
        if rec and rec.get("canti"):
            rem += "; cantilever beam (CANTI)"
        row = A.row("RCC", bm_el, f"{tag} ({int(b_mm)}x{int(d_mm)})" + (" – cantilever" if rec and rec.get("canti") else ""), 1, L, b_mm / 1000.0, d_mm / 1000.0,
                    "m³", rem, status, [rf] + ([rec["ref"]] if rec else []), None, fg, fo, drg)
        if fw["beam_soffit"]:
            A.row("SHUTTERING", "Shuttering – beam soffit", f"{tag} ({int(b_mm)}x{int(d_mm)})", 1, L, b_mm / 1000.0, None, "m²", rem, status, [rf], None, fg, fo, drg)
        if fw["beam_sides"]:
            hs = sum(max(0.0, x) for x in d_side) / 1000.0
            A.row("SHUTTERING", "Shuttering – beam sides", f"{tag} ({int(b_mm)}x{int(d_mm)})", 1, L, None, hs, "m²",
                  f"both faces: D less slab thickness on each side ({t_side[0]} / {t_side[1]} mm, 0 = no slab); {SCALED}", status, [rf], None, fg, fo, drg)
        beam_out.append({"tag": tag, "L": L, "b": b_mm, "d": d_mm, "rec": rec, "row": row, "ref": rf})
        if rec and not rec["not_used"]:
            beam_steel(A, rec, L, b_mm, d_mm, steel_cfg, ld_x, Ld, cov_b, sup_x, cen_x, rf, fg, fo, drg, ld_note)
    meta["beam_counts"] = {"tags_on_plan": len({t.s.strip() for t in T["beam_tag"]}), "tag_labels": len(T["beam_tag"]), "strips_found": len(strips),
                           "strips_tagged": sum(1 for s in strips if s.tag), "beams_measured": len(beam_out), "schedule_rows": len(beam_rows)}
    # ---- plan tags not matched to a strip
    for t in orph_tag:
        A.flag(f"struct:beam-tag-no-strip:{t.s.strip()}:{int(t.cx)}", "warning", "beam", f"Beam tag {t.s.strip()} on the plan could not be matched to a drawn beam",
               "Not measured (no pair of lines of the written width next to the tag).", [ref("beam", t.s.strip(), [t], n)])
    on_plan_types = set()
    for t in T["beam_tag"]:
        r_ = lookup(beam_rows, t.s.strip())
        if r_:
            on_plan_types.add(r_["tag"])
    for k, rec in beam_rows.items():
        if k not in on_plan_types and not rec["not_used"]:
            A.flag(f"struct:beam-not-on-plan:{k}", "info", "schedule", f"Beam type {k} is in the schedule but its tag is not on this plan",
                   "May belong to another floor / sheet (the schedule is titled for the same floor).", [rec["ref"]])
    unmatched = [s for s in strips if s.tag is None]
    if unmatched:
        A.flag(f"struct:beam-untagged:{drg}", "warning", "beam", f"{len(unmatched)} beam-like strips have no tag next to them (stair flights, voids, hidden beams?) – not measured",
               "Strips are pairs of lines of a written beam width; these have no beam tag within reach.",
               [ref("beam", "untagged", [s.bb], n) for s in unmatched[:40]])

    # ---- columns, walls, stubs / hangers / SP
    heights_fid = f"struct:columns:height:{drg}"
    cols_out = columns(A, T, blobs, notes, ppm, grade, cov_b, heights_fid, fg, fo, drg, n, fw, steel_cfg)
    walls_out = rcc_walls(A, sh, reg, notes, ppm, grade, heights_fid, fg, fo, drg, n, fw)
    stubs_out = stubs(A, T, notes, grade, heights_fid, fg, fo, drg, n, Ld)

    for o in shs:
        if o is sh or o.n in beam_sheets:
            continue
        sec = [t for t in o.texts if re.match(r"^SECTION\b", t.s.strip(), re.I)]
        if len(sec) >= 3:
            A.flag(f"struct:sections-not-read:{o.n}", "info", "beam", f"{label(o)}: {len(sec)} section drawings are not read",
                   "Bar callouts on typical sections (e.g. 11T32 (4+4+3), S/F 3Ø12 E/F, 4-legged details) are not parsed. Beam steel comes from the beam schedule only; "
                   "special deep beams may have more steel than the schedule says.", [ref("sheet", label(o), [o.bbox], o.n)])
    # ---- cantilever beam remark: slab/beam consistency
    meta["rcc"] = {"sheet": n, "region": reg, "scale_pt_per_mm": ppm, "grade": grade, "steel": fe, "beam_schedule_sheets": beam_sheets,
                   "slab_by_type": {k: {str(t): round(v, 3) for t, v in m.items()} for k, m in slab_tot.items()},
                   "slab_area_by_type_m2": {k: round(v, 2) for k, v in slab_area.items()}, "formwork_rules": fw, "steel_rules": steel_cfg,
                   "columns": cols_out, "walls": walls_out, "stubs": stubs_out}
    tabs = []
    tabs.append({"name": "Slab schedule", "sheet": n, "columns": ["Type", "Thick", "Main steel", "Distribution steel", "Remark"],
                 "rows": [{"label": k, "cells": [k, str(v["thk"] or ""), (v["main"] or {}).get("text", ""), (v["dist"] or {}).get("text", "") +
                                                 (" + " + v["extra"]["text"] if v["extra"] else ""), v["remark"]]} for k, v in sched.items()]})
    if beam_rows:
        tabs.append({"name": "Beam schedule", "sheet": beam_sheets[0], "columns": ["Type", "b", "D", "Bottom straight", "Bottom extra", "Top anchor", "Top left", "Top right",
                                                                                       "Stirrup dia", "Left", "Centre", "Right", "Legs", "Remark"],
                     "rows": [{"label": k, "cells": [k, _n(v["b"]), v["d_text"], _bs(v["bot_straight"]), _bs(v["bot_extra"]), _bs(v["top_anchor"]), _bs(v["top_left"]),
                                                     _bs(v["top_right"]), _n(v["stirrup"]["dia"]), f"{_n(v['stirrup']['left']['n'])}@{_n(v['stirrup']['left']['s'])}",
                                                     _n(v["stirrup"]["center_s"]), f"{_n(v['stirrup']['right']['n'])}@{_n(v['stirrup']['right']['s'])}", _n(v["legs"]),
                                                     ("NOT USED" if v["not_used"] else v["remark"])]} for k, v in beam_rows.items()]})
    meta["rcc"]["tables"] = tabs
    return {"sheet": sh, "region": reg, "handled": True, "tables": tabs, "beam_sheets": beam_sheets, "heights_fid": heights_fid, "rows_made": True,
            "n_floors": mult}


def _n(v):
    return "" if v is None else (str(int(v)) if float(v) == int(v) else str(v))


def _bs(l):
    return " + ".join(f"{x['n']}-{x['dia']}" for x in l)


# ------------------------------------------------------------------ steel

def slab_steel(A, pr, r, cfg, cover, ppm, rf, fg, fo, drg):
    p = pr["panel"]
    A_m2 = pr["area"]
    w_mm = min(p.bb[2] - p.bb[0], p.bb[3] - p.bb[1]) / ppm
    anchor = cfg["slab_anchor_mm"]
    ext = 1 + 2 * anchor / max(w_mm, 1.0)
    loc = f"{pr['type']} ({pr['thk']} mm)"
    for key, nm in (("main", "main"), ("dist", "distribution")):
        spec = r.get(key)
        if not spec:
            A.flag(f"struct:slab-steel-missing:{pr['type']}:{key}", "warning", "slab", f"Slab {pr['type']}: {nm} steel not readable in the schedule", "", [r["ref"]])
            continue
        mult = 2 if spec["top_and_bottom"] else 1
        per_m2 = kg_per_m(spec["dia"]) / (spec["s"] / 1000.0) * mult * ext
        rem = (f"{spec['text']}; area x kg/m ÷ spacing x {ext:.3f} (bars {anchor} mm into supports)" + ("; top and bottom = x2" if mult == 2 else "")
               + ("; alt. bars bent up counted straight" if spec["alt_bent"] else "") + f"; panel area {SCALED}")
        A.bars_area("Slabs – " + nm, loc, spec["dia"], A_m2, per_m2, "assumed", rem, [rf, r["ref"]], fg, fo, drg)
    if r.get("extra"):
        sp = r["extra"]
        mult = 2 if sp["bothways"] else 1
        per_m2 = kg_per_m(sp["dia"]) / (sp["s"] / 1000.0) * mult * ext
        A.bars_area("Slabs – extra top steel", loc, sp["dia"], A_m2, per_m2, "assumed",
                    f"{sp['text']}: extent not drawn, taken over the whole panel; {SCALED}", [rf, r["ref"]], fg, fo, drg)
        A.flag(f"struct:slab-extra-top:{pr['type']}", "warning", "slab", f"Slab {pr['type']}: extra top steel taken over the whole panel",
               "The schedule gives bars and spacing but not where they stop; check against the drawing.", [r["ref"]])
    if r["canti"] and (r.get("main") or {}).get("spring"):
        A.assume("Cantilever slabs ('ALT. BAR SPRING'): the scheduled main bars are the top steel; their length is taken as the panel width plus anchorage, no extra bend allowance.")


def beam_steel(A, rec, L, b, D, cfg, ld_x, Ld, cov, sup_x, cen_x, rf, fg, fo, drg, ld_note):
    tag = rec["tag"]
    af = cfg["anchor_factor"]
    loc = f"B{tag}" if not tag[0].isalpha() else tag
    refs = [rf, rec["ref"]]

    def emit(el, grp, dia_n, length_fn, rem, dep_ld, status="assumed"):
        for x in dia_n:
            ln = length_fn(x["dia"])
            lap = 0.0
            if ln > cfg["lap_every_m"]:
                lap = (math.ceil(ln / cfg["lap_every_m"]) - 1) * cfg["lap_factor"] * Ld(x["dia"])
            A.bars(el, f"{loc} – {grp}", x["dia"], x["n"], ln + lap, status, f"{rem}{'; ' + ld_note if dep_ld else ''}{'; lap added' if lap else ''}; L {SCALED}", refs, fg, fo, None, drg)

    emit("Beams – bottom bars", "bottom straight", rec["bot_straight"], lambda d: L + 2 * af * Ld(d), "L + 2 x anchorage", True)
    emit("Beams – bottom bars", "bottom extra at centre", rec["bot_extra"], lambda d: cen_x * L, f"{cen_x:g} L", False)
    emit("Beams – top bars", "top thru (anchor)", rec["top_anchor"], lambda d: L + 2 * af * Ld(d), "L + 2 x anchorage", True)
    emit("Beams – top bars", "top extra at left support", rec["top_left"], lambda d: sup_x * L + af * Ld(d), f"{sup_x:g} L + anchorage", True)
    emit("Beams – top bars", "top extra at right support", rec["top_right"], lambda d: sup_x * L + af * Ld(d), f"{sup_x:g} L + anchorage", True)
    if rec.get("side_each_face"):
        sf = rec["side_each_face"]
        A.bars("Beams – side bars", f"{loc} – side face (E/F)", sf["dia"], sf["n"] * 2, L + 2 * af * Ld(sf["dia"]), "assumed",
               f"E/F {sf['n']}-{sf['dia']} each face (schedule remark); {ld_note}; L {SCALED}", refs, fg, fo, None, drg)
    elif D >= 750:
        A.flag(f"struct:beam-side-face:{tag}", "info", "beam", f"Beam {tag} is {int(D)} mm deep: side-face steel is not on the schedule",
               "IS 456 cl. 26.5.1.3 asks for side-face steel above 750 mm; none is measured unless the remarks say E/F.", [rec["ref"]])
    st = rec["stirrup"]
    dia = int(st["dia"] or 0)
    s_c = st.get("center_s")
    if not dia or not s_c:
        A.flag(f"struct:beam-stirrup-missing:{tag}", "warning", "beam", f"Beam {tag}: stirrup size or spacing not readable", "", [rec["ref"]])
        return
    c = cov
    per = 2 * ((b - 2 * c) + (D - 2 * c)) + 2 * cfg["hook_d"] * dia
    nl = (st["left"]["n"] or 0)
    nr = (st["right"]["n"] or 0)
    sl = st["left"]["s"] or s_c
    sr = st["right"]["s"] or s_c
    Lmm = L * 1000
    if nl or nr:
        zl, zr = nl * sl, nr * sr
        n_mid = max(0, math.ceil((Lmm - zl - zr) / s_c) - 1)
        cnt = nl + nr + n_mid + 1
        zone = f"{int(nl)}@{int(sl)} + {n_mid}@{int(s_c)} + {int(nr)}@{int(sr)}"
    else:
        cnt = math.floor(Lmm / s_c) + 1
        zone = f"@{int(s_c)} throughout"
    legs = rec.get("legs") or 2
    A.bars("Beams – stirrups", f"{loc} – stirrups {zone}", dia, cnt, per / 1000.0, "assumed",
           f"perimeter 2((b-2c)+(D-2c)) + hooks 2x{cfg['hook_d']:g}d, c = {c:g} mm (cover default flag); count {cnt}; L {SCALED}", refs, fg, fo, None, drg)
    if legs >= 4 or rec.get("legs4"):
        inner = 2 * (((b - 2 * c) / 2) + (D - 2 * c)) + 2 * cfg["hook_d"] * dia
        A.bars("Beams – stirrups", f"{loc} – inner stirrup (4-legged)", dia, cnt, inner / 1000.0, "assumed",
               f"4 legs: second stirrup of half width, same count; c = {c:g} mm; L {SCALED}", refs, fg, fo, None, drg)


# ------------------------------------------------------------------ columns, walls, stubs

def _height(A, fid, drg, what, refs):
    A.flag(fid, "missing", "missing-data", f"{drg}: storey / column height not given",
           f"{what} need a height, which this set does not give. Rows stay 0 until it is entered.",
           refs, input={"type": "number", "label": f"Floor height for {drg} (columns, walls, stubs, hangers)", "unit": "mm", "placeholder": "e.g. 3300"})
    v = A.answer(fid)
    if v in (None, ""):
        fh = (A.cfg.get("floor_heights_mm") or {}).get(drg)
        v = fh
    return float(v) / 1000.0 if v not in (None, "") else None


def columns(A, T, blobs, notes, ppm, grade, cov, hfid, fg, fo, drg, n, fw, scfg):
    tags = T["col_tag"]
    sizes = T["col_size"]
    rows = []
    h = _height(A, hfid, drg, "Columns", [])
    used = set()
    for b in sorted(blobs, key=lambda b: (-b.cy, b.cx)):
        near = min(tags, key=lambda t: math.hypot(t.cx - b.cx, t.cy - b.cy), default=None)
        tag = ""
        if near is not None and math.hypot(near.cx - b.cx, near.cy - b.cy) < 60 + max(b.w, b.h):
            tag = near.s.strip()
        wr = None
        if near is not None:
            ws = [s for s in sizes if math.hypot(s.cx - near.cx, s.cy - near.cy) < 30]
            if ws:
                m = RP.COL_SIZE.match(ws[0].s.strip())
                wr = (int(m.group(1)), int(m.group(2)), ws[0])
        if wr:
            # written as long x short on the plan; orient it like the drawn blob
            a_, b_ = max(wr[0], wr[1]), min(wr[0], wr[1])
            dx, dy = (a_, b_) if b.w >= b.h else (b_, a_)
            src = "written on the plan"
            st = "ok"
        else:
            dx, dy = round(b.w / ppm / 10) * 10, round(b.h / ppm / 10) * 10
            src = f"drawn size scaled, rounded to 10 mm"
            st = "assumed"
        rf = ref("column", tag or "column", [b.bb] + ([near] if near else []) + ([wr[2]] if wr else []), n)
        rf["handles"] = list(dict.fromkeys(rf["handles"] + b.handles))[:40]
        rem = f"{src}; height {'not given – enter it' if h is None else 'from your answer'}"
        row = A.row("RCC", f"RCC {grade} in columns", f"{tag or 'col'} ({int(dx)}x{int(dy)})", 1, dx / 1000.0, dy / 1000.0, h, "m³", rem,
                    st if h is not None else "unresolved", [rf], [hfid], fg, fo, drg, qty=0.0 if h is None else None)
        if fw["column_sides"]:
            A.row("SHUTTERING", "Shuttering – column sides", f"{tag or 'col'} ({int(dx)}x{int(dy)})", 1, 2 * (dx + dy) / 1000.0, h, None, "m²", rem,
                  st if h is not None else "unresolved", [rf], [hfid], fg, fo, drg, qty=0.0 if h is None else None)
        rows.append({"tag": tag, "w": dx, "d": dy, "src": src})
        if tag:
            used.add(tag)
    missing = sorted({t.s.strip() for t in tags} - used)
    if missing:
        A.flag(f"struct:column-tags-no-blob:{drg}", "warning", "column", f"{len(missing)} column tag(s) have no drawn column next to them: {', '.join(missing[:20])}",
               "Not measured (a column is drawn as a filled rectangle in a column layer).", [ref("column", t.s, [t], n) for t in tags if t.s.strip() in missing][:30])
    A.flag(f"struct:columns:no-schedule:{drg}", "info", "column", "No column schedule or column steel in this set",
           "Column concrete and shuttering are measured from the drawn / written size; column reinforcement is not measured.")
    return {"count": len(rows), "tags": len(tags), "written_sizes": len(sizes), "height_m": h}


def rcc_walls(A, sh, reg, notes, ppm, grade, hfid, fg, fo, drg, n, fw):
    segs = [s for s in sh.segs if re.search(r"rcc\s*wall", s.layer, re.I) and P.inside(s.x1, s.y1, reg)]
    fills = [f for f in sh.fills if re.search(r"rcc\s*wall", f.layer, re.I) and P.inside(f.bb[0], f.bb[1], reg)]
    items = [(s.x1, s.y1, s.x2, s.y2) for s in segs]
    bbs = [(min(a, c), min(b, d), max(a, c), max(b, d)) for a, b, c, d in items] + [f.bb for f in fills]
    out = []
    if not bbs:
        return out
    h = A.answer(hfid)
    hh = float(h) / 1000.0 if h else None
    handles = {}
    for s in segs:
        handles[(round(s.x1), round(s.y1))] = s.handle
    labels = [t for t in sh.texts if re.fullmatch(r"RCC\s*WALL\s*\d*", t.s.strip(), re.I) and P.inside(t.cx, t.cy, reg)]
    for grp in P._components(bbs, 3.0):
        xs = [bbs[i][0] for i in grp] + [bbs[i][2] for i in grp]
        ys = [bbs[i][1] for i in grp] + [bbs[i][3] for i in grp]
        bb = (min(xs), min(ys), max(xs), max(ys))
        L = max(bb[2] - bb[0], bb[3] - bb[1]) / ppm / 1000.0
        if L < 0.3:
            continue
        lab = min(labels, key=lambda t: math.hypot(t.cx - (bb[0] + bb[2]) / 2, t.cy - (bb[1] + bb[3]) / 2), default=None)
        key = re.sub(r"\s+", "", lab.s).upper() if lab and math.hypot(lab.cx - (bb[0] + bb[2]) / 2, lab.cy - (bb[1] + bb[3]) / 2) < 120 else None
        wdef = notes["walls"].get(key) if key else None
        if wdef is None and len(notes["walls"]) == 1:
            wdef = next(iter(notes["walls"].values()))
        if wdef is None:
            A.flag(f"struct:wall-type:{int(bb[0])}:{int(bb[1])}", "warning", "wall", "RCC wall with no readable type (150 / 230 thick) next to it", "Not measured.",
                   [ref("wall", "RCC wall", [bb], n)])
            continue
        rf = ref("wall", f"{wdef['tag']}", [bb] + ([lab] if lab else []), n)
        tm = wdef["t"] / 1000.0
        row = A.row("RCC", f"RCC {grade} in walls, {wdef['t']} mm thick", f"{wdef['tag']}", 1, round(L, 3), tm, hh, "m³",
                    f"length {SCALED}; thickness from note; height {'not given' if hh is None else 'from your answer'}",
                    "assumed" if hh is not None else "unresolved", [rf, wdef["ref"]], [hfid], fg, fo, drg, qty=0.0 if hh is None else None)
        if fw["wall_both_faces"]:
            A.row("SHUTTERING", "Shuttering – RCC wall (both faces)", f"{wdef['tag']}", 2, round(L, 3), None, hh, "m²", f"two faces; length {SCALED}",
                  "assumed" if hh is not None else "unresolved", [rf], [hfid], fg, fo, drg, qty=0.0 if hh is None else None)
        if hh is not None:
            per_face = 2 * kg_per_m(wdef["dia"]) / (wdef["s"] / 1000.0)      # horizontal + vertical
            A.bars_area("RCC walls", wdef["tag"], wdef["dia"], round(L * hh, 3), per_face * 2, "assumed",
                        f"{wdef['dia']}@{wdef['s']} both ways, both faces (note): kg/m² = 4 x kg/m ÷ spacing; length {SCALED}", [rf, wdef["ref"]], fg, fo, drg)
        out.append({"tag": wdef["tag"], "L": round(L, 2)})
    return out


def stubs(A, T, notes, grade, hfid, fg, fo, drg, n, Ld):
    cnt = Counter()
    where = defaultdict(list)
    for t in T["stub"]:
        k = re.sub(r"\s+", "", t.s.strip()).upper()
        if k == "STUB":
            continue
        cnt[k] += 1
        where[k].append(t)
    out = []
    h = A.answer(hfid)
    hh = float(h) / 1000.0 if h else None
    for k, c in sorted(cnt.items()):
        d = notes["stubs"].get(k)
        if d is None:
            A.flag(f"struct:stub-undefined:{k}", "warning", "stub", f"{k} ({c} on the plan) has no definition in the notes", "Not measured.",
                   [ref("stub", k, [t], n) for t in where[k][:20]])
            continue
        rf = ref("stub", f"{k} x{c}", where[k], n)
        kind = "hangers" if d["hanger"] else "stubs / column stubs"
        row = A.row("RCC", f"RCC {grade} in {kind}", f"{k} ({d['b']}x{d['d']}) – {d['upto'].lower()}", c, d["b"] / 1000.0, d["d"] / 1000.0, hh, "m³",
                    f"{c} tag(s) counted on the plan; height ('{d['upto'].lower()}') {'not given' if hh is None else 'floor height from your answer'}",
                    "assumed" if hh is not None else "unresolved", [rf, d["ref"]], [hfid], fg, fo, drg, qty=0.0 if hh is None else None)
        A.row("SHUTTERING", f"Shuttering – {kind} sides", f"{k} ({d['b']}x{d['d']})", c, 2 * (d["b"] + d["d"]) / 1000.0, hh, None, "m²", "perimeter x height",
              "assumed" if hh is not None else "unresolved", [rf], [hfid], fg, fo, drg, qty=0.0 if hh is None else None)
        if hh is not None:
            A.bars("Stubs, hangers, SP", f"{k} x{c} – {d['n']}-{d['bar']}", d["bar"], d["n"] * c, hh + Ld(d["bar"]), "assumed", f"bars over the height + Ld", [rf, d["ref"]], fg, fo, None, drg)
            cover = 0.04
            per = 2 * ((d["b"] / 1000 - cover) + (d["d"] / 1000 - cover)) + 20 * d["tie"]["dia"] / 1000
            A.bars("Stubs, hangers, SP", f"{k} x{c} – ties {d['tie']['dia']}@{d['tie']['s']}", d["tie"]["dia"], (math.floor(hh * 1000 / d["tie"]["s"]) + 1) * c, per, "assumed",
                   "ties over the height; cover 40 mm", [rf, d["ref"]], fg, fo, None, drg)
        out.append({"tag": k, "n": c, "def": d["text"]})
    return out
