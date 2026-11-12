"""Drawing conversion API shared by the server (app/main.py) and the in-browser worker (app/browser.py).

PDF -> DXF and DXF/DWG -> PDF, one shot each: validate the options, run `convert/`, return the fidelity
report plus a short plain-words summary. Uploaded files are untrusted: the caller checks type, size and
magic bytes (`check_upload`); nothing here executes anything from them. Output is DXF or PDF only; DWG output
is refused (LibreDWG's writer makes files AutoCAD rejects)."""
from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path

from convert.pdf_to_dxf import ConvertError, parse_calibration, pdf_to_dxf
from convert.to_pdf import PAPERS, dxf_to_pdf, to_dxf_path

from . import jobs
from .pdf_extract import ScannedPDFError
from .service import MAX_PDF_MB, MAX_UPLOAD_MB, ServiceError

DWG_OUT_NOTE = "Output is DXF; open it in AutoCAD and Save As DWG."
DWG_OUT_ERROR = "DWG output is not supported. Write DXF, open it in AutoCAD and use Save As DWG."
KEEP_S = 1800                      # converted files are removed this long after they were made
ID_RE = re.compile(r"^[0-9a-f]{32}$")
KINDS = {"pdf2dxf": {".pdf"}, "dxf2pdf": {".dxf", ".dwg"}}
SCALE_RE = re.compile(r"^\s*(auto|none|1\s*:\s*\d+(\.\d+)?|\d+(\.\d+)?)?\s*$", re.I)


def convert_root() -> Path:
    return jobs.DATA_DIR / "convert"


def limit_mb(ext: str) -> int:
    return MAX_PDF_MB if ext == ".pdf" else MAX_UPLOAD_MB


def check_name(kind: str, filename: str) -> str:
    """Extension rule for the conversion; returns the lower-case extension."""
    ext = Path(filename or "").suffix.lower()
    if ext not in KINDS[kind]:
        raise ServiceError(400, "PDF to DXF takes a .pdf file" if kind == "pdf2dxf" else "DXF/DWG to PDF takes a .dxf or .dwg file")
    return ext


def check_upload(path: Path, ext: str) -> None:
    if path.stat().st_size == 0 or not jobs.sniff(path, ext):
        what = "PDF (no %PDF- header)" if ext == ".pdf" else f"{ext[1:].upper()} drawing"
        raise ServiceError(400, f"File does not look like a valid {what}")


def _num(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ServiceError(422, f"{name} must be a number") from None
    if not lo <= x <= hi:
        raise ServiceError(422, f"{name} must be between {lo:g} and {hi:g}")
    return x


def _choice(v, name, allowed, default):
    v = (v or default)
    if not isinstance(v, str) or v.lower() not in allowed:
        raise ServiceError(422, f"{name} must be one of {', '.join(allowed)}")
    return v.lower()


def _bool(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def _names(v) -> list[str] | None:
    out = [s.strip() for s in str(v or "").split(",") if s.strip()]
    return out[:500] or None


def parse_pdf2dxf(o: dict) -> dict:
    page = o.get("page")
    page = None if page in (None, "", "all", "0") else int(_num(page, "page", 1, 100000, None))
    scale = o.get("scale") or None
    if scale is not None and not SCALE_RE.match(str(scale)):
        raise ServiceError(422, "scale must be auto, 1:100 or none")
    if scale and str(scale).lower() == "none":
        scale = None
    cal = None
    if o.get("calib"):
        try:
            cal = parse_calibration(str(o["calib"]))
        except ConvertError as e:
            raise ServiceError(422, str(e)) from None
        if not cal["mm"] > 0 or cal["p1"] == cal["p2"]:
            raise ServiceError(422, "calibration needs two different points and a length above 0")
    return {"page": page, "scale": scale, "calibration": cal,
            "tolerance": _num(o.get("tolerance"), "tolerance", 0.01, 10, 0.2),
            "layer_by": _choice(o.get("layers"), "layers", ("auto", "ocg", "color", "weight"), "auto"),
            "sheets": _choice(o.get("sheets"), "sheets", ("offset", "blocks"), "offset")}


def parse_dxf2pdf(o: dict) -> dict:
    orient = _choice(o.get("orientation"), "orientation", ("auto", "landscape", "portrait"), "auto")
    return {"paper": _choice(o.get("paper"), "paper", tuple(p.lower() for p in PAPERS), "a3").upper(),
            "landscape": None if orient == "auto" else orient == "landscape",
            "mono": _bool(o.get("mono")),
            "layouts": _choice(o.get("layouts"), "layouts", ("model", "paper", "all"), "model"),
            "hide_layers": _names(o.get("hide_layers"))}


def summarise(kind: str, rep: dict) -> list[str]:
    """Plain-words lines for the fidelity report (the full report stays available)."""
    out: list[str] = []
    if kind == "pdf2dxf":
        n = rep.get("entity_total", 0)
        parts = ", ".join(f"{c} {t.lower()}" for t, c in sorted((rep.get("entities") or {}).items(), key=lambda kv: -kv[1])[:5])
        out.append(f"Wrote {n:,} entities ({parts})." if n else "Wrote an empty drawing: the PDF had no vector content.")
        for k, c in (rep.get("dropped") or {}).items():
            out.append(f"Dropped {c:,} {k}.")
        out.append("Real-world size set from: " + (rep.get("scale_source") or "the scale you gave") if rep.get("scaled") else
                   "Sized as on paper (millimetres on the sheet), not real-world size: give a scale or calibrate to fix that.")
        if (rep.get("entities") or {}).get("TEXT"):
            out.append("Text is sized approximately (PDF font size, plain TEXT, no CAD text style).")
        if rep.get("truncated"):
            out.append("The drawing was cut off at the entity limit; convert fewer pages.")
    else:
        pages = rep.get("pages") or []
        out.append(f"Wrote {len(pages)} page{'s' if len(pages) != 1 else ''}: " + ", ".join(f"{p['layout']} on {p['paper']} at {p['scale']}" for p in pages[:6]) + ".")
        skipped = sum(p.get("images_skipped", 0) for p in pages)
        if skipped:
            out.append(f"Skipped {skipped:,} raster images.")
        out.extend(rep.get("deviations") or [])
    return out + list(rep.get("notes") or [])


def run(kind: str, src: Path, out: Path, opts: dict) -> dict:
    """Convert src to out (a .dxf or .pdf path); returns {"report", "summary"}. Raises ServiceError."""
    work = Path(tempfile.mkdtemp(prefix="naap-conv-"))
    try:
        if kind == "pdf2dxf":
            kw = parse_pdf2dxf(opts)
            if kw["calibration"]:
                kw["scale"] = None
            with jobs.PDF_LOCK:        # MuPDF is not thread-safe
                rep = pdf_to_dxf(src, out, **kw)
        else:
            kw = parse_dxf2pdf(opts)
            dxf = to_dxf_path(src, work)           # DWG goes through dwg2dxf, outside the PDF lock
            with jobs.PDF_LOCK:
                rep = dxf_to_pdf(dxf, out, **kw)
    except ScannedPDFError as e:
        raise ServiceError(422, str(e)) from None
    except ConvertError as e:
        raise ServiceError(422, str(e)) from None
    except (jobs.JobError, OSError) as e:
        raise ServiceError(422, f"Could not read this file: {e}"[:400]) from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    rep.pop("output", None)        # a path on this machine: not for the client
    return {"report": rep, "summary": summarise(kind, rep)}


def safe_stem(filename: str) -> str:
    stem = re.sub(r"[^\w.-]+", "_", Path(filename or "drawing").stem)[:80].strip("._")
    return stem or "drawing"


def sweep(now: float | None = None) -> None:
    """Delete conversions older than KEEP_S."""
    root, now = convert_root(), now or time.time()
    if not root.is_dir():
        return
    for d in root.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > KEEP_S:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def new_dir() -> tuple[str, Path]:
    import uuid
    sweep()
    cid = uuid.uuid4().hex
    d = convert_root() / cid
    d.mkdir(parents=True)
    return cid, d


def output(cid: str, ext: str) -> tuple[Path, str]:
    if not ID_RE.match(cid or "") or ext not in ("dxf", "pdf"):
        raise ServiceError(404, "no such file")
    d = convert_root() / cid
    f = d / f"out.{ext}"
    if not f.is_file():
        raise ServiceError(404, "This converted file is gone (files are kept for 30 minutes). Convert again.")
    name = (d / "name.txt").read_text() if (d / "name.txt").exists() else "drawing"
    return f, f"{name}.{ext}"
