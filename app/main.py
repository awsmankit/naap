"""FastAPI app: upload -> async job -> review config -> build MB -> download xlsx.

The routes are thin: the work is in app/service.py, which the in-browser worker (app/browser.py)
shares."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import jobs, service
from .service import MAX_PDF_MB, MAX_UPLOAD_MB, ServiceError

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"         # current UI, served at /

app = FastAPI(title="Naap: drawing to Measurement Book", docs_url="/api/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.get("/api/meta")
def meta():
    return service.meta()


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
    return service.list_jobs()


@app.get("/api/jobs/{job_id}")
def status(job_id: str):
    return service.status(job_id)


@app.get("/api/jobs/{job_id}/geometry")
def geometry(job_id: str):
    return FileResponse(service.ready(job_id) / "geometry.json", media_type="application/json")


@app.get("/api/jobs/{job_id}/config")
def get_config(job_id: str):
    return service.read_config(service.ready(job_id))


@app.put("/api/jobs/{job_id}/config")
async def put_config(job_id: str, request: Request):
    d = service.ready(job_id)
    return service.put_config(d, await request.json())


@app.post("/api/jobs/{job_id}/cluster")
async def recluster(job_id: str, request: Request):
    d = service.ready(job_id)
    return service.recluster(d, await request.json())


@app.post("/api/jobs/{job_id}/check")
async def check(job_id: str, request: Request):
    """Run the engine (checks included) without writing the workbook; returns flags and the
    abstract, so resolving a flag shows its effect on the quantities."""
    d = service.ready(job_id)
    cfg = await request.json()
    return await run_in_threadpool(service.check, d, cfg)


@app.post("/api/jobs/{job_id}/build")
async def build(job_id: str, request: Request):
    d = service.ready(job_id)
    cfg = await request.json()
    return JSONResponse(await run_in_threadpool(service.build, d, cfg))


@app.get("/api/jobs/{job_id}/mb")
def get_mb(job_id: str):
    """Full MB result for the web UI: kind ("architectural" | "structural"), rows (with drawing
    refs), abstract, rooms, openings, dimensions, flags, meta; structural jobs add steel,
    schedules and calibration. Re-runs the engine if the saved config is newer than the cache."""
    return FileResponse(service.fresh_cache(service.ready(job_id)), media_type="application/json")


# ---------------------------------------------------------------- PDF jobs: sheets, calibration, renders

@app.get("/api/jobs/{job_id}/sheets")
def sheets(job_id: str):
    """PDF sheets: page size, offset in the shared drawing space (y up, pt), bbox, title block
    (drawing no, title, scale, NOT TO SCALE, floor scope), path/text/image counts."""
    return service.sheets(service.pdf_job(job_id))


@app.get("/api/jobs/{job_id}/calibration")
def get_calibration(job_id: str):
    """Per-sheet scale: pt/mm, ~1:N, samples (each with `at` and `handles`), spread %, method,
    plus the user's saved calibration from the config."""
    return service.calibration(service.pdf_job(job_id))


@app.put("/api/jobs/{job_id}/calibration")
async def put_calibration(job_id: str, request: Request):
    """Body {"<sheet>": {"p1": [x, y], "p2": [x, y], "mm": L} | {"scale": N} | {"pt_per_mm": r} | null}.
    Points are in the shared drawing space (as in geometry.json). Merged into config.calibration
    (null removes the sheet's override), then the MB is rebuilt."""
    d = service.pdf_job(job_id)
    body = await request.json()
    return await run_in_threadpool(service.put_calibration, d, body)


@app.get("/api/jobs/{job_id}/sheet/{n}.png")
def sheet_png(job_id: str, n: int, dpi: float = 72, clip: str = "", layers: str | None = None):
    """Raster of one PDF sheet (1-based n). `clip=x0,y0,x1,y1` is in page points (y down, origin at
    the page's top-left, as displayed); `layers=a,b` renders only those OCG layers. Whole-sheet
    renders without layers are cached on disk."""
    png = service.sheet_png(service.pdf_job(job_id), n, dpi, clip, layers)
    headers = {"Cache-Control": "private, max-age=3600"}
    if isinstance(png, Path):
        return FileResponse(png, media_type="image/png", headers=headers)
    return Response(png, media_type="image/png", headers=headers)


@app.get("/api/jobs/{job_id}/entities")
def entities_by_handle(job_id: str, handles: str = ""):
    """Geometry records for the given DXF handles (comma separated), for highlighting refs.
    Returns {handle: [entity records]} (exploded block children share the parent's handle)."""
    return service.entities(service.ready(job_id), handles)


@app.get("/api/jobs/{job_id}/mb.xlsx")
def download(job_id: str):
    f, name = service.workbook(service.ready(job_id))
    return FileResponse(f, filename=name,
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
