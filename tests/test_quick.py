"""Quick build: CLI --quick on synthetic data, the 'Read me first' sheet, and the UI wiring."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from openpyxl import load_workbook

from engine import cli as arch_cli
from engine_struct import cli as struct_cli
from tests.test_struct_synthetic import floor_sheet

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _cells(ws):
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def test_arch_quick_cli(tmp_path, capsys):
    plain, quick = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    assert arch_cli.main([str(ROOT / "samples" / "sample-house.dxf"), "--out", str(plain)]) == 0
    assert arch_cli.main([str(ROOT / "samples" / "sample-house.dxf"), "--out", str(quick), "--quick"]) == 0
    assert "quick build:" in capsys.readouterr().out
    wp, wq = load_workbook(plain), load_workbook(quick)
    assert "Read me first" not in wp.sheetnames  # nothing changes without --quick
    assert wq.sheetnames[0] == "Read me first" and wq.sheetnames[1:] == wp.sheetnames
    txt = " | ".join(_cells(wq["Read me first"]))
    assert "verify before certification" in txt and "Open flags" in txt
    assert {"Assumptions", "Flags"} <= set(wq.sheetnames)
    assert len(_cells(wq["Assumptions"])) > len(_cells(wp["Assumptions"]))  # assumed rows are listed
    n = [r for r in wq["Read me first"].iter_rows(values_only=True) if r[0] and str(r[0]).startswith("Values taken")][0][1]
    assert n > 0
    assert _cells(wp["MB"]) == _cells(wq["MB"])  # same quantities


def test_struct_quick_cli(tmp_path):
    pdf = floor_sheet(tmp_path)
    out = tmp_path / "s.xlsx"
    assert struct_cli.main([str(pdf), "--out", str(out), "--quick"]) == 0
    wb = load_workbook(out)
    assert wb.sheetnames[0] == "Read me first" and {"Assumptions", "Flags"} <= set(wb.sheetnames)
    assert any("Steel" in c or "RCC" in c for c in _cells(wb["Read me first"]))


def test_service_quick_flag(tmp_path):
    from app import service
    from app.extract import extract
    from engine.mb import build_mb

    cfg = arch_cli.effective_config(extract(ROOT / "samples" / "sample-house.dxf"), {}, "s")
    d = tmp_path
    (d / "job.json").write_text('{"kind":"dxf","status":"ready","filename":"s.dxf"}')
    res = build_mb(extract(ROOT / "samples" / "sample-house.dxf"), cfg)
    service.write_workbook(d, res, {**cfg, "quick": True})
    assert load_workbook(d / "mb.xlsx").sheetnames[0] == "Read me first"
    service.write_workbook(d, res, cfg)
    assert load_workbook(d / "mb.xlsx").sheetnames[0] == "MB"


def test_quick_ui_wiring():
    html = (WEB / "index.html").read_text()
    for i in ("btnQuick", "btnQuickUp", "quickBanner", "quickReview"):
        assert f'id="{i}"' in html
    js = (WEB / "js" / "main.js").read_text()
    assert "quickBuild" in js and "Built with" in js
