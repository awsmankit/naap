"""MBResult -> .xlsx (sheets: MB, Abstract, Room schedule, Door-Window schedule,
All dimensions, Assumptions)."""
from __future__ import annotations

import math
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .mb import MBResult

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(bold=True, color="FFFFFF")
ITEM_FILL = PatternFill("solid", fgColor="E8EEF6")
TOTAL_FONT = Font(bold=True)
SUB_FONT = Font(bold=True, italic=True, color="1F3A5F")
THIN = Side(style="thin", color="B7C3D0")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
NUM = "0.000"


def _header(ws, cols: list[str], widths: list[int], title: str | None = None) -> int:
    row = 1
    if title:
        ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
        row = 3
    for i, (c, w) in enumerate(zip(cols, widths), start=1):
        cell = ws.cell(row=row, column=i, value=c)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=row + 1, column=1)
    return row + 1


def _product(*vals) -> float:
    present = [v for v in vals if v is not None]
    return math.prod(present) if present else 0.0


def write_xlsx(result: MBResult, dest: str | Path | BytesIO, project: str = "", quick: bool = False) -> None:
    wb = Workbook()

    # ---- MB -------------------------------------------------------------
    ws = wb.active
    ws.title = "MB"
    cols = ["S.No", "Item", "Location", "Nos", "L", "B", "H/D", "Qty", "Unit", "Remarks"]
    r = _header(ws, cols, [7, 44, 30, 7, 10, 10, 10, 12, 7, 40],
                f"Measurement Book{(' - ' + project) if project else ''}")
    groups: dict[tuple[str, str], list] = {}
    for row in result.mb:
        groups.setdefault((row.item, row.unit), []).append(row)
    total_cell: dict[tuple[str, str], str] = {}
    for sno, ((item, unit), rows) in enumerate(groups.items(), start=1):
        code = next((x.code for x in rows if x.code), "")
        ws.cell(row=r, column=1, value=sno)
        ws.cell(row=r, column=2, value=f"{item}{(' [' + code + ']') if code else ''}")
        for c in range(1, 11):
            ws.cell(row=r, column=c).fill = ITEM_FILL
            ws.cell(row=r, column=c).font = TOTAL_FONT
            ws.cell(row=r, column=c).border = BOX
        r += 1
        first = r
        if any(x.floor_group for x in rows):
            # Indian MB style: within each item, the measurements floor by floor (lowest first)
            rows = sorted(rows, key=lambda x: (x.floor_order if x.floor_group else 1e9, x.floor_group))
        cur = None
        for x in rows:
            if x.floor_group and x.floor_group != cur:
                cur = x.floor_group
                hc = ws.cell(row=r, column=3, value=cur)
                hc.font = SUB_FONT
                for c in range(1, 11):
                    ws.cell(row=r, column=c).border = BOX
                r += 1
            vals = [None, x.desc or None, x.location, x.nos, x.l, x.b, x.h]
            for c, v in enumerate(vals, start=1):
                if v is not None:
                    ws.cell(row=r, column=c, value=v)
            if abs(_product(x.nos, x.l, x.b, x.h) - x.qty) <= max(1e-3, abs(x.qty) * 1e-4):
                ws.cell(row=r, column=8, value=f"=PRODUCT(D{r}:G{r})")
            else:
                ws.cell(row=r, column=8, value=x.qty)
            ws.cell(row=r, column=9, value=x.unit)
            ws.cell(row=r, column=10, value=x.remarks)
            for c in range(1, 11):
                ws.cell(row=r, column=c).border = BOX
            for c in (5, 6, 7, 8):
                ws.cell(row=r, column=c).number_format = NUM
            r += 1
        ws.cell(row=r, column=3, value="Total").font = TOTAL_FONT
        tc = ws.cell(row=r, column=8, value=f"=SUM(H{first}:H{r - 1})")
        tc.font, tc.number_format = TOTAL_FONT, NUM
        ws.cell(row=r, column=9, value=unit).font = TOTAL_FONT
        for c in range(1, 11):
            ws.cell(row=r, column=c).border = BOX
        total_cell[(item, unit)] = f"MB!H{r}"
        r += 2

    # ---- Abstract -------------------------------------------------------
    ws = wb.create_sheet("Abstract")
    r = _header(ws, ["S.No", "Item", "Code", "Qty", "Unit", "Remarks"], [7, 50, 12, 14, 8, 40], "Abstract of quantities")
    for i, a in enumerate(result.abstract, start=1):
        ref = total_cell.get((a.item, a.unit))
        vals = [i, a.item, a.code, f"={ref}" if ref else a.qty, a.unit, a.remarks]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = BOX
        ws.cell(row=r, column=4).number_format = NUM
        r += 1

    # ---- Abstract by floor (multi-storey) -------------------------------
    from .mb import abstract_by_floor
    abf = abstract_by_floor(result)
    if abf:
        groups = list(abf[0]["floors"])
        ws = wb.create_sheet("Abstract by floor")
        cols = ["S.No", "Item", "Unit"] + groups + ["Other", "Total"]
        r = _header(ws, cols, [7, 50, 7] + [14] * len(groups) + [10, 14],
                    "Abstract by floor (typical-floor groups already include their floor multiplier)")
        for row_ in abf:
            vals = [row_["sno"], row_["item"], row_["unit"]] + [row_["floors"][g] for g in groups] + [row_["general"]]
            for c, v in enumerate(vals, start=1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.border = BOX
                if c > 3:
                    cell.number_format = NUM
            c_tot = len(vals) + 1
            tc = ws.cell(row=r, column=c_tot, value=f"=SUM({get_column_letter(4)}{r}:{get_column_letter(c_tot - 1)}{r})")
            tc.font, tc.number_format, tc.border = TOTAL_FONT, NUM, BOX
            r += 1

    # ---- Room schedule --------------------------------------------------
    ws = wb.create_sheet("Room schedule")
    r = _header(ws, ["S.No", "Room", "Location", "W (mm)", "L (mm)", "Area (sqm)", "Label text", "Measured box (mm)",
                     "Check", "Remarks"], [7, 30, 28, 10, 10, 12, 30, 16, 26, 50])
    for i, x in enumerate(result.rooms, start=1):
        box = f"{x.measured_l_mm}x{x.measured_b_mm}" if x.measured_l_mm else ""
        for c, v in enumerate([i, x.name, x.location, x.width_mm, x.length_mm, x.area_m2,
                               x.source_text.replace("\n", " / "), box, x.check, x.remarks], start=1):
            ws.cell(row=r, column=c, value=v).border = BOX
        ws.cell(row=r, column=6).number_format = NUM
        r += 1

    # ---- Door-Window schedule -------------------------------------------
    ws = wb.create_sheet("Door-Window schedule")
    r = _header(ws, ["Tag", "Type", "W (mm)", "H (mm)", "Count", "By location", "Remarks"], [10, 12, 10, 10, 8, 50, 30])
    for x in result.openings:
        loc = ", ".join(f"{k}: {v}" for k, v in sorted(x.locations.items()))
        for c, v in enumerate([x.tag, x.kind, x.width_mm, x.height_mm, x.count, loc, x.remarks], start=1):
            ws.cell(row=r, column=c, value=v).border = BOX
        r += 1

    # ---- All dimensions -------------------------------------------------
    ws = wb.create_sheet("All dimensions")
    r = _header(ws, ["S.No", "Location", "Layer", "Measurement", "Text override", "X", "Y", "Measured (geometry)",
                     "Status", "Remarks"], [7, 28, 22, 14, 18, 14, 14, 14, 10, 50])
    for i, d in enumerate(sorted(result.dimensions, key=lambda d: (d.location, d.layer, -d.y, d.x)), start=1):
        for c, v in enumerate([i, d.location, d.layer, d.measurement, d.text, d.x, d.y, d.measured,
                               d.status if d.status != "ok" else None, d.remarks or None], start=1):
            ws.cell(row=r, column=c, value=v)
        r += 1
    ws.auto_filter.ref = f"A1:J{max(r - 1, 1)}"

    # ---- Assumptions ----------------------------------------------------
    ws = wb.create_sheet("Assumptions")
    r = _header(ws, ["#", "Assumption / note"], [5, 120])
    for i, a in enumerate(result.assumptions, start=1):
        ws.cell(row=r, column=1, value=i)
        ws.cell(row=r, column=2, value=a).alignment = Alignment(wrap_text=True)
        r += 1
    for k, v in (result.meta or {}).items():
        ws.cell(row=r, column=2, value=f"{k}: {v}")
        r += 1
    if quick:
        from .quick import add_assumed_rows
        add_assumed_rows(ws, result, r)

    # ---- Flags ----------------------------------------------------------
    ws = wb.create_sheet("Flags")
    r = _header(ws, ["ID", "Severity", "Status", "Category", "Title", "Detail", "Location", "Evidence",
                     "Suggested fix", "User value"], [20, 10, 10, 16, 50, 50, 26, 50, 40, 20],
                "Drawing issues (open flags still affect the MB - see Remarks)")
    sev_fill = {"error": "FDECEA", "warning": "FFF4E5", "missing": "EEF2FF"}
    for f in result.flags:
        loc = ""
        if f.location:
            if "bbox" in f.location and f.location["bbox"]:
                b = f.location["bbox"]
                loc = f"box ({b[0]:.0f}, {b[1]:.0f}) - ({b[2]:.0f}, {b[3]:.0f})"
            elif "x" in f.location:
                loc = f"({f.location['x']:.0f}, {f.location['y']:.0f})"
        vals = [f.id, f.severity, f.status, f.category, f.title, f.detail, loc, "\n".join(f.evidence),
                f.suggested_fix, "" if f.value is None else str(f.value)]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = BOX
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if f.status == "open":
                cell.fill = PatternFill("solid", fgColor=sev_fill.get(f.severity, "FFFFFF"))
        r += 1
    if not result.flags:
        ws.cell(row=r, column=1, value="No flags")

    if quick:
        from .quick import add_readme
        add_readme(wb, result, project)
    wb.calculation.fullCalcOnLoad = True  # Qty/Total/Abstract cells are formulas
    wb.save(dest)
