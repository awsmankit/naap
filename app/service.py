"""The job API without a web framework, shared by both runtimes.

`app/main.py` (FastAPI, the server) and `app/browser.py` (Pyodide, the in-browser worker) call
these functions; both keep a job as a folder (job.json, geometry.json, config.json, ...), so
quantities, flags and the workbook come from the same code either way. Errors are raised as
ServiceError(status, detail), which main.py turns into an HTTP error.
"""
from __future__ import annotations

import importlib
import json
import math
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from engine.export_xlsx import write_xlsx
from engine.mb import result_json as arch_result_json

from . import jobs
from .cluster import KINDS, cluster
from .roles import ROLES

MAX_UPLOAD_MB = int(os.environ.get("DWG_MB_MAX_UPLOAD_MB", "300"))
MAX_PDF_MB = int(os.environ.get("DWG_MB_MAX_PDF_MB", "100"))
ENGINE_MODULE = os.environ.get("MB_ENGINE", "engine.mb")
MAX_RENDER_PX = 24_000_000  # pixel budget per sheet render (dpi x clip), guards memory

# The browser keeps parsed geometry in memory between calls (re-reading a large geometry.json
# costs seconds under WebAssembly); the server reads the file each time.
GEOMETRY_CACHE = False
_geo_cache: dict[str, tuple[float, int, dict]] = {}


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------- jobs

def job_path(job_id: str) -> Path:
    try:
        return jobs.job_dir(job_id)
    except jobs.JobError as exc:
        raise ServiceError(404, str(exc))


def ready(job_id: str) -> Path:
    d = job_path(job_id)
    if jobs.read_state(d)["status"] != "ready":
        raise ServiceError(409, "job is not ready")
    return d


def is_struct(d: Path) -> bool:
    """PDF jobs use the structural engine (engine_struct); DWG/DXF jobs the architectural one."""
    return jobs.read_state(d).get("kind") == "pdf"


def meta() -> dict:
    return {"roles": ROLES, "kinds": KINDS, "max_upload_mb": MAX_UPLOAD_MB, "max_pdf_mb": MAX_PDF_MB,
            "engine": ENGINE_MODULE, "accept": sorted(jobs.ALLOWED_EXT)}


def list_jobs() -> list[dict]:
    out = []
    if jobs.DATA_DIR.is_dir():
        for d in jobs.DATA_DIR.iterdir():
            if jobs.JOB_ID_RE.match(d.name) and (d / "job.json").exists():
                st = jobs.read_state(d)
                out.append({k: st.get(k) for k in ("id", "filename", "status", "created", "kind")})
    return sorted(out, key=lambda s: -(s["created"] or 0))[:20]


def status(job_id: str) -> dict:
    st = jobs.read_state(job_path(job_id))
    st["elapsed_s"] = round(time.time() - st.get("created", time.time()), 1)
    return st


def load_geometry(d: Path) -> dict:
    f = d / "geometry.json"
    if not GEOMETRY_CACHE:
        return json.loads(f.read_text())
    st = f.stat()
    hit = _geo_cache.get(str(d))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    geo = json.loads(f.read_text())
    _geo_cache.clear()  # one drawing at a time: large geometries are hundreds of MB in memory
    _geo_cache[str(d)] = (st.st_mtime, st.st_size, geo)
    return geo


def forget(d: Path) -> None:
    _geo_cache.pop(str(d), None)


def read_config(d: Path) -> dict:
    return json.loads((d / "config.json").read_text())


def put_config(d: Path, cfg) -> dict:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("regions", []), list):
        raise ServiceError(400, "config must be an object")
    if is_struct(d):  # keep the engine marker and the saved calibration unless the client sends them
        old = read_config(d)
        cfg["engine"] = "struct"
        cfg.setdefault("calibration", old.get("calibration") or {})
    (d / "config.json").write_text(json.dumps(cfg, indent=1))
    return {"ok": True}


def recluster(d: Path, body: dict) -> dict:
    gap = float((body or {}).get("gap") or 0)
    geo = load_geometry(d)
    if is_struct(d):  # a PDF is already split into sheets
        return {"gap": 0, "regions": jobs.pdf_regions(geo)}
    res = cluster(geo["entities"], gap if gap > 0 else None, layouts=geo.get("layouts"))
    return {"gap": res["gap"], "regions": res["regions"]}


# ---------------------------------------------------------------- engine runs

def _engine():
    return importlib.import_module(ENGINE_MODULE).build_mb


def flag_summary(result) -> dict:
    counts = {"error": 0, "warning": 0, "missing": 0}
    for f in result.flags:
        if f.status == "open" and f.severity in counts:
            counts[f.severity] += 1
    return {"open": counts, "total": len(result.flags)}


def run(d: Path, cfg: dict):
    (d / "config.json").write_text(json.dumps(cfg, indent=1))
    geo = load_geometry(d)
    if is_struct(d):
        from engine_struct.build import build_struct_mb
        return build_struct_mb(geo, cfg)
    return _engine()(geo, cfg)


def write_workbook(d: Path, result, cfg: dict) -> None:
    project = (cfg.get("project") or {}).get("name", "")
    if is_struct(d):
        from engine_struct.export import write_struct_xlsx
        write_struct_xlsx(result, d / "mb.xlsx", project=project)
    else:
        write_xlsx(result, d / "mb.xlsx", project=project)


def result_json(d: Path, result) -> dict:
    if is_struct(d):
        from engine_struct.export import result_json as struct_result_json
        data = struct_result_json(result)
        kind = "structural"
    else:
        data = arch_result_json(result)
        kind = "architectural"
    data.pop("kind", None)
    return {"kind": kind, **data}  # "kind" first, so a cache can be checked by its first bytes


def cache_result(d: Path, result) -> None:
    """Keep the latest full result for GET /mb (rows with drawing refs)."""
    tmp = d / "mb_result.json.tmp"
    tmp.write_text(json.dumps(result_json(d, result), separators=(",", ":"), default=str))
    tmp.replace(d / "mb_result.json")


def run_and_cache(d: Path, cfg: dict, xlsx: bool):
    result = run(d, cfg)
    if xlsx:
        write_workbook(d, result, cfg)
    cache_result(d, result)
    return result


def check(d: Path, cfg: dict) -> dict:
    """Run the engine (checks included) without writing the workbook; returns flags and the
    abstract, so resolving a flag shows its effect on the quantities."""
    t = time.time()
    result = run_and_cache(d, cfg, False)
    return {"seconds": round(time.time() - t, 2), "flags": [asdict(f) for f in result.flags],
            "summary": flag_summary(result),
            "abstract": [{"item": a.item, "qty": a.qty, "unit": a.unit, "trade": a.trade} for a in result.abstract]}


def review(d: Path, cfg: dict | None) -> dict:
    """What the Review step shows (meta["review"] of an engine run: view purposes, floors per plan, heights
    with their sources, and the open questions). Reuses the cached result when `cfg` is the saved config
    and the cache is fresh; otherwise runs the engine on `cfg` (which saves it, like /check)."""
    t = time.time()
    data = None
    if not isinstance(cfg, dict) or cfg == read_config(d):
        data = json.loads(fresh_cache(d).read_text())
        if not (data.get("meta") or {}).get("review"):
            data = None   # cached before the review summary existed
        cfg = read_config(d)
    if data is None:
        if not isinstance(cfg.get("regions", []), list):
            raise ServiceError(400, "config must be an object")
        data = result_json(d, run_and_cache(d, cfg, False))
    rv = dict((data.get("meta") or {}).get("review") or {})
    counts = {"error": 0, "warning": 0, "missing": 0}
    for f in data.get("flags") or []:
        if f.get("status") == "open" and f.get("severity") in counts:
            counts[f["severity"]] += 1
    rv["summary"] = {"open": counts, "total": len(data.get("flags") or [])}
    rv["seconds"] = round(time.time() - t, 2)
    return rv


def build(d: Path, cfg: dict) -> dict:
    t = time.time()
    result = run_and_cache(d, cfg, True)
    return {
        "seconds": round(time.time() - t, 2),
        "counts": {"mb_rows": len(result.mb), "rooms": len(result.rooms), "openings": len(result.openings),
                   "dimensions": len(result.dimensions), "flags": len(result.flags)},
        "abstract": [asdict(a) for a in result.abstract],
        "rooms": [asdict(r) for r in result.rooms[:300]],
        "openings": [asdict(o) for o in result.openings],
        "flags": [asdict(f) for f in result.flags],
        "summary": flag_summary(result),
        "meta": result.meta,
    }


def fresh_cache(d: Path) -> Path:
    """mb_result.json, re-running the engine if the saved config is newer than the cache."""
    cache, cfgf = d / "mb_result.json", d / "config.json"
    if not cache.exists() or cache.stat().st_mtime < cfgf.stat().st_mtime:
        result = run(d, read_config(d))
        cache_result(d, result)
    else:
        with cache.open("rb") as fh:
            head = fh.read(16)
        if not head.startswith(b'{"kind":'):  # cache written before "kind" existed: add it once
            data = json.loads(cache.read_text())
            data.pop("kind", None)
            tmp = d / "mb_result.json.tmp"
            kind = "structural" if is_struct(d) else "architectural"
            tmp.write_text(json.dumps({"kind": kind, **data}, separators=(",", ":"), default=str))
            tmp.replace(cache)
    return cache


def workbook(d: Path) -> tuple[Path, str]:
    f = d / "mb.xlsx"
    if not f.exists():
        raise ServiceError(404, "build the MB first")
    stem = "".join(c for c in Path(jobs.read_state(d)["filename"]).stem if c.isalnum() or c in "-_ ")[:60] or "drawing"
    return f, f"MB - {stem}.xlsx"


def entities(d: Path, handles: str) -> dict:
    """Geometry records for the given DXF handles (comma separated), for highlighting refs.
    Returns {handle: [entity records]} (exploded block children share the parent's handle)."""
    want = {h.strip().upper() for h in (handles or "").split(",") if h.strip()}
    if not want or len(want) > 5000:
        raise ServiceError(400, "pass 1..5000 handles")
    out: dict[str, list] = {h: [] for h in want}
    for e in load_geometry(d)["entities"]:
        h = str(e.get("handle") or "").upper()
        if h in want:
            out[h].append(e)
    return out


# ---------------------------------------------------------------- PDF jobs: sheets, calibration, renders

def pdf_job(job_id: str) -> Path:
    d = ready(job_id)
    if not is_struct(d):
        raise ServiceError(404, "only PDF jobs have sheets")
    return d


def sheets(d: Path) -> list[dict]:
    f = d / "sheets.json"
    if f.exists() and f.stat().st_mtime >= (d / "geometry.json").stat().st_mtime:
        return json.loads(f.read_text())
    geo = load_geometry(d)
    counts: dict[int, dict] = {}
    for e in geo["entities"]:
        c = counts.setdefault(e.get("sheet"), {"entities": 0, "texts": 0})
        c["entities"] += 1
        c["texts"] += e["t"] == "T"
    out = [{**s, "counts": counts.get(s["sheet"], {"entities": 0, "texts": 0})} for s in geo["sheets"]]
    f.write_text(json.dumps(out))
    return out


def calibration(d: Path) -> dict:
    data = json.loads(fresh_cache(d).read_text())
    cfg = read_config(d)
    return {"calibration": data.get("calibration") or {}, "user": cfg.get("calibration") or {}}


def _num(v, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ServiceError(400, f"{name} must be a number")
    if not math.isfinite(f):
        raise ServiceError(400, f"{name} must be finite")
    return f


def _point(v, name: str) -> list[float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ServiceError(400, f"{name} must be [x, y]")
    return [_num(v[0], name), _num(v[1], name)]


def calib_spec(spec, sheet: dict) -> dict | None:
    """Validate one sheet's calibration: {p1, p2, mm} | {scale} | {pt_per_mm} | null (clear)."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ServiceError(400, "calibration per sheet must be an object or null")
    if "p1" in spec or "p2" in spec:
        p1, p2, mm = _point(spec.get("p1"), "p1"), _point(spec.get("p2"), "p2"), _num(spec.get("mm"), "mm")
        if mm <= 0 or mm > 1e7:
            raise ServiceError(400, "mm must be a positive length")
        b = sheet["bbox"]
        pad = max(b[2] - b[0], b[3] - b[1]) * 0.05
        for p in (p1, p2):
            if not (b[0] - pad <= p[0] <= b[2] + pad and b[1] - pad <= p[1] <= b[3] + pad):
                raise ServiceError(400, f"point {p} is not on sheet {sheet['sheet']}")
        if math.dist(p1, p2) < 0.5:
            raise ServiceError(400, "the two points are too close together")
        return {"p1": p1, "p2": p2, "mm": mm}
    if "scale" in spec:
        n = _num(spec["scale"], "scale")
        if not 1 <= n <= 100000:
            raise ServiceError(400, "scale must be N in 1:N, between 1 and 100000")
        return {"scale": n}
    if "pt_per_mm" in spec:
        r = _num(spec["pt_per_mm"], "pt_per_mm")
        if not 1e-6 < r < 100:
            raise ServiceError(400, "pt_per_mm out of range")
        return {"pt_per_mm": r}
    raise ServiceError(400, "give {p1, p2, mm}, {scale} or {pt_per_mm}")


def put_calibration(d: Path, body) -> dict:
    """Body {"<sheet>": {"p1": [x, y], "p2": [x, y], "mm": L} | {"scale": N} | {"pt_per_mm": r} | null}.
    Points are in the shared drawing space (as in geometry.json). Merged into config.calibration
    (null removes the sheet's override), then the MB is rebuilt."""
    if not isinstance(body, dict) or not body:
        raise ServiceError(400, "body must be {sheet: spec}")
    by_n = {str(s["sheet"]): s for s in sheets(d)}
    cfg = read_config(d)
    cal = dict(cfg.get("calibration") or {})
    for key, spec in body.items():
        k = str(key)
        if k not in by_n:
            raise ServiceError(400, f"no sheet {k}")
        v = calib_spec(spec, by_n[k])
        if v is None:
            cal.pop(k, None)
        else:
            cal[k] = v
    cfg["calibration"] = cal
    t = time.time()
    result = run_and_cache(d, cfg, True)
    return {"seconds": round(time.time() - t, 2), "user": cal, "calibration": result.meta.get("calibration") or {},
            "summary": flag_summary(result)}


_CLIP_RE = re.compile(r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$")
_LAYERS_RE = re.compile(r"^[^\x00-\x1f]{1,4000}$")


def sheet_png(d: Path, n: int, dpi: float = 72, clip: str = "", layers: str | None = None) -> bytes | Path:
    """Raster of one PDF sheet (1-based n). `clip=x0,y0,x1,y1` is in page points (y down, origin at
    the page's top-left, as displayed); `layers=a,b` renders only those OCG layers. Whole-sheet
    renders without layers are cached on disk. Returns the PNG bytes, or the cached file."""
    from app.pdf_backends import open_pdf

    sh = {s["sheet"]: s for s in sheets(d)}.get(n)
    if sh is None:
        raise ServiceError(404, "no such sheet")
    if not 4 <= dpi <= 600:
        raise ServiceError(400, "dpi must be between 4 and 600")
    W, H = sh["size"]
    box = None
    if clip:
        if not _CLIP_RE.match(clip):
            raise ServiceError(400, "clip must be x0,y0,x1,y1")
        x0, y0, x1, y1 = (float(v) for v in clip.split(","))
        x0, x1 = max(0.0, min(x0, x1)), min(W, max(x0, x1))
        y0, y1 = max(0.0, min(y0, y1)), min(H, max(y0, y1))
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise ServiceError(400, "clip is outside the sheet")
        box = (x0, y0, x1, y1)
    bw, bh = (box[2] - box[0], box[3] - box[1]) if box else (W, H)
    if bw * bh * (dpi / 72) ** 2 > MAX_RENDER_PX:
        raise ServiceError(400, f"render too large: lower dpi or clip (limit {MAX_RENDER_PX // 1_000_000} MP)")
    lay = None
    if layers is not None:
        if not _LAYERS_RE.match(layers):
            raise ServiceError(400, "bad layers")
        lay = [x for x in layers.split(",") if x]
    dpi = round(dpi, 1)
    cache = d / "render" / f"s{n}_{dpi:g}.png"
    if box is None and lay is None and cache.exists():
        return cache
    with jobs.PDF_LOCK:
        doc = open_pdf(d / "input.pdf")
        try:
            png = doc.page(n - 1).render_png(box, dpi, lay)
        finally:
            doc.close()
    if box is None and lay is None:
        cache.parent.mkdir(exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(png)
        tmp.replace(cache)
    return png
