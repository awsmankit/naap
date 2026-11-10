// Device guard for the in-browser engine: will this drawing fit in this device's memory?
//
// The engine (Python on WebAssembly) needs a few GB for big drawings, and phones give a web page
// far less than a laptop. Before reading a file in the browser we estimate the peak memory from
// the file's size and type and compare it with what the device is likely to allow. If it looks
// too big we say so and offer choices; we never block silently.
//
// Peak memory measured in Chromium (renderer process, scripts/browser_smoke.py):
//   0.2 MB DXF (the sample house)   0.6 GB
//   50 MB DXF                        2.4 GB
//   150 MB DXF  (= 22 MB DWG)        1.9 GB
//   1 MB PDF, 11 pages               1.6 GB
// The estimate is a conservative envelope over those points (DXF memory depends on what is in the
// file, not just its size, so the 50 MB drawing sets the slope up to 50 MB).

export const BASE_GB = 0.6;                     // Pyodide + the engine + the sample

/** Estimated peak memory (GB) to read `bytes` of a .dxf, .dwg or .pdf in the browser. */
export function estimateGB(name, bytes) {
  const ext = String(name || "").toLowerCase().split(".").pop();
  const mb = Math.max(0, bytes) / 1e6;
  const dxf = (m) => BASE_GB + 1.8 * Math.min(1, m / 50) + 0.005 * Math.max(0, m - 50);
  if (ext === "dxf") return dxf(mb);
  // a DWG is about 7x smaller than its DXF; LibreDWG holds both while converting
  if (ext === "dwg") return dxf(mb * 7) + 0.2;
  // PyMuPDF on top of the runtime; grows with the size of the vector content
  if (ext === "pdf") return 1.2 + 0.4 * Math.sqrt(mb);
  // a saved project: geometry JSON parsed in Python (no DXF reading)
  if (ext === "json") return BASE_GB + 0.012 * mb;
  return BASE_GB;
}

/** What we know about this device. */
export function device(nav = globalThis.navigator || {}, win = globalThis) {
  const ua = String(nav.userAgent || "");
  const touch = (nav.maxTouchPoints || 0) > 0;
  const ios = /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && touch);   // iPadOS reports a Mac
  const android = /Android/.test(ua);
  const small = Math.min(win.screen?.width || 9999, win.screen?.height || 9999) < 820;
  const mobile = ios || android || /Mobi|Tablet/i.test(ua) || (touch && small);
  const mem = Number(nav.deviceMemory) || null;   // GB, rounded down, capped at 8 (Chromium only)
  // Memory a page can use before the browser kills it: a share of RAM, less on phones.
  // Unknown RAM (Safari, Firefox): phones and tablets are taken as 4 GB, computers as 8 GB.
  const ram = mem ?? (mobile ? 4 : 8);
  let budget = mobile ? ram * 0.35 : ram * 0.5;
  // Safari caps a page's WebAssembly memory well below 4 GB on iPhone and iPad
  if (ios) budget = Math.min(budget, 1.5);
  return { mobile, ios, android, touch, mem, ram, budget: Math.round(budget * 10) / 10 };
}

/** {est, budget, risky, ...device} for a file about to be read in this browser. */
export function assess(file, nav, win) {
  const d = device(nav, win);
  const est = Math.round(estimateGB(file.name, file.size) * 10) / 10;
  // WebAssembly in a browser tab tops out near 4 GB whatever the machine has
  const risky = est > d.budget || est > 3.5;
  return { ...d, est, risky };
}

/** Error text that means the engine ran out of memory (or was killed for it). */
export const OOM_RE = /out of memory|could not allocate|cannot allocate|allocation failed|Array buffer allocation|Aborted\(|RuntimeError: unreachable|memory access out of bounds|Maximum call stack|too big for (this|the) (device|browser)/i;

export function oomMessage(device = "browser") {
  return `This ${device} ran out of memory while Naap was reading the drawing. A web page on a phone or tablet `
    + "gets much less memory than on a laptop, and big drawings need a few GB. Open it on a laptop or desktop "
    + "(8 GB or more), or use the server version; to review here, save a project there and open the .naap.json file on this device.";
}

/** Remember a file being read, so a crash (the page reloads) can be explained afterwards. */
const KEY = "naap.reading";
export function markReading(info) { try { localStorage.setItem(KEY, JSON.stringify({ ...info, t: Date.now() })); } catch { /* storage off */ } }
export function clearReading() { try { localStorage.removeItem(KEY); } catch { /* storage off */ } }
export function lastReading() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "null");
    return v && Date.now() - v.t < 6 * 3600e3 ? v : null;
  } catch { return null; }
}
