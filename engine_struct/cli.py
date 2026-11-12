"""Command line: structural PDF set -> structural Measurement Book workbook.

    uv run python -m engine_struct.cli a.pdf [b.pdf c.pdf ...] [--config cfg.json] --out mb.xlsx
                                       [--result-json r.json] [--flags-json flags.json] [--geometry-json geo.json]

cfg.json keys: "flags": {id: {"status": "resolved", "value": ...}} (answers to flags),
"calibration": {"<sheet no>": {"p1": [x, y], "p2": [x, y], "mm": L} | {"scale": 100} | {"pt_per_mm": r}},
"floor_heights_mm": {"S-05": 3300, ...}. PDF_BACKEND=<name> selects the PDF reader.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine_struct.cli", description="Structural PDF -> Measurement Book (xlsx)")
    ap.add_argument("pdf", nargs="+", help="one or more PDFs, read as ONE drawing set")
    ap.add_argument("--config")
    ap.add_argument("--out", required=True)
    ap.add_argument("--result-json")
    ap.add_argument("--flags-json")
    ap.add_argument("--geometry-json", help="also write the extracted geometry (viewer format)")
    ap.add_argument("--quick", action="store_true", help="no questions: build with the engine's defaults (assumed values marked) and add a 'Read me first' sheet; --config answers are still applied")
    a = ap.parse_args(argv)

    from app.pdf_extract import ScannedPDFError, extract_pdfs
    from engine_struct.build import build_struct_mb
    from engine_struct.export import result_json, write_struct_xlsx

    srcs = [Path(x).expanduser().resolve() for x in a.pdf]
    for src in srcs:
        if not src.is_file() or src.suffix.lower() != ".pdf":
            raise SystemExit(f"not a .pdf file: {src}")
    src = srcs[0]
    cfg = json.loads(Path(a.config).read_text()) if a.config else {}
    t0 = time.time()
    try:
        geo = extract_pdfs(srcs)
    except ScannedPDFError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.geometry_json:
        Path(a.geometry_json).write_text(json.dumps(geo))
    res = build_struct_mb(geo, cfg)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_struct_xlsx(res, out, project=src.stem + (f" (+{len(srcs) - 1} more)" if len(srcs) > 1 else ""), quick=a.quick)
    if a.result_json:
        Path(a.result_json).write_text(json.dumps(result_json(res), default=str))
    if a.flags_json:
        Path(a.flags_json).write_text(json.dumps([asdict(f) for f in res.flags], indent=1, default=str))

    print(f"{', '.join(x.name for x in srcs)}: {len(geo['sheets'])} sheets, {len(geo['entities'])} entities, {len(geo['layers'])} layers "
          f"({time.time() - t0:.1f}s)")
    for s in res.meta["sheets"]:
        print(f"  p{s['sheet']:<3} {s.get('file', ''):14s} {s['drawing_no'] or '':16s} {s['role']:7s} {s['title'] or ''}"
              + (f"  [x{s['floor_scope']['multiplier']}]" if s.get("floor_scope") else ""))
    for n, c in res.meta["calibration"].items():
        print(f"  scale p{n}: {c['pt_per_mm'] or 0:.5f} pt/mm (~1:{c['scale_1_to']}) {c['method']}, n={c['n']}, spread {c['spread_pct']}%")
    print(f"{'Item':64s} {'Qty':>12s} Unit  Status")
    for x in res.abstract:
        print(f"{x.item[:64]:64s} {x.qty:12.3f} {x.unit:5s} {x.remarks}")
    st = res.meta.get("steel") or {}
    print("steel by dia (t): " + ", ".join(f"#{d}: {kg / 1000:.2f}" for d, kg in (st.get("by_dia_kg") or {}).items())
          + f"; total {st.get('total_kg', 0) / 1000:.2f} t")
    by = {}
    for f in res.flags:
        by[f.severity] = by.get(f.severity, 0) + 1
    print(f"flags: {len(res.flags)} {by}")
    for f in res.flags[:60]:
        print(f"  [{f.severity}] {f.id}: {f.title}")
    if a.quick:
        from engine.quick import counts
        print(f"quick build: {counts(res)['assumed']} rows use assumed values; see 'Read me first', Assumptions and Flags in the workbook")
    print(f"wrote {out}" + (f", {a.result_json}" if a.result_json else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
