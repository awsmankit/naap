"""Quick build: the book made with the engine's defaults and no review. Shared by both workbooks.

`assumed_rows` and `counts` define "N assumed values" (the number shown on the MB page and in the
workbook); `add_readme` puts a "Read me first" sheet in front, `add_assumed_rows` completes the
Assumptions sheet with every row that rests on a default.
"""
from __future__ import annotations

from openpyxl.styles import Alignment, Font

NOTE = "Drawing-based quantity book; verify before certification."
ASSUMED = ("assumed", "unresolved")


def assumed_rows(res) -> list:
    return [r for r in res.mb if r.status in ASSUMED]


def counts(res) -> dict:
    open_ = [f for f in res.flags if f.status == "open"]
    by = {k: sum(f.severity == k for f in open_) for k in ("error", "warning", "missing")}
    return {"assumed": len(assumed_rows(res)), "assumptions": len(res.assumptions), "open_flags": len(open_), "by_severity": by,
            "flags": len(res.flags)}


def add_readme(wb, res, project: str = "") -> None:
    ws = wb.create_sheet("Read me first", 0)
    c = counts(res)
    ws.column_dimensions["A"].width = 58
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 10
    ws.cell(row=1, column=1, value=f"Quick build{(' - ' + project) if project else ''}").font = Font(bold=True, size=14)
    ws.cell(row=2, column=1, value=NOTE).font = Font(italic=True)
    ws.cell(row=4, column=1, value="Built with the engine's defaults, without the review step.").alignment = Alignment(wrap_text=True)
    rows = [("Values taken from defaults or assumptions (rows)", c["assumed"]),
            ("Assumptions listed (sheet Assumptions)", c["assumptions"]),
            ("Open flags (sheet Flags)", c["open_flags"]),
            ("   of which errors", c["by_severity"]["error"]),
            ("   of which warnings", c["by_severity"]["warning"]),
            ("   of which missing data", c["by_severity"]["missing"])]
    r = 6
    for k, v in rows:
        ws.cell(row=r, column=1, value=k)
        ws.cell(row=r, column=2, value=v)
        r += 1
    ws.cell(row=r + 1, column=1, value="Headline totals").font = Font(bold=True, size=12)
    r += 2
    for a in res.abstract:
        ws.cell(row=r, column=1, value=a.item).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=r, column=2, value=round(a.qty, 3)).number_format = "#,##0.000"
        ws.cell(row=r, column=3, value=a.unit)
        r += 1
    ws.cell(row=r + 1, column=1, value="Open the Assumptions and Flags sheets, answer them in the app (Review) and build again to improve accuracy.").alignment = Alignment(wrap_text=True)


def add_assumed_rows(ws, res, r: int) -> int:
    """Append the rows that rest on a default (the Assumptions sheet's column B), return the next row."""
    rows = assumed_rows(res)
    if not rows:
        return r
    ws.cell(row=r + 1, column=2, value=f"Rows using assumed values ({len(rows)}): item / location / how").font = Font(bold=True)
    r += 2
    for x in rows:
        ws.cell(row=r, column=2, value=f"{x.item} | {x.location or '-'} | {x.status}: {(x.remarks or '').strip()}").alignment = Alignment(wrap_text=True)
        r += 1
    return r
