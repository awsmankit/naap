"""In-browser runtime: the job API on Pyodide (WebAssembly), driven by web/worker/naap-worker.js.

The worker keeps jobs in Pyodide's in-memory file system under /jobs, in the same layout as the
server, and saves them to IndexedDB. Every route runs through app/service.py, the module the
FastAPI server uses, so the engine, checks and workbook are the same code. Differences:

- DWG is converted to DXF by LibreDWG compiled to WebAssembly in the worker, before process()
  runs; `jobs._convert` only checks that the DXF is there.
- Calls are answered as {"status", "body"} (JSON) or {"status", "file", "type", "filename"}
  (a file in the in-memory file system that the worker reads and hands to the page).
- Nothing here touches the network.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs

os.environ.setdefault("DWG_MB_DATA", "/jobs")

from . import jobs, service  # noqa: E402
from .service import ServiceError  # noqa: E402

PROJECT_FORMAT = "naap-project"
PROJECT_VERSION = 1
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_progress = None
_update_state = jobs.update_state
_convert_s: dict[str, float] = {}   # job id -> seconds LibreDWG (WebAssembly) took, timed by the worker


def _posting_update_state(d: Path, **kw) -> dict:
    if d.name in _convert_s and "convert_s" in (kw.get("timings") or {}):
        kw["timings"]["convert_s"] = _convert_s[d.name]
    st = _update_state(d, **kw)
    if _progress is not None:
        _progress(json.dumps(st))
    return st


def _converted_dxf(d: Path) -> Path:
    out = d / "input.dxf"
    if not out.exists() or out.stat().st_size == 0:
        raise jobs.JobError("LibreDWG (WebAssembly) did not produce a DXF from this DWG")
    return out


def init(progress) -> str:
    """Set up the browser runtime; progress(state_json) is called on every job state change
    (stage, message, error). Returns the meta the server's GET /api/meta returns."""
    global _progress, _update_state
    _progress = progress
    if jobs.update_state is not _posting_update_state:
        _update_state = jobs.update_state
        jobs.update_state = _posting_update_state
    jobs._convert = _converted_dxf
    service.GEOMETRY_CACHE = True
    jobs.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return json.dumps(service.meta())


# ---------------------------------------------------------------- upload + processing

def _envelope(fn, *args) -> str:
    """Call fn and answer as request() does: {"status", "body"}, errors included."""
    try:
        return json.dumps({"status": 200, "body": fn(*args)}, default=str)
    except ServiceError as exc:
        return json.dumps({"status": exc.status, "body": {"detail": exc.detail}})


def new_job(filename: str, size: int) -> str:
    """Validate the name and size, create the job folder; the worker then writes input.<ext>."""
    return _envelope(_new_job, filename, size)


def _new_job(filename: str, size: int) -> dict:
    name = Path(filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in jobs.ALLOWED_EXT:
        raise ServiceError(400, "Only .dwg, .dxf and .pdf files are accepted")
    max_mb = service.MAX_PDF_MB if ext == ".pdf" else service.MAX_UPLOAD_MB
    if size > max_mb * 1024 * 1024:
        raise ServiceError(413, f"File larger than {max_mb} MB")
    job_id, _ = jobs.new_job(name, ext)
    return {"id": job_id}


def accept_input(job_id: str) -> str:
    """Check the bytes the worker wrote really are a drawing of that type; queue the job."""
    return _envelope(_accept_input, job_id)


def _accept_input(job_id: str) -> dict:
    d = service.job_path(job_id)
    st = jobs.read_state(d)
    ext = "." + st["kind"]
    dest = d / f"input{ext}"
    size = dest.stat().st_size if dest.exists() else 0
    if size == 0 or not jobs.sniff(dest, ext):
        jobs.delete_job(job_id)
        what = "PDF (no %PDF- header)" if ext == ".pdf" else f"{ext[1:].upper()} drawing"
        raise ServiceError(400, f"File does not look like a valid {what}")
    return jobs.update_state(d, status="queued", message="Queued", size=size)


def set_state(job_id: str, fields_json: str) -> str:
    return json.dumps(jobs.update_state(service.job_path(job_id), **json.loads(fields_json)))


def process(job_id: str, convert_s: float | None = None) -> str:
    """Convert (already done for DWG) -> parse -> cluster, as on the server; then drop the
    drawing itself unless it is a PDF (sheet renders need it)."""
    d = service.job_path(job_id)
    service.forget(d)
    if convert_s is not None:
        _convert_s[job_id] = round(float(convert_s), 1)
    try:
        jobs._process(job_id)
    finally:
        _convert_s.pop(job_id, None)
    for name in ("input.dxf", "input.dwg"):
        (d / name).unlink(missing_ok=True)
    return json.dumps(jobs.read_state(d))


def delete(job_id: str) -> None:
    d = service.job_path(job_id)
    service.forget(d)
    jobs.delete_job(job_id)


# ---------------------------------------------------------------- projects (.naap.json)

def export_project(job_id: str) -> str:
    """One self-contained file: job state, review answers (config), the geometry and, once the book
    is built, the result (so the book opens on a phone without running the engine); a PDF job
    also carries the PDF (sheet images and calibration need it). Written to a temp file."""
    d = service.ready(job_id)
    st = jobs.read_state(d)
    cache, cfgf = d / "mb_result.json", d / "config.json"
    fresh = cache.exists() and cache.stat().st_mtime >= cfgf.stat().st_mtime
    out = d / "project.naap.json"
    with out.open("w") as fh:
        head = {"format": PROJECT_FORMAT, "version": PROJECT_VERSION, "saved": time.time(),
                "job": {k: st.get(k) for k in ("filename", "kind", "size", "timings", "stats", "created")}}
        fh.write(json.dumps(head)[:-1])
        fh.write(',"config":')
        fh.write((d / "config.json").read_text())
        fh.write(',"geometry":')
        fh.write((d / "geometry.json").read_text())
        if fresh:
            fh.write(',"result":')
            fh.write(cache.read_text())
        if st.get("kind") == "pdf":
            fh.write(',"input_pdf":"')
            fh.write(base64.b64encode((d / "input.pdf").read_bytes()).decode("ascii"))
            fh.write('"')
        fh.write("}")
    stem = re.sub(r"[^\w\- ]", "", Path(st["filename"]).stem)[:60] or "drawing"
    return json.dumps({"file": str(out), "filename": f"{stem}.naap.json"})


def import_project(path: str) -> str:
    """A .naap.json file (written to `path` by the worker) becomes a new ready job."""
    return _envelope(_import_project, path)


def _import_project(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceError(400, f"Not a Naap project file ({exc})")
    finally:
        Path(path).unlink(missing_ok=True)
    if not isinstance(data, dict) or data.get("format") != PROJECT_FORMAT:
        raise ServiceError(400, "Not a Naap project file (.naap.json)")
    if not isinstance(data.get("version"), int) or data["version"] > PROJECT_VERSION:
        raise ServiceError(400, "This project was saved by a newer version of Naap")
    job, cfg, geo = data.get("job") or {}, data.get("config"), data.get("geometry")
    kind = job.get("kind")
    if kind not in ("dwg", "dxf", "pdf") or not isinstance(cfg, dict) or not isinstance(geo, dict) \
            or not isinstance(geo.get("entities"), list):
        raise ServiceError(400, "The project file is incomplete (job, config or geometry missing)")
    if kind == "pdf" and not isinstance(data.get("input_pdf"), str):
        raise ServiceError(400, "The project file has no PDF in it")
    result = data.get("result")
    has_result = isinstance(result, dict) and isinstance(result.get("rows", result.get("mb")), list)
    job_id, d = jobs.new_job(str(job.get("filename") or "drawing"), "." + kind)
    try:
        (d / "geometry.json").write_text(json.dumps(geo, separators=(",", ":")))
        if kind == "pdf":
            (d / "input.pdf").write_bytes(base64.b64decode(data["input_pdf"], validate=True))
        (d / "config.json").write_text(json.dumps(cfg, indent=1))
        if has_result:   # written after the config, so GET /mb serves it without running the engine
            (d / "mb_result.json").write_text(json.dumps(result, separators=(",", ":"), default=str))
    except Exception as exc:
        jobs.delete_job(job_id)
        raise ServiceError(400, f"The project file could not be read ({exc})")
    jobs.update_state(d, status="ready", message="Opened from a project file", size=job.get("size"),
                      timings=job.get("timings") or {}, stats=job.get("stats") or {})
    return {"id": job_id, "has_result": has_result}


# ---------------------------------------------------------------- the routes main.py serves

_JOB = r"/api/jobs/(?P<id>[0-9a-f]{32})"
_ROUTES: list[tuple[str, re.Pattern, str]] = [(m, re.compile(f"^{p}$"), name) for m, p, name in [
    ("GET", r"/api/meta", "meta"),
    ("GET", r"/api/jobs", "list"),
    ("GET", _JOB, "status"),
    ("GET", _JOB + r"/geometry", "geometry"),
    ("GET", _JOB + r"/config", "get_config"),
    ("PUT", _JOB + r"/config", "put_config"),
    ("POST", _JOB + r"/cluster", "cluster"),
    ("POST", _JOB + r"/check", "check"),
    ("POST", _JOB + r"/build", "build"),
    ("GET", _JOB + r"/mb", "mb"),
    ("GET", _JOB + r"/sheets", "sheets"),
    ("GET", _JOB + r"/calibration", "get_calibration"),
    ("PUT", _JOB + r"/calibration", "put_calibration"),
    ("GET", _JOB + r"/sheet/(?P<n>\d+)\.png", "sheet_png"),
    ("GET", _JOB + r"/entities", "entities"),
    ("GET", _JOB + r"/mb\.xlsx", "xlsx"),
    ("GET", _JOB + r"/project", "project"),
    ("DELETE", _JOB, "delete"),
]]


def _file(path: Path, ctype: str, filename: str | None = None) -> dict:
    return {"status": 200, "file": str(path), "type": ctype, "filename": filename}


def _dispatch(name: str, m: dict, q: dict, body):
    jid = m.get("id")
    if name == "meta":
        return service.meta()
    if name == "list":
        return service.list_jobs()
    if name == "status":
        return service.status(jid)
    if name == "delete":
        delete(jid)
        return {"ok": True}
    if name == "project":
        return {"status": 200, **json.loads(export_project(jid)), "type": "application/vnd.naap.project+json"}
    if name in ("sheets", "get_calibration", "put_calibration", "sheet_png"):
        d = service.pdf_job(jid)
    else:
        d = service.ready(jid)
    if name == "geometry":
        return _file(d / "geometry.json", "application/json")
    if name == "get_config":
        return service.read_config(d)
    if name == "put_config":
        return service.put_config(d, body)
    if name == "cluster":
        return service.recluster(d, body)
    if name == "check":
        return service.check(d, body)
    if name == "build":
        return service.build(d, body)
    if name == "mb":
        return _file(service.fresh_cache(d), "application/json")
    if name == "sheets":
        return service.sheets(d)
    if name == "get_calibration":
        return service.calibration(d)
    if name == "put_calibration":
        return service.put_calibration(d, body)
    if name == "entities":
        return service.entities(d, q.get("handles", ""))
    if name == "xlsx":
        f, fname = service.workbook(d)
        return _file(f, XLSX, fname)
    if name == "sheet_png":
        try:
            dpi = float(q.get("dpi", 72))
        except ValueError:
            raise ServiceError(422, "dpi must be a number")
        png = service.sheet_png(d, int(m["n"]), dpi, q.get("clip", ""), q.get("layers"))
        if not isinstance(png, Path):
            tmp = d / "render" / "last.png"
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(png)
            png = tmp
        return _file(png, "image/png")
    raise ServiceError(404, "Not Found")


def request(method: str, url: str, body_json: str | None = None) -> str:
    """One API call, as the server would answer it. Returns a JSON envelope (see the module doc)."""
    path, _, query = url.partition("?")
    q = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
    try:
        body = json.loads(body_json) if body_json else None
    except json.JSONDecodeError:
        return json.dumps({"status": 400, "body": {"detail": "body is not JSON"}})
    for m, rx, name in _ROUTES:
        hit = rx.match(path)
        if hit and m == method.upper():
            try:
                out = _dispatch(name, hit.groupdict(), q, body)
            except ServiceError as exc:
                return json.dumps({"status": exc.status, "body": {"detail": exc.detail}})
            except Exception as exc:  # report it like a server 500, with the traceback in the console
                traceback.print_exc()
                return json.dumps({"status": 500, "body": {"detail": f"{type(exc).__name__}: {exc}"[:800]}})
            if isinstance(out, dict) and "file" in out and "status" in out:
                return json.dumps(out)
            return json.dumps({"status": 200, "body": out}, default=str)
    if any(rx.match(path) for _, rx, _ in _ROUTES):
        return json.dumps({"status": 405, "body": {"detail": "Method Not Allowed"}})
    return json.dumps({"status": 404, "body": {"detail": "Not Found"}})
