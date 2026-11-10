"""External references: xref inserts, attached images and underlays, and xref-named layers.

Content that lives in another file is not in the DXF the engine reads, so it cannot be measured. The
extractor lists it in geometry["xrefs"]; here it becomes one "missing" flag naming every reference and
path. Layer names are checked too: an "XREF" layer, "xref|layer" names (attached xrefs) and
"xref$0$layer" names (xrefs bound into the drawing, whose content *is* present).
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from .types import Flag

XREF_LAYER = re.compile(r"X[\s_-]?REF|\bXRF\b", re.I)


def _fid(key: str) -> str:
    return f"missing-data-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def xref_report(a, geometry: dict, saved: dict) -> tuple[list[Flag], list[str], dict]:
    """(flags, assumption notes, meta) for external references and xref layers."""
    flags, notes = [], []
    unit = 1 / (a.to_m * 1000)
    xs = geometry.get("xrefs") or []
    groups: dict = defaultdict(list)
    for x in xs:
        groups[(x.get("kind"), x.get("name") or "", x.get("path") or "")].append(x)
    meta = {"external": [], "layers": []}
    if groups:
        ev, pts = [], []
        for (kind, name, path), lst in sorted(groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
            ins = [x for x in lst if x.get("p")]
            where = Counter(a.region_name(a.region_of(*x["p"])) for x in ins)
            pts += [x["p"] for x in ins]
            loaded = any(x.get("loaded") for x in lst)
            s = f"{kind} '{name}'" + (f" -> {path}" if path else " (no path)")
            if ins:
                s += f": {len(ins)} placement(s) in " + ", ".join(f"{k} x{v}" if v > 1 else k for k, v in where.items())
                layers = sorted({x.get('layer') for x in ins if x.get('layer')})
                if layers:
                    s += f" on layer(s) {', '.join(layers)}"
            else:
                s += ": defined but not placed in model space"
            if kind == "xref":
                s += " (content loaded as a block)" if loaded else " (not loaded: content missing)"
            ev.append(s)
            meta["external"].append({"kind": kind, "name": name, "path": path, "count": len(ins), "loaded": loaded,
                                     "regions": dict(where)})
        placed = [g for g in groups.values() if any(x.get("p") for x in g)]
        bbox = None
        if pts:
            bbox = [min(p[0] for p in pts) - 2000 * unit, min(p[1] for p in pts) - 2000 * unit,
                    max(p[0] for p in pts) + 2000 * unit, max(p[1] for p in pts) + 2000 * unit]
        kinds = Counter(k for k, _, _ in groups)
        f = Flag(
            id=_fid("external-references"), severity="missing", category="missing-data",
            title=f"{len(groups)} external reference(s) ({', '.join(f'{v} {k}' for k, v in kinds.items())}) not in this "
                  "file: their content is not measured",
            detail="Xrefs, attached images and PDF/DWF/DGN underlays live in other files. Whatever they show (walls of "
                   "another wing, a site plan, a scanned plan) is not in the uploaded DXF and is not in the MB. "
                   + ("" if placed else "None of them is placed in model space, so the plans are probably complete."),
            location={"bbox": bbox} if bbox else None,
            evidence=ev[:20],
            suggested_fix="Bind the xrefs (XREF > Bind) or upload the referenced drawings; accept if the referenced "
                          "content is not part of the work (title-block logo, site photo).",
            refs=["xrefs"])
        st = saved.get(f.id) or {}
        if st.get("status") in ("resolved", "accepted"):
            f.status, f.value = st["status"], st.get("value")
        flags.append(f)

    # layer names
    ents = geometry["entities"]
    per_layer: dict = defaultdict(lambda: {"n": 0, "types": Counter(), "regions": Counter(), "pts": []})
    xl = [k for k, n in enumerate(a.layer_names) if XREF_LAYER.search(n) or "|" in n]
    bound = sorted({n.split("$0$")[0] for n in a.layer_names if "$0$" in n})
    if xl:
        want = set(xl)
        for e in ents:
            li = e.get("l")
            if li not in want or e["t"] == "X":
                continue
            d = per_layer[li]
            d["n"] += 1
            d["types"][e["t"]] += 1
            p = e.get("p") or e.get("p1")
            if p:
                p0 = p[0] if isinstance(p[0], list) else p
                rid = a.region_of(p0[0], p0[1])
                d["regions"][a.region_name(rid)] += 1
                if a.is_plan(rid):
                    d["pts"].append(p0)
    names = {"L": "line", "P": "polyline", "T": "text", "H": "hatch", "I": "insert", "A": "arc", "C": "circle", "D": "dimension"}
    for li in xl:
        d = per_layer.get(li) or {"n": 0, "types": Counter(), "regions": Counter(), "pts": []}
        lname = a.layer_names[li]
        what = ", ".join(f"{v} {names.get(k, k)}(s)" for k, v in d["types"].items()) or "no entities"
        where = ", ".join(f"{k} x{v}" for k, v in d["regions"].items()) or "nowhere"
        meta["layers"].append({"layer": lname, "entities": d["n"], "types": dict(d["types"]), "regions": dict(d["regions"])})
        if "|" in lname:
            notes.append(f"Layer '{lname}' belongs to an attached xref ('{lname.split('|')[0]}'): its content is in "
                         f"another file and is not measured ({what} here).")
        if d["pts"]:
            xs_ = [p[0] for p in d["pts"]]
            ys_ = [p[1] for p in d["pts"]]
            f = Flag(
                id=_fid(f"xref-layer:{lname}"), severity="warning", category="missing-data",
                title=f"Layer '{lname}' (an xref layer) has content inside a measured plan",
                detail=f"{what} on '{lname}' in {where}. Xref layers usually hold a referenced drawing; this content has "
                       "no measurement role, so it is not in the MB.",
                location={"bbox": [min(xs_) - 1000 * unit, min(ys_) - 1000 * unit, max(xs_) + 1000 * unit, max(ys_) + 1000 * unit]},
                evidence=[f"{lname}: {what}; regions: {where}"],
                suggested_fix="Give the layer a role if its content is part of the work; otherwise accept.",
                refs=["xrefs", f"layer:{lname}"])
            st = saved.get(f.id) or {}
            if st.get("status") in ("resolved", "accepted"):
                f.status, f.value = st["status"], st.get("value")
            flags.append(f)
        elif "|" not in lname:
            notes.append(f"Layer '{lname}' is named as an xref layer but holds only {what} ({where}); no external "
                         "content is attached through it, nothing on it is in a measured plan.")
    if bound:
        notes.append("Layers from xrefs bound into the drawing (" + ", ".join(f"'{b}'" for b in bound[:8])
                     + "): their content is part of this file and is measured by its layer role.")
        meta["bound_xrefs"] = bound
    return flags, notes, meta
