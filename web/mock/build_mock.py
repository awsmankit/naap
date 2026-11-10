"""Build web/mock/{geometry,config,mb}.json from the public sample drawing samples/sample-house.dxf.

    uv run python web/mock/build_mock.py

The files are what the app itself would produce for that upload: geometry from app/extract.py, the
default config from app/jobs.py and the GET /api/jobs/{id}/mb result of the engine. The UI's
?mock=1 mode loads them, so the book can be previewed without the app server. Dev-only: the web UI
never runs this, and the app does not serve .py files. tests/test_sample.py checks that the saved
rows still match the engine, so re-run this after engine changes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.cluster import cluster  # noqa: E402
from app.extract import extract  # noqa: E402
from app.jobs import default_config  # noqa: E402
from engine.mb import build_mb, result_json  # noqa: E402

SAMPLE = ROOT / "samples" / "sample-house.dxf"
OUT = Path(__file__).resolve().parent


def main() -> None:
    geo = extract(SAMPLE)
    cfg = default_config({"filename": SAMPLE.name}, geo, cluster(geo["entities"], layouts=geo.get("layouts")))
    res = result_json(build_mb(geo, cfg))
    res["meta"]["source"] = "engine run on samples/sample-house.dxf (made-up 2BHK house), built by web/mock/build_mock.py"
    mb = {"kind": "architectural", **res}
    (OUT / "geometry.json").write_text(json.dumps(geo, separators=(",", ":")))
    (OUT / "config.json").write_text(json.dumps(cfg, indent=1))
    (OUT / "mb.json").write_text(json.dumps(mb, separators=(",", ":"), default=str))
    print(f"wrote {OUT}/geometry.json, config.json, mb.json: {len(mb['rows'])} rows, {len(mb['flags'])} flags")


if __name__ == "__main__":
    main()
