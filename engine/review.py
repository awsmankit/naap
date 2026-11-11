"""What the Review step shows, derived from what the engine used in this run (engine/mb.py calls
arch_review at the end of build_mb and puts the result in meta["review"]).

    views     every view region: purpose ("measured" | "heights" | "finishes" | "sizes" | "legend" | "unused"),
              a one-line reason, the plans it feeds, and the plans a tick would double-count
    plans     floors, multiplier, floor group and storey height per measured plan, with the flag ids that
              confirm them (answers go into user_config like any flag answer)
    building  a one-line summary ("5 storeys, +450 to +15,450, 1 typical group")
    heights   wall heights per block / storey, wall types, slab, beam depth, lintel: value, source, assumed?,
              and the flag ids an answer resolves
    questions open flags that decide quantities (heights, floors, sizes, mismatches that need a choice)

Nothing here changes a quantity: it only reports.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .checks import _fid, block_name
from .storeys import floor_name, group_name

LABEL = {"measured": "Measured", "heights": "Used for heights", "finishes": "Used for finishes", "sizes": "Sizes",
         "legend": "Legend", "unused": "Not used"}
KEY_PLAN = re.compile(r"\b(KEY|LOCATION|INDEX|SITE)\s*PLAN\b", re.I)
TITLE_BLOCK = re.compile(r"\b(DRAWN|CHECKED|APPROVED|SCALE|DRG\.?\s*NO|DRAWING\s*(NO|TITLE)|PROJECT|CLIENT|ARCHITECT|"
                         r"SHEET\s*NO|REVISION|SIGNATURE)\b", re.I)
NOTES = re.compile(r"\bNOTES?\b|\bLEGEND\b|\bABBREVIATIONS?\b", re.I)
MATERIAL = re.compile(r"\b(BRICK|AAC|FLY\s*ASH|STONE|CONCRETE BLOCK|BLOCK|RCC|CONCRETE)\b", re.I)


def _lv(v: float) -> str:
    return f"{v:+,.0f}" if v else "±0"


def _short(names: list[str], n: int = 2) -> str:
    names = list(dict.fromkeys(names))
    if len(names) <= n:
        return " and ".join(names)
    return ", ".join(names[:n]) + f" and {len(names) - n} more"


def flag_q(f, fid) -> dict:
    return {"id": fid(f), "severity": f.severity, "category": f.category, "title": f.title, "detail": f.detail,
            "input": f.input, "status": f.status, "value": f.value, "evidence": list(f.evidence or [])[:3],
            "suggested_fix": f.suggested_fix}


# ---------------------------------------------------------------- views
def _views(a, st, rv, roof, roof_rids, fps, flags, copies, stale, fid, lintel_ent) -> list[dict]:
    ents = a.geometry["entities"]
    text_rid = {i: rid for i, _, rid in a.texts}
    measured = {fp.rid: fp for fp in fps}
    floor_plans = [a.region_name(fp.rid) for fp in fps]
    # level labels (storey heights) by the region they are written in
    lv_by_rid: dict = defaultdict(list)
    for key, idxs in (st.get("levels") or {}).get("sources", {}).items():
        for i in idxs:
            lv_by_rid[text_rid.get(i)].append(key)
    # roof heights read from sections (engine/sections.roof_height_from_sections)
    tos_by_rid: dict = defaultdict(list)
    for h in rv["heights"]:
        if h.get("section_rid") is not None:
            tos_by_rid[h["section_rid"]].append(h)
    smap_by_rid: dict = defaultdict(list)
    for sm in rv["smaps"]:
        smap_by_rid[sm.region["id"]].append(sm)
    sched_by_rid: dict = defaultdict(list)
    for e in a.schedule:
        sched_by_rid[e.region].append(e.tag)
    # grooves on elevations (quantities.grooves_rows)
    grooves: dict = defaultdict(int)
    groove_layers = {k for k, r in enumerate(a.roles) if r == "groove"}
    if groove_layers:
        for e in ents:
            if e["t"] in ("L", "P") and e["l"] in groove_layers:
                grooves[a.region_of(e["p"][0], e["p"][1])] += 1
    # the hatch legend (analysis.find_legend): regions holding a legend swatch and its material word
    legend_rids = set()
    if a.legend:
        sw = {a.region_of(e["p"][0][0], e["p"][0][1]) for e in ents
              if e["t"] == "H" and e.get("pn") in a.legend and e["p"] and e["p"][0]}
        words = {rid for _, t, rid in a.texts if MATERIAL.search(t["s"])}
        legend_rids = {r for r in sw & words if r is not None and not a.is_plan(r)}
    texts_by_rid: dict = defaultdict(list)
    for _, t, rid in a.texts:
        texts_by_rid[rid].append(t["s"])
    copy_of: dict = {}
    for c in copies or []:
        copy_of.setdefault(c["b"], c["a"])
        copy_of.setdefault(c["a"], c["b"])
    copy_flag = {r: f for f in flags if "plan-copy" in f.refs for r in f.refs if r.startswith("region:")}
    lv_names = _level_names(st)

    out = []
    for r in a.regions:
        rid, kind = r["id"], r.get("kind", "other")
        v = {"id": rid, "kind": kind, "purpose": "unused", "reason": "", "feeds": [], "overlaps": [], "warn": ""}
        overlaps = [n for n in floor_plans if n != r.get("name")]
        if kind != "plan":
            v["overlaps"] = ([a.region_name(sm.cut["region"]) for sm in smap_by_rid.get(rid, [])] or overlaps)[:3]
        if a.is_plan(rid):
            v["purpose"] = "measured"
            p = (st.get("plans") or {}).get(rid) or {}
            if roof is not None and rid == roof.region:
                nlev = len(roof.levels)
                v["reason"] = "Roof: slab, parapet, coping and khurras" + (f"; {nlev} roof levels" if nlev > 1 else "")
            elif rid in roof_rids:
                v["reason"] = f"A second roof plan: only {a.region_name(roof.region) if roof else 'the first'} is measured"
                v["warn"] = v["reason"]
            elif rid in measured:
                v["reason"] = "Walls, rooms, doors and windows, plaster and floors"
            else:
                v["reason"] = "Ticked, but no walls or rooms were found on it"
                v["warn"] = v["reason"]
            if kind != "plan":
                v["warn"] = (f"This will double-count walls already measured in {_short(v['overlaps'])}"
                             if v["overlaps"] else "A non-plan view measured as a plan")
            elif rid in copy_flag and copy_flag[rid].status == "open":
                other = a.region_name(copy_of.get(rid))
                v["warn"] = f"Looks like a copy of {other}: say whether one is stale (question above)"
            if p.get("group") and st.get("multi"):
                v["feeds"] = [p["group"]]
        elif rid in stale or r.get("excluded_as"):
            v["reason"] = f"Stale copy of {a.region_name(copy_of.get(rid))} (your answer): not measured"
        elif kind == "plan":
            v["reason"] = "Unticked: not measured"
        elif (kind == "section" and (smap_by_rid.get(rid) or tos_by_rid.get(rid))) or (kind == "elevation" and grooves.get(rid)) \
                or lv_by_rid.get(rid):
            bits, feeds = [], []
            if tos_by_rid.get(rid):
                hs = tos_by_rid[rid]
                feeds += [h["plan"] for h in hs]
                bits.append("wall heights under roof " + ", ".join(f"T.O.S. {_lv(h['tos'])}" for h in hs if h.get("tos") is not None)
                            + f" on {_short([h['plan'] for h in hs])}")
            if lv_by_rid.get(rid):
                keys = lv_by_rid[rid]
                bits.append("level labels give storey heights" + (f" ({lv_names(keys)})" if lv_names(keys) else ""))
                feeds += [p["name"] for p in (st.get("plans") or {}).values() if p.get("floors")]
            if smap_by_rid.get(rid):
                sms = smap_by_rid[rid]
                cut_plans = [a.region_name(sm.cut["region"]) for sm in sms]
                feeds += cut_plans
                bits.append(f"cut {sms[0].letter}–{sms[0].letter} through {_short(cut_plans)}: floor finishes")
            if grooves.get(rid) and kind == "elevation":   # grooves drawn in sections are not counted
                bits.append(f"grooves ({grooves[rid]} lines)")
            v["feeds"] = list(dict.fromkeys(feeds))
            v["purpose"] = ("heights" if (tos_by_rid.get(rid) or lv_by_rid.get(rid))
                            else "finishes" if ((grooves.get(rid) and kind == "elevation") or smap_by_rid.get(rid))
                            else "unused")
            v["reason"] = "; ".join(bits)
            v["reason"] = v["reason"][:1].upper() + v["reason"][1:]
            if KEY_PLAN.search(r.get("name", "")) and kind not in ("section", "elevation"):
                # an area named after a KEY PLAN title it holds, with level labels from another drawing in it
                # (a key plan itself has no levels): say what is read and where
                v["reason"] = ("Not the key plan itself: this area is named after a KEY PLAN title in it, but also holds "
                               "level labels that are read. " + v["reason"])
        elif kind == "section":
            v["reason"] = ("Not linked to a measured plan: no section line with its letter on a plan, and no levels "
                           "written in it")
        elif kind == "elevation":
            v["reason"] = ("Nothing on it is read: no groove lines or level labels (external plaster is measured "
                           "from the plan walls)")
        elif sched_by_rid.get(rid):
            tags = sorted(set(sched_by_rid[rid]), key=lambda t: (t[0], len(t), t))
            v["purpose"] = "sizes"
            what = ("Door/window sizes" if kind == "schedule" else
                    "Key plan, but the door/window sizes written in it are read" if KEY_PLAN.search(r.get("name", ""))
                    else "Door/window sizes from the detail headings")
            v["reason"] = f"{what}: {', '.join(tags[:6])}" + (f" and {len(tags) - 6} more" if len(tags) > 6 else "")
        elif kind == "schedule":
            v["reason"] = "No door/window sizes could be read from it"
        elif rid in legend_rids:
            v["purpose"] = "legend"
            mats = sorted(set(a.legend.values()))
            v["reason"] = f"Hatch legend: tells {' / '.join(m.title() for m in mats)} walls apart"
        elif lintel_ent is not None and text_rid.get(lintel_ent) == rid:
            v["purpose"] = "sizes"
            v["reason"] = "Notes: lintel size"
        elif kind == "detail":
            v["reason"] = "Detail: drawn again at a larger scale, so it is not measured (it would double-count)"
        elif KEY_PLAN.search(r.get("name", "")) or any(KEY_PLAN.search(s) for s in texts_by_rid.get(rid, [])[:50]):
            v["reason"] = "Key plan: shows where the building sits, not measured"
        elif sum(1 for s in texts_by_rid.get(rid, []) if TITLE_BLOCK.search(s)) >= 2:
            v["reason"] = "Title block or sheet border"
        elif any(NOTES.search(s) for s in texts_by_rid.get(rid, [])):
            v["reason"] = "Notes or a legend/table: nothing in it is measured"
        else:
            v["reason"] = "Not a view the engine reads (no title it recognises)"
        v["label"] = LABEL[v["purpose"]]
        out.append(v)
    return out


def _level_names(st):
    lv = (st or {}).get("levels") or {"floors": {}, "special": {}}

    def names(keys):
        fl = sorted({k for k in keys if isinstance(k, int) and k in lv["floors"]})
        sp = [k for k in dict.fromkeys(keys) if isinstance(k, str) and k in lv["special"]]
        bits = []
        if fl:
            lo, hi = fl[0], fl[-1]
            bits.append(f"{floor_name(lo)} {_lv(lv['floors'][lo])}" + (
                f" … {floor_name(hi)} {_lv(lv['floors'][hi])}" if hi != lo else ""))
        bits += [f"{k.title()} {_lv(lv['special'][k])}" for k in sp]
        return ", ".join(bits)
    return names


# ---------------------------------------------------------------- plans and building
def _plans(a, st, roof_rids, fid_of, all_flags) -> list[dict]:
    by_id = {f.id: f for f in all_flags}
    lv = (st.get("levels") or {}).get("floors", {})
    out = []
    for rid, p in (st.get("plans") or {}).items():
        reg = a.reg_by_id.get(rid, {})
        ftf_flag = by_id.get(fid_of(_fid("storeys", f"ftf:{p['name']}")))
        floors = p.get("floors") or []
        out.append({
            "id": rid, "name": p["name"], "floors": floors, "special": p.get("special"),
            "multiplier": p.get("multiplier", 1), "group": p.get("group") or group_name(floors, p.get("special")),
            "source": p.get("source") or "", "ftf_mm": p.get("ftf"), "ftf_source": p.get("ftf_src") or "",
            "ftf_user": bool(p.get("ftf_user")) or str(reg.get("floor_to_floor_source") or "").startswith("user"),
            "level_mm": lv.get(floors[0]) if floors else (st.get("levels") or {}).get("special", {}).get(p.get("special")),
            "roof": rid in roof_rids, "user": str(reg.get("floors_source") or "").startswith("user"),
            "flag_floors": fid_of(p["flag"].id) if p.get("flag") is not None else None,
            "flag_ftf": ftf_flag.id if ftf_flag is not None else None,
        })
    out.sort(key=lambda q: (q["special"] is not None, q["floors"][0] if q["floors"] else 500, q["name"]))
    return out


def _building(st, plans, roof) -> dict:
    lv = st.get("levels") or {"floors": {}, "special": {}}
    claimed = sorted({n for p in plans for n in p["floors"] if not p["special"]})
    typical = sum(1 for p in plans if not p["special"] and p["multiplier"] > 1)
    if st.get("multi"):
        n = len(claimed)
        lo = lv["floors"].get(claimed[0]) if claimed else None
        hi = lv["special"].get("terrace") or (max(lv["floors"].values()) if lv["floors"] else None)
        bits = [f"{n} storeys" if n != 1 else "1 storey"]
        if lo is not None and hi is not None:
            bits.append(f"{_lv(lo)} to {_lv(hi)}")
        bits.append(f"{typical} typical group{'s' if typical != 1 else ''}" if typical else "no typical floors")
        return {"multi": True, "storeys": n, "lo_mm": lo, "hi_mm": hi, "typical_groups": typical, "text": ", ".join(bits)}
    measured = [p for p in plans if not p["special"] and not p["roof"]]
    n = sum(p["multiplier"] for p in measured) or (1 if measured else 0)
    text = "No floor plan found" if n == 0 else "Single storey" if n == 1 else f"{n} floors"
    if roof is not None:
        tos = sorted({L.tos_mm for L in roof.levels if L.tos_mm is not None})
        if tos:
            text += ", roof at T.O.S. " + ", ".join(_lv(t) for t in tos)
    return {"multi": False, "storeys": n, "lo_mm": None, "hi_mm": None, "typical_groups": typical, "text": text}


# ---------------------------------------------------------------- heights
def _heights(a, st, rv, roof, all_flags, cfg, walls_cfg, fid_of, brick_state, depth) -> dict:
    from .is1200 import beam_depth, brick_cfg, lintel_spec
    rows = []
    default_h = float((walls_cfg.get("wall-brick") or {}).get("height_mm") or 3000)
    fps = rv["fps"]
    text_rid = {i: rid for i, _, rid in a.texts}
    lv_src = (st.get("levels") or {}).get("sources", {})

    def ids(fs):
        return list(dict.fromkeys(fid_of(f.id) for f in fs))

    def label_views(keys):
        rids = [text_rid.get(i) for k in keys for i in lv_src.get(k, []) if text_rid.get(i) is not None]
        rank = {"section": 0, "elevation": 1}
        names = sorted({(rank.get(a.kind(r), 2), a.region_name(r)) for r in rids})
        best = [n for k, n in names if k == names[0][0]] if names else []
        return best

    if st.get("multi"):
        by_id = {f.id: f for f in all_flags}
        for p in sorted(st["plans"].values(), key=lambda p: p.get("order", 500)):
            if p.get("special") or not a.is_plan(p["rid"]):
                continue
            ftf = p.get("ftf")
            reg = a.reg_by_id.get(p["rid"], {})
            user = bool(p.get("ftf_user")) or str(reg.get("floor_to_floor_source") or "").startswith("user")
            ftf_flag = by_id.get(fid_of(_fid("storeys", f"ftf:{p['name']}")))
            fl = p.get("floors") or []
            above = fl[0] + 1 if fl else None
            views = label_views(fl[:1] + ([above] if above in (st["levels"]["floors"]) else ["terrace"]))
            src = ("your answer" if user else
                   (f"from level labels in {_short(views)}" if views else "from level labels") if ftf
                   else "not found: please enter")
            rows.append({"key": f"storey:{p['rid']}", "kind": "storey", "rid": p["rid"], "plan": p["name"],
                         "name": p.get("group") or p["name"], "ftf_mm": ftf, "ftf_source": p.get("ftf_src") or "",
                         "height_mm": (ftf - (depth or 0)) if ftf else None, "source": src,
                         "assumed": not ftf or depth is None, "confirmed": user,
                         "flag_ids": [ftf_flag.id] if ftf_flag is not None else []})
    else:
        for h in rv["heights"]:
            f = h.get("flag")
            rows.append({"key": f"roof:{h['rid']}:{h['level']}", "kind": "roof", "rid": h["rid"], "plan": h["plan"],
                         "name": h["name"], "height_mm": h.get("height_mm"), "suggested_mm": h.get("suggested"),
                         "to_slab_top_mm": h.get("to_slab_top_mm"),
                         "source": (h.get("source") or ("your answer" if f is not None and f.status == "resolved"
                                                         else "not found: default used, please enter")),
                         "assumed": f is None or f.status != "resolved", "confirmed": f is not None and f.status != "open",
                         "section": h.get("section"), "flag_ids": [fid_of(f.id)] if f is not None else []})
        for f in all_flags:
            if "wall-height" not in f.refs:
                continue
            rid = next((int(r[7:]) for r in f.refs if r.startswith("region:") and r[7:].lstrip("-").isdigit()), None)
            val = float(f.value) if f.status == "resolved" and _isnum(f.value) else None
            rows.append({"key": f"block:{f.id}", "kind": "block", "rid": rid, "plan": a.region_name(rid),
                         "name": block_name(f.title),
                         "height_mm": val if val is not None else default_h,
                         "source": "your answer" if val is not None else "default – please confirm (no section through it)",
                         "assumed": val is None, "confirmed": f.status != "open", "flag_ids": [f.id]})
    for k, z in enumerate(cfg.get("zones") or []):
        if z.get("bbox"):
            rows.append({"key": f"zone:{k}", "kind": "zone", "zone": k, "rid": z.get("region"), "plan": "",
                         "name": z.get("name") or f"Zone {k + 1}", "height_mm": z.get("wall_height_mm"),
                         "source": "your config (zones)", "assumed": bool(z.get("assumed")), "confirmed": True, "flag_ids": []})
    # the default row is shown only for walls outside every row above (an open block row already
    # stands for the walls inside it, at the same default height)
    open_boxes = [(r["rid"], f.location["bbox"]) for f in all_flags if "wall-height" in f.refs and f.status == "open"
                  and (f.location or {}).get("bbox")
                  for r in [{"rid": next((int(x[7:]) for x in f.refs if x.startswith("region:") and x[7:].lstrip("-").isdigit()), None)}]]
    default_used = False
    for fp in fps:
        mw = (fp.masonry | (fp.linewall >= 0)) & (fp.hsrc == 0)
        for rid, bb in open_boxes:
            if rid == fp.rid:
                inside = fp.grid.zeros()
                fp.grid.fill_rect(inside, bb)
                mw &= ~inside
        if bool(mw.any()):
            default_used = True
            break
    wb = (cfg.get("walls") or {}).get("wall-brick") or {}
    rows.append({"key": "default", "kind": "default", "rid": None, "plan": "",
                 "name": "Any other walls (not in a building, roof level or storey above)", "height_mm": default_h,
                 "source": "your value" if wb.get("height_confirmed") else "default – please confirm",
                 "assumed": not wb.get("height_confirmed"), "confirmed": bool(wb.get("height_confirmed")),
                 "used": default_used, "flag_ids": []})

    # wall types (the parapet: the size the book uses, where it came from, and the brick height under the coping)
    rc = cfg.get("roof") or {}
    coping = float(rc.get("coping_mm") or 100)
    types = []
    for role in ("wall-brick", "wall-AAC", "parapet"):
        w = walls_cfg.get(role) or {}
        t = {"role": role, "thickness_mm": w.get("thickness_mm"), "height_mm": None, "height_source": ""}
        if role == "parapet":
            if roof is None:
                t.update(height_mm=w.get("height_mm"), height_source="not used: no roof plan is measured", used=False)
            else:
                src = getattr(roof, "parapet_h_source", "note")
                note = roof.parapet_note[:60]
                t.update(thickness_mm=roof.parapet_t_mm, height_mm=roof.parapet_h_mm, used=True,
                         thickness_source={"user": "your value", "note": f"from note '{note}'"}.get(
                             getattr(roof, "parapet_t_source", "note"), "default – please confirm"),
                         height_source={"user": "your value", "note": f"from note '{note}'"}.get(src, "default – please confirm (no parapet note)"),
                         height_assumed=src == "default", coping_mm=coping,
                         brick_height_mm=max((roof.parapet_h_mm or 0) - coping, 0))
        types.append(t)

    # slab, beam, lintel
    slab_fl = [f for f in all_flags if "slab" in f.refs or "storey-depth" in f.refs]
    slab = rv.get("slab_thk")
    slab_src = ""
    if slab:
        slab_src = "your answer" if any(f.status == "resolved" for f in slab_fl) else "your config"
    elif depth:
        slab, slab_src = depth, "your answer (storey deduction)"
    slab_q = {"value": slab, "source": slab_src or "not given – please enter", "assumed": not slab,
              "flag_ids": ids(slab_fl)}

    beam_fl = [f for f in all_flags if "beam-depth" in f.refs and f.input]
    bc = brick_cfg(cfg)
    D, dsrc = (None, "")
    if fps:
        D, dsrc = beam_depth(a, cfg, a.region_name(fps[0].rid), None)
    if D is None:
        res = [f for f in beam_fl if f.status == "resolved" and _isnum(f.value)]
        if res:
            D, dsrc = float(res[0].value), "your answer"
    beam_q = {"value": D, "source": _src_word(dsrc) if D is not None else ("IS 1200 beam rule is off" if not bc.get(
        "is1200_beam_soffit") else "not given – brickwork taken to the slab soffit"),
              "assumed": D is None, "flag_ids": ids(beam_fl), "on": bool(bc.get("is1200_beam_soffit"))}

    lin_fl = [f for f in all_flags if ("lintel" in f.refs) and f.input]
    lin = brick_state.get("lintel") or lintel_spec(a, cfg)
    lv, lsrc = lin[0], lin[2]
    res = [f for f in lin_fl if f.status == "resolved" and f.value]
    if res:
        lv, lsrc = res[0].value, "your answer"
    lintel_q = {"value": lv, "source": _src_word(lsrc) if lv else "not given – lintels not deducted",
                "assumed": not lv, "flag_ids": ids(lin_fl), "on": bool(bc.get("deduct_lintels"))}
    return {"multi": bool(st.get("multi")), "rows": rows, "default_used": default_used, "wall_types": types,
            "slab": slab_q, "beam": beam_q, "lintel": lintel_q, "storey_deduction_mm": depth}


def _isnum(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _src_word(src: str) -> str:
    s = src or ""
    if s.startswith("per user") or s.startswith("user"):
        return "your answer" if "flag" in s or s.startswith("per user") else "your config"
    if s.startswith("drawing note"):
        return "from note " + s[len("drawing note "):]
    if "schedule" in s or "written in the drawing" in s:
        return "from " + s
    return s


# ---------------------------------------------------------------- questions
Q_ORDER = {"floors": 0, "heights": 1, "structure": 2, "choice": 3}


def _topic(f) -> str | None:
    refs = set(f.refs)
    if f.category == "storeys":
        return "floors" if (f.input and "Floors" in (f.input.get("label") or "")) or f.severity == "error" else (
            "structure" if "storey-depth" in refs else "heights")
    if "wall-height" in refs or any(r.startswith("height-level:") for r in refs):
        return "heights"
    if refs & {"beam-depth", "slab", "lintel"} and f.input:
        return "structure"
    if "plan-copy" in refs:
        return "choice"
    if f.severity == "error" and f.input and f.category in ("opening-size", "room-size", "schedule"):
        return "choice"
    return None


def questions(flags, fid=lambda f: f.id) -> list[dict]:
    out = []
    for f in flags:
        t = _topic(f)
        if t is None:
            continue
        q = flag_q(f, fid)
        q["topic"] = t
        out.append(q)
    out.sort(key=lambda q: (Q_ORDER[q["topic"]], q["severity"] != "error"))
    return out


# ---------------------------------------------------------------- entry point
def arch_review(a, st, rv, roof, roof_rids, all_flags, cfg, walls_cfg, copies, stale, remap, brick_state) -> dict:
    def fid_of(i):
        return remap.get(i, i)
    fps = rv["fps"]
    lintel = brick_state.get("lintel")
    views = _views(a, st, rv, roof, roof_rids, fps, all_flags, copies, stale, fid_of, lintel[3] if lintel else None)
    plans = _plans(a, st, roof_rids, fid_of, all_flags)
    depth = (st.get("depth") or (None, None))[0]
    return {"kind": "architectural", "views": views, "plans": plans, "building": _building(st, plans, roof),
            "heights": _heights(a, st, rv, roof, all_flags, cfg, walls_cfg, fid_of, brick_state, depth),
            "questions": questions(all_flags)}
