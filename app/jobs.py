"""Job store and the async processing pipeline (convert -> parse -> cluster)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.mb import DEFAULT_WALLS

from .cluster import cluster
from .extract import extract
from .roles import guess_roles

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DWG_MB_DATA", ROOT / "data" / "jobs"))
DWG2DXF = os.environ.get("DWG2DXF") or shutil.which("dwg2dxf") or "/opt/homebrew/bin/dwg2dxf"
CONVERT_TIMEOUT_S = int(os.environ.get("DWG_MB_CONVERT_TIMEOUT", "1200"))
MAX_LOG_BYTES = 200_000
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_EXT = {".dwg", ".dxf", ".pdf"}
MAX_PDF_PAGES = int(os.environ.get("DWG_MB_MAX_PDF_PAGES", "80"))

_lock = threading.Lock()
# MuPDF is not thread-safe: every PDF open/extract/render in this process goes through this lock.
PDF_LOCK = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")


class JobError(Exception):
    pass


def job_dir(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise JobError("invalid job id")
    d = DATA_DIR / job_id
    if not d.is_dir():
        raise JobError("job not found")
    return d


def new_job(filename: str, ext: str) -> tuple[str, Path]:
    job_id = uuid.uuid4().hex
    d = DATA_DIR / job_id
    d.mkdir(parents=True, exist_ok=False)
    _write_state(d, {
        "id": job_id,
        "filename": Path(filename).name[:200],
        "kind": ext.lstrip("."),
        "status": "uploading",
        "message": "",
        "error": None,
        "created": time.time(),
        "timings": {},
        "stats": {},
    })
    return job_id, d


def read_state(d: Path) -> dict:
    return json.loads((d / "job.json").read_text())


def _write_state(d: Path, state: dict) -> None:
    state["updated"] = time.time()
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(state))
    tmp.replace(d / "job.json")


def update_state(d: Path, **kw) -> dict:
    with _lock:
        st = read_state(d)
        st.update(kw)
        _write_state(d, st)
        return st


def sniff(path: Path, ext: str) -> bool:
    """Cheap content check so a renamed random file is rejected early."""
    with path.open("rb") as fh:
        head = fh.read(4096)
    if ext == ".pdf":
        return head.startswith(b"%PDF-") and head[5:6].isdigit()
    if ext == ".dwg":
        return head[:3] in (b"AC1", b"AC2") and head[3:6].isdigit()
    if head.startswith(b"AutoCAD Binary DXF"):
        return True
    text = head.decode("latin-1", errors="replace").lstrip("﻿ \r\n\t")
    first = [ln.strip() for ln in text.splitlines()[:4]]
    return len(first) >= 2 and first[0] in ("0", "999") and (first[1] == "SECTION" or first[0] == "999")


def submit(job_id: str) -> None:
    _executor.submit(_process, job_id)


def _convert(d: Path) -> Path:
    src, out = d / "input.dwg", d / "input.dxf"
    log = d / "convert.log"
    if not Path(DWG2DXF).exists():
        raise JobError(f"dwg2dxf not found at {DWG2DXF}")
    with log.open("wb") as fh:
        # Argument list (no shell); the drawing is only ever read by dwg2dxf/ezdxf.
        proc = subprocess.run([DWG2DXF, "-y", "-o", str(out), str(src)], cwd=d, stdout=fh,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=CONVERT_TIMEOUT_S, env={"PATH": "/usr/bin:/bin"})
    if log.stat().st_size > MAX_LOG_BYTES:  # "Unstable Class" warnings can be very chatty
        data = log.read_bytes()
        log.write_bytes(b"[...truncated...]\n" + data[-MAX_LOG_BYTES:])
    if not out.exists() or out.stat().st_size == 0:
        tail = log.read_text(errors="replace")[-600:]
        raise JobError(f"dwg2dxf failed (exit {proc.returncode}): {tail}")
    return out


def default_config(state: dict, geometry: dict, clusters: dict) -> dict:
    return {
        "project": {"name": Path(state["filename"]).stem},
        "layer_roles": guess_roles(geometry["layers"]),
        "walls": DEFAULT_WALLS,
        "column": {"height_mm": 3000},
        "slab_thickness_mm": 150,
        "levels": [{"name": "Ground floor (FFL)", "level_mm": 0}, {"name": "First floor slab", "level_mm": 3300}],
        "cluster_gap": clusters["gap"],
        "regions": clusters["regions"],
        "layer_style": {},
    }


def pdf_regions(geometry: dict) -> list[dict]:
    """A PDF comes split into sheets: each sheet is one region (no clustering)."""
    out = []
    for i, s in enumerate(geometry["sheets"]):
        tb = s.get("title_block") or {}
        out.append({"id": i, "name": tb.get("drawing_no") or f"Sheet {s['sheet']}", "title": tb.get("title") or "",
                    "bbox": s["bbox"], "kind": "sheet", "include": not s.get("scanned"), "sheet": s["sheet"],
                    "count": s.get("paths", 0) + s.get("texts", 0)})
    return out


def _process_pdf(d: Path, st: dict) -> None:
    from app.pdf_backends import open_pdf
    from app.pdf_extract import ScannedPDFError, extract_pdf

    timings = {}
    update_state(d, status="parsing", message="Reading the PDF sheets (vector paths and text)...")
    t = time.time()
    try:
        with PDF_LOCK:
            doc = open_pdf(d / "input.pdf")
            try:
                pages = doc.page_count
            finally:
                doc.close()
            if pages < 1:
                raise JobError("the PDF has no pages")
            if pages > MAX_PDF_PAGES:
                raise JobError(f"the PDF has {pages} pages; the limit is {MAX_PDF_PAGES} (set DWG_MB_MAX_PDF_PAGES)")
            geometry = extract_pdf(d / "input.pdf")
    except ScannedPDFError as exc:
        update_state(d, status="error", error=str(exc), error_kind="scanned")
        return
    except ValueError as exc:  # encrypted / malformed
        update_state(d, status="error", error=f"The PDF could not be read: {exc}")
        return
    timings["parse_s"] = round(time.time() - t, 1)
    (d / "geometry.json").write_text(json.dumps(geometry, separators=(",", ":")))
    regions = pdf_regions(geometry)
    config = {"engine": "struct", "project": {"name": Path(st["filename"]).stem}, "regions": regions,
              "flags": {}, "calibration": {}, "layer_style": {}}
    (d / "config.json").write_text(json.dumps(config, indent=1))
    update_state(d, status="ready", message="Ready for review", timings=timings, stats={
        "entities": len(geometry["entities"]),
        "layers": len(geometry["layers"]),
        "regions": len(regions),
        "sheets": len(geometry["sheets"]),
        "scanned_pages": geometry.get("scanned_pages", []),
        "truncated": geometry["truncated"],
        "units": "pt",
    })


def _process(job_id: str) -> None:
    d = DATA_DIR / job_id
    try:
        st = read_state(d)
        if st["kind"] == "pdf":
            return _process_pdf(d, st)
        timings = {}
        if st["kind"] == "dwg":
            update_state(d, status="converting", message="Converting DWG to DXF with LibreDWG (large files take a minute)...")
            t = time.time()
            dxf = _convert(d)
            timings["convert_s"] = round(time.time() - t, 1)
        else:
            dxf = d / "input.dxf"

        update_state(d, status="parsing", message="Reading DXF and extracting geometry...", timings=timings)
        t = time.time()
        geometry = extract(dxf)
        timings["parse_s"] = round(time.time() - t, 1)
        (d / "geometry.json").write_text(json.dumps(geometry, separators=(",", ":")))

        update_state(d, status="clustering", message="Finding view regions...", timings=timings)
        t = time.time()
        clusters = cluster(geometry["entities"], layouts=geometry.get("layouts"))
        timings["cluster_s"] = round(time.time() - t, 1)
        config = default_config(st, geometry, clusters)
        (d / "config.json").write_text(json.dumps(config, indent=1))

        update_state(d, status="ready", message="Ready for review", timings=timings, stats={
            "entities": len(geometry["entities"]),
            "layers": len(geometry["layers"]),
            "regions": len(clusters["regions"]),
            "truncated": geometry["truncated"],
            "units": geometry["units"],
            "dxf_mb": round(dxf.stat().st_size / 1e6, 1),
        })
    except subprocess.TimeoutExpired:
        update_state(d, status="error", error=f"DWG conversion timed out after {CONVERT_TIMEOUT_S}s")
    except JobError as exc:
        update_state(d, status="error", error=str(exc)[:800])
    except Exception as exc:  # report, never crash the worker
        (d / "error.log").write_text(traceback.format_exc())
        update_state(d, status="error", error=f"{type(exc).__name__}: {exc}"[:800])


def delete_job(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id))
