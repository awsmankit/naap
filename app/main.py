"""FastAPI app: upload -> async job -> review config -> build MB -> download xlsx.

The routes are thin: the work is in app/service.py, which the in-browser worker (app/browser.py)
shares."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import convert_api, jobs, service
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
async def upload(request: Request, file: list[UploadFile]):
    """One drawing, or several PDFs (repeat the `file` field) as one drawing set."""
    names = [Path(f.filename or "").name for f in file]
    exts = [Path(n).suffix.lower() for n in names]
    if not file or any(e not in jobs.ALLOWED_EXT for e in exts):
        raise HTTPException(400, "Only .dwg, .dxf and .pdf files are accepted")
    if len(file) > 1 and set(exts) != {".pdf"}:
        raise HTTPException(400, "A drawing set can only be several PDFs: upload a DWG or DXF on its own, not mixed with other files")
    if len(file) > service.MAX_SET_FILES:
        raise HTTPException(400, f"A set can have at most {service.MAX_SET_FILES} files")
    ext = exts[0]
    max_mb = MAX_PDF_MB if ext == ".pdf" else MAX_UPLOAD_MB
    limit = max_mb * 1024 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > limit + 1024 * 1024 * len(file):
        raise HTTPException(413, f"Files larger than {max_mb} MB in total")

    job_id, d = jobs.new_job(names[0], ext, names)
    size = 0
    for i, up in enumerate(file):
        dest = d / (f"input{ext}" if ext != ".pdf" else jobs.pdf_input_name(i, len(file)))  # never use the client-supplied name on disk
        fsize = 0
        with dest.open("wb") as fh:
            while chunk := await up.read(1024 * 1024):
                fsize += len(chunk)
                size += len(chunk)
                if size > limit:
                    fh.close()
                    jobs.delete_job(job_id)
                    raise HTTPException(413, f"Files larger than {max_mb} MB in total")
                fh.write(chunk)
        if fsize == 0 or not jobs.sniff(dest, ext):
            jobs.delete_job(job_id)
            what = "PDF (no %PDF- header)" if ext == ".pdf" else f"{ext[1:].upper()} drawing"
            raise HTTPException(400, f"{names[i]} does not look like a valid {what}" if len(file) > 1
                                else f"File does not look like a valid {what}")
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


@app.post("/api/jobs/{job_id}/review")
async def review(job_id: str, request: Request):
    """Review summary for a config: view purposes, floors per plan, heights with their sources and the
    open questions (meta.review of an engine run; the cached run is reused when the config is unchanged)."""
    d = service.ready(job_id)
    body = await request.body()
    cfg = await request.json() if body else None
    return await run_in_threadpool(service.review, d, cfg)


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


# ---------------------------------------------------------------- drawing conversion (PDF <-> DXF), one shot

async def _convert(kind: str, request: Request, file: UploadFile, out_ext: str):
    ext = convert_api.check_name(kind, file.filename)
    max_mb = convert_api.limit_mb(ext)
    limit = max_mb * 1024 * 1024
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > limit + 1024 * 1024:
        raise HTTPException(413, f"File larger than {max_mb} MB")
    form = await request.form()
    opts = {k: v for k, v in form.items() if isinstance(v, str)}
    cid, d = await run_in_threadpool(convert_api.new_dir)
    try:
        src, size = d / f"input{ext}", 0   # never use the client-supplied name on disk
        with src.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"File larger than {max_mb} MB")
                fh.write(chunk)
        convert_api.check_upload(src, ext)
        out = d / f"out.{out_ext}"
        res = await run_in_threadpool(convert_api.run, kind, src, out, opts)
        (d / "name.txt").write_text(convert_api.safe_stem(file.filename))
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    src.unlink(missing_ok=True)             # the output is kept for a short while, the upload is not
    url = f"/api/convert/{cid}/out.{out_ext}"
    name = convert_api.safe_stem(file.filename) + "." + out_ext
    return {**res, "id": cid, "filename": name, "download": url, f"{out_ext}_url": url,
            "note": convert_api.DWG_OUT_NOTE if out_ext == "dxf" else None}


@app.post("/api/convert/pdf-to-dxf")
async def convert_pdf_to_dxf(request: Request, file: UploadFile):
    """Vector PDF to DXF. Form: page, scale (auto | 1:N | none), calib ("x1,y1,x2,y2,mm"), tolerance, layers, sheets."""
    if str((await request.form()).get("format", "dxf")).lower() == "dwg":
        raise HTTPException(422, convert_api.DWG_OUT_ERROR)
    return await _convert("pdf2dxf", request, file, "dxf")


@app.post("/api/convert/to-pdf")
async def convert_to_pdf(request: Request, file: UploadFile):
    """DXF or DWG to PDF. Form: paper (A0..A4), orientation, mono, layouts, hide_layers."""
    return await _convert("dxf2pdf", request, file, "pdf")


@app.get("/api/convert/{cid}/out.{ext}")
def convert_download(cid: str, ext: str):
    f, name = convert_api.output(cid, ext)
    return FileResponse(f, filename=name, media_type="application/dxf" if ext == "dxf" else "application/pdf")


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
        res = await super().get_response(path, scope)
        # revalidate every time (ETag, so a 304 when unchanged): ES modules otherwise get heuristic
        # caching, and after an update the browser can mix an old review.js with a new main.js
        if not path.startswith(("fonts/", "icons/")):
            res.headers["Cache-Control"] = "no-cache"
        return res


app.mount("/", _UI(directory=WEB, html=True), name="web")
