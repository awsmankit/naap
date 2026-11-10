"""Brickwork per IS 1200 (Part 3) / CPWD: what is NOT brickwork inside the wall envelope.

Switches (user_config["brickwork"], documented defaults):

  is1200_beam_soffit  (True)   brick height stops at the underside of a beam running over the wall:
                               height = (floor-to-floor - slab, or the zone/flag wall height) - beam depth
  deduct_columns      (True)   RCC columns embedded in the wall (column-layer outline or hatch lying on
                               masonry) are deducted: footprint x brick height
  deduct_lintels      (True)   RCC lintels over openings are deducted when the lintel size is known
                               (config or a drawing note); otherwise a "missing" flag says they are not

  beam_depth_mm            beam projection BELOW the slab soffit (D - slab), mm
  beam_depth_by_region     {"<plan name>": mm}: default for a plan without beam lines (applied to all its walls)
  beam_schedule            {"B1": "230x450", ...}: beam sizes (e.g. from the structural schedule); the depth
                           below the slab = deepest D - slab thickness (slab_thickness_mm)
  beam_width_mm      (230) width of a beam drawn as a single centre line
  lintel_depth_mm          lintel depth (mm); lintel_bearing_mm (150) each side; lintel_width = wall thickness

Beam depth: user_config > resolved flag > schedule (config or a "B1 230X450" note in the drawing) >
"missing" flag (bricks are then taken to the slab soffit and the rows are unresolved).
Beam position: beam-role layers / layers named like "A-BEAM" (beam projection lines) on the plan;
without them, the per-plan default is applied to every wall of that plan, with a warning flag.

Zone / flag wall heights are read as floor-to-slab-soffit heights; a zone with "to_beam_soffit": true
(heights already read to the underside of a beam band in a section) gets no further beam deduction.
"""
from __future__ import annotations

import re

DEFAULTS = {"is1200_beam_soffit": True, "deduct_columns": True, "deduct_lintels": True, "beam_width_mm": 230,
            "lintel_bearing_mm": 150}
CITE_BEAM = "IS 1200 (Part 3): brickwork measured up to the underside of the beam/slab"
CITE_COL = "IS 1200 (Part 3): RCC columns embedded in masonry deducted"
CITE_LINTEL = "IS 1200 (Part 3) / CPWD: RCC lintels in masonry deducted"

BEAM_LAYER = re.compile(r"\bBEAMS?\b|\bBM\b")
NOT_BEAM_LAYER = re.compile(r"\bNO\b|\bNOS\b|TAG|TEXT|STIRRUP|MARK|LABEL|SCHED|DETAIL|HATCH")
LINTEL_NOTE = [
    re.compile(r"LINTEL[^0-9\n]{0,40}?(\d{2,3})\s*(?:MM)?\s*(?:THK|THICK|DEEP|DEPTH)", re.I),
    re.compile(r"(\d{2,3})\s*(?:MM)?\s*(?:THK|THICK|DEEP)\.?\s*(?:R\.?C\.?C\.?\s*)?LINTEL", re.I),
    re.compile(r"LINTEL[^0-9\n]{0,30}?\d{3}\s*[xX×]\s*(\d{2,3})\b", re.I),
]
BEAM_SIZE = re.compile(r"\b(B\d{1,3})\b[^0-9\n]{0,12}?(\d{3})\s*[xX×]\s*(\d{3,4})\b")


def brick_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("brickwork") or {})}


def beam_layer_ids(a) -> set[int]:
    out = set()
    for k, n in enumerate(a.layer_names):
        role = a.roles[k]
        if role == "beam":
            out.add(k)
            continue
        u = n.upper().replace("-", " ").replace("_", " ").split("$")[-1]
        if role == "ignore" and BEAM_LAYER.search(u) and not NOT_BEAM_LAYER.search(u):
            out.add(k)
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _size(v):
    m = re.search(r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})", str(v)) if not isinstance(v, (list, tuple)) else None
    if m:
        return float(m.group(1)), float(m.group(2))
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return float(v[0]), float(v[1])
    return None


def beam_schedule(a, cfg: dict) -> tuple[dict, str]:
    """{tag: (b, D)} from config, else from 'B1 230X450' notes in the drawing."""
    bc = brick_cfg(cfg)
    out = {}
    for k, v in (bc.get("beam_schedule") or {}).items():
        s = _size(v)
        if s:
            out[str(k)] = s
    if out:
        return out, "user config (brickwork.beam_schedule)"
    for _, t, _ in a.texts:
        for m in BEAM_SIZE.finditer(" ".join(t["s"].split())):
            out.setdefault(m.group(1).upper(), (float(m.group(2)), float(m.group(3))))
    return out, ("beam sizes written in the drawing" if out else "")


def slab_thickness(cfg: dict) -> float | None:
    bc = brick_cfg(cfg)
    for v in (bc.get("slab_thickness_mm"), cfg.get("slab_thickness_mm") if cfg.get("slab_thickness_mm_confirmed") else None,
              cfg.get("storey_deduction_mm")):
        if _num(v):
            return _num(v)
    return None


def beam_depth(a, cfg: dict, region_name: str, saved_value=None) -> tuple[float | None, str]:
    """Beam depth below the slab soffit for a plan, and where it came from."""
    bc = brick_cfg(cfg)
    by_reg = bc.get("beam_depth_by_region") or {}
    if _num(by_reg.get(region_name)) is not None:
        return _num(by_reg[region_name]), f"user config (brickwork.beam_depth_by_region['{region_name}'])"
    if _num(bc.get("beam_depth_mm")) is not None:
        return _num(bc["beam_depth_mm"]), "user config (brickwork.beam_depth_mm)"
    if _num(saved_value) is not None:
        return _num(saved_value), "per user (flag)"
    sched, src = beam_schedule(a, cfg)
    if sched:
        slab = slab_thickness(cfg)
        D = max(d for _, d in sched.values())
        if slab is not None:
            return max(D - slab, 0.0), f"{src}: deepest beam D {D:.0f} - slab {slab:.0f}"
    return None, ""


def lintel_spec(a, cfg: dict) -> tuple[float | None, float, str, int | None]:
    """(depth mm, bearing mm, source, text entity) of the RCC lintel over openings."""
    bc = brick_cfg(cfg)
    bearing = _num(bc.get("lintel_bearing_mm")) or 150.0
    if _num(bc.get("lintel_depth_mm")):
        return _num(bc["lintel_depth_mm"]), bearing, "user config (brickwork.lintel_depth_mm)", None
    for i, t, _ in a.texts:
        s = " ".join(t["s"].split())
        for rx in LINTEL_NOTE:
            m = rx.search(s)
            if m and 75 <= float(m.group(1)) <= 450:
                return float(m.group(1)), bearing, f"drawing note '{s[:60]}'", i
    return None, bearing, "", None
