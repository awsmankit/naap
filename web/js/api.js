// API client + data contract normalisation.
//
// Two transports, one API. "server" mode calls the FastAPI app with fetch (as always); "local"
// mode runs the same Python engine in a Web Worker on Pyodide (web/worker/naap-worker.js), so the
// drawing never leaves the browser. The mode comes from config.js (set at build time; the GitHub
// Pages site is "local") and `?mode=local|server` overrides it. Everything outside this file
// calls the same functions in either mode.
//
// The UI codes against GET /api/jobs/{id}/mb (see web/README.md). `?mock=1` uses
// web/mock/mb.json, and real mode falls back to the POST /build response (no row-level MB).

import { CONFIG } from "../config.js";
import { guessTrade } from "./theme.js";

const params = new URLSearchParams(location.search);
export const MOCK = params.get("mock") === "1";
const asked = params.get("mode");
export const MODE = asked === "local" || asked === "server" ? asked : CONFIG.mode === "local" ? "local" : "server";
export const LOCAL = MODE === "local";
/** Save / open a whole job as a .naap.json file (the in-browser version keeps jobs in this browser). */
export const CAN_PROJECTS = LOCAL;

export class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

/** What to tell the user when the API cannot be reached at all. */
export const UNREACHABLE = LOCAL
  ? "The in-browser engine could not start in this browser. Use a current Chrome, Edge, Firefox or Safari, or run the server version (see the README)."
  : "The app server is not reachable on port 8777, so uploads are off. Start it with: uv run python -m app";

// ------------------------------------------------------------------ local mode: the worker
class LocalEngine {
  constructor() {
    this.w = null; this.seq = 0; this.pending = new Map();
    this.state = new Map();      // job id -> latest state pushed by the worker
    this.busy = new Set();       // jobs being converted/read (status answered from this.state)
    this.onProgress = null;      // (p) => void, during an upload
  }

  start() {
    if (this.w) return this.w;
    const w = new Worker(new URL("../worker/naap-worker.js", import.meta.url), { type: "module" });
    w.onmessage = (e) => this.message(e.data);
    w.onerror = (e) => { e.preventDefault?.(); this.crash(e.message || "the worker failed to load"); };
    this.w = w;
    return w;
  }

  crash(why) {
    this.w?.terminate();
    this.w = null;
    for (const [, p] of this.pending) p.reject(new ApiError(`The in-browser engine stopped (${why}). Your jobs are kept in this browser; try again, or use the server version for very large drawings.`, 500));
    this.pending.clear();
    for (const id of this.busy) {
      const st = this.state.get(id);
      if (st) this.state.set(id, { ...st, status: "error", error: `The in-browser engine stopped while reading this drawing (${why}). It may be too large for the browser: try the server version.` });
    }
    this.busy.clear();
  }

  message(m) {
    if (m.type === "reply") {
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      p.resolve(m);
    } else if (m.type === "state") {
      this.state.set(m.state.id, m.state);
      if (m.state.status === "ready" || m.state.status === "error") this.busy.delete(m.state.id);
    } else if (m.type === "progress") {
      this.onProgress?.({ msg: m.msg, pct: m.total ? Math.round((m.loaded / m.total) * 100) : null });
    } else if (m.type === "fatal") {
      this.crash(m.error);
    }
  }

  send(msg, transfer = []) {
    const w = this.start();
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      w.postMessage({ ...msg, id }, transfer);
    });
  }

  /** A call answered like fetch + api() would: parsed JSON, or a Blob for files. */
  async request(method, path, body) {
    const id = path.match(/^\/api\/jobs\/([0-9a-f]{32})$/)?.[1];
    if (method === "GET" && id && this.busy.has(id)) {
      const st = this.state.get(id);
      return { ...st, elapsed_s: Math.round((Date.now() / 1000 - st.created) * 10) / 10 };
    }
    return unpack(await this.send({ type: "call", method, url: path, body }));
  }
}

function unpack(r) {
  if (r.status >= 400) throw new ApiError(r.body?.detail || `Error ${r.status}`, r.status);
  if (!r.file) return r.body;
  if (r.file.type === "application/json") return JSON.parse(new TextDecoder().decode(r.file.buf));
  const blob = new Blob([r.file.buf], { type: r.file.type });
  blob.filename = r.file.filename;
  return blob;
}

const engine = LOCAL ? new LocalEngine() : null;

/** Start loading the in-browser engine early (e.g. when a file is dragged in). */
export function warmup() {
  engine?.start().postMessage({ type: "warmup" });
}

// ------------------------------------------------------------------ the API
export async function api(path, opts = {}) {
  if (engine && (path.startsWith("/api/") || path === "/openapi.json")) {
    if (path === "/openapi.json") return { paths: { "/api/jobs/{job_id}/mb": {} } };
    return engine.request((opts.method || "GET").toUpperCase(), path, opts.body ? JSON.parse(opts.body) : null);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(`Cannot reach the app server (${e.message}). Is it running on port 8777?`, 0);
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch { /* not json */ }
    throw new ApiError(typeof msg === "string" ? msg : JSON.stringify(msg), res.status);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res;
}

const json = (body) => ({ method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
export const putConfig = (id, cfg) => api(`/api/jobs/${id}/config`, { ...json(cfg), method: "PUT" });
export const runCheck = (id, cfg) => api(`/api/jobs/${id}/check`, json(cfg));
export const runBuild = (id, cfg) => api(`/api/jobs/${id}/build`, json(cfg));
export const putCalibration = (id, body) => api(`/api/jobs/${id}/calibration`, { ...json(body), method: "PUT" });

/**
 * Send a drawing to the engine. onProgress({msg, pct}) reports the upload (server) or loading
 * the in-browser engine (local). Resolves {id}; then poll GET /api/jobs/{id} for the stages.
 */
export function uploadJob(file, onProgress = () => {}) {
  if (engine) {
    return (async () => {
      engine.onProgress = onProgress;
      try {
        const buf = await file.arrayBuffer();
        const r = unpack(await engine.send({ type: "upload", name: file.name, size: file.size, buf }, [buf]));
        engine.state.set(r.id, r.state);
        if (r.state.status !== "ready" && r.state.status !== "error") engine.busy.add(r.id);
        return { id: r.id };
      } finally { engine.onProgress = null; }
    })();
  }
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append("file", file);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress({ msg: `${Math.round((e.loaded / e.total) * 100)}% of ${(e.total / 1e6).toFixed(1)} MB`, pct: Math.round((e.loaded / e.total) * 100) }); };
    xhr.onerror = () => reject(new ApiError("The upload failed: the app server did not answer. Check it is running on port 8777.", 0));
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 400) reject(new ApiError(body.detail || `Upload failed (${xhr.status}).`, xhr.status));
      else resolve(body);
    };
    xhr.send(fd);
  });
}

/** The public sample drawing (samples/sample-house.dxf) as a File. */
export async function sampleFile(name = "sample-house.dxf") {
  let r;
  try { r = await fetch(engine ? `samples/${name}` : `/api/sample/${name}`); } catch (e) { throw new ApiError(e.message, 0); }
  if (!r.ok) throw new ApiError(`HTTP ${r.status}`, r.status);
  return new File([await r.blob()], name, { type: "application/dxf" });
}

// sheet images: a URL (server) or a promise of an object URL (local; kept until the job changes)
const rasterCache = new Map();
let rasterJob = null;
export function sheetImage(id, n, dpi, clip) {
  const path = `/api/jobs/${id}/sheet/${n}.png?dpi=${dpi}${clip ? `&clip=${clip}` : ""}`;
  if (!engine) return path;
  if (rasterJob !== id) { for (const p of rasterCache.values()) p.then(URL.revokeObjectURL, () => {}); rasterCache.clear(); rasterJob = id; }
  if (!rasterCache.has(path)) {
    const p = api(path).then((blob) => URL.createObjectURL(blob));
    p.catch(() => rasterCache.delete(path));
    rasterCache.set(path, p);
    if (rasterCache.size > 300) { const [k, old] = rasterCache.entries().next().value; rasterCache.delete(k); old.then(URL.revokeObjectURL, () => {}); }
  }
  return rasterCache.get(path);
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: filename || "" });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** href for the Export button (the click handler calls downloadXlsx). */
export const xlsxHref = (id) => (engine ? "#export" : `/api/jobs/${id}/mb.xlsx`);

/** Download the job's workbook (build it first if needed: see main.js). */
export async function downloadXlsx(id) {
  if (!engine) {
    const link = Object.assign(document.createElement("a"), { href: `/api/jobs/${id}/mb.xlsx?t=${Date.now()}`, download: "" });
    document.body.append(link); link.click(); link.remove();
    return;
  }
  const blob = await api(`/api/jobs/${id}/mb.xlsx`);
  saveBlob(blob, blob.filename || "MB.xlsx");
}

/** Save the job (drawing geometry + review answers; a PDF job also carries its PDF) as .naap.json. */
export async function saveProject(id) {
  if (!CAN_PROJECTS) throw new ApiError("Projects are saved by the in-browser version only.", 400);
  const blob = await api(`/api/jobs/${id}/project`);
  saveBlob(blob, blob.filename || "drawing.naap.json");
}

/** Open a saved .naap.json as a new job. Resolves {id}. */
export async function openProject(file, onProgress = () => {}) {
  if (!CAN_PROJECTS) throw new ApiError("Projects are opened by the in-browser version only.", 400);
  engine.onProgress = onProgress;
  try {
    const buf = await file.arrayBuffer();
    return unpack(await engine.send({ type: "import", buf }, [buf]));
  } finally { engine.onProgress = null; }
}

/**
 * Load the measurement book for a job.
 * Returns {data, source: "api"|"mock"|"build", note}
 */
let hasMbRoute = null;
/** Ask FastAPI's OpenAPI schema whether GET /api/jobs/{id}/mb exists (avoids 404 noise). */
async function mbRouteExists() {
  if (hasMbRoute !== null) return hasMbRoute;
  try {
    const spec = await api("/openapi.json");
    hasMbRoute = Object.keys(spec.paths || {}).some((p) => /^\/api\/jobs\/\{[^}]+\}\/mb$/.test(p));
  } catch { hasMbRoute = true; /* no schema: just try it */ }
  return hasMbRoute;
}

export async function loadMB(jobId, cfg, { rebuild = false } = {}) {
  if (MOCK || !jobId) {
    const data = await api("mock/mb.json");
    return { data: normalise(data), source: "mock" };
  }
  if (!(await mbRouteExists())) return { data: normalise(fromBuild(await runBuild(jobId, cfg))), source: "build", built: true };
  if (!rebuild) {
    try {
      return { data: normalise(await api(`/api/jobs/${jobId}/mb`)), source: "api" };
    } catch (e) {
      if (e.status !== 404 && e.status !== 409) throw e;
    }
  }
  const built = await runBuild(jobId, cfg);
  try {
    return { data: normalise(await api(`/api/jobs/${jobId}/mb`)), source: "api", built };
  } catch (e) {
    if (e.status !== 404) throw e;
    return { data: normalise(fromBuild(built)), source: "build", built };
  }
}

/** Adapt the legacy POST /build response to the MB contract (no rows). */
function fromBuild(b) {
  return {
    rows: [],
    abstract: (b.abstract || []).map((a) => ({ ...a, row_ids: [] })),
    rooms: b.rooms || [],
    openings: b.openings || [],
    dimensions: [],
    flags: b.flags || [],
    meta: { ...(b.meta || {}), counts: b.counts },
  };
}

const num = (v) => (v === null || v === undefined || v === "" ? null : Number(v));

function bboxAround(x, y, w = 2400, h = 2400) {
  return [x - w / 2, y - h / 2, x + w / 2, y + h / 2];
}

function refsFromLocation(loc, kind, label) {
  if (!loc) return [];
  const sheet = loc.sheet ?? null;
  if (loc.bbox) return [{ bbox: loc.bbox, handles: [], kind, label, sheet }];
  if (loc.x != null) return [{ bbox: bboxAround(loc.x, loc.y), handles: [], kind, label, sheet }];
  return [];
}

const KIND_ALIAS = { dim: "dimension", dims: "dimension" };

function normRefs(refs, kind, label) {
  if (!Array.isArray(refs)) return [];
  return refs.filter((r) => r && typeof r === "object" && (r.bbox || (r.handles && r.handles.length)))
    .map((r) => ({ bbox: r.bbox || null, handles: r.handles || [], kind: KIND_ALIAS[r.kind] || r.kind || kind, label: r.label || label, sheet: r.sheet ?? null }));
}

/** "B. MASONRY (superstructure…)" -> "Masonry" (a palette trade) or a tidy title. */
function tradeName(t) {
  if (!t) return "";
  const bare = String(t).replace(/^[A-Z]\.\s*/, "").replace(/\s*\(.*$/, "").trim();
  const g = guessTrade(bare);
  if (g !== "General") return g;
  return bare.charAt(0) + bare.slice(1).toLowerCase();
}

/** Accept both the contract shape and the engine's dataclass shapes. */
export function normalise(d) {
  const flags = (d.flags || []).map((f) => {
    const strRefs = Array.isArray(f.refs) && f.refs.some((r) => typeof r === "string");
    let refs = strRefs ? [] : normRefs(f.refs, "flag", f.title);
    if (!refs.length) refs = refsFromLocation(f.location, "flag", f.title);
    return { ...f, status: f.status || "open", refs, affects: f.affects || (strRefs ? f.refs : []) };
  });

  const rows = (d.rows || d.mb || []).map((r, i) => {
    const trade = tradeName(r.trade) || guessTrade(r.item);
    const rem = String(r.remarks || "");
    const status = r.status || (/unresolved/i.test(rem) ? "unresolved" : /assum|inferred/i.test(rem) ? "assumed" : "ok");
    const flagIds = r.flag_ids || [...rem.matchAll(/([a-z]+(?:-[a-z]+)*-[0-9a-f]{8})/g)].map((m) => m[1]);
    // "scaled from drawing – verify": a length taken off a NOT-TO-SCALE plan via the sheet calibration
    const scaled = SCALED_RE.test(rem);
    return {
      ...r, id: String(r.id ?? `r${i + 1}`), sno: r.sno ?? String(i + 1), trade,
      nos: num(r.nos), l: num(r.l), b: num(r.b), h: num(r.h), qty: num(r.qty) ?? 0,
      status, scaled, chip: status === "unresolved" ? "unresolved" : scaled ? "scaled" : status,
      flag_ids: flagIds, refs: normRefs(r.refs, "region", r.location || r.item),
    };
  });

  // Item numbers: the engine sends the item's S.No on every line; number the
  // lines within an item as 5.1, 5.2 ... and keep lines of an item together.
  const itemNo = new Map();
  rows.forEach((r, i) => {
    r._i = i;
    if (r.item_sno == null) {
      const k = `${r.trade}|${r.item}`;
      if (Number.isFinite(Number(r.sno)) && !String(r.sno).includes(".")) r.item_sno = Number(r.sno);
      else { if (!itemNo.has(k)) itemNo.set(k, itemNo.size + 1); r.item_sno = itemNo.get(k); }
      r._renumber = true;
    }
  });
  rows.sort((a, b) => a.item_sno - b.item_sno || a._i - b._i);
  const sub = new Map();
  for (const r of rows) {
    if (!r._renumber) continue;
    const n = (sub.get(r.item_sno) || 0) + 1;
    sub.set(r.item_sno, n);
    r.sno = `${r.item_sno}.${n}`;
  }
  const byItem = new Map();
  for (const r of rows) {
    const k = r.item;
    if (!byItem.has(k)) byItem.set(k, []);
    byItem.get(k).push(r.id);
  }

  const abstract = (d.abstract || []).map((a, i) => ({
    ...a, sno: a.sno ?? i + 1, qty: num(a.qty) ?? 0, note: a.note ?? a.remarks ?? "",
    trade: tradeName(a.trade) || rows.find((r) => r.item === a.item)?.trade || guessTrade(a.item),
    row_ids: (a.row_ids && a.row_ids.length ? a.row_ids : byItem.get(a.item) || []).map(String),
  }));

  const rooms = (d.rooms || []).map((r) => {
    if ("measured_l" in r || "label_size" in r) return { ...r, refs: normRefs(r.refs, "room", r.name) };
    const w = num(r.width_mm), l = num(r.length_mm);
    return {
      name: r.name, level: r.location, label_size: w && l ? `${w}X${l}` : "", measured_l: null, measured_b: null,
      area: num(r.area_m2), check: "", remarks: r.source_text ? `label “${String(r.source_text).replace(/\n/g, " ")}”` : "",
      refs: r.x != null ? [{ bbox: bboxAround(r.x, r.y, w || 3000, l || 3000), handles: [], kind: "room", label: r.name }] : [],
    };
  });

  const openings = (d.openings || []).map((o) => {
    if ("type" in o) return { ...o, refs: normRefs(o.refs, "opening", o.type) };
    const w = num(o.width_mm), h = num(o.height_mm), n = num(o.count) || 0;
    return {
      type: o.tag, kind: o.kind, w, h, nos: n, area: w && h ? (n * w * h) / 1e6 : null,
      location: Object.entries(o.locations || {}).map(([k, v]) => `${k} (${v})`).join(", "), description: o.remarks || "",
      refs: [],
    };
  });

  const dimensions = (d.dimensions || []).map((m) => {
    if ("value" in m) return { ...m, refs: normRefs(m.refs, "dimension", `${m.value}`) };
    return {
      value: num(m.measurement), text: m.text, layer: m.layer, region: m.location,
      refs: m.x != null ? [{ bbox: bboxAround(m.x, m.y, 1200, 1200), handles: [], kind: "dimension", label: `${m.measurement}` }] : [],
    };
  });

  const kind = d.kind === "structural" || d.meta?.source === "pdf" ? "structural" : "architectural";
  if (kind === "structural") flags.sort((a, b) => flagRank(a) - flagRank(b));
  return {
    kind, rows, abstract, rooms, openings, dimensions, flags, notes: d.notes || [], meta: d.meta || {},
    assumptions: d.assumptions || [],
    steel: d.steel || d.meta?.steel || null,
    schedules: d.schedules || d.meta?.schedules || null,
    calibration: d.calibration || d.meta?.calibration || null,
  };
}

const SCALED_RE = /scaled from (the )?drawing\s*[–—-]\s*verify/i;

// INTEGRATION.md: answer these first (they gate whole groups of quantities)
const FLAG_FIRST = [/^struct:foundation:pile-vs-raft/, /^struct:columns:height:/, /^struct:calibration:/, /^struct:beams:untagged:/];
function flagRank(f) {
  const k = FLAG_FIRST.findIndex((re) => re.test(f.id));
  return k < 0 ? FLAG_FIRST.length : k;
}
