"""convert CLI.

  uv run python -m convert.cli pdf2dxf in.pdf out.dxf [--page N] [--scale 1:100|auto] [--calibrate x1,y1,x2,y2,mm]
                                [--tolerance 0.2] [--layers auto|ocg|color|weight] [--sheets offset|blocks]
  uv run python -m convert.cli dxf2pdf in.dxf|in.dwg out.pdf [--paper A1] [--landscape|--portrait] [--mono]
                                [--layouts model|paper|all] [--only-layers A,B] [--hide-layers C,D]
  Writing DWG is not supported: write DXF, open it in AutoCAD and use Save As DWG.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.pdf_extract import ScannedPDFError

from .pdf_to_dxf import ConvertError, parse_calibration, pdf_to_dxf
from .to_pdf import dxf_to_pdf

DWG_NOTE = "DWG output is not supported. Write DXF, open it in AutoCAD and use Save As DWG."


def _split(s):
    return [v.strip() for v in s.split(",") if v.strip()] if s else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="convert", description="PDF to DXF and DXF/DWG to PDF. " + DWG_NOTE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("pdf2dxf", help="vector PDF to DXF (mm)")
    a.add_argument("pdf")
    a.add_argument("out")
    a.add_argument("--page", type=int, help="1-based page (default: all pages)")
    a.add_argument("--scale", help="drawing scale such as 1:100, or auto (title block); default none = paper size")
    a.add_argument("--calibrate", help="x1,y1,x2,y2,real_mm: two PDF points (from top-left) and their real distance")
    a.add_argument("--tolerance", type=float, default=0.2, help="Bezier flattening / arc fit tolerance in mm (default 0.2)")
    a.add_argument("--layers", default="auto", choices=["auto", "ocg", "color", "weight"])
    a.add_argument("--sheets", default="offset", choices=["offset", "blocks"])
    b = sub.add_parser("dxf2pdf", help="DXF or DWG to PDF")
    b.add_argument("src")
    b.add_argument("out")
    b.add_argument("--paper", default="A3", help="A0..A4 (default A3)")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--landscape", action="store_true")
    g.add_argument("--portrait", action="store_true")
    b.add_argument("--mono", action="store_true", help="all black instead of ACI colours")
    b.add_argument("--layouts", default="model", choices=["model", "paper", "all"])
    b.add_argument("--only-layers")
    b.add_argument("--hide-layers")
    c = sub.add_parser("dxf2dwg", help="not supported")
    c.add_argument("args", nargs="*")
    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "dxf2dwg":
            print(DWG_NOTE, file=sys.stderr)
            return 2
        if ns.cmd == "pdf2dxf":
            if ns.out.lower().endswith(".dwg"):
                print(DWG_NOTE, file=sys.stderr)
                return 2
            rep = pdf_to_dxf(ns.pdf, ns.out, page=ns.page, scale=ns.scale,
                             calibration=parse_calibration(ns.calibrate) if ns.calibrate else None,
                             tolerance=ns.tolerance, layer_by=ns.layers, sheets=ns.sheets)
        else:
            if ns.out.lower().endswith(".dwg"):
                print(DWG_NOTE, file=sys.stderr)
                return 2
            rep = dxf_to_pdf(ns.src, ns.out, paper=ns.paper, landscape=True if ns.landscape else False if ns.portrait else None,
                             mono=ns.mono, layouts=ns.layouts, layers=_split(ns.only_layers), hide_layers=_split(ns.hide_layers))
    except (ConvertError, ScannedPDFError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
