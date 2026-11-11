"""Turn roof / floor-plan measurements into MB rows (with evidence and drawing refs)."""
from __future__ import annotations

import math
import re
from collections import defaultdict

import numpy as np
from scipy import ndimage

from .pairs import axis_segments, pair_lines
from .plan import FINISH_RE, NOT_FINISH_RE, local_thickness, norm_finish
from .raster import unique_rows
from .is1200 import CITE_BEAM, CITE_COL, CITE_LINTEL
from .types import MBRow

# trades (MB section headings)
T_MASONRY = "B. MASONRY"
T_RCC = "C. RCC"
T_PLASTER = "D. PLASTERING"
T_FLOOR = "E. FLOORING"
T_ROOF = "F. ROOF / TERRACE"
T_RAMP = "G. RAMP, STEPS, RAILINGS"
T_DW = "H. DOORS AND WINDOWS"

ITEM_PARAPET = "Brick masonry in parapet wall"
ITEM_SLAB = "RCC roof slab - plan area at each T.O.S. level (outer edge)"
ITEM_SLAB_VOL = "RCC roof slab - volume (area x thickness)"
ITEM_COPING = "Coping over parapet"
ITEM_COBA = "Brick coba / terrace treatment inside parapets"
ITEM_KHURRA = "Khurra"
ITEM_PL_INT = "Cement plaster to internal wall faces (CPWD deductions)"
ITEM_PL_EXT = "Cement plaster to external wall faces (CPWD deductions)"
ITEM_CEIL = "Cement plaster to ceiling (soffit) - roofed floor areas"
ITEM_GROOVE = "Groove in external plaster"
ITEM_FLOOR_NONE = "Floor areas with finish NOT specified (for information)"
ITEM_RAMP = "Ramp (plan area)"
ITEM_RAIL = "Railing / handrail"
ITEM_OPEN = "Doors, windows and ventilators - opening area (W x H)"


def masonry_item(word: str, half: bool, t_mm: float) -> tuple[str, str]:
    if word == "BRICK":
        if half:
            return f"Half-brick masonry ({t_mm:.0f} mm) in superstructure", "sqm"
        return f"Brick masonry ({t_mm:.0f} mm and thicker) in superstructure", "cum"
    if word == "AAC":
        return f"AAC block masonry ({t_mm:.0f} mm) in superstructure", "cum"
    return f"{word.title()} masonry in superstructure", "cum"


def finish_item(f: str) -> str:
    return f"Flooring: {f}"


# ---------------------------------------------------------------- refs
def ref(kind, label, bbox=None, handles=()):
    out = {"kind": kind, "label": label, "bbox": [round(float(v), 1) for v in bbox] if bbox else None,
           "handles": sorted({h for h in handles if h})}
    return out


class Ctx:
    """Shared state for row building."""

    def __init__(self, a, cfg, flags):
        self.a = a
        self.cfg = cfg
        self.flags = flags
        self.ents = a.geometry["entities"]
        self.rows: list[MBRow] = []
        self.flag_by_ref = defaultdict(list)
        for f in flags:
            for r in f.refs:
                self.flag_by_ref[r].append(f)
        self.assumptions: list[str] = []
        self.floor = ("", 0.0, "")   # (floor group, order, plan name) stamped on new rows

    def set_floor(self, group: str = "", order: float = 0.0, plan: str = ""):
        self.floor = (group, order, plan)

    def handle(self, i):
        if i is None or i < 0 or i >= len(self.ents):
            return None
        e = self.ents[i]
        return e.get("handle") or e.get("hd")

    def text_bbox(self, i, pad=0.0):
        e = self.ents[i]
        x, y = e["p"]
        h = e.get("h", 0) or 0
        n = max((len(s) for s in e["s"].split("\n")), default=1)
        ln = e["s"].count("\n") + 1
        return [x - pad, y - h * ln * 1.4 - pad, x + 0.7 * h * n + pad, y + h + pad]

    def text_ref(self, i, kind="text", label=None):
        if i is None:
            return None
        return ref(kind, label or " ".join(self.ents[i]["s"].split())[:60], self.text_bbox(i), [self.handle(i)])

    def flags_for(self, refs):
        out = []
        for r in refs:
            out += self.flag_by_ref.get(r, [])
        return out

    def add(self, trade, item, unit, location, nos, l=None, b=None, h=None, remarks="", refs=(), flags=(),
            assumed=False, qty=None, desc=""):
        r3 = lambda v: None if v is None else round(float(v), 3)  # noqa: E731
        nos = None if nos is None else (round(float(nos), 3) if not float(nos).is_integer() else int(nos))
        l, b, h = r3(l), r3(b), r3(h)
        if qty is None:
            q = 1.0
            any_ = False
            for v in (nos, l, b, h):
                if v is not None:
                    q *= v
                    any_ = True
            qty = q if any_ else 0.0
        flags = list({f.id: f for f in flags}.values())
        bits = [remarks] if remarks else []
        status = "assumed" if assumed else "ok"
        for f in flags:
            if f.status == "open":
                bits.append(f"UNRESOLVED {f.id}: {f.title}")
                status = "unresolved"
            elif f.status == "resolved":
                bits.append(f"per user ({f.id}): {f.value}")
            else:
                bits.append(f"accepted as drawn ({f.id})")
        if assumed and status == "ok":
            status = "assumed"
        row = MBRow(item=item, location=location, nos=nos, l=l, b=b, h=h, qty=round(qty, 4), unit=unit,
                    remarks="; ".join(dict.fromkeys(b_ for b_ in bits if b_)), id=f"r{len(self.rows) + 1}",
                    trade=trade, status=status, flag_ids=[f.id for f in flags],
                    refs=[r for r in refs if r][:200], floor_group=self.floor[0], floor_order=self.floor[1],
                    plan=self.floor[2], desc=desc or ("Deduct" if (nos or 0) < 0 else ""))
        self.rows.append(row)
        return row


# ---------------------------------------------------------------- roof rows
def roof_rows(ctx: Ctx, roof, cfg_roof: dict, slab_thk_mm: float | None, slab_flags):
    a = ctx.a
    to_m = a.to_m
    c2 = (roof.grid.cell * to_m) ** 2
    ptot = float(roof.parapet_h_mm or 600)
    coping_mm = float(cfg_roof.get("coping_mm") or 100)
    coping_assumed = "coping_mm" not in cfg_roof
    p_note = {"user": "your value (Review)",
              "note": f"note '{roof.parapet_note}'",
              "default": "default (no parapet note found) – ASSUMED"}[getattr(roof, "parapet_h_source", "note")]
    h_assumed = getattr(roof, "parapet_h_source", "note") == "default"
    brick_h = max(ptot - coping_mm, 0)
    t = roof.parapet_t_mm
    out = {"coping_len": 0.0, "levels": []}
    for li, L in enumerate(roof.levels):
        lvl = f"T.O.S. {L.tos_mm:+.0f}" if L.tos_mm is not None else f"roof level {li + 1} (no T.O.S. text)"
        loc = f"{a.region_name(roof.region)} {lvl}"
        area = (L.cells - L.slab_corr) * c2
        sref = ref("slab", f"roof outline {lvl}", L.bbox, [ctx.handle(e) for s in L.strips for e in s.ents])
        tref = ctx.text_ref(L.text_ent, "text")
        plen = sum(s.length for s in L.strips) * to_m
        strip_refs = [ref("parapet", f"parapet {s.length * to_m:.2f} m x {s.t * to_m * 1000:.0f}", s.rect(),
                          [ctx.handle(e) for e in s.ents]) for s in L.strips]
        out["levels"].append({"tos": L.tos_mm, "area": area, "parapet_len": plen, "loc": loc})
        out["coping_len"] += plen
        unk = [] if L.tos_mm is not None else ["level not given in the drawing"]
        ctx.add(T_RCC, ITEM_SLAB, "sqm", loc, 1, area, None, None,
                "area enclosed by the parapet outer faces on the roof plan "
                + (f"({lvl} from text '{L.text}')" if L.tos_mm is not None else "(no T.O.S. level written on the roof plan)")
                + "; thickness/beams not in drawing", [sref, tref], slab_flags, assumed=bool(unk),
                desc="Slab area")
        if slab_thk_mm:
            ctx.add(T_RCC, ITEM_SLAB_VOL, "cum", loc, 1, area, None, slab_thk_mm / 1000,
                    "slab area x thickness entered by the user (beams not included)", [sref], slab_flags, desc="Slab volume")
        if plen:
            ctx.add(T_MASONRY, ITEM_PARAPET, "cum", loc, 1, plen, t / 1000, brick_h / 1000,
                    f"centre line of paired parapet lines ({len(L.strips)} runs, joined at corners); thickness {t:.0f} and "
                    f"total height {ptot:.0f} from {p_note}; brick height = total - coping {coping_mm:.0f}"
                    + (" (coping thickness ASSUMED)" if coping_assumed else ""), strip_refs, assumed=coping_assumed or h_assumed,
                    desc="Parapet")
            ctx.add(T_ROOF, ITEM_COPING, "m", loc, 1, plen, None, None,
                    "same centre line as the parapet; coping section not dimensioned", strip_refs, desc="Coping")
        # coba / terrace treatment
        ctx.add(T_ROOF, ITEM_COBA, "sqm", loc, 1, area, None, None, "slab area at this level", [sref], desc="Slab area")
        if plen:
            ctx.add(T_ROOF, ITEM_COBA, "sqm", loc, -1, plen, t / 1000, None, "deduct parapet footprint", strip_refs, desc="Deduct parapet footprint")
        tank_cells = L.tank_cells
        if tank_cells:
            tk = [tk for tk in roof.tanks if tk.get("bbox")]
            ctx.add(T_ROOF, ITEM_COBA, "sqm", loc, -1, tank_cells * c2, None, None,
                    "deduct tank footprint (closed outline around the tank text, inside the parapets)",
                    [ref("tank", tk_["text"], tk_["bbox"], [ctx.handle(tk_["ent"])]) for tk_ in tk],
                    ctx.flags_for(["tank"]), desc="Deduct tank footprint")
    # khurras
    by_level = defaultdict(list)
    for i, tx in roof.khurras:
        j, k = roof.grid.row(tx["p"][1]), roof.grid.col(tx["p"][0])
        li = int(roof.level_of[j, k]) if roof.grid.inside(j, k) else -1
        by_level[li].append(i)
    for li, ids in sorted(by_level.items()):
        L = roof.levels[li] if li >= 0 else None
        lvl = f"T.O.S. {L.tos_mm:+.0f}" if L and L.tos_mm is not None else "roof"
        ctx.add(T_ROOF, ITEM_KHURRA, "nos", f"{a.region_name(roof.region)} {lvl}", len(ids), None, None, None,
                "count of 'KHURRA' notes on the roof plan (one per khurra; duplicates within 1 m merged)",
                [ctx.text_ref(i, "khurra") for i in ids], desc="Khurras")
    out["ptot"] = ptot
    out["coping_mm"] = coping_mm
    return out


# ---------------------------------------------------------------- floor rows
def _zone_label(fp, zi):
    z = fp.zones[zi]
    return z["name"]


def _height_flags(ctx, fp, zi, height_flag_for_level):
    z = fp.zones[zi]
    fl = []
    if z.get("flag"):
        fl.append(z["flag"])
    if not z.get("height_mm"):
        lv = z.get("level")
        if lv is not None and lv in height_flag_for_level:
            fl.append(height_flag_for_level[lv])
    return fl


def floor_rows(ctx: Ctx, fp, roof_info, cfg, height_flag_for_level, finish_flags):
    a = ctx.a
    g = fp.grid
    c2 = fp.cell_m ** 2
    walls_cfg = cfg.get("walls", {})
    nom = {"BRICK": float((walls_cfg.get("wall-brick") or {}).get("thickness_mm") or 230),
           "AAC": float((walls_cfg.get("wall-AAC") or {}).get("thickness_mm") or 200)}
    half_t = float((walls_cfg.get("wall-brick-half") or {}).get("thickness_mm") or 115)
    thr = (half_t + nom["BRICK"]) / 2
    floors = fp.floors
    fl_note = f" x {floors} floors" if floors > 1 else ""
    region = fp.name

    def zone_of(j, i):
        return int(fp.zone[j, i])

    def hsrc_text(zi, hmm):
        z = fp.zones[zi]
        if z.get("height_mm"):
            return f"height {hmm:.0f} from {z['source']}"
        return f"height {hmm:.0f} ASSUMED (default; {z['name']})"

    def hflags_at(j, i):
        hz = int(fp.hsrc[j, i])
        fl = []
        if hz and fp.zones[hz].get("flag"):
            fl.append(fp.zones[hz]["flag"])
        if not hz:
            fl += _height_flags(ctx, fp, int(fp.zone[j, i]), height_flag_for_level)
        return fl, not hz

    # ---------- masonry
    brick = cfg.get("_brick") or {}
    mw = fp.masonry | (fp.linewall >= 0)
    tl = local_thickness(mw, fp.cell_mm)
    words = fp.mat_codes
    half = mw & (fp.mat == words.index("BRICK")) & (tl <= thr)
    dh = getattr(fp, "brick_dh", None)
    if dh is None:
        dh = np.zeros(g.shape, dtype=np.float32)
    brick_h = fp.wall_h - dh
    hkey = np.round(brick_h).astype(np.int32)
    ukey = np.round(dh).astype(np.int32)
    sel = np.nonzero(mw)
    zz = fp.zone[sel]
    mm = fp.mat[sel]
    hh = hkey[sel]
    hs = half[sel]
    lw = fp.linewall[sel]
    hsrc = fp.hsrc[sel]
    ub = ukey[sel]
    combo = np.stack([zz, mm, hs.astype(np.int32), hh, (lw >= 0).astype(np.int32), hsrc, ub], axis=1)
    uniq, inv, counts = unique_rows(combo, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    # columns embedded in the wall (IS 1200): cells of each group that are column footprint
    colm = (fp.col_in_wall[sel] if brick.get("cols") and getattr(fp, "col_in_wall", None) is not None
            else np.zeros(len(inv), dtype=bool))
    col_counts = np.bincount(inv[colm], minlength=len(uniq)) if colm.any() else np.zeros(len(uniq), dtype=np.int64)
    col_refs = defaultdict(list)       # group -> refs of the column pieces in it
    if colm.any():
        cm = np.zeros(g.shape, dtype=bool)
        cm[sel[0][colm], sel[1][colm]] = True
        lab_c, nc = ndimage.label(cm)
        cref = {}
        for k, sl in enumerate(ndimage.find_objects(lab_c), 1):
            if sl is not None:
                cref[k] = ref("column", "RCC column in wall", [g.x0 + sl[1].start * g.cell, g.y0 + sl[0].start * g.cell,
                                                               g.x0 + sl[1].stop * g.cell, g.y0 + sl[0].stop * g.cell], [])
        for gi_, k in unique_rows(np.stack([inv[colm], lab_c[sel[0][colm], sel[1][colm]]], axis=1)):
            if int(k) in cref and len(col_refs[int(gi_)]) < 60:
                col_refs[int(gi_)].append(cref[int(k)])
    # refs per group: hatch entities / line strips via find_objects on a key image
    key_img = np.zeros(g.shape, dtype=np.int32)
    key_img[sel] = inv + 1
    gid_hatch = defaultdict(set)
    hid_sel = fp.hid[sel]
    gh = unique_rows(np.stack([inv, hid_sel], axis=1))
    for gi, h_ in gh:
        gid_hatch[int(gi)].add(int(h_))
    gl = unique_rows(np.stack([inv, lw], axis=1))
    gid_lines = defaultdict(set)
    for gi, k in gl:
        if k >= 0:
            gid_lines[int(gi)].add(int(k))
    hatch_bbox = {}
    hobjs = ndimage.find_objects(fp.hid + 1)
    for i_, sl in enumerate(hobjs):
        if sl is not None:
            hatch_bbox[i_] = [g.x0 + sl[1].start * g.cell, g.y0 + sl[0].start * g.cell,
                              g.x0 + sl[1].stop * g.cell, g.y0 + sl[0].stop * g.cell]
    beam_flags = [brick["beam_flag"]] if brick.get("beam_flag") is not None else []
    beam_note = brick.get("beam_note", "")
    mas_rows = {}
    for gi, ((zi, mc, is_half, H, is_line, hz, U), n) in enumerate(zip(uniq, counts)):
        word = words[mc] if 0 <= mc < len(words) else "BRICK"
        area = n * c2
        t_nom = half_t if is_half else nom.get(word, nom["BRICK"])
        item, unit = masonry_item(word, bool(is_half), t_nom)
        refs = [ref("wall", f"{word} wall hatch", hatch_bbox.get(h_), [ctx.handle(h_)]) for h_ in sorted(gid_hatch[gi]) if h_ >= 0]
        low_txt = ""
        for k in sorted(gid_lines[gi]):
            ls = fp.strips[k]
            s = ls["strip"]
            refs.append(ref("wall", f"wall drawn as lines ({s.t * a.to_m * 1000:.0f} thk)", s.rect(),
                            [ctx.handle(e) for e in s.ents]))
            if ls["low"]:
                refs.append(ctx.text_ref(ls["low"][0], "text"))
                low_txt = f"height {ls['low'][1]:.0f} from text '{' '.join(ctx.ents[ls['low'][0]]['s'].split())}'"
        zfl = []
        assumed = False
        Hf = H + U
        if not low_txt:
            if hz:
                if fp.zones[hz].get("flag"):
                    zfl.append(fp.zones[hz]["flag"])
                htxt = f"height {Hf:.0f} from {fp.zones[hz]['source']} ({fp.zones[hz]['name']})"
            else:
                zfl += _height_flags(ctx, fp, int(zi), height_flag_for_level)
                htxt = f"height {Hf:.0f} ASSUMED default"
                assumed = True
        else:
            htxt = low_txt
        if U:
            htxt += (f" less beam {U:.0f} = {H:.0f} under the beam ({CITE_BEAM}; beam depth {brick.get('beam_src', '')}"
                     + (f"; {beam_note}" if beam_note else "") + ")")
            assumed = assumed or brick.get("beam_assumed", False)
        src = ("legend hatch" if not is_line else "paired wall-role lines (no hatch)")
        rem = (f"plan area {area:.3f} m2 of {word} {src} in {region}{fl_note} / {t_nom:.0f} nominal thk = gross length; "
               f"{'local thickness <= ' + format(thr, '.0f') + ' -> half brick; ' if word == 'BRICK' else ''}{htxt}; "
               + ("RCC columns in the wall deducted below" if col_counts[gi] else "columns (voids/column layers) excluded"))
        zname = _zone_label(fp, int(zi))
        loc = f"{region} / {zname}"
        L = area / (t_nom / 1000)
        bfl = beam_flags if (brick.get("beam_flag_rows") and not low_txt) else []
        desc = "Gross wall" + (" (under beam)" if U else "")
        if unit == "cum":
            r = ctx.add(T_MASONRY, item, unit, loc, floors, L, t_nom / 1000, H / 1000, rem, refs, zfl + bfl, assumed, desc=desc)
        else:
            r = ctx.add(T_MASONRY, item, unit, loc, floors, L, None, H / 1000, rem, refs, zfl + bfl, assumed, desc=desc)
        mas_rows[gi] = r
        if col_counts[gi]:
            ca = col_counts[gi] * c2
            Lc = ca / (t_nom / 1000)
            ctx.add(T_MASONRY, item, unit, loc, -floors, Lc, t_nom / 1000 if unit == "cum" else None, H / 1000,
                    f"deduct RCC columns embedded in this wall: column footprint on the masonry {ca:.3f} m2 / {t_nom:.0f} thk "
                    f"x height {H:.0f} ({CITE_COL})", col_refs.get(gi, []), zfl + bfl, assumed, desc="Deduct RCC columns in wall")

    # openings: gross across the gap, then deduct (CPWD: deduct openings > 0.1 m2)
    lintel = brick.get("lintel") or (None, 150.0, "", None)
    lint_d, lint_b, lint_src = lintel[0], lintel[1], lintel[2]
    lint_flag = brick.get("lintel_flag")
    groups = defaultdict(list)
    for op in fp.placed:
        if not op.gap or not op.w_mm or not op.h_mm:
            continue
        cj, ci = g.row(op.gap["y"]), g.col(op.gap["x"])
        word = words[op.gap["mat"]] if 0 <= op.gap["mat"] < len(words) else "BRICK"
        is_half = word == "BRICK" and op.gap["t_mm"] <= thr
        H = float(fp.height[cj, ci]) - float(dh[cj, ci])
        groups[(op.tag, int(fp.zone[cj, ci]), word, is_half, H, int(fp.hsrc[cj, ci]), float(dh[cj, ci]))].append(op)
    for (tag, zi, word, is_half, H, hz, U), ops in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        op = ops[0]
        t_nom = half_t if is_half else nom.get(word, nom["BRICK"])
        item, unit = masonry_item(word, is_half, t_nom)
        n = len(ops) * floors
        refs = []
        for o in ops:
            refs.append(ctx.text_ref(o.ent, "opening", f"tag {o.tag}"))
            refs.append(ref("opening", f"{o.tag} gap {o.gap['width_mm']:.0f} wide in {o.gap['t_mm']:.0f} wall", o.gap["bbox"], []))
        fl = ctx.flags_for([f"tag:{tag}"])
        zfl = [fp.zones[hz]["flag"]] if hz and fp.zones[hz].get("flag") else ([] if hz else _height_flags(ctx, fp, zi, height_flag_for_level))
        sz_assumed = op.size_note.startswith("ASSUMED")
        loc = f"{region} / {_zone_label(fp, zi)}"
        B = None if unit == "sqm" else t_nom / 1000
        bfl = beam_flags if brick.get("beam_flag_rows") else []
        ctx.add(T_MASONRY, item, unit, loc, n, op.w_mm / 1000, B, H / 1000,
                f"add wall across {tag} opening (gap not hatched); width {op.size_note}; wall height {H:.0f}"
                + (f" (under beam {U:.0f}; {CITE_BEAM})" if U else ""),
                refs, fl + zfl + bfl, sz_assumed or not hz, desc=f"Add wall across {tag}")
        lint_txt = ""
        if brick.get("lintels_on"):
            if lint_d is None:
                lint_txt = "; lintel NOT deducted (lintel size not given)"
            elif op.h_mm + lint_d > H + 1:
                lint_txt = f"; no lintel deducted: opening head + lintel {op.h_mm + lint_d:.0f} reaches the beam/slab ({H:.0f})"
            else:
                lint_txt = ""
        else:
            lint_txt = "; lintels not deducted" + (" (switched off: brickwork.deduct_lintels)" if brick.get("is1200") else
                                                    " (size not in drawing)")
        ctx.add(T_MASONRY, item, unit, loc, -n, op.w_mm / 1000, B, op.h_mm / 1000,
                f"deduct {tag} {op.w_mm:.0f}x{op.h_mm:.0f} ({op.size_note}){lint_txt}",
                refs, fl + ([lint_flag] if (brick.get("lintels_on") and lint_d is None and lint_flag is not None) else []),
                sz_assumed, desc=f"Deduct {tag} ({op.w_mm:.0f}x{op.h_mm:.0f})")
        if brick.get("lintels_on") and lint_d is not None and op.h_mm + lint_d <= H + 1:
            Ll = op.w_mm + 2 * lint_b
            ctx.add(T_MASONRY, item, unit, loc, -n, Ll / 1000, B, lint_d / 1000,
                    f"deduct RCC lintel over {tag}: ({op.w_mm:.0f} + 2 x {lint_b:.0f} bearing) x {lint_d:.0f} deep"
                    + (f" x wall {t_nom:.0f}" if B else "") + f"; lintel size from {lint_src} ({CITE_LINTEL})",
                    refs, fl + ([lint_flag] if lint_flag is not None else []), sz_assumed or brick.get("lintel_assumed", False),
                    desc=f"Deduct lintel over {tag}")

    # ---------- plaster faces
    pl = plaster(ctx, fp, roof_info, cfg, thr, nom, half_t)
    plaster_rows(ctx, fp, pl, cfg)
    # ---------- spaces: ceiling + flooring
    space_rows(ctx, fp, cfg, finish_flags)
    return pl


def plaster(ctx, fp, roof_info, cfg, thr, nom, half_t):
    a = ctx.a
    g = fp.grid
    cell_m = fp.cell_m
    solid = fp.solid
    mw = fp.masonry | (fp.linewall >= 0)
    lab, n = ndimage.label(solid)
    keep = np.unique(lab[mw])
    keep = keep[keep > 0]
    fsolid = np.isin(lab, keep)
    ground = float(cfg.get("ground_level_mm") or 0)
    ground_assumed = "ground_level_mm" not in cfg
    ptot = (roof_info or {}).get("ptot") or 0.0
    lowmask = np.zeros(g.shape, dtype=bool)
    for k, ls in enumerate(fp.strips):
        if ls["low"]:
            lowmask |= fp.linewall == k
    reach = max(int(round(80 / fp.cell_mm)), 2)
    internal = defaultdict(float)      # (owner, H) -> length m
    external = defaultdict(float)      # (zone, Hext, tos) -> m
    band = defaultdict(float)          # (zone, tos_s, tos_e, band_h) -> m
    face_hatch = defaultdict(set)
    ny, nx = g.shape
    for dj, di in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        js, is_ = np.nonzero(fsolid)
        je, ie = js + dj, is_ + di
        ok = (je >= 0) & (je < ny) & (ie >= 0) & (ie < nx)
        js, is_, je, ie = js[ok], is_[ok], je[ok], ie[ok]
        m = ~solid[je, ie]
        js, is_ = js[m], is_[m]
        # classify the face by what lies beyond it: a room (owned roofed cell) -> internal,
        # unroofed -> external; barriers/voids/slivers are stepped over (up to ~600 mm)
        jp = np.clip(js + reach * dj, 0, ny - 1)
        ip = np.clip(is_ + reach * di, 0, nx - 1)
        decided = np.zeros(len(js), dtype=bool)
        kind = np.zeros(len(js), dtype=np.int8)      # 1 internal, 2 external, 0 skip
        for k in (reach, 2 * reach, 4 * reach, max(int(round(600 / fp.cell_mm)), 8 * reach)):
            jk = np.clip(js + k * dj, 0, ny - 1)
            ik = np.clip(is_ + k * di, 0, nx - 1)
            und = ~decided
            sol = solid[jk, ik]
            dec_int = und & ~sol & (fp.owner[jk, ik] >= 0)
            dec_ext = und & ~sol & ~fp.roofed[jk, ik]
            decided |= und & sol           # solid again: narrow gap / enclosed void -> no face
            for m_, kv in ((dec_int, 1), (dec_ext, 2)):
                kind[m_] = kv
                jp[m_] = jk[m_]
                ip[m_] = ik[m_]
                decided |= m_
        keep = kind > 0
        js, is_, jp, ip, kind = js[keep], is_[keep], jp[keep], ip[keep], kind[keep]
        roofed = kind == 1
        Hs = fp.wall_h[js, is_]
        He = fp.height[jp, ip]
        low = lowmask[js, is_]
        own = fp.owner[jp, ip]
        tos_s = fp.tos[js, is_]
        tos_e = fp.tos[jp, ip]
        zs = fp.zone[js, is_]
        hid = fp.hid[js, is_]
        # internal
        ii = roofed
        # a room under a higher roof than the wall's own block: the wall continues up (gable)
        higher = np.isfinite(tos_e[ii]) & np.isfinite(tos_s[ii]) & (tos_e[ii] > tos_s[ii] + 1) & ~low[ii]
        Hf = np.where(higher, He[ii], np.minimum(Hs[ii], He[ii]))
        for (o, h), c in zip(*unique_rows(np.stack([own[ii], np.round(Hf).astype(np.int32)], axis=1), return_counts=True)):
            internal[(int(o), int(h))] += c * cell_m
        for o, h_ in unique_rows(np.stack([own[ii], hid[ii]], axis=1)):
            if h_ >= 0:
                face_hatch[("i", int(o))].add(int(h_))
        # external
        ee = ~roofed
        Hx = np.where(low[ee] | ~np.isfinite(tos_s[ee]), Hs[ee], tos_s[ee] + ptot - ground)
        ts = np.where(np.isfinite(tos_s[ee]), tos_s[ee], -1)
        for (z, h, t), c in zip(*unique_rows(np.stack([zs[ee], np.round(Hx).astype(np.int32), np.round(ts).astype(np.int32)], axis=1),
                                            return_counts=True)):
            external[(int(z), int(h), int(t))] += c * cell_m
        for z, h_ in unique_rows(np.stack([zs[ee], hid[ee]], axis=1)):
            if h_ >= 0:
                face_hatch[("e", int(z))].add(int(h_))
        # band above a lower roof
        bb = roofed & ~low & np.isfinite(tos_s) & np.isfinite(tos_e) & (tos_s > tos_e + 1)
        if bb.any():
            bh = tos_s[bb] + ptot - tos_e[bb]
            for (z, t1, t2, h), c in zip(*unique_rows(np.stack([zs[bb], np.round(tos_s[bb]).astype(np.int32),
                                                              np.round(tos_e[bb]).astype(np.int32), np.round(bh).astype(np.int32)], axis=1),
                                                    return_counts=True)):
                band[(int(z), int(t1), int(t2), int(h))] += c * cell_m

    # opening sides for deductions
    words = fp.mat_codes
    open_ded = []
    for op in fp.placed:
        if not op.gap or not op.w_mm or not op.h_mm:
            continue
        gp = op.gap
        word = words[gp["mat"]] if 0 <= gp["mat"] < len(words) else "BRICK"
        t_nom = half_t if (word == "BRICK" and gp["t_mm"] <= thr) else nom.get(word, nom["BRICK"])
        sides = []
        off = (gp["t_mm"] / 2 + 100) / (a.to_m * 1000)
        for sgn in (-1, 1):
            px, py = (gp["x"], gp["y"] + sgn * off) if gp["horizontal"] else (gp["x"] + sgn * off, gp["y"])
            j, i = g.row(py), g.col(px)
            if not g.inside(j, i) or solid[j, i]:
                sides.append(("x", None))
                continue
            if fp.roofed[j, i]:
                sides.append(("i", int(fp.owner[j, i])))
            else:
                sides.append(("e", int(fp.zone[g.row(gp["y"]), g.col(gp["x"])])))
        open_ded.append((op, t_nom, sides))
    return {"internal": internal, "external": external, "band": band, "open": open_ded, "face_hatch": face_hatch,
            "ground": ground, "ground_assumed": ground_assumed, "ptot": ptot}


def plaster_rows(ctx, fp, pl, cfg):
    a = ctx.a
    floors = fp.floors
    region = fp.name
    sp_by_id = {s.id: s for s in fp.spaces}
    hatch_refs = lambda key: [ref("wall", "wall face", None, [ctx.handle(h) for h in sorted(pl["face_hatch"].get(key, ()))])]  # noqa: E731

    def sp_name(o):
        s = sp_by_id.get(o)
        return s.name if s else "roofed area (small/unallocated)"

    def sp_refs(o):
        s = sp_by_id.get(o)
        if not s:
            return []
        return [ref("room", s.name, s.bbox, [ctx.handle(e) for e in s.text_ents])]

    def sp_flags(o):
        s = sp_by_id.get(o)
        if not s:
            return []
        return ctx.flags_for([f"room:{r.name}@{r.x:.0f},{r.y:.0f}" for r in s.labels])

    # deductions per CPWD
    ded_int = defaultdict(list)
    ded_ext = defaultdict(list)
    for op, t_nom, sides in pl["open"]:
        A = op.w_mm * op.h_mm / 1e6
        if A <= 0.5:
            continue
        ints = [s for s in sides if s[0] == "i"]
        exts = [s for s in sides if s[0] == "e"]
        if A <= 3:
            if ints:
                tgt = min(ints, key=lambda s: sp_by_id[s[1]].cells if s[1] in sp_by_id else 1e18)
                ded_int[tgt[1]].append((op, "0.5-3 m2: deducted on one face only"))
            elif exts:
                ded_ext[exts[0][1]].append((op, "0.5-3 m2: deducted on one face only"))
        else:
            for s in ints:
                ded_int[s[1]].append((op, "> 3 m2: deducted on both faces"))
            for s in exts:
                ded_ext[s[1]].append((op, "> 3 m2: deducted on both faces"))
            per = 2 * op.h_mm + op.w_mm * (2 if op.kind in ("window", "ventilator") else 1)
            tgt = ints[0] if ints else (exts[0] if exts else None)
            if tgt:
                (ded_int if tgt[0] == "i" else ded_ext)[tgt[1]].append((op, ("reveal", per, t_nom)))

    def ded_rows(item, loc, lst):
        for op, how in lst:
            fl = ctx.flags_for([f"tag:{op.tag}"])
            assumed = op.size_note.startswith("ASSUMED")
            orefs = [ctx.text_ref(op.ent, "opening", f"tag {op.tag}"), ref("opening", op.tag, op.gap["bbox"], [])]
            if isinstance(how, tuple):
                _, per, t_nom = how
                ctx.add(T_PLASTER, item, "sqm", loc, floors, per / 1000, None, t_nom / 1000,
                        f"add jambs/soffit{'/sill' if op.kind in ('window', 'ventilator') else ''} of {op.tag} (> 3 m2): "
                        f"reveal perimeter {per / 1000:.2f} m x wall thickness {t_nom:.0f} (CPWD; IS 1200 Part 12)", orefs, fl,
                        assumed, desc=f"Add jambs/soffit of {op.tag}")
            else:
                ctx.add(T_PLASTER, item, "sqm", loc, -floors, op.w_mm / 1000, None, op.h_mm / 1000,
                        f"deduct {op.tag} {op.w_mm:.0f}x{op.h_mm:.0f}: {how} (CPWD; IS 1200 Part 12)", orefs, fl, assumed,
                        desc=f"Deduct {op.tag} ({op.w_mm:.0f}x{op.h_mm:.0f})")

    # internal
    by_owner = defaultdict(list)
    for (o, h), L in pl["internal"].items():
        by_owner[o].append((h, L))
    for o in sorted(by_owner, key=lambda o: sp_name(o)):
        loc = f"{region} / {sp_name(o)}"
        for h, L in sorted(by_owner[o]):
            ctx.add(T_PLASTER, ITEM_PL_INT, "sqm", loc, floors, L, None, h / 1000,
                    f"wall faces bordering this space (incl. across tagged openings), height {h:.0f} "
                    "(lower of wall height and room height; room height where the room's roof is higher)", sp_refs(o) + hatch_refs(("i", o)), sp_flags(o),
                    desc="Wall faces")
        ded_rows(ITEM_PL_INT, loc, ded_int.get(o, []))
    # external
    ground_txt = f"ground {pl['ground']:.0f}" + (" ASSUMED (FGL +-0)" if pl["ground_assumed"] else "")
    for (z, h, t), L in sorted(pl["external"].items()):
        zname = _zone_label(fp, z)
        loc = f"{region} / {zname}"
        if t >= 0:
            how = f"from {ground_txt} to top of parapet (T.O.S. {t:+d} + parapet {pl['ptot']:.0f})"
        else:
            how = "wall height (no roof over this wall)"
        ctx.add(T_PLASTER, ITEM_PL_EXT, "sqm", loc, floors, L, None, h / 1000,
                f"external wall faces (outside the roof outline), height {how}", hatch_refs(("e", z)),
                assumed=pl["ground_assumed"], desc="External wall faces")
    for z in sorted(ded_ext):
        ded_rows(ITEM_PL_EXT, f"{region} / {_zone_label(fp, z)}", ded_ext[z])
    for (z, t1, t2, h), L in sorted(pl["band"].items()):
        ctx.add(T_PLASTER, ITEM_PL_EXT, "sqm", f"{region} / {_zone_label(fp, z)}", floors, L, None, h / 1000,
                f"wall of the higher block above the lower roof: T.O.S. {t1:+d} + parapet - lower roof T.O.S. {t2:+d}",
                hatch_refs(("e", z)), desc="Wall above lower roof")


def parapet_inner_rows(ctx, roof_info, cfg):
    rc = cfg.get("roof") or {}
    coba = float(rc.get("coba_mm") or 150)
    for lv in roof_info["levels"]:
        if lv["parapet_len"]:
            ctx.add(T_PLASTER, ITEM_PL_EXT, "sqm", lv["loc"], 1, lv["parapet_len"], None, (roof_info["ptot"] - coba) / 1000,
                    f"parapet inner faces above the terrace treatment: parapet {roof_info['ptot']:.0f} - coba {coba:.0f}"
                    + (" (coba thickness ASSUMED)" if "coba_mm" not in rc else ""), [], assumed="coba_mm" not in rc,
                    desc="Parapet inner faces")


VOID_RE = re.compile(r"\b(LIFT|ELEVATOR|SHAFT|DUCT|VOID|CUT\s*-?OUT|O\.?T\.?S\.?)\b", re.I)


def is_void(name: str) -> bool:
    """Lift wells, shafts, ducts and cut-outs have no floor or ceiling finish."""
    return bool(VOID_RE.search(name or ""))


def space_rows(ctx, fp, cfg, finish_flags):
    c2 = fp.cell_m ** 2
    floors = fp.floors
    region = fp.name
    voids = [sp for sp in fp.spaces if sp.labels and is_void(sp.name)]
    if voids:
        ctx.assumptions.append(f"{region}: no floor/ceiling measured for " + ", ".join(sorted({sp.name for sp in voids})))
    keep = [sp for sp in fp.spaces if sp not in voids]
    for sp in sorted(keep, key=lambda s: s.name):
        area = sp.cells * c2
        loc = f"{region} / {sp.name}"
        refs = [ref("room", sp.name, sp.bbox, [ctx.handle(e) for e in sp.text_ents])]
        fl = ctx.flags_for([f"room:{r.name}@{r.x:.0f},{r.y:.0f}" for r in sp.labels])
        ctx.add(T_PLASTER, ITEM_CEIL, "sqm", loc, floors, area, None, None,
                "net enclosed roofed area of this space (walls and columns excluded)" + (f"; {sp.note}" if sp.note else ""),
                refs, fl, desc=sp.name)
    for sp in sorted(keep, key=lambda s: s.name):
        area = sp.cells * c2
        loc = f"{region} / {sp.name}"
        refs = [ref("room", sp.name, sp.bbox, [ctx.handle(e) for e in sp.text_ents])]
        if sp.finish_ref:
            refs.append(sp.finish_ref)
        fl = ctx.flags_for([f"room:{r.name}@{r.x:.0f},{r.y:.0f}" for r in sp.labels])
        ff = finish_flags.get(sp.id)
        if sp.finish:
            ctx.add(T_FLOOR, finish_item(sp.finish), "sqm", loc, floors, area, None, None,
                    f"finish from {sp.finish_src}; net enclosed area" + (f"; {sp.note}" if sp.note else ""), refs,
                    fl + ([ff] if ff else []), assumed=sp.finish_src.startswith("ASSUMED"), desc=sp.name)
        else:
            ctx.add(T_FLOOR, ITEM_FLOOR_NONE, "sqm", loc, floors, area, None, None,
                    "finish not given in the drawing - not a payable item until confirmed", refs,
                    fl + ([ff] if ff else []), desc=sp.name)


# ---------------------------------------------------------------- misc
def dedup_length(items, tol, unit):
    """Total length of axis-aligned lines with near-coincident duplicates (within tol) merged."""
    hs, vs, skipped = axis_segments(items, 2 * unit)
    total = 0.0
    for segs in (hs, vs):
        segs = sorted(segs)
        groups = []
        for pos, lo, hi, e in segs:
            if groups and pos - groups[-1][0] <= tol:
                groups[-1][1].append((lo, hi))
                groups[-1][2].add(e)
            else:
                groups.append([pos, [(lo, hi)], {e}])
        for pos, ivs, es in groups:
            ivs.sort()
            cur = None
            for lo, hi in ivs:
                if cur and lo <= cur[1]:
                    cur[1] = max(cur[1], hi)
                else:
                    if cur:
                        total += cur[1] - cur[0]
                    cur = [lo, hi]
            if cur:
                total += cur[1] - cur[0]
    return total, skipped


def grooves_rows(ctx):
    a = ctx.a
    unit = 1 / (a.to_m * 1000)
    ents = ctx.ents
    by_region = defaultdict(list)
    for i, e in enumerate(ents):
        if e["t"] not in ("L", "P") or a.roles[e["l"]] != "groove":
            continue
        rid = a.region_of(e["p"][0], e["p"][1])
        if a.kind(rid) != "elevation" or (a.reg_by_id[rid].get("include") is False and a.reg_by_id[rid].get("exclude_from_mb")):
            continue
        p = e["p"]
        pts = list(zip(p[0::2], p[1::2]))
        src = e["pi"] if e.get("v") and e.get("pi") is not None else i
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            by_region[rid].append((x1, y1, x2, y2, src))
    skipped_sections = sum(1 for e in ents if e["t"] in ("L", "P") and a.roles[e["l"]] == "groove"
                           and a.kind(a.region_of(e["p"][0], e["p"][1])) == "section")
    for rid, items in sorted(by_region.items()):
        L, sk = dedup_length(items, 40 * unit, unit)
        xs = [v for it in items for v in (it[0], it[2])]
        ys = [v for it in items for v in (it[1], it[3])]
        ctx.add(T_PLASTER, ITEM_GROOVE, "m", a.region_name(rid), 1, L * a.to_m, None, None,
                f"groove-role lines in this elevation; paired/duplicate lines within 40 mm counted once"
                + (f"; {sk} sloping segments ignored" if sk else "")
                + ("; grooves shown in sections are not counted (duplicates of elevations)" if skipped_sections else ""),
                [ref("groove", a.region_name(rid), [min(xs), min(ys), max(xs), max(ys)], [ctx.handle(it[4]) for it in items])],
                desc="Grooves")


RAMP_RE = re.compile(r"(\d{3,5})\s*MM\s*WIDE\s*RAMP", re.I)


def ramp_rows(ctx):
    a = ctx.a
    unit = 1 / (a.to_m * 1000)
    ents = ctx.ents
    rails = []
    for i, e in enumerate(ents):
        if e["t"] in ("L", "P") and a.roles[e["l"]] == "railing" and a.is_plan(a.region_of(e["p"][0], e["p"][1])):
            p = e["p"]
            pts = list(zip(p[0::2], p[1::2]))
            src = e["pi"] if e.get("v") and e.get("pi") is not None else i
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                rails.append((x1, y1, x2, y2, src))
    if not rails:
        return
    hs, vs, sk = axis_segments(rails, 2 * unit)
    strips = pair_lines(hs, True, 10 * unit, 150 * unit, 200 * unit) + pair_lines(vs, False, 10 * unit, 150 * unit, 200 * unit)
    used = set()
    total = 0.0
    for s in strips:
        total += s.length
        used |= s.ents
    single = [r for r in rails if r[4] not in used]
    single_len = sum(math.hypot(r[2] - r[0], r[3] - r[1]) for r in single)
    total_m = (total + single_len) * a.to_m
    xs = [v for r in rails for v in (r[0], r[2])]
    ys = [v for r in rails for v in (r[1], r[3])]
    bb = [min(xs), min(ys), max(xs), max(ys)]
    rrefs = [ref("railing", "railing lines", bb, [ctx.handle(r[4]) for r in rails])]
    st = getattr(a, "storeys", None) or {}
    if not st.get("multi"):
        ctx.add(T_RAMP, ITEM_RAIL, "m", "plan", 1, total_m, None, None,
                f"railing-role lines on the plan; double lines {10}-{150} mm apart counted once ({len(strips)} rails"
                + (f", {len(single)} single lines" if single else "") + "); height/specification not given", rrefs, desc="Railings")
    else:
        # multi-storey: per plan, x the floors it represents
        by_rid = defaultdict(list)
        for r in rails:
            by_rid[a.region_of(r[0], r[1])].append(r)
        for rid, rr in sorted(by_rid.items(), key=lambda kv: (st["plans"].get(kv[0]) or {}).get("order", 500)):
            p = st["plans"].get(rid) or {}
            h2, v2, _ = axis_segments(rr, 2 * unit)
            ss = pair_lines(h2, True, 10 * unit, 150 * unit, 200 * unit) + pair_lines(v2, False, 10 * unit, 150 * unit, 200 * unit)
            u2 = set().union(*[x.ents for x in ss]) if ss else set()
            sg = [r for r in rr if r[4] not in u2]
            tm = (sum(x.length for x in ss) + sum(math.hypot(r[2] - r[0], r[3] - r[1]) for r in sg)) * a.to_m
            xs2 = [v for r in rr for v in (r[0], r[2])]
            ys2 = [v for r in rr for v in (r[1], r[3])]
            mult = p.get("multiplier", 1) if not p.get("special") else 1
            ctx.set_floor(p.get("group", ""), p.get("order", 500), a.region_name(rid))
            ctx.add(T_RAMP, ITEM_RAIL, "m", f"{p.get('group') or a.region_name(rid)} / railings", mult, tm, None, None,
                    f"railing-role lines on {a.region_name(rid)}" + (f" x {mult} floors" if mult > 1 else "")
                    + f"; double lines 10-150 mm apart counted once ({len(ss)} rails"
                    + (f", {len(sg)} single lines" if sg else "") + "); height/specification not given",
                    [ref("railing", "railing lines", [min(xs2), min(ys2), max(xs2), max(ys2)], [ctx.handle(r[4]) for r in rr])],
                    desc="Railings")
        ctx.set_floor()
    for i, t, rid in a.texts:
        m = RAMP_RE.search(" ".join(t["s"].split()))
        if not m or not a.is_plan(rid):
            continue
        w = float(m.group(1))
        tx, ty = t["p"]
        near = [s for s in strips if math.dist(((s.lo + s.hi) / 2, s.c) if s.horizontal else (s.c, (s.lo + s.hi) / 2), (tx, ty))
                < 15000 * unit]
        run = sum(s.length for s in near) * a.to_m / 2
        if run <= 0:
            continue
        ctx.add(T_RAMP, ITEM_RAMP, "sqm", a.region_name(rid), 1, run, w / 1000, None,
                f"width {w:.0f} from text '{' '.join(t['s'].split())}'; length ASSUMED = mean of the railing runs on its two sides "
                f"({len(near)} rails within 15 m)", [ctx.text_ref(i, "text")] + rrefs, assumed=True, desc="Ramp")
