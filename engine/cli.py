"""Command line: drawing -> Measurement Book workbook.

    uv run python -m engine.cli drawing.dwg|dxf [--config cfg.json] --out mb.xlsx
                                [--flags-json flags.json] [--result-json mb.json] [--dump-config used.json]

* A .dwg is converted with LibreDWG's dwg2dxf (DWG2DXF env var, default /opt/homebrew/bin/dwg2dxf)
  into a temporary folder; the input is never modified.
* Without --config, layer roles and view regions are guessed and every value the drawing
  does not give is a flagged assumption (see the Flags sheet / --flags-json).
* cfg.json may hold any user_config key (see engine/mb.py). Extra keys understood here:
    "extra_regions":    [{"name", "kind", "include", "bbox", "floors"?}]  appended to the guessed regions
    "region_overrides": {"<region name or id>": {"kind"?, "include"?, "floors"?, "name"?}}
    "layer_roles":      merged over the guessed roles
* --flags-json writes the flags (id, severity, title, input, status, value...) so answers can be
  put back into cfg.json as {"flags": {id: {"status": "resolved", "value": ...}}}.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

DWG2DXF = os.environ.get("DWG2DXF", "/opt/homebrew/bin/dwg2dxf")


def convert_dwg(src: Path, workdir: Path, timeout: int = 1200) -> Path:
    exe = DWG2DXF if Path(DWG2DXF).exists() else shutil.which("dwg2dxf")
    if not exe:
        raise SystemExit("dwg2dxf (LibreDWG) not found; set DWG2DXF=/path/to/dwg2dxf or pass a .dxf")
    out = workdir / "input.dxf"
    log = workdir / "convert.log"
    with log.open("wb") as fh:
        subprocess.run([exe, "-y", "-o", str(out), str(src)], cwd=workdir, stdout=fh, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, timeout=timeout, env={"PATH": "/usr/bin:/bin"})
    if not out.exists() or out.stat().st_size == 0:
        raise SystemExit(f"dwg2dxf failed; see {log}")
    return out


def effective_config(geo: dict, user: dict, name: str) -> dict:
    from app.cluster import cluster
    from app.roles import guess_roles
    from engine.mb import DEFAULT_WALLS

    cl = cluster(geo["entities"], user.get("cluster_gap"), layouts=geo.get("layouts"))
    cfg = {"project": {"name": name}, "layer_roles": guess_roles(geo["layers"]),
           "walls": json.loads(json.dumps(DEFAULT_WALLS)), "regions": cl["regions"], "cluster_gap": cl["gap"]}
    for k, v in user.items():
        if k.startswith("_") or k in ("extra_regions", "region_overrides"):
            continue
        if k == "layer_roles":
            cfg["layer_roles"].update(v)
        elif k == "walls":
            for role, vals in v.items():
                cfg["walls"].setdefault(role, {}).update(vals)
        else:
            cfg[k] = v
    nid = max((r["id"] for r in cfg["regions"]), default=-1) + 1
    for k, r in enumerate(user.get("extra_regions", []) or []):
        cfg["regions"].append({"count": 0, "title_guess": "", "level": "", "include": False, **r, "id": nid + k})
    for key, patch in (user.get("region_overrides") or {}).items():
        for r in cfg["regions"]:
            if str(r["id"]) == str(key) or r["name"] == key:
                r.update(patch)
                if "floor_to_floor_mm" in patch:
                    r["floor_to_floor_source"] = "user config (region_overrides)"
                if isinstance(patch.get("floors"), list) and "multiplier" not in patch:
                    r["multiplier"] = max(len(patch["floors"]), 1)
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine.cli", description="DWG/DXF -> Measurement Book (xlsx)")
    ap.add_argument("drawing")
    ap.add_argument("--config", help="user_config JSON (answers to flags, zones, finishes, region fixes)")
    ap.add_argument("--out", required=True, help="output .xlsx")
    ap.add_argument("--flags-json", help="write the flags here (JSON)")
    ap.add_argument("--result-json", help="write the full result (rows with drawing refs) here (JSON)")
    ap.add_argument("--dump-config", help="write the effective config (guessed roles/regions + yours) here")
    ap.add_argument("--quick", action="store_true", help="no questions: build with the engine's defaults (assumed values marked) and add a 'Read me first' sheet; --config answers are still applied")
    args = ap.parse_args(argv)

    from app.extract import extract
    from engine.export_xlsx import write_xlsx
    from engine.mb import build_mb, result_json

    src = Path(args.drawing).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() not in (".dwg", ".dxf"):
        raise SystemExit(f"not a .dwg/.dxf file: {src}")
    user = json.loads(Path(args.config).read_text()) if args.config else {}
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="dwgmb-") as tmp:
        dxf = convert_dwg(src, Path(tmp)) if src.suffix.lower() == ".dwg" else src
        geo = extract(dxf)
    cfg = effective_config(geo, user, src.stem)
    if args.dump_config:
        Path(args.dump_config).write_text(json.dumps(cfg, indent=1))
    res = build_mb(geo, cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_xlsx(res, out, project=cfg["project"]["name"], quick=args.quick)
    if args.flags_json:
        Path(args.flags_json).write_text(json.dumps([asdict(f) for f in res.flags], indent=1, default=str))
    if args.result_json:
        Path(args.result_json).write_text(json.dumps(result_json(res), default=str))

    print(f"{src.name}: {len(geo['entities'])} entities, units {geo['units']}, plans {res.meta.get('plans')}, "
          f"roof {res.meta.get('roof_plan')} ({time.time() - t0:.1f}s)")
    print(f"{'Item':72s} {'Qty':>11s} Unit  Status")
    for a in res.abstract:
        print(f"{a.item[:72]:72s} {a.qty:11.3f} {a.unit:5s} {a.remarks}")
    open_ = [f for f in res.flags if f.status == "open"]
    by = {}
    for f in open_:
        by[f.severity] = by.get(f.severity, 0) + 1
    print(f"flags: {len(res.flags)} total, {len(open_)} open {by}")
    for f in open_[:40]:
        print(f"  [{f.severity}] {f.id}: {f.title}" + (f"  (input: {f.input.get('label')})" if f.input else ""))
    if args.quick:
        from engine.quick import counts
        print(f"quick build: {counts(res)['assumed']} rows use assumed values; see 'Read me first', Assumptions and Flags in the workbook")
    print(f"wrote {out}" + (f", {args.flags_json}" if args.flags_json else "") + (f", {args.result_json}" if args.result_json else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
