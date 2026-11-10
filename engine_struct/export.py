"""Structural MBResult -> .xlsx: MB, Abstract, Steel summary, Schedules, Flags,
Assumptions, Calibration. Same MB layout as engine/export_xlsx.py (Qty = PRODUCT(Nos:H)
where the factors give the quantity; totals and Abstract are formulas)."""
from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from engine.types import MBResult

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
ITEM_FILL = PatternFill("solid", fgColor="E8EEF6")
ASSUMED_FILL = PatternFill("solid", fgColor="FFF4D6")
OPEN_FILL = PatternFill("solid", fgColor="FBE3E3")
BOLD = Font(bold=True)
SUB = Font(bold=True, italic=True, color="1F3A5F")
THIN = Side(style="thin", color="B7C3D0")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
NUM = "0.000"


def _header(ws, cols, widths, title=None) -> int:
    r = 1
    if title:
        ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
        r = 3
    for i, (c, w) in enumerate(zip(cols, widths), start=1):
        cell = ws.cell(row=r, column=i, value=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=r + 1, column=1)
    return r + 1


def write_struct_xlsx(res: MBResult, dest, project: str = "") -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "MB"
    r = _header(ws, ["S.No", "Item", "Location", "Nos", "L", "B", "H/D", "Qty", "Unit", "Remarks", "Status"],
                [7, 46, 46, 8, 10, 10, 10, 13, 6, 60, 10], f"Structural Measurement Book{(' - ' + project) if project else ''}")
    groups: dict[tuple, list] = {}
    for row in res.mb:
        groups.setdefault((row.item, row.unit), []).append(row)
    order = {(a.item, a.unit): k for k, a in enumerate(res.abstract)}
    total_cell = {}
    for sno, ((item, unit), rows) in enumerate(sorted(groups.items(), key=lambda kv: order.get(kv[0], 1e9)), start=1):
        ws.cell(row=r, column=1, value=sno)
        ws.cell(row=r, column=2, value=item)
        for c in range(1, 12):
            ws.cell(row=r, column=c).fill = ITEM_FILL
            ws.cell(row=r, column=c).font = BOLD
            ws.cell(row=r, column=c).border = BOX
        r += 1
        first = r
        rows = sorted(rows, key=lambda x: (x.floor_order if x.floor_group else -1e9, x.floor_group))
        cur = None
        for x in rows:
            if x.floor_group and x.floor_group != cur:
                cur = x.floor_group
                ws.cell(row=r, column=3, value=cur).font = SUB
                for c in range(1, 12):
                    ws.cell(row=r, column=c).border = BOX
                r += 1
            for c, v in enumerate([None, None, x.location, x.nos, x.l, x.b, x.h], start=1):
                if v is not None:
                    ws.cell(row=r, column=c, value=v)
            fac = [v for v in (x.nos, x.l, x.b, x.h) if v is not None]
            if fac and abs(math.prod(fac) - x.qty) <= max(1e-3, abs(x.qty) * 1e-4):
                ws.cell(row=r, column=8, value=f"=PRODUCT(D{r}:G{r})")
            else:
                ws.cell(row=r, column=8, value=x.qty)
            ws.cell(row=r, column=9, value=x.unit)
            ws.cell(row=r, column=10, value=x.remarks + (f"  [flags: {', '.join(x.flag_ids)}]" if x.flag_ids else ""))
            ws.cell(row=r, column=11, value=x.status)
            for c in range(1, 12):
                ws.cell(row=r, column=c).border = BOX
                if x.status == "assumed":
                    ws.cell(row=r, column=c).fill = ASSUMED_FILL
                elif x.status == "unresolved":
                    ws.cell(row=r, column=c).fill = OPEN_FILL
            for c in (5, 6, 7, 8):
                ws.cell(row=r, column=c).number_format = NUM
            r += 1
        ws.cell(row=r, column=3, value="Total").font = BOLD
        tc = ws.cell(row=r, column=8, value=f"=SUM(H{first}:H{r - 1})")
        tc.font, tc.number_format = BOLD, NUM
        ws.cell(row=r, column=9, value=unit).font = BOLD
        total_cell[(item, unit)] = f"MB!H{r}"
        r += 2

    ws = wb.create_sheet("Abstract")
    r = _header(ws, ["S.No", "Trade", "Item", "Qty", "Unit", "Remarks"], [7, 12, 52, 14, 7, 40], "Abstract of quantities")
    for i, a in enumerate(res.abstract, start=1):
        ref = total_cell.get((a.item, a.unit))
        for c, v in enumerate([i, a.trade, a.item, f"={ref}" if ref else a.qty, a.unit, a.remarks], start=1):
            ws.cell(row=r, column=c, value=v).border = BOX
        ws.cell(row=r, column=4).number_format = NUM
        r += 1

    st = res.meta.get("steel") or {}
    ws = wb.create_sheet("Steel summary")
    dias = sorted({int(d) for m in (st.get("by_element_kg") or {}).values() for d in m})
    r = _header(ws, ["Element"] + [f"#{d} (kg)" for d in dias] + ["Total (kg)", "Total (t)"],
                [24] + [12] * len(dias) + [14, 10], "Reinforcement by bar diameter and element (all floors)")
    first = r
    for el, m in (st.get("by_element_kg") or {}).items():
        ws.cell(row=r, column=1, value=el).border = BOX
        for k, d in enumerate(dias, start=2):
            c = ws.cell(row=r, column=k, value=m.get(d, 0) or None)
            c.border, c.number_format = BOX, "#,##0.0"
        lc = get_column_letter(len(dias) + 1)
        ws.cell(row=r, column=len(dias) + 2, value=f"=SUM(B{r}:{lc}{r})").number_format = "#,##0.0"
        ws.cell(row=r, column=len(dias) + 3, value=f"={get_column_letter(len(dias) + 2)}{r}/1000").number_format = "0.000"
        r += 1
    ws.cell(row=r, column=1, value="Total").font = BOLD
    for k in range(2, len(dias) + 3):
        col = get_column_letter(k)
        c = ws.cell(row=r, column=k, value=f"=SUM({col}{first}:{col}{r - 1})")
        c.font, c.number_format = BOLD, "#,##0.0"
    c = ws.cell(row=r, column=len(dias) + 3, value=f"={get_column_letter(len(dias) + 2)}{r}/1000")
    c.font, c.number_format = BOLD, "0.000"
    r += 2
    ws.cell(row=r, column=1, value=f"Of which assumed (scaled lengths or detailing conventions): {st.get('assumed_kg', 0):,.1f} kg")

    ws = wb.create_sheet("Schedules")
    r = 1
    sch = res.meta.get("schedules") or {}
    for t in sch.get("tables", []):
        ws.cell(row=r, column=1, value=f"{t['name']} (sheet {t['sheet']})").font = Font(bold=True, size=12)
        r += 1
        for c, h in enumerate(["Mark"] + t["columns"][1:], start=1):
            cell = ws.cell(row=r, column=c, value=h)
            cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BOX
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        r += 1
        for row in t["rows"]:
            for c, v in enumerate([row["label"]] + row["cells"][1:], start=1):
                ws.cell(row=r, column=c, value=v).border = BOX
            r += 1
        r += 1
    pile = sch.get("pile") or {}
    if pile:
        ws.cell(row=r, column=1, value="Pile detail").font = Font(bold=True, size=12)
        r += 1
        for k, v in pile.items():
            ws.cell(row=r, column=1, value=k)
            ws.cell(row=r, column=2, value=str(v))
            r += 1
    for i in range(1, 15):
        ws.column_dimensions[get_column_letter(i)].width = 16

    ws = wb.create_sheet("Flags")
    r = _header(ws, ["Id", "Severity", "Category", "Title", "Detail", "Status", "Value", "Input"],
                [36, 10, 14, 60, 80, 10, 12, 30], "Flags (missing data, conflicts, assumptions)")
    for f in res.flags:
        vals = [f.id, f.severity, f.category, f.title, f.detail, f.status, str(f.value) if f.value is not None else "",
                (f.input or {}).get("label", "")]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = BOX
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        r += 1

    ws = wb.create_sheet("Assumptions")
    r = _header(ws, ["#", "Assumption"], [5, 140], "Assumptions")
    for i, a in enumerate(res.assumptions, start=1):
        ws.cell(row=r, column=1, value=i)
        ws.cell(row=r, column=2, value=a).alignment = Alignment(wrap_text=True)
        r += 1

    ws = wb.create_sheet("Calibration")
    r = _header(ws, ["Sheet", "Drawing", "pt per mm", "≈ 1:N", "Samples", "Spread %", "Method", "Sources"],
                [7, 18, 12, 10, 9, 9, 30, 50], "Scale calibration per plan (all sheets are NOT TO SCALE)")
    for n, c in (res.meta.get("calibration") or {}).items():
        for k, v in enumerate([n, c.get("drawing_no"), c.get("pt_per_mm"), c.get("scale_1_to"), c.get("n"),
                               c.get("spread_pct"), c.get("method"), str(c.get("sources"))], start=1):
            ws.cell(row=r, column=k, value=v).border = BOX
        r += 1
    wb.save(dest if not isinstance(dest, Path) else str(dest))


def result_json(res: MBResult) -> dict:
    """Same shape as engine.mb.result_json (rows/abstract/flags/assumptions/meta), plus
    steel, schedules and calibration at the top level."""
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
    return {"kind": "structural", "rows": rows, "abstract": abstract, "rooms": [], "openings": [], "dimensions": [],
            "flags": [asdict(f) for f in res.flags], "assumptions": res.assumptions, "meta": res.meta,
            "steel": res.meta.get("steel"), "schedules": res.meta.get("schedules"),
            "calibration": res.meta.get("calibration")}
