"""FastAPI app: upload -> async job -> review config -> build MB -> download xlsx."""
from __future__ import annotations

import importlib
import json
import math
import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from engine.export_xlsx import write_xlsx
from engine.mb import result_json

from . import jobs
from .cluster import KINDS, cluster
from .roles import ROLES

MAX_UPLOAD_MB = int(os.environ.get("DWG_MB_MAX_UPLOAD_MB", "300"))
MAX_PDF_MB = int(os.environ.get("DWG_MB_MAX_PDF_MB", "100"))
ENGINE_MODULE = os.environ.get("MB_ENGINE", "engine.mb")
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"         # current UI, served at /
MAX_RENDER_PX = 24_000_000  # pixel budget per sheet render (dpi x clip), guards memory

app = FastAPI(title="Naap: drawing to Measurement Book", docs_url="/api/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)


def _dir(job_id: str) -> Path:
    try:
        return jobs.job_dir(job_id)
    except jobs.JobError as exc:
        raise HTTPException(404, str(exc))


def _ready(job_id: str) -> Path:
    d = _dir(job_id)
    if jobs.read_state(d)["status"] != "ready":
        raise HTTPException(409, "job is not ready")
    return d


@app.get("/api/meta")
def meta():
    return {"roles": ROLES, "kinds": KINDS, "max_upload_mb": MAX_UPLOAD_MB, "max_pdf_mb": MAX_PDF_MB,
            "engine": ENGINE_MODULE, "accept": sorted(jobs.ALLOWED_EXT)}


@app.post("/api/jobs")
async def upload(request: Request, file: UploadFile):
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in jobs.ALLOWED_EXT:
        raise HTTPException(400, "Only .dwg, .dxf and .pdf files are accepted")
    max_mb = MAX_PDF_MB if ext == ".pdf" else MAX_UPLOAD_MB
    limit = max_mb * 1024 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > limit + 1024 * 1024:
        raise HTTPException(413, f"File larger than {max_mb} MB")

    job_id, d = jobs.new_job(name, ext)
    dest = d / f"input{ext}"  # never use the client-supplied name on disk
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                fh.close()
                jobs.delete_job(job_id)
                raise HTTPException(413, f"File larger than {max_mb} MB")
            fh.write(chunk)
    if size == 0 or not jobs.sniff(dest, ext):
        jobs.delete_job(job_id)
        what = "PDF (no %PDF- header)" if ext == ".pdf" else f"{ext[1:].upper()} drawing"
        raise HTTPException(400, f"File does not look like a valid {what}")
    jobs.update_state(d, status="queued", message="Queued", size=size)
    jobs.submit(job_id)
    return {"id": job_id}


@app.get("/api/jobs")
def list_jobs():
    out = []
    if jobs.DATA_DIR.is_dir():
        for d in jobs.DATA_DIR.iterdir():
            if jobs.JOB_ID_RE.match(d.name) and (d / "job.json").exists():
                st = jobs.read_state(d)
                out.append({k: st.get(k) for k in ("id", "filename", "status", "created", "kind")})
    return sorted(out, key=lambda s: -(s["created"] or 0))[:20]


@app.get("/api/jobs/{job_id}")
def status(job_id: str):
    st = jobs.read_state(_dir(job_id))
    st["elapsed_s"] = round(time.time() - st.get("created", time.time()), 1)
    return st


@app.get("/api/jobs/{job_id}/geometry")
def geometry(job_id: str):
    return FileResponse(_ready(job_id) / "geometry.json", media_type="application/json")


@app.get("/api/jobs/{job_id}/config")
def get_config(job_id: str):
    return json.loads((_ready(job_id) / "config.json").read_text())


@app.put("/api/jobs/{job_id}/config")
async def put_config(job_id: str, request: Request):
    d = _ready(job_id)
    cfg = await request.json()
    if not isinstance(cfg, dict) or not isinstance(cfg.get("regions", []), list):
        raise HTTPException(400, "config must be an object")
    if _is_struct(d):  # keep the engine marker and the saved calibration unless the client sends them
        old = json.loads((d / "config.json").read_text())
        cfg["engine"] = "struct"
        cfg.setdefault("calibration", old.get("calibration") or {})
    (d / "config.json").write_text(json.dumps(cfg, indent=1))
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cluster")
async def recluster(job_id: str, request: Request):
    d = _ready(job_id)
    body = await request.json()
    gap = float(body.get("gap") or 0)
    geo = json.loads((d / "geometry.json").read_text())
    if _is_struct(d):  # a PDF is already split into sheets
        return {"gap": 0, "regions": jobs.pdf_regions(geo)}
    res = cluster(geo["entities"], gap if gap > 0 else None, layouts=geo.get("layouts"))
    return {"gap": res["gap"], "regions": res["regions"]}


def _engine():
    mod = importlib.import_module(ENGINE_MODULE)
    return mod.build_mb


def _is_struct(d: Path) -> bool:
    """PDF jobs use the structural engine (engine_struct); DWG/DXF jobs the architectural one."""
    return jobs.read_state(d).get("kind") == "pdf"


def _flag_summary(result) -> dict:
    counts = {"error": 0, "warning": 0, "missing": 0}
    for f in result.flags:
        if f.status == "open" and f.severity in counts:
            counts[f.severity] += 1
    return {"open": counts, "total": len(result.flags)}


def _run(d: Path, cfg: dict):
    (d / "config.json").write_text(json.dumps(cfg, indent=1))
    geo = json.loads((d / "geometry.json").read_text())
    if _is_struct(d):
        from engine_struct.build import build_struct_mb
        return build_struct_mb(geo, cfg)
    return _engine()(geo, cfg)


def _write_xlsx(d: Path, result, cfg: dict) -> None:
    project = (cfg.get("project") or {}).get("name", "")
    if _is_struct(d):
        from engine_struct.export import write_struct_xlsx
        write_struct_xlsx(result, d / "mb.xlsx", project=project)
    else:
        write_xlsx(result, d / "mb.xlsx", project=project)


def _result_json(d: Path, result) -> dict:
    if _is_struct(d):
        from engine_struct.export import result_json as struct_result_json
        data = struct_result_json(result)
        kind = "structural"
    else:
        data = result_json(result)
        kind = "architectural"
    data.pop("kind", None)
    return {"kind": kind, **data}  # "kind" first, so a cache can be checked by its first bytes


def _run_and_cache(d: Path, cfg: dict, xlsx: bool):
    result = _run(d, cfg)
    if xlsx:
        _write_xlsx(d, result, cfg)
    _cache_result(d, result)
    return result


def _cache_result(d: Path, result) -> None:
    """Keep the latest full result for GET /mb (rows with drawing refs)."""
    tmp = d / "mb_result.json.tmp"
    tmp.write_text(json.dumps(_result_json(d, result), separators=(",", ":"), default=str))
    tmp.replace(d / "mb_result.json")


@app.post("/api/jobs/{job_id}/check")
async def check(job_id: str, request: Request):
    """Run the engine (checks included) without writing the workbook; returns flags and the
    abstract, so resolving a flag shows its effect on the quantities."""
    d = _ready(job_id)
    t = time.time()
    cfg = await request.json()
    result = await run_in_threadpool(_run_and_cache, d, cfg, False)
    return {"seconds": round(time.time() - t, 2), "flags": [asdict(f) for f in result.flags],
            "summary": _flag_summary(result),
            "abstract": [{"item": a.item, "qty": a.qty, "unit": a.unit, "trade": a.trade} for a in result.abstract]}


@app.post("/api/jobs/{job_id}/build")
async def build(job_id: str, request: Request):
    d = _ready(job_id)
    cfg = await request.json()
    t = time.time()
    result = await run_in_threadpool(_run_and_cache, d, cfg, True)
    return JSONResponse({
        "seconds": round(time.time() - t, 2),
        "counts": {"mb_rows": len(result.mb), "rooms": len(result.rooms), "openings": len(result.openings),
                   "dimensions": len(result.dimensions), "flags": len(result.flags)},
        "abstract": [asdict(a) for a in result.abstract],
        "rooms": [asdict(r) for r in result.rooms[:300]],
        "openings": [asdict(o) for o in result.openings],
        "flags": [asdict(f) for f in result.flags],
        "summary": _flag_summary(result),
        "meta": result.meta,
    })


@app.get("/api/jobs/{job_id}/mb")
def get_mb(job_id: str):
    """Full MB result for the web UI: kind ("architectural" | "structural"), rows (with drawing
    refs), abstract, rooms, openings, dimensions, flags, meta; structural jobs add steel,
    schedules and calibration. Re-runs the engine if the saved config is newer than the cache."""
    d = _ready(job_id)
    return FileResponse(_fresh_cache(d), media_type="application/json")


def _fresh_cache(d: Path) -> Path:
    cache, cfgf = d / "mb_result.json", d / "config.json"
    if not cache.exists() or cache.stat().st_mtime < cfgf.stat().st_mtime:
        result = _run(d, json.loads(cfgf.read_text()))
        _cache_result(d, result)
    else:
        with cache.open("rb") as fh:
            head = fh.read(16)
        if not head.startswith(b'{"kind":'):  # cache written before "kind" existed: add it once
            data = json.loads(cache.read_text())
            data.pop("kind", None)
            tmp = d / "mb_result.json.tmp"
            kind = "structural" if _is_struct(d) else "architectural"
            tmp.write_text(json.dumps({"kind": kind, **data}, separators=(",", ":"), default=str))
            tmp.replace(cache)
    return cache


# ---------------------------------------------------------------- PDF jobs: sheets, calibration, renders

def _pdf_job(job_id: str) -> Path:
    d = _ready(job_id)
    if not _is_struct(d):
        raise HTTPException(404, "only PDF jobs have sheets")
    return d


def _sheets(d: Path) -> list[dict]:
    f = d / "sheets.json"
    if f.exists() and f.stat().st_mtime >= (d / "geometry.json").stat().st_mtime:
        return json.loads(f.read_text())
    geo = json.loads((d / "geometry.json").read_text())
    counts: dict[int, dict] = {}
    for e in geo["entities"]:
        c = counts.setdefault(e.get("sheet"), {"entities": 0, "texts": 0})
        c["entities"] += 1
        c["texts"] += e["t"] == "T"
    out = [{**s, "counts": counts.get(s["sheet"], {"entities": 0, "texts": 0})} for s in geo["sheets"]]
    f.write_text(json.dumps(out))
    return out


@app.get("/api/jobs/{job_id}/sheets")
def sheets(job_id: str):
    """PDF sheets: page size, offset in the shared drawing space (y up, pt), bbox, title block
    (drawing no, title, scale, NOT TO SCALE, floor scope), path/text/image counts."""
    return _sheets(_pdf_job(job_id))


@app.get("/api/jobs/{job_id}/calibration")
def get_calibration(job_id: str):
    """Per-sheet scale: pt/mm, ~1:N, samples (each with `at` and `handles`), spread %, method,
    plus the user's saved calibration from the config."""
    d = _pdf_job(job_id)
    data = json.loads(_fresh_cache(d).read_text())
    cfg = json.loads((d / "config.json").read_text())
    return {"calibration": data.get("calibration") or {}, "user": cfg.get("calibration") or {}}


def _num(v, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a number")
    if not math.isfinite(f):
        raise HTTPException(400, f"{name} must be finite")
    return f


def _point(v, name: str) -> list[float]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise HTTPException(400, f"{name} must be [x, y]")
    return [_num(v[0], name), _num(v[1], name)]


def _calib_spec(spec, sheet: dict) -> dict | None:
    """Validate one sheet's calibration: {p1, p2, mm} | {scale} | {pt_per_mm} | null (clear)."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise HTTPException(400, "calibration per sheet must be an object or null")
    if "p1" in spec or "p2" in spec:
        p1, p2, mm = _point(spec.get("p1"), "p1"), _point(spec.get("p2"), "p2"), _num(spec.get("mm"), "mm")
        if mm <= 0 or mm > 1e7:
            raise HTTPException(400, "mm must be a positive length")
        b = sheet["bbox"]
        pad = max(b[2] - b[0], b[3] - b[1]) * 0.05
        for p in (p1, p2):
            if not (b[0] - pad <= p[0] <= b[2] + pad and b[1] - pad <= p[1] <= b[3] + pad):
                raise HTTPException(400, f"point {p} is not on sheet {sheet['sheet']}")
        if math.dist(p1, p2) < 0.5:
            raise HTTPException(400, "the two points are too close together")
        return {"p1": p1, "p2": p2, "mm": mm}
    if "scale" in spec:
        n = _num(spec["scale"], "scale")
        if not 1 <= n <= 100000:
            raise HTTPException(400, "scale must be N in 1:N, between 1 and 100000")
        return {"scale": n}
    if "pt_per_mm" in spec:
        r = _num(spec["pt_per_mm"], "pt_per_mm")
        if not 1e-6 < r < 100:
            raise HTTPException(400, "pt_per_mm out of range")
        return {"pt_per_mm": r}
    raise HTTPException(400, "give {p1, p2, mm}, {scale} or {pt_per_mm}")


@app.put("/api/jobs/{job_id}/calibration")
async def put_calibration(job_id: str, request: Request):
    """Body {"<sheet>": {"p1": [x, y], "p2": [x, y], "mm": L} | {"scale": N} | {"pt_per_mm": r} | null}.
    Points are in the shared drawing space (as in geometry.json). Merged into config.calibration
    (null removes the sheet's override), then the MB is rebuilt."""
    d = _pdf_job(job_id)
    body = await request.json()
    if not isinstance(body, dict) or not body:
        raise HTTPException(400, "body must be {sheet: spec}")
    by_n = {str(s["sheet"]): s for s in _sheets(d)}
    cfg = json.loads((d / "config.json").read_text())
    cal = dict(cfg.get("calibration") or {})
    for key, spec in body.items():
        k = str(key)
        if k not in by_n:
            raise HTTPException(400, f"no sheet {k}")
        v = _calib_spec(spec, by_n[k])
        if v is None:
            cal.pop(k, None)
        else:
            cal[k] = v
    cfg["calibration"] = cal
    t = time.time()
    result = await run_in_threadpool(_run_and_cache, d, cfg, True)
    return {"seconds": round(time.time() - t, 2), "user": cal, "calibration": result.meta.get("calibration") or {},
            "summary": _flag_summary(result)}


_CLIP_RE = re.compile(r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$")
_LAYERS_RE = re.compile(r"^[^\x00-\x1f]{1,4000}$")


@app.get("/api/jobs/{job_id}/sheet/{n}.png")
def sheet_png(job_id: str, n: int, dpi: float = 72, clip: str = "", layers: str | None = None):
    """Raster of one PDF sheet (1-based n). `clip=x0,y0,x1,y1` is in page points (y down, origin at
    the page's top-left, as displayed); `layers=a,b` renders only those OCG layers. Whole-sheet
    renders without layers are cached on disk."""
    from app.pdf_backends import open_pdf

    d = _pdf_job(job_id)
    sh = {s["sheet"]: s for s in _sheets(d)}.get(n)
    if sh is None:
        raise HTTPException(404, "no such sheet")
    if not 4 <= dpi <= 600:
        raise HTTPException(400, "dpi must be between 4 and 600")
    W, H = sh["size"]
    box = None
    if clip:
        if not _CLIP_RE.match(clip):
            raise HTTPException(400, "clip must be x0,y0,x1,y1")
        x0, y0, x1, y1 = (float(v) for v in clip.split(","))
        x0, x1 = max(0.0, min(x0, x1)), min(W, max(x0, x1))
        y0, y1 = max(0.0, min(y0, y1)), min(H, max(y0, y1))
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise HTTPException(400, "clip is outside the sheet")
        box = (x0, y0, x1, y1)
    bw, bh = (box[2] - box[0], box[3] - box[1]) if box else (W, H)
    if bw * bh * (dpi / 72) ** 2 > MAX_RENDER_PX:
        raise HTTPException(400, f"render too large: lower dpi or clip (limit {MAX_RENDER_PX // 1_000_000} MP)")
    lay = None
    if layers is not None:
        if not _LAYERS_RE.match(layers):
            raise HTTPException(400, "bad layers")
        lay = [x for x in layers.split(",") if x]
    dpi = round(dpi, 1)
    cache = d / "render" / f"s{n}_{dpi:g}.png"
    headers = {"Cache-Control": "private, max-age=3600"}
    if box is None and lay is None and cache.exists():
        return FileResponse(cache, media_type="image/png", headers=headers)
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
    return Response(png, media_type="image/png", headers=headers)


@app.get("/api/jobs/{job_id}/entities")
def entities_by_handle(job_id: str, handles: str = ""):
    """Geometry records for the given DXF handles (comma separated), for highlighting refs.
    Returns {handle: [entity records]} (exploded block children share the parent's handle)."""
    d = _ready(job_id)
    want = {h.strip().upper() for h in handles.split(",") if h.strip()}
    if not want or len(want) > 5000:
        raise HTTPException(400, "pass 1..5000 handles")
    geo = json.loads((d / "geometry.json").read_text())
    out: dict[str, list] = {h: [] for h in want}
    for e in geo["entities"]:
        h = str(e.get("handle") or "").upper()
        if h in want:
            out[h].append(e)
    return out


@app.get("/api/jobs/{job_id}/mb.xlsx")
def download(job_id: str):
    d = _ready(job_id)
    f = d / "mb.xlsx"
    if not f.exists():
        raise HTTPException(404, "build the MB first")
    stem = "".join(c for c in Path(jobs.read_state(d)["filename"]).stem if c.isalnum() or c in "-_ ")[:60] or "drawing"
    return FileResponse(f, filename=f"MB - {stem}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


SAMPLES = ROOT / "samples"


@app.get("/api/sample/{name}")
def sample(name: str):
    """The public sample drawings in samples/ (made-up buildings), so the UI can offer a demo upload."""
    if not re.fullmatch(r"[\w-]+\.dxf", name) or not (SAMPLES / name).is_file():
        raise HTTPException(404, "no such sample")
    return FileResponse(SAMPLES / name, filename=name, media_type="application/dxf")


class _UI(StaticFiles):
    """Static files minus server-side sources (web/ holds dev_server.py and the mock builder)."""

    async def get_response(self, path, scope):
        if path.endswith((".py", ".pyc")) or "__pycache__" in path:
            raise StarletteHTTPException(404)
        return await super().get_response(path, scope)


app.mount("/", _UI(directory=WEB, html=True), name="web")
