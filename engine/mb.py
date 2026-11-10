"""Measurement Book engine.

    build_mb(geometry: dict, user_config: dict) -> MBResult

`geometry` is the JSON produced by app/extract.py. `user_config` is what the review
UI (or the CLI's cfg.json) provides:

{
  "project":     {"name": str},
  "layer_roles": {layer_name: role},   # role in app.roles.ROLES
  "walls": {                            # nominal sizes in mm (defaults below; heights are fallbacks)
     "wall-brick": {"thickness_mm": 230, "height_mm": 3000},
     "wall-brick-half": {"thickness_mm": 115},
     "wall-AAC":   {"thickness_mm": 200, "height_mm": 3000},
     "parapet":    {"thickness_mm": 230, "height_mm": 600}
  },
  "zones": [{"name": str, "bbox": [x0, y0, x1, y1], "wall_height_mm": float}],  # wall height per block
  "finishes": {"ROOM NAME" | "ROOM NAME WxL" | "ROOM NAME@x,y": "finish text"},
  "roof": {"coping_mm": 100, "coba_mm": 150},
  "ground_level_mm": 0,
  "slab_thickness_mm": float | None,
  "levels":  [{"name", "level_mm"}],
  "regions": [{"id", "name", "kind", "include", "level", "floors", "bbox"}],
  "flags":   {flag_id: {"status": "open|resolved|accepted", "value": ...}},
  "brickwork": {"is1200_beam_soffit": true, "deduct_columns": true, "deduct_lintels": true,
                "beam_depth_mm", "beam_depth_by_region", "beam_schedule", "lintel_depth_mm", ...},  # engine/is1200.py
  "dimensions": {"use": "written" | "measured"},          # engine/dims.py (written dimensions govern)
  "dedupe": true                                            # engine/dedup.py (same-place duplicates)
}

Anything the drawing does not give (wall heights, finishes, which of two sizes) is
read from user_config or resolved flags; otherwise a default is used, the MB row
is marked "assumed"/"unresolved" and a flag says what to enter. Resolving a flag and
re-running changes the quantities.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

import numpy as np

from .analysis import (Analysis, _looks_like_mm, analyse, opening_kind, parse_opening_tag, parse_room_label,  # noqa: F401
                       parse_schedule_entry, preferred_size, schedule_sizes)
from .checks import _fid, group_flags, run_checks, saved_status
from .dedup import copy_flags, dedup_flags, dedupe_geometry, plan_copies
from .geom import UNIT_TO_M
from .xrefs import xref_report
from .dims import dim_row_fields
from .mb_sizes import opening_size
from .plan import FINISH_RE, NOT_FINISH_RE, build_floor, norm_finish, roof_offset
from .quantities import (ITEM_OPEN, T_DW, Ctx, floor_rows, grooves_rows, is_void, parapet_inner_rows, ramp_rows, ref,
                         roof_rows)
from .roof import TOS_RE, analyse_roof
from .is1200 import CITE_BEAM, CITE_LINTEL, beam_depth, beam_schedule, brick_cfg, lintel_spec, slab_thickness
from .rowid import assign_stable_ids
from .sections import section_maps
from .storeys import plan_storeys
from .types import AbstractRow, DimRow, Flag, MBResult, MBRow, Opening, Room  # noqa: F401

DEFAULT_WALLS = {
    "wall-brick": {"thickness_mm": 230, "height_mm": 3000},
    "wall-brick-half": {"thickness_mm": 115},
    "wall-AAC": {"thickness_mm": 200, "height_mm": 3000},
    "parapet": {"thickness_mm": 230, "height_mm": 600},
}
_WXH = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pair(v) -> tuple[float, float] | None:
    m = _WXH.search(str(v or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None


def _apply_saved(f: Flag, saved: dict) -> Flag:
    st = saved_status(saved, f)
    if st.get("status") in ("resolved", "accepted"):
        f.status = st["status"]
        f.value = st.get("value")
    return f


def _plan_kinds(a: Analysis):
    floors, roofs = [], []
    has_rooms = {r.region for r in a.rooms}
    for r in a.regions:
        if not a.is_plan(r["id"]):
            continue
        if r.get("special") in ("terrace", "mumty"):
            roofs.append(r["id"])
            continue
        tos = any(rid == r["id"] and TOS_RE.search(t["s"]) for _, t, rid in a.texts)
        roofish = tos or re.search(r"TERRACE|ROOF", r["name"], re.I)
        if roofish and r["id"] not in has_rooms:
            roofs.append(r["id"])
        else:
            floors.append(r["id"])
    return floors, roofs


def build_mb(geometry: dict, user_config: dict) -> MBResult:
    cfg = user_config or {}
    saved = cfg.get("flags", {}) or {}
    # same-place duplicates are made inert before anything is measured (engine/dedup.py)
    dinfo = {"removed": 0}
    if cfg.get("dedupe", True):
        to_m0 = UNIT_TO_M.get(geometry.get("units", "mm"), 0.001)
        if to_m0 != 0.001 and _looks_like_mm(geometry):
            to_m0 = 0.001
        geometry, dinfo = dedupe_geometry(geometry, cfg.get("layer_roles") or {}, to_m0)
    a = analyse(geometry, cfg)
    st, st_flags = plan_storeys(a, cfg, saved, _fid, Flag)
    # whole-plan copies in different places: flagged; a copy the user calls stale is excluded
    copies = plan_copies(a)
    copy_fl, copy_notes, stale = copy_flags(a, copies, st, saved)
    if stale:
        cfg = {**cfg, "regions": [{**r, "include": False, "excluded_as": "stale copy"} if r["id"] in stale else r
                                  for r in cfg.get("regions", [])]}
        a = analyse(geometry, cfg)
        st, st_flags = plan_storeys(a, cfg, saved, _fid, Flag)
        copy_notes.append("Excluded as stale copies (per user): " + ", ".join(a.region_name(r) for r in sorted(stale)))
    a.dedup = dinfo
    a.storeys = st
    multi = st["multi"]
    flags = run_checks(a)
    xr_flags, xr_notes, xr_meta = xref_report(a, geometry, saved)
    flags += dedup_flags(a, dinfo, saved) + copy_fl + xr_flags
    walls_cfg = {k: {**v, **((cfg.get("walls") or {}).get(k) or {})} for k, v in DEFAULT_WALLS.items()}
    cfg = {**cfg, "walls": walls_cfg}
    to_m = a.to_m
    unit = 1 / (to_m * 1000)

    # user-resolved opening sizes feed the engine
    overrides = {}
    for f in flags:
        if f.status != "resolved":
            continue
        for r in f.refs:
            if r.startswith("tag:"):
                tag = r[4:]
                if _pair(f.value):
                    w, h = _pair(f.value)
                    overrides[tag] = (w, h, f"per user ({f.id}) {w:.0f}X{h:.0f}")
                elif _num(f.value):
                    w0, h0, _ = opening_size(a, tag)
                    if h0:
                        overrides[tag] = (_num(f.value), h0, f"per user ({f.id}) width {_num(f.value):.0f}")
    a.size_overrides = overrides

    ctx = Ctx(a, cfg, flags)
    engine_flags: list[Flag] = list(st_flags)
    for f in st_flags:
        for r in f.refs:
            ctx.flag_by_ref[r].append(f)

    def new_flag(f: Flag) -> Flag:
        _apply_saved(f, saved)
        engine_flags.append(f)
        for r in f.refs:
            ctx.flag_by_ref[r].append(f)
        return f

    if a.units_note:
        new_flag(Flag(id=_fid("setup", "units"), severity="warning", category="setup", title="Drawing units corrected to mm",
                      detail=a.units_note + ". All quantities assume mm.", suggested_fix="Accept if the drawing is in mm.",
                      refs=["units"]))
    floor_rids, roof_rids = _plan_kinds(a)
    if multi:
        floor_rids.sort(key=lambda rid: st["plans"].get(rid, {}).get("order", 500))
    roof = None
    roof_info = None
    assumptions = []
    if roof_rids:
        pc = walls_cfg["parapet"]
        roof = analyse_roof(a, roof_rids[0], float(pc.get("thickness_mm") or 230), _num(pc.get("height_mm")))
        terr = st["levels"]["special"].get("terrace") if multi else None
        if roof is not None and terr is not None:
            # no T.O.S. text on the terrace plan: the terrace level label from the sections
            src = st["levels"]["sources"].get("terrace") or [None]
            for L in roof.levels:
                if L.tos_mm is None:
                    L.tos_mm, L.text_ent = terr, src[0]
                    L.text = " ".join(a.geometry["entities"][src[0]]["s"].split()) if src[0] is not None else f"{terr:+.0f}"
        if roof is not None:
            assumptions.extend(f"{a.region_name(roof.region)}: {n}" for n in roof.notes)
    slab_flags = ctx.flags_for(["slab"])
    slab_thk = _num(cfg.get("slab_thickness_mm")) if cfg.get("slab_thickness_mm_confirmed") else None
    for f in slab_flags:
        if f.status == "resolved" and _num(f.value):
            slab_thk = _num(f.value)
    if roof is not None:
        if multi:
            pr = st["plans"].get(roof.region, {})
            ctx.set_floor(pr.get("group") or "Terrace", pr.get("order", 1000), a.region_name(roof.region))
        roof_info = roof_rows(ctx, roof, cfg.get("roof") or {}, slab_thk, slab_flags)
        ctx.set_floor()

    # wall-height zones from resolved flags (checks' missing-height blocks and per-roof-level flags)
    flag_zones = []
    for f in flags:
        if "wall-height" in f.refs and f.status == "resolved" and _num(f.value) and f.location and f.location.get("bbox"):
            flag_zones.append({"name": f.title.replace("Wall height missing for block: ", "Block: "), "bbox": f.location["bbox"],
                               "height_mm": _num(f.value), "source": f"user ({f.id})", "assumed": False, "flag": f})
    smaps = []
    try:
        smaps = section_maps(a)
    except Exception:  # noqa: BLE001
        smaps = []

    plans = []
    brick_state: dict = {}
    depth, depth_flag = st["depth"]
    for rid in floor_rids:
        level_flags = {}
        off = None
        sp_ = st["plans"].get(rid) or {}
        storey_zones = []
        if multi:
            # a multi-storey plan is roofed by the floor above (its own walls), not by the terrace plan
            ctx.set_floor(sp_.get("group", ""), sp_.get("order", 500), a.region_name(rid))
            if sp_.get("ftf"):
                h = sp_["ftf"] - (depth or 0)
                src = (f"storey: floor-to-floor {sp_['ftf']:.0f} ({sp_['ftf_src']})"
                       + (f" - slab/beam {depth:.0f}" if depth else " (slab/beam depth NOT deducted)"))
                storey_zones.append({"name": f"{sp_.get('group')} storey", "bbox": a.reg_by_id[rid]["bbox"], "height_mm": h,
                                     "source": src, "assumed": False, "region": rid,
                                     "flag": depth_flag if (depth_flag is not None and depth is None) else None})
        if roof is not None and not multi:
            off, note = roof_offset(a, rid, roof.region)
            if off is None:
                new_flag(Flag(id=_fid("setup", f"roof-align:{rid}"), severity="warning", category="setup",
                              title=f"Roof plan could not be placed over {a.region_name(rid)}",
                              detail=note + ". Roofed area is taken as the area enclosed by walls; external heights use wall heights.",
                              location={"bbox": a.reg_by_id[rid]["bbox"]}, refs=[f"region:{rid}"]))
            else:
                for li, L in enumerate(roof.levels):
                    tos = f"{L.tos_mm:+.0f}" if L.tos_mm is not None else f"level{li + 1}"
                    f = new_flag(Flag(
                        id=_fid("missing-data", f"height-level:{rid}:{tos}"), severity="missing", category="missing-data",
                        title=f"Wall height assumed under roof T.O.S. {tos} ({a.region_name(rid)})",
                        detail="Wall height (floor to underside of slab/beam) is read from user zones or resolved flags; "
                               f"walls under this roof that have none use the default {walls_cfg['wall-brick']['height_mm']:.0f} mm.",
                        location={"bbox": [L.bbox[0] - off[0], L.bbox[1] - off[1], L.bbox[2] - off[0], L.bbox[3] - off[1]]} if L.bbox else None,
                        suggested_fix="Enter the wall height for this block (read it from a section), or add zones in the config.",
                        input={"type": "number", "label": "Wall height (mm)", "unit": "mm", "placeholder": "3000"},
                        refs=[f"height-level:{rid}:{li}"]))
                    level_flags[li] = f
                    if f.status == "resolved" and _num(f.value) and f.location:
                        flag_zones.append({"name": f"Roof T.O.S. {tos}", "bbox": f.location["bbox"], "height_mm": _num(f.value),
                                           "source": f"user ({f.id})", "assumed": False, "flag": f, "level": li})
        mat_ov = [(f.location["bbox"], str(f.value).strip().upper()) for f in flags
                  if f.category == "material" and f.status == "resolved" and f.value and f.location and f.location.get("bbox")]
        fp = build_floor(a, rid, cfg, roof if off is not None else None, storey_zones + flag_zones, mat_ov,
                         close_gaps_mm=float(cfg.get("close_gaps_mm") or 2600) if multi else None)
        if fp is None:
            ctx.set_floor()
            continue
        if multi:
            fp.floors = sp_.get("multiplier", fp.floors)
            fp.group = sp_.get("group", "")
        # drop per-level height flags that nothing depends on
        mw = fp.masonry | (fp.linewall >= 0)
        for li, f in list(level_flags.items()):
            zi = next((k for k, z in enumerate(fp.zones) if z.get("auto") and z.get("level") == li), None)
            uncovered = int((mw & (fp.hsrc == 0) & (fp.zone == zi)).sum()) if zi is not None else 0
            if not uncovered and f.status == "open":
                engine_flags.remove(f)
                ctx.flag_by_ref[f.refs[0]].remove(f)
                del level_flags[li]
        for k, z in enumerate(fp.zones):
            if z.get("auto") and z.get("level") in level_flags:
                z["flag"] = None
        cfg["_brick"] = brick_setup(a, fp, rid, cfg, new_flag, brick_state, multi, flags)
        finish_flags = assign_finishes(ctx, fp, smaps, cfg, flags, new_flag)
        if fp.unplaced:
            tags = sorted({op.tag for op in fp.unplaced})
            new_flag(Flag(id=_fid("opening-place", f"{rid}:" + ",".join(tags)), severity="warning", category="opening-size",
                          title=f"{len(fp.unplaced)} tagged opening(s) not found in a wall: {', '.join(tags)}",
                          detail="No wall gap of about the scheduled width was found near these tags; their area is counted but "
                                 "the wall above/below them is not added and no plaster deduction is made.",
                          location={"bbox": _bbox_pts([(op.x, op.y) for op in fp.unplaced], 1000 * unit)},
                          evidence=[f"{op.tag} at ({op.x:.0f}, {op.y:.0f}), size {op.w_mm}x{op.h_mm}" for op in fp.unplaced[:10]],
                          refs=[f"tag:{t}" for t in tags]))
        floor_rows(ctx, fp, roof_info, cfg, level_flags, finish_flags)
        plans.append(fp)
        assumptions.append(f"{fp.name}: {fp.roof_note}; raster cell {fp.cell_mm:.0f} mm"
                           + (f"; counted x {fp.floors} floors" if fp.floors > 1 else "")
                           + (f"; {sp_.get('group')}" if multi else ""))
        if getattr(fp, "rcc_upstands", None):
            assumptions.append(f"{fp.name}: {len(fp.rcc_upstands)} wall-line run(s) next to an 'NNN mm high RCC wall' "
                               "note taken as RCC upstands (balcony edges), not masonry; not measured")
        ctx.set_floor()
    if roof_info:
        if multi:
            pr = st["plans"].get(roof.region, {})
            ctx.set_floor(pr.get("group") or "Terrace", pr.get("order", 1000), a.region_name(roof.region))
        parapet_inner_rows(ctx, roof_info, cfg)
        ctx.set_floor()
    try:
        grooves_rows(ctx)
    except Exception as exc:  # noqa: BLE001
        assumptions.append(f"grooves not measured: {exc}")
    try:
        ramp_rows(ctx)
    except Exception as exc:  # noqa: BLE001
        assumptions.append(f"ramp/railing not measured: {exc}")

    # ---- openings
    openings = opening_rows(ctx, plans, st)
    # ---- rooms
    rooms = room_list(ctx, plans, st)
    # ---- dimensions
    dims = []
    for i, e, rid in a.dims:
        x, y = (e.get("tp") or e.get("p1") or [0, 0])
        pts = [e[k] for k in ("p1", "p2", "tp") if k in e]
        bb = _bbox_pts(pts, 100 * unit) if pts else None
        dims.append(DimRow(a.region_name(rid), a.layer_names[e["l"]], e.get("m"), e.get("s", ""), x, y,
                           refs=[ref("dim", f"{e.get('m') or ''}", bb, [ctx.handle(i)])], **dim_row_fields(a, e)))

    all_flags = flags + engine_flags
    if multi:
        # one flag per repeated finding (same check in every flat / every typical floor)
        def plan_of(f):
            loc = f.location or {}
            bb = loc.get("bbox")
            x, y = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2) if bb else (loc.get("x"), loc.get("y"))
            rid = a.region_of(x, y) if x is not None else None
            return a.region_name(rid) if rid is not None else ""
        remap: dict = {}
        all_flags = group_flags(all_flags, plan_of, remap)
        for row in ctx.rows:
            if any(i in remap for i in row.flag_ids):
                row.flag_ids = list(dict.fromkeys(remap.get(i, i) for i in row.flag_ids))
                for old_id, new_id in remap.items():
                    if old_id in row.remarks:
                        row.remarks = row.remarks.replace(old_id, new_id)
    order = {"error": 0, "warning": 1, "missing": 2}
    all_flags.sort(key=lambda f: (f.status != "open", order.get(f.severity, 3), f.category))

    assign_stable_ids(ctx.rows)
    abstract = make_abstract(ctx.rows)
    assumptions = (base_assumptions(geometry, cfg, walls_cfg, roof, a) + assumptions + ctx.assumptions + copy_notes
                   + xr_notes)
    if dinfo.get("removed"):
        assumptions.append(f"{dinfo['removed']} duplicate entities (drawn twice in the same place) ignored: "
                           + ", ".join(f"{n} {k[1]} on {k[0]}" for k, n in sorted(dinfo['by'].items(), key=lambda kv: -kv[1])))
    for f in all_flags:
        if f.status == "resolved":
            assumptions.append(f"[{f.id}] {f.title} -> user value: {f.value}")
        elif f.status == "accepted":
            assumptions.append(f"[{f.id}] {f.title} -> accepted as drawn")
        elif f.severity == "missing":
            assumptions.append(f"[{f.id}] OPEN QUERY: {f.title}")
    counts = defaultdict(int)
    for f in all_flags:
        if f.status == "open":
            counts[f.severity] += 1
    return MBResult(ctx.rows, abstract, rooms, openings, dims, assumptions,
                    meta={"engine": "engine.mb", "project": cfg.get("project", {}).get("name", ""),
                          "open_flags": dict(counts), "units": "mm (corrected)" if a.units_note else geometry.get("units", "mm"),
                          "plans": [p.name for p in plans], "roof_plan": a.region_name(roof.region) if roof else None,
                          "duplicates_removed": dinfo.get("removed", 0), "external_refs": xr_meta,
                          "plan_copies": [{"a": a.region_name(c["a"]), "b": a.region_name(c["b"]), "similarity": c["sim"],
                                           "offset": [round(v, 1) for v in c["offset"]]} for c in copies],
                          "brickwork": brick_cfg(cfg)},
                    flags=all_flags)


def brick_setup(a, fp, rid, cfg, new_flag, state: dict, multi: bool, check_flags=()) -> dict:
    """IS 1200 brickwork rules for one plan: beam deduction per cell (fp.brick_dh), column and lintel
    deduction switches, and the flags for values the drawing does not give (see engine/is1200.py)."""
    bc = brick_cfg(cfg)
    out = {"is1200": True, "cols": bool(bc.get("deduct_columns")), "lintels_on": bool(bc.get("deduct_lintels"))}
    name = a.region_name(rid)
    unit = 1 / (a.to_m * 1000)
    mw = fp.masonry | (fp.linewall >= 0)
    if not mw.any():
        return out
    # ---- beams over walls
    slab_f = next((f for f in check_flags if "slab" in f.refs and f.status == "resolved" and _num(f.value)), None)
    if slab_f is not None and slab_thickness(cfg) is None:
        cfg = {**cfg, "brickwork": {**(cfg.get("brickwork") or {}), "slab_thickness_mm": _num(slab_f.value)}}
    if bc.get("is1200_beam_soffit"):
        lines = bool(fp.beam_lines) and bool((fp.beam_mask & mw).any())
        target = (fp.beam_mask.copy() if lines else np.ones(fp.grid.shape, dtype=bool)) & ~fp.low_wall & ~fp.to_beam
        if not (target & mw).any():
            target = None
        if target is not None:
            D, src = beam_depth(a, cfg, name, None)
            f = None
            if D is None:
                slab = slab_thickness(cfg)
                sched, s_src = beam_schedule(a, cfg)
                why = (f"The beam schedule ({s_src}) gives the overall depth but the slab thickness is not known."
                       if sched and slab is None else "The drawing gives no beam sizes.")
                f = new_flag(Flag(
                    id=_fid("missing-data", f"beam-depth:{name}"), severity="missing", category="missing-data",
                    title="Beam depth not given: brickwork taken up to the slab soffit",
                    detail=(f"{CITE_BEAM}. " + ("Beam lines are drawn on this plan over some walls. " if lines else
                            "No beam lines on this plan, so a beam is assumed over every wall once a depth is entered. ")
                            + why + " Until a depth is entered the brick height includes the beam depth (overstated); "
                            "accept if the wall heights already stop under the beams or there are no beams."
                            + (f" Walls already have the storey deduction ({cfg.get('storey_deduction_mm')} mm) taken off; "
                               "enter only the beam projection below the slab." if multi and cfg.get("storey_deduction_mm") else "")),
                    location={"bbox": fp.grid.bbox_of(mw & target)},
                    suggested_fix="Enter the beam depth BELOW the slab (overall depth D minus slab thickness), or set "
                                  "brickwork.beam_depth_mm / beam_depth_by_region / beam_schedule in the config.",
                    input={"type": "number", "label": "Beam depth below slab (mm)", "unit": "mm", "placeholder": "300"},
                    refs=["beam-depth", f"region:{rid}"]))
                if f.status == "resolved" and _num(f.value) is not None:
                    D, src = _num(f.value), f"per user ({f.id})"
            elif not lines:
                new_flag(Flag(
                    id=_fid("missing-data", f"beam-default:{name}"), severity="warning", category="missing-data",
                    title="No beam lines on the plan: beam depth from the config deducted under every wall",
                    detail=f"{CITE_BEAM}. {name} has no beam projection lines, so the beam depth {D:.0f} mm ({src}) is "
                           "taken off the brick height of every wall on this plan. Check against the structural drawings.",
                    location={"bbox": fp.grid.bbox_of(mw)},
                    suggested_fix="Accept, or draw/assign the beam lines (layer role 'beam') so only walls under beams are reduced.",
                    refs=["beam-depth", f"region:{rid}"]))
            if D and D > 0:
                fp.brick_dh[target] = D
                out["beam_src"] = src
                out["beam_assumed"] = not lines
                out["beam_note"] = "beam assumed over every wall (no beam lines)" if not lines else "beam lines on the plan"
            if f is not None:
                out["beam_flag"] = f
                out["beam_flag_rows"] = f.status != "accepted" or bool(D)
    # ---- lintels
    if out["lintels_on"]:
        if "lintel" not in state:
            state["lintel"] = lintel_spec(a, cfg)
        out["lintel"] = state["lintel"]
        if state["lintel"][0] is None and fp.placed:
            if "lintel_flag" not in state:
                # the drawing check's "Lintel sizes not given" flag (answer 'BxD' or a depth) when it exists
                state["lintel_flag"] = next((f for f in check_flags if "lintel" in f.refs), None)
            if state.get("lintel_flag") is None:
                state["lintel_flag"] = new_flag(Flag(
                    id=_fid("missing-data", "lintel-size"), severity="missing", category="missing-data",
                    title="Lintel size not given: lintels over openings not deducted from brickwork",
                    detail=f"{CITE_LINTEL}: the lintel volume (opening width + bearing each side) x depth x wall "
                           "thickness. The drawing gives no lintel depth, so brickwork is overstated by the lintels.",
                    suggested_fix="Enter the lintel depth (mm); bearing defaults to 150 mm each side "
                                  "(brickwork.lintel_bearing_mm). Accept to leave lintels in the brickwork.",
                    input={"type": "number", "label": "Lintel depth (mm)", "unit": "mm", "placeholder": "150"},
                    refs=["lintel"]))
            lf = state["lintel_flag"]
            out["lintel_flag"] = lf
            dv = _pair(lf.value)[1] if _pair(lf.value) else _num(lf.value)
            if lf.status == "resolved" and dv:
                out["lintel"] = (dv, state["lintel"][1], f"per user ({lf.id}: {lf.value})", None)
            elif lf.status == "accepted":
                out["lintels_on"] = False
    return out


def _bbox_pts(pts, pad=0.0):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def _finish_key_match(key: str, sp) -> bool:
    k = " ".join(key.upper().split())
    for r in sp.labels:
        name = " ".join(r.name.upper().split())
        if k == name or k == f"{name} {r.w:.0f}X{r.l:.0f}" or k == f"{name}@{r.x:.0f},{r.y:.0f}":
            return True
    return bool(not sp.labels and k == " ".join(sp.name.upper().split()))


def assign_finishes(ctx, fp, smaps, cfg, flags, new_flag):
    a = ctx.a
    g = fp.grid
    unit = 1 / (a.to_m * 1000)
    by_id = {s.id: s for s in fp.spaces}
    # 1) finish notes in sections, mapped back onto the plan along the cut line
    for sm in smaps:
        if sm.cut["region"] != fp.rid:
            continue
        sec_id = sm.region["id"]
        for i, t, rid in a.texts:
            if rid != sec_id:
                continue
            s = norm_finish(t["s"])
            if not FINISH_RE.search(s) or NOT_FINISH_RE.search(s):
                continue
            pos = sm.to_plan(_text_cx(t))
            px, py = (pos, sm.cut["c"]) if sm.cut["horizontal"] else (sm.cut["c"], pos)
            sp = _space_at(fp, px, py, 800 * unit)
            if sp is None:
                continue
            if sp.finish and norm_finish(sp.finish).upper() != s.upper():
                sp.note = (sp.note + "; " if sp.note else "") + f"section also says '{s}'"
                continue
            if not sp.finish:
                sp.finish = s
                sp.finish_src = f"{sm.region['name']} note '{s}' (cut {sm.letter}-{sm.letter}, {sm.mode} mapping)"
                sp.finish_ref = ctx.text_ref(i, "text")
    # 2) user config
    for key, val in (cfg.get("finishes") or {}).items():
        for sp in fp.spaces:
            if _finish_key_match(key, sp) and val:
                sp.finish = norm_finish(str(val))
                sp.finish_src = f"user config (finishes['{key}'])"
    # 3) per-room flags for labelled rooms still without a finish
    default = None
    for f in flags:
        if "floor-finish" in f.refs and f.status == "resolved" and f.value:
            default = (norm_finish(str(f.value)), f)
    out = {}
    for sp in fp.spaces:
        if is_void(sp.name) and sp.labels:
            continue   # lift well / shaft: no floor finish
        if sp.finish or not sp.labels:
            if not sp.finish and default:
                sp.finish, sp.finish_src = default[0], f"default finish per user ({default[1].id})"
            continue
        r = sp.labels[0]
        f = new_flag(Flag(
            id=_fid("missing-data", f"finish:{r.name}@{r.x:.0f},{r.y:.0f}"), severity="missing", category="missing-data",
            title=f"Floor finish not given for {sp.name}",
            detail="No finish note on the plan or in a section through this room.",
            location={"bbox": sp.bbox} if sp.bbox else {"x": r.x, "y": r.y},
            suggested_fix="Enter the floor finish (e.g. '600x600 vitrified tiles').",
            input={"type": "text", "label": "Floor finish", "placeholder": "40 mm thk. CC flooring"},
            refs=[f"finish:{sp.id}"]))
        out[sp.id] = f
        if f.status == "resolved" and f.value:
            sp.finish, sp.finish_src = norm_finish(str(f.value)), f"user ({f.id})"
        elif default:
            sp.finish, sp.finish_src = default[0], f"ASSUMED default finish per user ({default[1].id})"
    return out


def _text_cx(e) -> float:
    """Horizontal centre of a text (MTEXT attachment aware; first line width estimate)."""
    x = e["p"][0]
    h = e.get("h", 0) or 0
    w = 0.6 * h * max((len(s) for s in e["s"].split("\n")), default=1)
    ap = e.get("ap")
    col = (ap - 1) % 3 if ap else 0
    return x + (0.5 - 0.5 * col) * w


def _space_at(fp, x, y, radius):
    g = fp.grid
    j, i = g.row(y), g.col(x)
    r = int(radius / g.cell) + 1
    j0, j1, i0, i1 = max(j - r, 0), min(j + r + 1, g.ny), max(i - r, 0), min(i + r + 1, g.nx)
    sub = fp.owner[j0:j1, i0:i1]
    if not (sub >= 0).any():
        return None
    jj, ii = np.nonzero(sub >= 0)
    k = np.argmin((jj + j0 - j) ** 2 + (ii + i0 - i) ** 2)
    sid = int(sub[jj[k], ii[k]])
    return next((s for s in fp.spaces if s.id == sid), None)


def opening_rows(ctx, plans, st=None):
    """One row per tag (per floor group in a multi-storey drawing); counts x the plan's floor multiplier."""
    a = ctx.a
    st = st or {"multi": False, "plans": {}}
    multi = st["multi"]
    sizes = schedule_sizes(a)
    per_tag = defaultdict(list)
    for k, (tag, x, y, rid) in enumerate(a.plan_tags):
        per_tag[tag].append((k, x, y, rid))
    placed_by_ent = {op.ent: (fp, op) for fp in plans for op in fp.placed}
    fp_by_rid = {fp.rid: fp for fp in plans}
    openings = []
    for tag in sorted(set(per_tag) | set(sizes), key=lambda t: (t[0], t)):
        w, h, note = opening_size(a, tag)
        hits = per_tag.get(tag, [])
        locs = defaultdict(int)
        refs = []
        by_rid = defaultdict(lambda: (defaultdict(int), []))
        total = 0
        for k, x, y, rid in hits:
            ent = a.plan_tag_ents[k]
            fpop = placed_by_ent.get(ent)
            where = a.region_name(rid)
            mult = fp_by_rid[rid].floors if rid in fp_by_rid else 1
            if fpop:
                fp, op = fpop
                sp_names = _sides(fp, op)
                if sp_names:
                    where = " / ".join(sp_names)
                if op.gap:
                    refs.append(ref("opening", f"{tag} opening", op.gap["bbox"], []))
                    by_rid[rid][1].append(ref("opening", f"{tag} opening", op.gap["bbox"], []))
            grp = (st["plans"].get(rid) or {}).get("group") if multi else None
            locs[f"{grp} / {where}" if grp else where] += mult
            total += mult
            by_rid[rid][0][where] += 1
            tr = ctx.text_ref(ent, "opening", f"tag {tag}")
            refs.append(tr)
            by_rid[rid][1].append(tr)
        fl = ctx.flags_for([f"tag:{tag}"])
        rem = note if tag in sizes or note.startswith(("ASSUMED", "per user")) else "size not in schedule"
        o = Opening(tag, opening_kind(tag), w, h, total, dict(locs), rem, [r for r in refs if r])
        openings.append(o)
        if not (o.count and w and h):
            continue
        if not multi:
            ctx.add(T_DW, ITEM_OPEN, "sqm", ", ".join(f"{k} x{v}" if v > 1 else k for k, v in locs.items())[:200],
                    o.count, w / 1000, None, h / 1000,
                    f"{tag}: {o.count} tag(s) on the plan; size {note}", o.refs, fl, note.startswith("ASSUMED"),
                    desc=f"{tag} ({w:.0f}x{h:.0f})")
            continue
        for rid, (wl, rrefs) in sorted(by_rid.items(), key=lambda kv: (st["plans"].get(kv[0]) or {}).get("order", 500)):
            p = st["plans"].get(rid) or {}
            mult = fp_by_rid[rid].floors if rid in fp_by_rid else 1
            n = sum(wl.values())
            ctx.set_floor(p.get("group", ""), p.get("order", 500), a.region_name(rid))
            ctx.add(T_DW, ITEM_OPEN, "sqm", f"{p.get('group', a.region_name(rid))} / {tag}", n * mult, w / 1000, None, h / 1000,
                    f"{tag}: {n} tag(s) on {a.region_name(rid)}" + (f" x {mult} floors" if mult > 1 else "") + f"; size {note}",
                    [r for r in rrefs if r], fl, note.startswith("ASSUMED"), desc=f"{tag} ({w:.0f}x{h:.0f})")
        ctx.set_floor()
    return openings


def _sides(fp, op):
    if not op.gap:
        return []
    g = fp.grid
    gp = op.gap
    names = []
    unit = g.cell / (fp.cell_m * 1000)
    off = (gp["t_mm"] / 2 + 150) * unit
    for sgn in (-1, 1):
        px, py = (gp["x"], gp["y"] + sgn * off) if gp["horizontal"] else (gp["x"] + sgn * off, gp["y"])
        j, i = g.row(py), g.col(px)
        if g.inside(j, i) and fp.owner[j, i] >= 0:
            sp = next((s for s in fp.spaces if s.id == fp.owner[j, i]), None)
            if sp and sp.name not in names:
                names.append(sp.name)
    return names or ["outside"]


def room_list(ctx, plans, st=None):
    a = ctx.a
    st = st or {"multi": False, "plans": {}}
    rooms = []
    done = set()
    for fp in plans:
        c2 = fp.cell_m ** 2
        for sp in fp.spaces:
            if not sp.labels:
                continue
            tot_lab = sum(r.w * r.l for r in sp.labels) or 1
            for r in sp.labels:
                done.add(id(r))
                ref_id = f"room:{r.name}@{r.x:.0f},{r.y:.0f}"
                w, ln = r.w, r.l
                rem = []
                for f in ctx.flags_for([ref_id]):
                    if f.status == "resolved" and _pair(f.value):
                        w, ln = _pair(f.value)
                        rem.append(f"per user ({f.id}): {f.value}")
                    elif f.status == "open":
                        rem.append(f"UNRESOLVED {f.id}: {f.title}")
                area = sp.cells * c2 * (r.w * r.l / tot_lab)
                bw = (sp.bbox[2] - sp.bbox[0]) * a.to_m * 1000 if sp.bbox else None
                bl = (sp.bbox[3] - sp.bbox[1]) * a.to_m * 1000 if sp.bbox else None
                if len(sp.labels) > 1:
                    check = "open space shared by labels"
                elif bw and abs(bw * bl / 1e6 - area) < 0.02 * area and \
                        {round(bw / 10), round(bl / 10)} == {round(r.w / 10), round(r.l / 10)}:
                    check = "OK"
                elif bw and abs(area - r.w * r.l / 1e6) <= 0.03 * r.w * r.l / 1e6:
                    check = "OK (area)"
                else:
                    check = f"MISMATCH: labelled {r.w * r.l / 1e6:.2f} m2, measured {area:.2f} m2"
                if sp.note:
                    rem.append(sp.note)
                grp = (st["plans"].get(fp.rid) or {}).get("group") if st["multi"] else None
                rooms.append(Room(r.name, f"{grp} / {fp.name}" if grp else f"{fp.name}", w, ln, round(area, 3), r.x, r.y, r.text,
                                  measured_l_mm=round(bw) if bw else None, measured_b_mm=round(bl) if bl else None,
                                  check=check, remarks="; ".join(rem),
                                  refs=[x for x in (ctx.text_ref(r.ent, "room", r.name),
                                                    ref("room", f"{r.name} boundary", sp.bbox, [])) if x]))
    for r in a.rooms:
        if id(r) in done:
            continue
        rooms.append(Room(r.name, a.region_name(r.region), r.w, r.l, round(r.w * r.l / 1e6, 3), r.x, r.y, r.text,
                          check="not enclosed / not under a roof", remarks="label not inside a measured space; not measured",
                          refs=[x for x in (ctx.text_ref(r.ent, "room", r.name),) if x]))
    return rooms


def make_abstract(rows: list[MBRow]) -> list[AbstractRow]:
    totals: dict[tuple, AbstractRow] = {}
    order = []
    for row in rows:
        key = (row.trade, row.item, row.unit)
        if key not in totals:
            totals[key] = AbstractRow(row.item, 0.0, row.unit, trade=row.trade)
            order.append(key)
        ab = totals[key]
        ab.qty += row.qty
        ab.row_ids.append(row.id)
        for r in row.refs:
            if len(ab.refs) < 400:
                ab.refs.append(r)
    trades = sorted({k[0] for k in order})
    order.sort(key=lambda k: trades.index(k[0]))
    out = []
    for k in order:
        ab = totals[k]
        ab.qty = round(ab.qty, 3)
        seen, refs = set(), []
        for r in ab.refs:
            key = (r.get("kind"), r.get("label"), tuple(r.get("bbox") or ()), tuple(r.get("handles") or ()))
            if key not in seen:
                seen.add(key)
                refs.append(r)
        ab.refs = refs
        st = {r.status for r in rows if r.id in set(ab.row_ids)}
        ab.remarks = "contains unresolved values" if "unresolved" in st else ("contains assumed values" if "assumed" in st else "")
        out.append(ab)
    return out


def base_assumptions(geometry, cfg, walls_cfg, roof, a) -> list[str]:
    out = [
        f"Drawing units taken as '{geometry.get('units', 'mm')}' ($INSUNITS={geometry.get('insunits')}).",
        "Masonry: plan area of legend wall hatches (material from the drawing's legend) and of walls drawn only as "
        "paired wall-role lines; hatch voids and column layers are columns and are excluded. Gross wall = hatched area "
        "+ wall across each tagged opening; openings deducted (W x H x nominal thickness).",
        "IS 1200 (Part 3) brickwork rules (user_config 'brickwork'): "
        + "; ".join(f"{k}={v}" for k, v in brick_cfg(cfg).items())
        + ". Beam soffit: brick height stops under beams running over the wall; columns: RCC column footprints lying on "
          "the masonry are deducted; lintels: (opening width + 2 x bearing) x depth x wall thickness deducted when the "
          "lintel size is known.",
        f"Half brick = brick whose local thickness <= midpoint of {walls_cfg['wall-brick-half']['thickness_mm']} and "
        f"{walls_cfg['wall-brick']['thickness_mm']} mm (measured in m2); thicker brick and AAC in m3 at nominal thickness.",
        "Wall height per cell = the smallest user zone / resolved flag covering it, else the default (flagged).",
        "Plaster: wall faces found on the plan raster; internal = face of a roofed space (height = lower of wall and "
        "room height); external = outside the roof outline (ground to top of parapet). CPWD: openings <= 0.5 m2 not "
        "deducted; 0.5-3 m2 deducted on one face (room side); > 3 m2 both faces, jambs/soffit/sill added.",
        "Ceiling plaster and floor areas = net enclosed roofed area of each space (walls and columns excluded).",
        "Floor finish per room from section finish notes mapped onto the plan along the section line, else the "
        "user's finishes; otherwise listed as 'finish not specified' (not payable).",
        "Roof: slab area = area inside parapet outer faces per T.O.S. level; parapet/coping on the centre line of "
        "paired parapet lines; terrace treatment = slab - parapet footprint - tank footprint.",
        "Not in drawing (not measured): foundations, excavation, PCC, slab/beam/lintel sizes unless entered, skirting/dado.",
    ]
    for k, v in walls_cfg.items():
        out.append(f"{k}: " + ", ".join(f"{a_}={b}" for a_, b in v.items()))
    for lv in cfg.get("levels", []) or []:
        out.append(f"Level '{lv.get('name')}': {lv.get('level_mm')} mm")
    return out


# ---------------------------------------------------------------- JSON for the web UI
def result_json(res: MBResult) -> dict:
    """Shape served by GET /api/jobs/{id}/mb."""
    from dataclasses import asdict
    sno = {}
    for k, ab in enumerate(res.abstract, 1):
        for rid in ab.row_ids:
            sno[rid] = k
    rows = [{"id": r.id, "sno": sno.get(r.id), "trade": r.trade, "item": r.item, "location": r.location,
             "nos": r.nos, "l": r.l, "b": r.b, "h": r.h, "qty": r.qty, "unit": r.unit, "remarks": r.remarks,
             "status": r.status, "refs": r.refs, "flag_ids": r.flag_ids, "floor_group": r.floor_group, "plan": r.plan,
             "desc": r.desc}
            for r in res.mb]
    abstract = [{"sno": k, "trade": ab.trade, "item": ab.item, "qty": ab.qty, "unit": ab.unit, "row_ids": ab.row_ids,
                 "remarks": ab.remarks, "refs": ab.refs} for k, ab in enumerate(res.abstract, 1)]
    rooms = [{"name": r.name, "location": r.location, "label_size": f"{r.width_mm:.0f}X{r.length_mm:.0f}",
              "measured_l": r.measured_l_mm, "measured_b": r.measured_b_mm, "area": r.area_m2, "check": r.check,
              "remarks": r.remarks, "refs": r.refs} for r in res.rooms]
    openings = []
    for o in res.openings:
        locs = o.locations or {"": 0}
        for loc, n in locs.items():
            area = round(n * (o.width_mm or 0) * (o.height_mm or 0) / 1e6, 3)
            openings.append({"type": o.tag, "kind": o.kind, "w": o.width_mm, "h": o.height_mm, "location": loc, "nos": n,
                             "area": area, "remarks": o.remarks, "refs": o.refs})
    dims = [{"value": d.measurement, "text": d.text, "region": d.location, "layer": d.layer, "refs": d.refs,
             "measured": d.measured, "written": d.written, "status": d.status, "remarks": d.remarks}
            for d in res.dimensions]
    flags = [{k: v for k, v in asdict(f).items()} for f in res.flags]
    return {"rows": rows, "abstract": abstract, "rooms": rooms, "openings": openings, "dimensions": dims,
            "flags": flags, "assumptions": res.assumptions, "meta": res.meta,
            "abstract_by_floor": abstract_by_floor(res)}


def floor_groups(rows: list[MBRow]) -> list[str]:
    """Floor groups in building order (lowest floor first); [] for a single-storey MB."""
    order: dict[str, float] = {}
    for r in rows:
        if r.floor_group:
            order[r.floor_group] = min(order.get(r.floor_group, r.floor_order), r.floor_order)
    return sorted(order, key=lambda g: (order[g], g))


def abstract_by_floor(res: MBResult) -> list[dict]:
    """Abstract items split per floor group (multi-storey); totals equal the Abstract."""
    groups = floor_groups(res.mb)
    if not groups:
        return []
    by_id = {r.id: r for r in res.mb}
    out = []
    for k, ab in enumerate(res.abstract, 1):
        per = {g: 0.0 for g in groups}
        other = 0.0
        for rid in ab.row_ids:
            r = by_id[rid]
            if r.floor_group in per:
                per[r.floor_group] += r.qty
            else:
                other += r.qty
        out.append({"sno": k, "item": ab.item, "unit": ab.unit, "total": ab.qty,
                    "floors": {g: round(v, 3) for g, v in per.items()}, "general": round(other, 3)})
    return out
