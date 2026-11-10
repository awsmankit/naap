// Naap's in-browser engine: a module Web Worker that runs the Python engine on Pyodide.
//
// The page (js/api.js, "local" mode) sends it the same calls the server answers. Python does the
// work (app/browser.py -> app/service.py, the code the FastAPI server runs); this file loads the
// runtime, converts DWG with LibreDWG (WebAssembly, in a short-lived sub-worker), keeps jobs in
// IndexedDB and moves bytes in and out. Nothing is sent over the network: the only fetches are
// the site's own runtime files.
//
// Messages in:  {type: "call", id, method, url, body} | {type: "upload", id, name, size, buf}
//               {type: "import", id, buf} | {type: "warmup"}
// Messages out: {type: "reply", id, status, body} | {type: "reply", id, status, file: {buf, type, filename}}
//               {type: "state", state} (job stage changes) | {type: "progress", phase, msg, loaded, total}
//               {type: "fatal", error} (the runtime crashed; the page starts a new worker)

const ROOT = new URL("../", import.meta.url);
const JOBS = "/jobs";
const KEEP_JOBS = 20;                      // the upload page lists the 20 latest
// files saved per job (inputs other than the PDF are dropped after reading: they are large and
// the geometry is all the engine needs)
const SAVED = ["config.json", "geometry.json", "mb_result.json", "mb.xlsx", "input.pdf"];

let manifest = null;
let py = null, naap = null, booting = null, pdfReady = null;
let active = null;                         // the job whose files are in memory
const saved = new Map();                   // path -> mtime (ms) last written to IndexedDB

const post = (msg, transfer = []) => self.postMessage(msg, transfer);
const progress = (phase, msg, loaded = null, total = null) => post({ type: "progress", phase, msg, loaded, total });

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

// ------------------------------------------------------------------ IndexedDB
let dbp = null;
function db() {
  dbp ??= new Promise((resolve, reject) => {
    const r = indexedDB.open("naap", 1);
    r.onupgradeneeded = () => { r.result.createObjectStore("jobs"); r.result.createObjectStore("files"); };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
  return dbp;
}
async function tx(store, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(store, mode), s = t.objectStore(store);
    let out;
    const r = fn(s);
    if (r) r.onsuccess = () => { out = r.result; };
    t.oncomplete = () => resolve(out);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error || new Error("IndexedDB transaction aborted"));
  });
}
const idbGet = (store, key) => tx(store, "readonly", (s) => s.get(key));
const idbPut = (store, key, val) => tx(store, "readwrite", (s) => { s.put(val, key); });
const idbAll = (store) => tx(store, "readonly", (s) => s.getAll());
const idbDeleteJob = (id) => Promise.all([
  tx("jobs", "readwrite", (s) => { s.delete(id); }),
  tx("files", "readwrite", (s) => { s.delete(IDBKeyRange.bound(`${id}/`, `${id}/￿`)); }),
]);

// ------------------------------------------------------------------ runtime
async function loadManifest() {
  if (manifest) return manifest;
  const r = await fetch(new URL("worker/assets.json", ROOT));
  if (!r.ok) throw new HttpError(503, "The in-browser engine is not part of this build (worker/assets.json is missing). Build the site with scripts/build_site.py, or use the server version.");
  manifest = await r.json();
  return manifest;
}

/** fetch() that counts the bytes Pyodide downloads, for the progress bar. */
let counting = null;
const rawFetch = self.fetch.bind(self);
/** fetch with a few retries: a dropped connection should not leave the engine half-loaded. */
async function retryFetch(input, init) {
  for (let i = 0; ; i++) {
    try { return await rawFetch(input, init); } catch (e) {
      if (i >= 3 || (init?.method && init.method !== "GET")) throw e;
      await new Promise((r) => setTimeout(r, 400 * 2 ** i));
    }
  }
}
self.fetch = async (input, init) => {
  const res = await retryFetch(input, init);
  if (!counting || !res.ok || !res.body) return res;
  const c = counting, reader = res.body.getReader();
  const body = new ReadableStream({
    async pull(ctl) {
      const { done, value } = await reader.read();
      if (done) { ctl.close(); return; }
      c.loaded += value.byteLength;
      c.tick();
      ctl.enqueue(value);
    },
    cancel(why) { reader.cancel(why); },
  });
  return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
};

function counter(phase, label, total) {
  let last = 0;
  const c = {
    loaded: 0,
    tick() {
      const now = Date.now();
      if (now - last < 120) return;
      last = now;
      progress(phase, `${label}: ${(Math.min(c.loaded, total) / 1e6).toFixed(1)} of ${(total / 1e6).toFixed(0)} MB (first time only, then cached)`, Math.min(c.loaded, total), total);
    },
  };
  return c;
}

function boot() {
  booting ??= (async () => {
    const m = await loadManifest();
    const dir = new URL(m.pyodide.dir, ROOT).href;
    counting = counter("runtime", "Loading the Naap engine into this browser", m.runtime_bytes);
    progress("runtime", "Loading the Naap engine into this browser…", 0, m.runtime_bytes);
    const { loadPyodide } = await import(`${dir}pyodide.mjs`);
    py = await loadPyodide({ indexURL: dir, lockFileURL: `${dir}pyodide-lock.json`, stdout: () => {}, stderr: (s) => console.warn(s) });
    await loadPackages(m.packages);
    const zip = await (await fetch(new URL(m.naap_zip, ROOT))).arrayBuffer();
    counting = null;
    progress("runtime", "Starting the engine…", m.runtime_bytes, m.runtime_bytes);
    py.unpackArchive(zip, "zip", { extractDir: "/naap" });
    py.runPython("import sys; sys.path.insert(0, '/naap')");
    naap = py.pyimport("app.browser");
    naap.init((s) => onState(JSON.parse(s)));
    await restoreIndex();
  })();
  booting.catch(() => { booting = null; counting = null; });
  return booting;
}

/** loadPackage only logs a failed download; make it an error (once retried) so it is reported. */
async function loadPackages(names) {
  for (let attempt = 0; ; attempt++) {
    const errors = [];
    await py.loadPackage(names, { messageCallback: () => {}, errorCallback: (s) => errors.push(s) });
    const norm = (n) => n.toLowerCase().replace(/[-_.]+/g, "-");     // "PyMuPDF" is loaded as pymupdf
    const have = new Set(Object.keys(py.loadedPackages).map(norm));
    const missing = names.filter((n) => !have.has(norm(n)));
    if (!missing.length) return;
    if (attempt >= 2) throw new HttpError(503, `Could not download part of the engine (${missing.join(", ")}). Check the connection and try again. ${errors.join(" ").slice(0, 300)}`);
  }
}

/** PyMuPDF (vector PDF reading and sheet images) is loaded the first time a PDF is used. */
function pdfSupport() {
  pdfReady ??= (async () => {
    const m = await loadManifest();
    counting = counter("runtime", "Loading the PDF reader (PyMuPDF)", m.pdf_bytes);
    progress("runtime", "Loading the PDF reader (PyMuPDF)…", 0, m.pdf_bytes);
    try { await loadPackages(m.pdf_packages); } finally { counting = null; }
  })();
  pdfReady.catch(() => { pdfReady = null; });
  return pdfReady;
}

function fatal(err) {
  // a crashed Pyodide cannot be used again; jobs are safe in IndexedDB, the page restarts us
  post({ type: "fatal", error: String(err?.message || err) });
  self.close();
}

// ------------------------------------------------------------------ jobs on disk <-> IndexedDB
const FS = () => py.FS;
const exists = (p) => FS().analyzePath(p).exists;
const mtimeOf = (p) => { const t = FS().stat(p).mtime; return t instanceof Date ? t.getTime() : Number(t); };

function onState(st) {
  post({ type: "state", state: st });
}

async function restoreIndex() {
  const rows = await idbAll("jobs");
  for (const r of rows || []) {
    const d = `${JOBS}/${r.state.id}`;
    if (exists(`${d}/job.json`)) continue;
    FS().mkdirTree(d);
    writeFile(`${d}/job.json`, new TextEncoder().encode(JSON.stringify(r.state)), r.mtime);
  }
}

function writeFile(path, bytes, mtime) {
  FS().writeFile(path, bytes);
  if (mtime) FS().utime(path, mtime, mtime);
  saved.set(path, mtimeOf(path));
}

/** Put a job's files into the in-memory file system (only one job's large files at a time). */
async function hydrate(id) {
  const d = `${JOBS}/${id}`;
  if (!exists(`${d}/job.json`)) {
    const r = await idbGet("jobs", id);
    if (!r) return;
    FS().mkdirTree(d);
    writeFile(`${d}/job.json`, new TextEncoder().encode(JSON.stringify(r.state)), r.mtime);
  }
  if (active && active !== id) evict(active);
  active = id;
  for (const name of SAVED) {
    const p = `${d}/${name}`;
    if (exists(p)) continue;
    const f = await idbGet("files", `${id}/${name}`);
    if (f) writeFile(p, new Uint8Array(await f.data.arrayBuffer()), f.mtime);
  }
  if (exists(`${d}/input.pdf`)) await pdfSupport();
}

function evict(id) {
  const d = `${JOBS}/${id}`;
  for (const name of [...SAVED, "sheets.json", "project.naap.json"]) {
    const p = `${d}/${name}`;
    if (exists(p)) { FS().unlink(p); saved.delete(p); }
  }
  if (exists(`${d}/render`)) for (const f of FS().readdir(`${d}/render`)) if (f !== "." && f !== "..") FS().unlink(`${d}/render/${f}`);
}

/** Save what changed in a job folder since the last save. */
async function persist(id) {
  const d = `${JOBS}/${id}`;
  if (!exists(`${d}/job.json`)) { await idbDeleteJob(id); return; }
  const jp = `${d}/job.json`;
  if (saved.get(jp) !== mtimeOf(jp)) {
    const mt = mtimeOf(jp);
    await idbPut("jobs", id, { state: JSON.parse(FS().readFile(jp, { encoding: "utf8" })), mtime: mt });
    saved.set(jp, mt);
  }
  for (const name of SAVED) {
    const p = `${d}/${name}`;
    if (!exists(p)) continue;
    const mt = mtimeOf(p);
    if (saved.get(p) === mt) continue;
    await idbPut("files", `${id}/${name}`, { data: new Blob([FS().readFile(p)]), mtime: mt });
    saved.set(p, mt);
  }
}

async function prune() {
  const rows = (await idbAll("jobs")) || [];
  rows.sort((a, b) => (b.state.created || 0) - (a.state.created || 0));
  for (const r of rows.slice(KEEP_JOBS)) {
    try { naap.delete(r.state.id); } catch { /* not in memory */ }
    await idbDeleteJob(r.state.id);
  }
}

// ------------------------------------------------------------------ calls
function envelope(json) {
  const r = JSON.parse(json);
  if (r.file) {
    const bytes = FS().readFile(r.file);
    return { status: r.status, file: { buf: bytes.buffer, type: r.type, filename: r.filename || null } };
  }
  return { status: r.status, body: r.body };
}

async function call(method, url, body) {
  const path = url.split("?")[0];
  if (method === "GET" && path === "/api/meta") return { status: 200, body: (await loadManifest()).meta };
  if (method === "GET" && path === "/api/jobs" && !naap) {
    // the recent list, without starting the engine
    const rows = ((await idbAll("jobs")) || []).map((r) => r.state).sort((a, b) => (b.created || 0) - (a.created || 0));
    return { status: 200, body: rows.slice(0, KEEP_JOBS).map(({ id, filename, status, created, kind }) => ({ id, filename, status, created, kind })) };
  }
  await boot();
  const id = path.match(/^\/api\/jobs\/([0-9a-f]{32})/)?.[1];
  if (id) await hydrate(id);
  const res = envelope(naap.request(method, url, body == null ? null : JSON.stringify(body)));
  if (id) await persist(id);
  return res;
}

/** A Python envelope {status, body}: the body, or an HttpError like the server's answer. */
function unwrap(json) {
  const r = JSON.parse(json);
  if (r.status !== 200) throw new HttpError(r.status, r.body?.detail || "error");
  return r.body;
}

async function upload({ name, size, buf }) {
  await boot();
  const ext = name.toLowerCase().split(".").pop();
  const { id } = unwrap(naap.new_job(name, size));
  if (ext === "pdf") await pdfSupport();
  FS().writeFile(`${JOBS}/${id}/input.${ext}`, new Uint8Array(buf));
  const st = unwrap(naap.accept_input(id));
  if (active && active !== id) evict(active);
  active = id;
  await persist(id);
  await prune();
  return { id, state: st, ext };
}

async function convertDwg(id) {
  const d = `${JOBS}/${id}`;
  naap.set_state(id, JSON.stringify({ status: "converting", message: "Converting DWG to DXF with LibreDWG (WebAssembly, in this browser)…" }));
  const t0 = performance.now();
  const dwg = FS().readFile(`${d}/input.dwg`);
  FS().unlink(`${d}/input.dwg`);
  const res = await new Promise((resolve) => {
    const w = new Worker(new URL("dwg-worker.js", import.meta.url), { type: "module" });
    w.onmessage = (e) => { resolve(e.data); w.terminate(); };
    w.onerror = (e) => { resolve({ ok: false, error: e.message || "the DWG converter failed to start" }); w.terminate(); };
    w.postMessage({ buf: dwg.buffer }, [dwg.buffer]);
  });
  if (!res.ok) return { error: res.error };
  FS().writeFile(`${d}/input.dxf`, new Uint8Array(res.buf));
  return { seconds: (performance.now() - t0) / 1000 };
}

async function processJob(id, ext) {
  try {
    let seconds;  // undefined reaches Python as None (null would be JsNull)
    if (ext === "dwg") {
      const r = await convertDwg(id);
      if (r.error) { naap.set_state(id, JSON.stringify({ status: "error", error: `DWG conversion failed: ${r.error}`.slice(0, 800) })); return; }
      seconds = r.seconds;
    }
    naap.process(id, seconds);
  } finally {
    await persist(id);
  }
}

async function importProject(buf) {
  await boot();
  FS().writeFile("/import.naap.json", new Uint8Array(buf));
  const { id } = unwrap(naap.import_project("/import.naap.json"));
  await hydrate(id);
  await persist(id);
  await prune();
  return { id };
}

// one call at a time: Python is single-threaded and the job files are shared
let queue = Promise.resolve();
const enqueue = (fn) => { const p = queue.then(fn); queue = p.catch(() => {}); return p; };

function replyError(id, e) {
  if (e instanceof HttpError) { post({ type: "reply", id, status: e.status, body: { detail: e.message } }); return; }
  const msg = String(e?.message || e);
  post({ type: "reply", id, status: 500, body: { detail: msg.split("\n").filter(Boolean).slice(-1)[0] || msg } });
  if (/fatal error|Aborted\(|out of memory|RuntimeError: unreachable/i.test(msg)) fatal(e);
}

self.onmessage = (e) => {
  const m = e.data;
  if (m.type === "warmup") { enqueue(() => boot()).catch(() => {}); return; }
  if (m.type === "call") {
    enqueue(() => call(m.method, m.url, m.body)).then((r) => post({ type: "reply", id: m.id, ...r }, r.file ? [r.file.buf] : []), (err) => replyError(m.id, err));
    return;
  }
  if (m.type === "upload") {
    enqueue(() => upload(m)).then((r) => {
      post({ type: "reply", id: m.id, status: 200, body: { id: r.id, state: r.state } });
      enqueue(() => processJob(r.id, r.ext)).catch((err) => {
        const msg = String(err?.message || err);
        try { naap.set_state(r.id, JSON.stringify({ status: "error", error: msg.slice(0, 800) })); } catch { onState({ ...r.state, status: "error", error: msg }); }
        if (/fatal error|Aborted\(|out of memory/i.test(msg)) fatal(err);
      });
    }, (err) => replyError(m.id, err));
    return;
  }
  if (m.type === "import") {
    enqueue(() => importProject(m.buf)).then((r) => post({ type: "reply", id: m.id, status: 200, body: r }), (err) => replyError(m.id, err));
  }
};
