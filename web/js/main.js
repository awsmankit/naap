// App shell: Upload -> Review -> Measurement book, with two-way traceability
// between the ledger and the drawing.

import { S, A, rowById } from "./state.js";
import { $, $$, el, fq, fmt, unit, debounce, toast, unionBox, validBox } from "./util.js";
import { api, MOCK, LOCAL, CAN_PROJECTS, UNREACHABLE, loadMB, putConfig, runCheck, runBuild, putCalibration, normalise,
  uploadJob, sampleFile, sheetImage, xlsxHref, downloadXlsx, saveProject, openProject, warmup } from "./api.js";
import { Viewer } from "./viewer.js";
import { tradeOf, SEV } from "./theme.js";
import { renderLedger, applySelection, renderFoot, renderTabs } from "./ledger.js";
import { renderReview, loadReview, blocked } from "./review.js";
import { structGroups, sheetName } from "./struct.js";
import { assess, markReading, clearReading, lastReading, OOM_RE, device } from "./guard.js";
import { registerSW } from "./pwa.js";

const STEP_ORDER = ["upload", "review", "mb"];
const SIGNAL = "#e9c46a";
const STEP_LABEL = { upload: "Upload", review: "Review", mb: "Measurement book" };

// phones get cards and a full-screen drawing; touch screens get finger-sized hit areas
const PHONE = window.matchMedia("(max-width: 767px)");
const TOUCH = window.matchMedia("(pointer: coarse)").matches || (navigator.maxTouchPoints || 0) > 0;
S.compact = PHONE.matches;
S.touch = TOUCH;
const isFs = () => document.body.classList.contains("fs");

// ================================================================ routing
function setStep(step, { nav = true } = {}) {
  const prev = S.step;
  S.step = step;
  if (!S.compact || (prev && prev !== step)) closeFs({ silent: true });
  $("#view-upload").hidden = step !== "upload";
  $("#view-work").hidden = step === "upload";
  $("#reviewPanel").hidden = step !== "review";
  $("#ledger").hidden = step !== "mb";
  const cur = STEP_ORDER.indexOf(step);
  $$("#steps li").forEach((li) => {
    const i = STEP_ORDER.indexOf(li.dataset.step);
    li.classList.toggle("active", i === cur);
    li.classList.toggle("done", i < cur);
    li.classList.toggle("avail", i > cur && (i === 1 ? !!S.geo : !!S.mb));
    const b = li.querySelector("button");
    b.setAttribute("aria-current", i === cur ? "step" : "false");
    b.disabled = i === cur || (i === 1 && !S.geo) || (i === 2 && !S.mb);
  });
  if (S.viewer) {
    S.viewer.showRegions = step === "review";
    if (step !== "review") S.viewer.selectedRegion = null;
    S.viewer.resize();
  }
  $("#btnXlsx").hidden = step !== "mb";
  $("#btnSaveProject").hidden = !CAN_PROJECTS || step === "upload" || !S.jobId;
  $("#btnMore").hidden = step === "upload";
  closeMore();
  $("#miniHint").textContent = step === "mb" ? (S.touch ? "Tap a card to see it on the drawing" : "Select a card to see it on the drawing") : "Tap a view to select it";
  $("#sheetStrip").hidden = step === "upload" || !S.isPdf;
  if (step !== "mb") cancelCalibrate();
  const q = location.search;
  const hash = step === "upload" ? "" : S.jobId ? `#job=${S.jobId}&step=${step}` : `#step=${step}`;
  // a new step is a new history entry, so the browser's Back button walks back through the steps
  if (location.hash !== hash) history[nav && prev !== step && prev ? "pushState" : "replaceState"](null, "", `${location.pathname}${q}${hash}`);
  document.title = step === "upload" ? "Naap · Drawing to Measurement Book" : `${STEP_LABEL[step]} · ${S.job?.filename || "drawing"} · Naap`;
  if (step === "review") { renderReview(); if (prev && prev !== "review" && S.cfg) loadReview(); }
  if (step === "mb") renderLedger();
  // keyboard and screen-reader users: the control they used (Build, the sample link) is now hidden, so
  // focus would fall back to <body>; move it to the new step's heading or tab instead
  if (prev && prev !== step) setTimeout(() => {
    const a = document.activeElement;
    if (a && a !== document.body && !a.closest("[hidden]")) return;
    const t = step === "review" ? $("#reviewPanel h2") : step === "mb" ? $("#ledgerTitle") : $("#upTitle");
    if (!t) return;
    if (!t.matches("button, a, input")) t.tabIndex = -1;
    t.focus({ preventScroll: true });
  }, 0);
}

$$("#steps li").forEach((li) => li.querySelector("button").addEventListener("click", () => {
  const step = li.dataset.step;
  if (step === "upload") { resetUpload(); setStep("upload"); }
  else if (step === "review" && S.geo) setStep("review");
  else if (step === "mb" && S.mb) setStep("mb");
}));
$("#home").addEventListener("click", (e) => { e.preventDefault(); resetUpload(); setStep("upload"); });

// ================================================================ upload
const STAGES = [["upload", LOCAL ? "Load the engine in this browser" : "Upload"], ["converting", "Convert DWG to DXF"], ["parsing", "Read geometry"], ["clustering", "Find views on the sheet"], ["ready", "Ready to review"]];
const ACCEPT = ["dwg", "dxf", "pdf"];

function resetUpload() {
  $("#stages").hidden = true;
  $("#upErr").hidden = true;
  $("#fileInput").value = "";
  $("#dropzone").classList.remove("busy");
  setTB({ name: "Awaiting file", size: "—", units: "—", ents: "—", layers: "—", views: "—", time: "—" }, false);
  loadRecent();
}

function setTB(v, fill = true) {
  const map = { name: "#tbName", size: "#tbSize", units: "#tbUnits", ents: "#tbEnts", layers: "#tbLayers", views: "#tbViews", time: "#tbTime" };
  for (const [k, sel] of Object.entries(map)) if (v[k] != null) { $(sel).textContent = v[k]; $(sel).classList.toggle("fill", fill && v[k] !== "—"); }
}

function renderStages(status, kind, extra = {}) {
  const list = STAGES.filter(([k]) => (k !== "converting" || kind === "dwg") && (k !== "clustering" || kind !== "pdf"))
    .map(([k, label]) => [k, kind === "pdf" && k === "parsing" ? "Read the PDF sheets" : label]);
  const order = list.map(([k]) => k);
  let cur = status === "uploading" ? 0 : status === "queued" ? 1 : order.indexOf(status);
  if (cur < 0) cur = 1;
  $("#stages").hidden = false;
  $("#stages").replaceChildren(...list.map(([k, label], i) => {
    let cls = i < cur ? "done" : i === cur ? "run" : "";
    if (status === "ready") cls = "done";
    if (status === "error" && i === cur) cls = "fail";
    const running = i === cur && status !== "ready" && status !== "error";
    return el("li", { class: cls, "aria-current": i === cur ? "step" : null }, label,
      i === cur && extra.msg ? el("small", {}, extra.msg) : null,
      running ? progressBar(extra.pct) : null);
  }));
}

/** A determinate bar (pct) or, without a pct, an indeterminate one. */
function progressBar(pct) {
  const known = pct != null && Number.isFinite(pct);
  return el("span", { class: `pbar${known ? "" : " busy"}`, role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": known ? String(pct) : null },
    el("i", { style: known ? { width: `${Math.max(2, Math.min(100, pct))}%` } : {} }));
}

/** An error on the upload page; memory trouble and interrupted reads come with what to do next. */
function upErr(msg, { oom = OOM_RE.test(String(msg)), interrupted = false } = {}) {
  const box = $("#upErr");
  box.replaceChildren(msg);
  if ((oom || interrupted) && LOCAL) {
    box.append(el("span", { class: "err-acts" },
      CAN_PROJECTS ? el("button", { class: "btn", type: "button", onclick: () => $("#projectInput").click() }, "Open a saved project (.naap.json)") : null,
      el("button", { class: "btn", type: "button", onclick: () => { box.hidden = true; $("#fileInput").click(); } }, interrupted && !oom ? "Choose the file again" : "Try another file")));
  }
  box.hidden = false;
  $("#dropzone").classList.remove("busy");
  clearInterval(slowT);
}

let slowT = 0;
/** Unsupported files get a clear answer before anything is read. */
function badFile(file, ext) {
  if (/^(dwf|dwfx|dgn|rvt|ifc|skp|3dm|step|stp|stl|obj)$/.test(ext)) return `“${file.name}” is a ${ext.toUpperCase()} file. Naap reads AutoCAD .dwg or .dxf, or a vector .pdf: export the drawing from your CAD program in one of those.`;
  if (/^(png|jpe?g|gif|webp|heic|tiff?|bmp)$/.test(ext)) return `“${file.name}” is an image. Naap measures vector drawings: export the drawing as .dwg, .dxf or a vector .pdf (a scanned image has no lines to measure).`;
  if (/^(zip|rar|7z|gz)$/.test(ext)) return `“${file.name}” is an archive. Unzip it and drop the .dwg, .dxf or .pdf inside.`;
  if (/^(xlsx?|csv|docx?)$/.test(ext)) return `“${file.name}” is a document, not a drawing. Drop the .dwg, .dxf or .pdf drawing.`;
  return `“${file.name}” is not a .dwg, .dxf or .pdf file. Export the drawing from CAD as DWG, DXF or a vector PDF and try again.`;
}

async function uploadFile(file, { checked = false } = {}) {
  if (CAN_PROJECTS && /\.naap\.json$|\.json$/i.test(file.name)) return openProjectFile(file);
  const ext = file.name.toLowerCase().split(".").pop();
  if (!ACCEPT.includes(ext)) return upErr(badFile(file, ext));
  if (!file.size) return upErr(`“${file.name}” is empty (0 bytes). Save or export the drawing again and retry.`);
  const max = ext === "pdf" ? S.meta?.max_pdf_mb || 100 : S.meta?.max_upload_mb || 300;
  if (file.size > max * 1024 * 1024) return upErr(`The file is ${(file.size / 1e6).toFixed(0)} MB; the limit is ${max} MB.`);
  if (LOCAL && !checked) {
    const a = assess(file);
    if (a.risky) {
      const choice = await guardDialog(file, a);
      if (choice === "continue") return uploadFile(file, { checked: true });
      if (choice === "project") { $("#projectInput").click(); return; }
      if (choice === "laptop") upNote(laptopNote(file));
      return;
    }
  }
  if (LOCAL) markReading({ name: file.name, size: file.size });
  $("#upErr").hidden = true;
  $("#dropzone").classList.add("busy");
  setTB({ name: file.name, size: `${(file.size / 1e6).toFixed(1)} MB`, units: "—", ents: "—", layers: "—", views: "—", time: "0 s" });
  const t0 = Date.now();
  const timer = setInterval(() => setTB({ time: `${Math.round((Date.now() - t0) / 1000)} s` }), 500);
  renderStages("uploading", ext, { msg: LOCAL ? "Starting…" : "0%", pct: 0 });
  uploadJob(file, (p) => renderStages("uploading", ext, p))
    .then((body) => pollJob(body.id, t0).catch((e) => { clearReading(); upErr(e.message, { oom: e.oom || OOM_RE.test(e.message) }); }),
      (e) => { clearReading(); renderStages("error", ext); upErr(e.message, { oom: e.oom }); })
    .finally(() => clearInterval(timer));
}

function laptopNote(file) {
  return `“${file.name}” was not opened. Open ${location.host ? "this page" : "Naap"} on a laptop or desktop with 8 GB of memory or more, read the drawing there and use Save project; then open the .naap.json file here to review the book on this device.`;
}
function upNote(msg) {
  const box = $("#upErr");
  box.replaceChildren(msg, el("span", { class: "err-acts" },
    navigator.clipboard ? el("button", { class: "btn", type: "button", onclick: (e) => { navigator.clipboard.writeText(location.href.split("#")[0]).then(() => { e.target.textContent = "Link copied"; }, () => {}); } }, "Copy this page's link") : null,
    CAN_PROJECTS ? el("button", { class: "btn", type: "button", onclick: () => $("#projectInput").click() }, "Open a saved project") : null));
  box.hidden = false;
}

// ---------------------------------------------------------------- device guard dialog
/** Resolves "continue" | "laptop" | "project" | "cancel". Focus stays in the dialog until it closes. */
function guardDialog(file, a) {
  const dlg = $("#guard"), back = document.activeElement;
  const gb = (v) => `${fmt(v, 1, 1)} GB`;
  const what = a.mobile ? (a.ios ? "an iPhone or iPad" : "a phone or tablet") : "this computer";
  $("#guardTitle").textContent = a.mobile ? "This drawing may be too big for this device" : "This drawing may be too big for this browser";
  $("#guardBody").replaceChildren(
    el("p", {}, `Reading “${file.name}” in the browser needs about `, el("b", {}, gb(a.est)), ` of memory at its peak. `,
      a.mem ? `This device reports ${a.mem} GB of memory` : `This looks like ${what}`, `, and a web page here can usually use about `, el("b", {}, gb(a.budget)), ". ",
      "If it runs out, the page stops or reloads and the drawing is not read."),
    el("dl", {}, el("dt", {}, "File"), el("dd", {}, `${(file.size / 1e6).toFixed(1)} MB ${file.name.split(".").pop().toUpperCase()}`),
      el("dt", {}, "Needs about"), el("dd", {}, gb(a.est)), el("dt", {}, "Available about"), el("dd", {}, gb(a.budget))),
    el("p", {}, "A laptop or desktop with 8 GB or more reads it comfortably. To review on this device, save the project there and open the .naap.json file here: it opens without reading the drawing again."));
  return new Promise((resolve) => {
    const done = (v) => { dlg.hidden = true; dlg.removeEventListener("keydown", keys); back?.focus?.({ preventScroll: true }); resolve(v); };
    const acts = [
      el("button", { class: "btn primary", type: "button", onclick: () => done("laptop") }, "Use a laptop"),
      CAN_PROJECTS ? el("button", { class: "btn", type: "button", onclick: () => done("project") }, "Open a saved project (.naap.json) instead") : null,
      el("button", { class: "btn", type: "button", onclick: () => done("continue") }, "Continue anyway"),
    ].filter(Boolean);
    $("#guardActions").replaceChildren(...acts);
    const keys = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done("cancel"); return; }
      if (e.key !== "Tab") return;
      const f = $$("button", dlg), i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
    };
    dlg.addEventListener("keydown", keys);
    dlg.hidden = false;
    acts[0].focus();
  });
}

async function pollJob(id, t0 = Date.now()) {
  S.jobId = id;
  S.polling = id;
  // a reload while the drawing is read comes back to it (the server keeps going; the browser says it stopped)
  const deep = `#job=${id}&step=review`;
  if (S.step === "upload" && location.hash !== deep) history.pushState(null, "", `${location.pathname}${location.search}${deep}`);
  let lastStatus = null, since = Date.now();
  clearInterval(slowT);
  slowT = setInterval(() => {
    const quiet = (Date.now() - since) / 1000;
    if (quiet < (device().mobile ? 120 : 300) || $("#stages").hidden || $(".note-slow")) return;
    $("#stages").after(el("p", { class: "note-slow", role: "status" }, LOCAL
      ? "Still working. Big drawings take a few minutes. If this page stops responding or reloads, the drawing is too big for this device's memory: use a laptop or desktop, or the server version."
      : "Still working. Big drawings take a few minutes on the server."));
  }, 5000);
  for (;;) {
    const st = await api(`/api/jobs/${id}`);
    S.job = st;
    const s = st.stats || {};
    setTB({ name: st.filename, size: st.size ? `${(st.size / 1e6).toFixed(1)} MB` : undefined, units: s.units, ents: s.entities != null ? fmt(s.entities, 0) : undefined,
      layers: s.layers != null ? fmt(s.layers, 0) : undefined, views: s.regions != null ? `${s.regions}${st.kind === "pdf" ? " sheets" : ""}` : undefined, time: `${Math.round(st.elapsed_s ?? (Date.now() - t0) / 1000)} s` });
    renderStages(st.status, st.kind, { msg: st.message });
    if (st.status !== lastStatus) { lastStatus = st.status; since = Date.now(); }
    if (st.status === "error") {
      S.polling = null;
      clearInterval(slowT); $(".note-slow")?.remove(); clearReading();
      history.replaceState(null, "", `${location.pathname}${location.search}`);
      throw Object.assign(new Error(friendlyJobError(st)), { interrupted: !!st.interrupted });
    }
    if (st.status === "ready") break;
    await new Promise((r) => setTimeout(r, 700));
  }
  clearInterval(slowT); $(".note-slow")?.remove(); clearReading();
  S.polling = null;
  await openJob(id, "review");
}

/** Engine errors in words a user can act on (scanned PDFs, broken DWGs, memory). */
function friendlyJobError(st) {
  const e = String(st.error || "");
  if (!e) return "The drawing could not be read.";
  if (OOM_RE.test(e)) return e;
  if (/scann|no vector|raster only|no text or lines/i.test(e)) return `${e}${/\.$/.test(e) ? "" : "."} Naap measures vector drawings; a scanned sheet has no lines to measure. Ask for the CAD file or a PDF exported from CAD.`;
  if (st.kind === "dwg" && /conver|libredwg|dwg/i.test(e)) return `${e}${/\.$/.test(e) ? "" : "."} The DWG may be damaged or saved by a CAD version LibreDWG cannot read: save it again as DXF (or an older DWG) and try that.`;
  if (/not a dxf|invalid dxf|DXFStructureError|unexpected end|truncat/i.test(e)) return `${e}${/\.$/.test(e) ? "" : "."} The file looks damaged or incomplete: export it from CAD again.`;
  return e;
}

async function loadRecent() {
  if (S.offline && !S.meta) return;
  try {
    const jobs = await api("/api/jobs");
    if (!jobs.length) return;
    const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
    const ago = (t) => {
      const s = (Date.now() / 1000 - t);
      if (s < 3600) return rtf.format(-Math.max(1, Math.round(s / 60)), "minute");
      if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
      return rtf.format(-Math.round(s / 86400), "day");
    };
    $("#recent").replaceChildren(...jobs.slice(0, 8).map((j) => el("li", {}, el("a", {
      href: `#job=${j.id}&step=review`,
      onclick: (e) => { e.preventDefault(); resume(j.id); },
    }, el("span", {}, j.filename, j.status !== "ready" ? ` (${j.status})` : ""), el("time", {}, ago(j.created))))));
  } catch { /* server down: keep the empty list */ }
}

async function resume(id, step) {
  const st = await api(`/api/jobs/${id}`);
  if (st.status === "ready") return openJob(id, step);
  setStep("upload", { nav: false });
  pollJob(id).catch((e) => upErr(e.message, { interrupted: e.interrupted }));
}

const dz = $("#dropzone");
dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
dz.addEventListener("dragenter", warmup, { once: true });
dz.addEventListener("pointerenter", warmup, { once: true });
dz.addEventListener("dragleave", () => dz.classList.remove("over"));
dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); });
dz.addEventListener("click", (e) => { if (!dz.classList.contains("busy") && !e.target.closest("label, input")) $("#fileInput").click(); });
dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#fileInput").click(); } });
$("#fileInput").addEventListener("change", (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); });
$("#btnSample").addEventListener("click", async (e) => {
  e.preventDefault();
  try {
    uploadFile(await sampleFile("sample-house.dxf"));
  } catch (err) { upErr(`Could not load the sample drawing (${err.message}).${LOCAL ? "" : " Is the app server running?"}`); }
});

// ================================================================ open a job / workspace
function ensureViewer() {
  if (S.viewer) return;
  const v = new Viewer($("#canvas"), $("#minimap"), $("#scalebar"));
  v.on("coords", (x, y) => {
    if (S.isPdf) {
      const sh = v.sheetAt(x, y);
      $("#coords").textContent = sh ? `${sh.label} · ${fmt(x - sh.bbox[0], 1)}, ${fmt(sh.bbox[3] - y, 1)} pt` : "";
    } else $("#coords").textContent = `${fmt(x, 0)}, ${fmt(y, 0)} ${S.geo?.display_units || S.geo?.units || ""}`;
  });
  v.on("pick", onPick);
  v.on("flag", (id) => { if (S.step === "mb") { setTab("flags"); select("flag", id, { from: "drawing" }); } });
  v.on("region", (id) => { if (S.step === "review" && id != null) { v.selectedRegion = id; renderReview(); v.draw(); if (isFs()) renderFsBar(); } });
  v.on("measure", onMeasure);
  S.viewer = v;
}

function homeBox() {
  if (S.isPdf && S.sheet != null) return sheetBox(S.sheet);
  const regs = (S.cfg?.regions || []).filter((r) => r.include);
  const use = regs.length ? regs : S.cfg?.regions || [];
  const b = unionBox(use.map((r) => r.bbox));
  return validBox(b) ? b : null;
}

function allRegionsBox() {
  const b = unionBox((S.cfg?.regions || []).map((r) => r.bbox));
  return validBox(b) ? b : null;
}

async function openJob(id, step = "review") {
  S.jobId = id;
  ensureViewer();
  setStep(step === "mb" ? "mb" : "review");
  $("#stageMsg").hidden = false;
  $("#stageMsg").textContent = "Loading drawing…";
  const [job, geo, cfg] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/geometry`), api(`/api/jobs/${id}/config`)]);
  await useDrawing(job, geo, cfg);
  // opening the book: use the saved result as it is (saving the config first would make it stale)
  if (step === "mb") await buildAndShow({ rebuild: false, save: false, fallback: "review" });
  else { setStep("review", { nav: false }); loadReview(); }
}

async function useDrawing(job, geo, cfg) {
  S.job = job; S.geo = geo; S.cfg = cfg;
  S.isPdf = geo.source === "pdf" && Array.isArray(geo.sheets);
  S.sheet = null;
  cfg.flags = cfg.flags || {};
  cfg.layer_roles = cfg.layer_roles || {};
  cfg.layer_style = cfg.layer_style || {};
  S.mb = null;
  S.review = null; S.reviewState = null; S.reviewStale = false; S.reviewMs = null; S.askOpen = null; S.rvFloorEdit = null;
  await document.fonts.load("16px Osifont").catch(() => {});
  const v = S.viewer;
  v.setGeometry(geo, cfg.layer_roles, allRegionsBox());
  for (const L of v.layers) { const st = cfg.layer_style[L.name]; if (st && st.visible !== undefined) L.visible = st.visible; }
  v.regions = cfg.regions;
  v.flags = [];
  v.sheets = S.isPdf ? geo.sheets.map((s) => ({ sheet: s.sheet, bbox: s.bbox, label: sheetName(s.sheet) })) : [];
  v.raster = { on: v.raster.on && S.isPdf, url: S.isPdf && job.id ? rasterUrl(job.id) : null, base: new Map(), detail: null, want: null };
  v.unitMM = null; v.scaleMM = null; v.marks = []; v.measure = null;
  v.setClip(null);
  $("#tRaster").hidden = !S.isPdf || !job.id;
  $("#tCal").hidden = !S.isPdf || !job.id;
  $('#rvTabs [data-tab="views"]').firstChild.textContent = S.isPdf ? "Sheets " : "Views ";
  $("#rvIntro").textContent = S.isPdf
    ? "A structural PDF set: each sheet is read for plans, schedules and notes. Answer the questions, check the storey heights, then build the book. Scales are calibrated per sheet."
    : "See what each view is used for, confirm floors and heights, answer the questions, then build the book.";
  renderSheetStrip();
  if (S.isPdf) setSheet(geo.sheets.find((s) => !s.scanned)?.sheet ?? geo.sheets[0]?.sheet, { fit: false });
  v.fit(homeBox() || v.home, false);
  $("#stageMsg").hidden = true;
  $("#jobName").replaceChildren(el("b", {}, job.filename), S.isPdf
    ? ` · ${geo.sheets.length} sheets · ${fmt(geo.entities.length, 0)} entities`
    : ` · ${fmt(geo.entities.length, 0)} entities · ${geo.display_units || geo.units}`);
  renderLayerPop();
}

// ================================================================ PDF sheets
const rasterUrl = (id) => (n, dpi, clip) => sheetImage(id, n, dpi, clip);
const sheetBox = (n) => S.geo?.sheets?.find((s) => s.sheet === Number(n))?.bbox || null;

/** Show one sheet (n) or all sheets side by side (null). */
function setSheet(n, { fit = true } = {}) {
  const v = S.viewer;
  S.sheet = n == null ? null : Number(n);
  v.setClip(S.sheet == null ? null : sheetBox(S.sheet));
  const c = S.sheet != null && S.mb?.calibration ? S.mb.calibration[S.sheet] : null;
  v.scaleMM = c?.pt_per_mm || null;
  v.unitMM = c?.pt_per_mm || null;
  if (v.measure && v.measure.sheet !== S.sheet) cancelCalibrate();
  renderSheetStrip();
  if (fit) v.fit(homeBox() || v.home);
}

function sheetOfRef(r) {
  if (r.sheet != null) return Number(r.sheet);
  if (!r.bbox) return null;
  return S.viewer.sheetAt((r.bbox[0] + r.bbox[2]) / 2, (r.bbox[1] + r.bbox[3]) / 2)?.sheet ?? null;
}

/** The sheet a highlight lives on: explicit, else the sheet most of its refs point at
 *  (the current sheet wins a tie, so an item spread over floors does not jump around). */
function targetSheet(groups) {
  const count = new Map();
  for (const g of groups) {
    if (g.sheet != null) return Number(g.sheet);
    for (const r of g.refs || []) { const n = sheetOfRef(r); if (n != null) count.set(n, (count.get(n) || 0) + 1); }
  }
  if (!count.size) return null;
  if (S.sheet != null && count.has(S.sheet)) return S.sheet;
  return [...count.entries()].sort((a, b) => b[1] - a[1])[0][0];
}

function renderSheetStrip() {
  const strip = $("#sheetStrip");
  if (!S.isPdf) { strip.hidden = true; S.viewer.insetTop = 0; return; }
  strip.hidden = S.step === "upload";
  const openBySheet = new Map(), rowsBySheet = new Map();
  for (const f of S.mb?.flags || []) if (f.status === "open") { const n = f.refs[0] ? sheetOfRef(f.refs[0]) : null; if (n != null) openBySheet.set(n, (openBySheet.get(n) || 0) + 1); }
  for (const r of S.mb?.rows || []) { const n = r.refs[0] ? sheetOfRef(r.refs[0]) : null; if (n != null) rowsBySheet.set(n, (rowsBySheet.get(n) || 0) + 1); }
  const card = (n, label, title, img, extra) => el("button", {
    type: "button", class: `sh${S.sheet === n ? " on" : ""}`, "aria-pressed": String(S.sheet === n), title: title ? `${label} · ${title}` : label,
    onclick: () => { setSheet(n); if (S.sel && S.sel.kind !== "entity") select(S.sel.kind, S.sel.key, { from: "strip", fly: false }); },
  }, img, el("span", { class: "no" }, label), el("span", { class: "ti" }, title || ""), extra);
  const items = S.geo.sheets.map((s) => {
    const label = sheetName(s.sheet), tb = s.title_block || {};
    const img = S.jobId && !s.scanned ? el("img", { alt: "", loading: "lazy", decoding: "async" }) : el("span", { class: "noimg" }, s.scanned ? "scanned" : "");
    if (img.tagName === "IMG") Promise.resolve(rasterUrl(S.jobId)(s.sheet, 9)).then((u) => { img.src = u; }, () => {});
    const fl = openBySheet.get(s.sheet), nr = rowsBySheet.get(s.sheet);
    const extra = el("span", { class: "meta" }, nr ? `${fmt(nr, 0)} lines` : "", fl ? el("i", { title: `${fl} open flags` }, `${fl} ⚑`) : null,
      tb.floor_scope?.multiplier > 1 ? el("i", { class: "x" }, `×${tb.floor_scope.multiplier}`) : null);
    return card(s.sheet, label, tb.title, img, extra);
  });
  strip.replaceChildren(el("div", { class: "strip-in", role: "group", "aria-label": "Sheets" },
    card(null, "All", `${S.geo.sheets.length} sheets side by side`, el("span", { class: "noimg all" }, "▦"), null), ...items));
  S.viewer.insetTop = strip.hidden ? 0 : strip.offsetHeight + 12;
  strip.querySelector(".sh.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}
$("#tRaster").addEventListener("click", () => {
  const b = $("#tRaster"), on = b.getAttribute("aria-pressed") !== "true";
  b.setAttribute("aria-pressed", String(on));
  S.viewer.setRaster(on);
});

// ================================================================ calibration (two points + a known length)
function calBar(msg, { busy = false } = {}) {
  const bar = $("#calbar");
  if (!msg) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.replaceChildren(el("span", { class: busy ? "busy" : "" }, msg),
    busy ? null : el("button", { class: "link", type: "button", onclick: cancelCalibrate }, "Cancel"));
}

function startCalibrate(n = S.sheet) {
  if (n == null) { toast("Pick a sheet first, then calibrate it."); return; }
  if (S.sheet !== Number(n)) setSheet(n);
  hidePick();
  S.viewer.measure = { sheet: Number(n), p1: null, p2: null, hover: null };
  $("#tCal").setAttribute("aria-pressed", "true");
  $("#canvas").classList.add("measuring");
  if (S.compact) openFs();
  calBar(S.touch
    ? `Calibrate ${sheetName(n)}: touch and drag to aim at the first end of a length you know; the point sits above your finger (snaps to line ends). Lift to place it. Two fingers move the sheet.`
    : `Calibrate ${sheetName(n)}: click the first end of a length you know (snaps to line ends).`);
  $("#canvas").focus({ preventScroll: true });
  S.viewer.draw();
}

function cancelCalibrate() {
  if (!S.viewer?.measure) return;
  S.viewer.measure = null;
  $("#tCal").setAttribute("aria-pressed", "false");
  $("#canvas").classList.remove("measuring");
  $("#calpop").hidden = true;
  calBar(null);
  S.viewer.draw();
}
$("#tCal").addEventListener("click", () => (S.viewer.measure ? cancelCalibrate() : startCalibrate()));

function onMeasure(p, sx, sy) {
  const v = S.viewer, M = v.measure;
  const b = sheetBox(M.sheet);
  if (b && !(p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3])) { toast("Pick points on the sheet."); return; }
  if (!M.p1) { M.p1 = p; calBar(S.touch ? "Now aim at the other end and lift." : "Now click the other end."); }
  else if (!M.p2) { M.p2 = p; M.hover = null; calBar("Enter the real length between the two points."); showCalPop(sx, sy); }
  v.draw();
}

function showCalPop(sx, sy) {
  const M = S.viewer.measure, pop = $("#calpop");
  const len = Math.hypot(M.p2[0] - M.p1[0], M.p2[1] - M.p1[1]);
  const c = S.mb?.calibration?.[M.sheet];
  const now = c?.pt_per_mm ? len / c.pt_per_mm : null;
  const inp = el("input", { type: "number", min: 1, step: "any", required: true, inputmode: "decimal", name: "mm", placeholder: now ? String(Math.round(now / 50) * 50) : "e.g. 3000", "aria-label": "Real length in mm" });
  pop.replaceChildren(el("form", {
    onsubmit: (e) => {
      e.preventDefault();
      const mm = Number(inp.value);
      if (!(mm > 0)) { inp.focus(); return; }
      applyCalibration(M.sheet, { p1: M.p1.map((x) => +x.toFixed(2)), p2: M.p2.map((x) => +x.toFixed(2)), mm });
    },
  }, el("h3", {}, `Calibrate ${sheetName(M.sheet)}`),
  el("p", {}, `The points are ${fmt(len, 2)} pt apart on the sheet${now ? `; the current scale reads that as ${fmt(now, 0)} mm` : ""}.`),
  el("label", {}, "Real length (mm)", inp),
  el("div", { class: "row" },
    el("button", { class: "btn primary", type: "submit" }, "Apply and re-measure"),
    el("button", { class: "btn", type: "button", onclick: () => { $("#calpop").hidden = true; startCalibrate(M.sheet); } }, "Pick again"))));
  pop.hidden = false;
  const st = $("#stage").getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(sx + 18, st.width - 330))}px`;
  pop.style.top = `${Math.max(8, Math.min(sy + 14, st.height - 240))}px`;
  inp.focus();
}

/** PUT the calibration for one sheet (spec null clears it), rebuild, keep the selection. */
async function applyCalibration(n, spec) {
  const prev = S.sel, before = new Map(S.mb.rows.map((r) => [r.id, r.qty]));
  const old = S.mb.calibration?.[n]?.scale_1_to;
  $("#calpop").hidden = true;
  S.viewer.measure = null;
  $("#canvas").classList.remove("measuring");
  $("#tCal").setAttribute("aria-pressed", "false");
  calBar(`Re-measuring ${sheetName(n)} with the new scale…`, { busy: true });
  try {
    const res = await putCalibration(S.jobId, { [n]: spec });
    S.cfg.calibration = res.user;
    S.builtAt = Date.now();
    const mb = await loadMB(S.jobId, S.cfg);
    setMB(mb.data, mb.source);
    renderLedger();
    reselect(prev);
    const now = S.mb.calibration?.[n]?.scale_1_to;
    const changed = S.mb.rows.filter((r) => !before.has(r.id) || Math.abs(before.get(r.id) - r.qty) > 5e-4).length;
    toast(`${sheetName(n)}: ${spec ? `scale now ≈1:${fmt(now, 1)}${old ? ` (was 1:${fmt(old, 1)})` : ""}` : "back to the drawing's own calibration"}. ${changed ? `${changed} MB lines changed.` : "Quantities unchanged."}`);
  } catch (e) {
    toast(`Calibration failed: ${e.message}`, true);
  } finally {
    calBar(null);
    S.viewer.draw();
  }
}

/** After a re-run: select the same thing again if it still exists (row ids are stable). */
function reselect(prev) {
  if (!prev || prev.kind === "entity") { select(null); return; }
  if (prev.kind === "row" && !rowById(prev.key)) { select(null); return; }
  if (prev.kind === "flag" && !S.mb.flags.some((f) => f.id === prev.key)) { select(null); return; }
  select(prev.kind, prev.key, { from: "rerun", fly: false });
}

// ================================================================ MB
async function buildAndShow({ rebuild = true, save = true, fallback = null } = {}) {
  const btn = $("#btnBuild");
  if (rebuild && S.step === "review" && blocked()) { toast("Tick at least one plan to measure before building the book.", true); return; }
  btn.disabled = true; btn.textContent = "Building…";
  try {
    if (!S.offline && save) await saveConfig();
    const res = await loadMB(S.offline ? null : S.jobId, S.cfg, { rebuild: rebuild && !MOCK });
    if (res.built || (!MOCK && res.source === "api")) S.builtAt = Date.now();
    let mb = res.data;
    if (MOCK && !S.offline) mb = await withLiveFlags(mb);
    if (MOCK) mb = mockEngine(mb);
    setMB(mb, res.source);
    setStep("mb");
  } catch (e) {
    toast(`Could not build the book: ${e.message}`, true);
    if (fallback) setStep(fallback, { nav: false });
  } finally {
    btn.disabled = false; btn.textContent = "Build measurement book";
    if (S.step === "review") renderReview();
  }
}
$("#btnBuild").addEventListener("click", () => buildAndShow({ rebuild: true }));

/** Mock MB + the job's real flags from POST /check (so resolving is real). */
async function withLiveFlags(mb) {
  try {
    const res = await runCheck(S.jobId, S.cfg);
    const live = normalise({ flags: res.flags }).flags;
    const byId = new Map(live.map((f) => [f.id, f]));
    mb.flags = mb.flags.map((f) => (byId.has(f.id) ? { ...f, ...byId.get(f.id), refs: f.refs.length ? f.refs : byId.get(f.id).refs } : f));
    for (const f of live) if (!mb.flags.some((x) => x.id === f.id)) mb.flags.push(f);
  } catch (e) { toast(`Checks failed: ${e.message}`, true); }
  return mb;
}

/** Mock-only: re-derive row status and heights from flag resolutions. */
function mockEngine(mb) {
  const cf = S.cfg?.flags || {};
  for (const f of mb.flags) {
    const st = cf[f.id];
    if (st) { f.status = st.status; f.value = st.value; } else if (S.offline) { f.status = "open"; f.value = null; }
  }
  const open = new Set(mb.flags.filter((f) => f.status === "open").map((f) => f.id));
  const byId = new Map(mb.flags.map((f) => [f.id, f]));
  for (const r of mb.rows) {
    r._base ??= { h: r.h, qty: r.qty };
    const assumed = /inferred|assumed|scaled|derived|as drawn|not dimensioned/i.test(`${r.remarks} ${r.desc}`);
    r.status = r.flag_ids.some((id) => open.has(id)) ? "unresolved" : assumed ? "assumed" : "ok";
    r.chip = r.status === "unresolved" ? "unresolved" : r.scaled ? "scaled" : r.status;
    r.h = r._base.h; r.qty = r._base.qty;
    for (const id of r.flag_ids) {
      const f = byId.get(id);
      if (f?.status === "resolved" && /height/i.test(f.input?.label || "") && Number(f.value) > 0 && r.h != null) {
        r.h = Number(f.value) / (f.input?.unit === "mm" || Number(f.value) > 100 ? 1000 : 1);
        r.qty = Math.round([r.nos, r.l, r.b, r.h].filter((v) => v != null).reduce((a, b) => a * b, 1) * 1000) / 1000;
      }
    }
  }
  for (const a of mb.abstract) {
    const rows = a.row_ids.map((id) => mb.rows.find((r) => r.id === id)).filter(Boolean);
    if (rows.length) a.qty = Math.round(rows.reduce((s, r) => s + r.qty, 0) * 1000) / 1000;
  }
  return mb;
}

function setMB(mb, source) {
  S.mb = mb;
  S.source = source;
  mb._rowIdx = new Map(mb.rows.map((r) => [r.id, r]));
  mb._itemRows = new Map();
  for (const r of mb.rows) { if (!mb._itemRows.has(r.item)) mb._itemRows.set(r.item, []); mb._itemRows.get(r.item).push(r); }
  S.viewer.flags = mb.flags;
  if (S.isPdf && S.sheet != null) { const c = mb.calibration?.[S.sheet]; S.viewer.scaleMM = c?.pt_per_mm || null; S.viewer.unitMM = c?.pt_per_mm || null; }
  if (!mb.rows.length && S.tab === "mb") S.tab = mb.abstract.length ? "abstract" : "flags";
  indexRows();
  $("#modeBadge").hidden = source !== "mock";
  const n = $("#notice");
  if (source === "build") {
    n.hidden = false;
    n.replaceChildren("This server does not serve the row-level book yet (", el("code", {}, "GET /api/jobs/{id}/mb"), "). Showing the abstract, rooms and doors & windows from the build. ",
      el("a", { href: `?mock=1${location.hash}` }, "Preview with sample rows"), ".");
  } else if (source === "mock") {
    n.hidden = false;
    n.replaceChildren("Sample quantities: a saved engine run on the sample house (samples/sample-house.dxf). Flags ",
      S.offline ? "are simulated." : "come from the real checks on this job.");
  } else n.hidden = true;
  updateXlsx();
  if (S.isPdf) renderSheetStrip();
}

/** owner entity -> MB row ids, for click-an-entity reverse lookup. */
function indexRows() {
  const v = S.viewer, map = new Map();
  for (const r of S.mb.rows) {
    r._ents = [];
    for (const ref of r.refs) for (const i of v.refEntities(ref)) {
      r._ents.push(i);
      const o = v.owner[i];
      if (!map.has(o)) map.set(o, new Set());
      map.get(o).add(r.id);
    }
  }
  S.rowsByOwner = map;
}

// ================================================================ selection + traceability
function groupsFor(kind, key) {
  const mb = S.mb;
  if (!mb) return [];
  const sg = structGroups(kind, key);
  if (sg) return sg;
  if (kind === "row") {
    const r = rowById(key);
    if (!r) return [];
    return [{ color: tradeOf(r.trade).d, refs: r.refs, label: { title: `${r.qty >= 0 ? "+" : ""}${fq(r.qty)} ${unit(r.unit)}`, sub: [r.desc, r.location].filter(Boolean).join(" · ") } }];
  }
  if (kind === "abstract") {
    const a = mb.abstract.find((x) => String(x.sno) === key);
    if (!a) return [];
    const rows = a.row_ids.map(rowById).filter(Boolean);
    return [{ color: tradeOf(a.trade).d, refs: rows.flatMap((r) => r.refs), label: { title: `${fq(a.qty)} ${unit(a.unit)}`, sub: `${a.item} · ${rows.length} lines` } }];
  }
  if (kind === "room") {
    const r = mb.rooms[Number(key)];
    return r ? [{ color: SIGNAL, refs: r.refs, label: { title: r.area != null ? `${fq(r.area)} m²` : r.name, sub: `${r.name}${r.label_size ? ` · label ${r.label_size}` : ""}` } }] : [];
  }
  if (kind === "opening") {
    const o = mb.openings[Number(key)];
    return o ? [{ color: tradeOf("Doors & windows").d, refs: o.refs, label: { title: `${o.type} × ${o.nos}`, sub: `${o.w ? `${fmt(o.w, 0)} × ${fmt(o.h, 0)} mm` : ""} · ${o.location || ""}` } }] : [];
  }
  if (kind === "dim") {
    const d = mb.dimensions[Number(key)];
    return d ? [{ color: "#9fe3ff", refs: d.refs, label: { title: `${fmt(d.value, 1)} mm`, sub: `${d.region || ""} · ${d.layer || ""}` } }] : [];
  }
  if (kind === "flag") {
    const f = mb.flags.find((x) => x.id === key);
    return f ? [{ color: SEV[f.severity]?.d || "#ff6b57", refs: f.refs, label: { title: SEV[f.severity]?.label || f.severity, sub: f.title } }] : [];
  }
  return [];
}

function select(kind, key, { from = "table", fly = true } = {}) {
  hidePick();
  if (kind !== "entity") S.linked = new Set(kind === "row" && from === "drawing-list" ? S.linked : []);
  S.sel = kind ? { kind, key } : null;
  const v = S.viewer;
  v.selectedFlag = kind === "flag" ? key : null;
  const groups = kind ? groupsFor(kind, key) : [];
  // PDF sets: jump to the sheet the item is on, then zoom there
  let onSheet = null;
  if (S.isPdf && groups.length) {
    onSheet = targetSheet(groups);
    if (onSheet != null && S.sheet !== onSheet && (S.sheet != null || fly)) setSheet(onSheet, { fit: false });
  }
  v.setSelection(groups);
  v.marks = groups.flatMap((g) => g.marks || []);
  let boxes = groups.flatMap((g) => g.focus || g.refs.map((r) => r.bbox).filter(Boolean));
  if (onSheet != null && S.sheet != null) {
    const sb = sheetBox(S.sheet);
    const inside = boxes.filter((b) => b[0] < sb[2] && b[2] > sb[0] && b[1] < sb[3] && b[3] > sb[1]);
    if (inside.length) boxes = inside;
  }
  if (fly && boxes.length) {
    const b = unionBox(boxes);
    const big = Math.max(b[2] - b[0], b[3] - b[1]);
    const mm = v.mmUnit();
    v.focus(b, { pad: big > 40000 * mm ? 1.15 : 1.7, minW: 7000 * mm });
  }
  applySelection(from !== "table-click");
  renderFoot();
  if (isFs()) renderFsBar();
}

function preview(kind, key) {
  if (!S.viewer) return;
  if (!kind || (S.sel && S.sel.kind === kind && S.sel.key === key)) { S.viewer.setPreview(null); return; }
  const g = groupsFor(kind, key)[0];
  S.viewer.setPreview(g && g.refs.length ? g : null);
}

function onPick(i, sx, sy) {
  if (S.step !== "mb" || !S.mb) {
    if (i >= 0) {
      const L = S.viewer.layers[S.viewer.E[i].l];
      toast(`${L.name} · role ${S.cfg.layer_roles[L.name] || "ignore"}`);
    }
    return;
  }
  if (i < 0) { select(null); hidePick(); S.linked = new Set(); applySelection(false); return; }
  const v = S.viewer, e = v.E[i], o = v.owner[i];
  const ids = [...(S.rowsByOwner.get(o) || [])];
  const rows = ids.map(rowById).filter(Boolean).sort((a, b) => S.mb.rows.indexOf(a) - S.mb.rows.indexOf(b));
  S.linked = new Set(ids);
  S.sel = { kind: "entity", key: String(o) };
  v.selectedFlag = null;
  v.setSelection([{ color: SIGNAL, entities: v.family(i), refs: [], label: null }]);
  if (S.tab !== "mb") { S.tab = "mb"; renderLedger(); } else applySelection(false);
  // scroll the first linked row into view
  const first = rows[0] && $(`#scroller [data-kind="row"][data-key="${CSS.escape(rows[0].id)}"]`);
  if (!S.compact) first?.scrollIntoView({ block: "center", behavior: "smooth" });
  renderFoot();
  if (isFs()) renderFsBar();
  showPick(e, rows, sx, sy);
}

function entityLabel(e) {
  const L = S.viewer.layers[e.l];
  const T = { L: "Line", P: "Polyline", A: "Arc", C: "Circle", T: "Text", H: "Hatch", D: "Dimension", I: "Block" }[e.t] || e.t;
  const parent = e.v && e.pi != null ? S.viewer.E[e.pi] : null;
  const what = parent ? `${T} in ${parent.t === "I" ? `block ${parent.n}` : parent.t === "D" ? `dimension ${fmt(parent.m, 0)}` : parent.t}` : T;
  const txt = e.t === "T" ? ` “${e.s.replace(/\n/g, " ").slice(0, 40)}”` : "";
  const h = e.handle ?? (parent || e).hd;
  const hd = h ? ` · handle ${h}` : "";
  return { title: `${what}${txt}`, sub: `${L?.name || ""}${hd}` };
}

function showPick(e, rows, sx, sy) {
  const pop = $("#pickpop");
  const { title, sub } = entityLabel(e);
  const list = rows.length
    ? el("ul", {}, rows.map((r) => el("li", {}, el("button", {
      type: "button", onclick: () => { select("row", r.id, { from: "drawing-list" }); S.linked = new Set(rows.map((x) => x.id)); applySelection(true); renderFoot(); },
    }, el("span", { class: "tb", style: { background: tradeOf(r.trade).p } }),
    el("span", { class: "d" }, `${r.sno}  ${r.desc || r.item}`, el("small", {}, `${r.trade} · ${r.location}`)),
    el("span", { class: "q" }, `${fq(r.qty)} ${unit(r.unit)}`)))))
    : el("p", { class: "none" }, "No MB line uses this entity. It is not measured, or it sits outside every line’s drawing reference.");
  pop.replaceChildren(el("header", {}, el("div", {}, el("b", {}, title), el("br"), el("span", {}, sub)),
    el("span", {}, rows.length ? `${rows.length} line${rows.length > 1 ? "s" : ""}` : ""),
    el("button", { class: "pp-close", type: "button", "aria-label": "Close the list", onclick: hidePick }, svgIcon("M5 5l10 10M15 5 5 15"))), list);
  pop.hidden = false;
  if (S.compact) { pop.scrollTop = 0; pop.querySelector("li button, .pp-close")?.focus({ preventScroll: true }); return; }
  const stage = $("#stage").getBoundingClientRect();
  const w = 340, h = Math.min(320, pop.scrollHeight);
  let x = sx + 16, y = sy + 12;
  if (x + w > stage.width - 8) x = sx - w - 16;
  if (y + h > stage.height - 170) y = Math.max(8, stage.height - 170 - h);
  pop.style.left = `${Math.max(8, x)}px`; pop.style.top = `${Math.max(8, y)}px`;
}
function hidePick() { $("#pickpop").hidden = true; }
const svgIcon = (d) => { const t = document.createElement("template"); t.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="${d}"/></svg>`; return t.content.firstChild; };

// ================================================================ phone: full-screen drawing + summary
/** Tap on a card (phone): select it and show it on the drawing, full screen. */
function tapCard(kind, key) {
  select(kind, key, { from: "card", fly: false });
  const g = groupsFor(kind, key);
  if (!g.some((x) => x.refs?.length || x.focus?.length || x.entities?.length)) { toast("This line has no place on the drawing."); return; }
  openFs();
  requestAnimationFrame(() => select(kind, key, { from: "card", fly: true }));
}

function openFs() {
  if (!S.compact || isFs() || !S.viewer) return;
  document.body.classList.add("fs");
  history.pushState({ naapFs: true }, "", location.href);
  renderFsBar();
  $("#fsClose").focus({ preventScroll: true });
}

function closeFs({ silent = false, fromHistory = false } = {}) {
  if (!isFs()) return;
  document.body.classList.remove("fs");
  hidePick();
  if (S.viewer) S.viewer.insetBottom = 0;
  // the full-screen view has its own history entry, so Back closes it; closed by the app, drop the marker
  if (!fromHistory && history.state?.naapFs) { if (silent) history.replaceState(null, "", location.href); else history.back(); }
  if (silent) return;
  // the small view: back to the selection (or the whole drawing) at its own size
  requestAnimationFrame(() => {
    const v = S.viewer;
    if (!v) return;
    const boxes = S.sel && S.sel.kind !== "entity" ? groupsFor(S.sel.kind, S.sel.key).flatMap((g) => g.focus || g.refs.map((r) => r.bbox).filter(Boolean)) : [];
    if (boxes.length) { const b = unionBox(boxes), mm = v.mmUnit(); v._setView(...Object.values(v._target(b, 1.5, 7000 * mm))); v.draw(); } else v.fit(homeBox() || v.home, false);
  });
  const node = S.sel && $(`#scroller [data-kind="${S.sel.kind}"][data-key="${CSS.escape(S.sel.key)}"]`);
  node?.scrollIntoView({ block: "center" });
  (node?.querySelector(".k-main") || node || $("#btnFull"))?.focus({ preventScroll: true });
}

/** The selection's place in the list it came from (cards of the same kind). */
function fsSiblings() {
  if (!S.sel) return { list: [], i: -1 };
  const list = (S.visible || []).filter((x) => x.kind === S.sel.kind);
  return { list, i: list.findIndex((x) => x.key === S.sel.key) };
}
function fsStep(d) {
  const { list, i } = fsSiblings();
  const t = list[i + d];
  if (!t) return;
  select(t.kind, t.key, { from: "fs", fly: true });
}

function renderFsBar() {
  const bar = $("#fsBar");
  const sel = S.sel, { list, i } = fsSiblings();
  const arrow = (dir) => el("button", { class: "fs-nav", type: "button", disabled: dir < 0 ? i <= 0 : i < 0 || i >= list.length - 1,
    "aria-label": dir < 0 ? "Previous line" : "Next line", onclick: () => fsStep(dir) }, svgIcon(dir < 0 ? "m12 4-6 6 6 6" : "m8 4 6 6-6 6"));
  let body;
  if (S.step === "review") {
    const r = S.cfg?.regions?.find((x) => x.id === S.viewer?.selectedRegion);
    body = el("div", { class: "fs-sum" }, r ? el("div", { class: "k-head" }, el("span", { class: "k-desc" }, r.name)) : null,
      el("span", { class: "k-loc" }, r ? `${r.include ? "Measured" : "Not measured"} · ${r.kind || ""} · ${r.count ?? ""} entities` : `${S.touch ? "Tap" : "Click"} a view to select it. Pinch or scroll to zoom.`));
  } else if (!sel) body = el("div", { class: "fs-sum" }, el("span", { class: "k-loc" }, S.touch ? "Tap the drawing to list the MB lines that use what you tapped." : "Click the drawing to list the MB lines that use it."));
  else if (sel.kind === "entity") body = el("div", { class: "fs-sum" }, el("div", { class: "k-head" }, el("span", { class: "k-desc" }, `${S.linked.size} MB line${S.linked.size === 1 ? "" : "s"} use the picked entity`)));
  else if (sel.kind === "row" && rowById(sel.key)) {
    const r = rowById(sel.key);
    const prod = [r.nos, r.l, r.b, r.h].filter((v) => v != null).map((v) => fmt(v, 3)).join(" × ");
    const open = r.flag_ids.map((id) => S.mb.flags.find((f) => f.id === id)).filter((f) => f && f.status === "open");
    body = el("div", { class: `fs-sum${(r.nos ?? 1) < 0 || r.qty < 0 ? " neg" : ""}` },
      el("div", { class: "k-head" }, el("span", { class: "k-sno" }, r.sno), el("span", { class: "k-desc" }, r.desc || r.item)),
      el("div", { class: "k-loc" }, [r.desc ? r.item : "", r.location].filter(Boolean).join(" · ")),
      el("div", { class: "k-f" }, prod ? `${prod} = ` : "", el("b", {}, fq(r.qty)), ` ${unit(r.unit)}`),
      el("div", { class: "fs-pos" }, chipEl(r.chip || r.status), open.length ? el("span", {}, `${open.length} open flag${open.length > 1 ? "s" : ""}`) : null, i >= 0 ? el("span", {}, `${i + 1} of ${list.length}`) : null));
    bar.style.setProperty("--tc", tradeOf(r.trade).p);
  } else {
    const g = groupsFor(sel.kind, sel.key)[0];
    body = el("div", { class: "fs-sum" },
      el("div", { class: "k-head" }, el("span", { class: "k-desc" }, g?.label?.title || "")),
      el("div", { class: "k-loc" }, g?.label?.sub || ""),
      el("div", { class: "fs-pos" }, i >= 0 ? el("span", {}, `${i + 1} of ${list.length}`) : null));
    bar.style.removeProperty("--tc");
  }
  if (sel?.kind !== "row") bar.style.removeProperty("--tc");
  bar.replaceChildren(arrow(-1), body, arrow(1));
  const h = bar.offsetHeight || 110;
  document.body.style.setProperty("--fsb", `${h}px`);
  if (S.viewer) S.viewer.insetBottom = h;
}
const chipEl = (st) => el("span", { class: `chip ${st}` }, { ok: "OK", assumed: "Assumed", unresolved: "Unresolved", scaled: "Scaled" }[st] || st);

// swipe the summary left or right for the next or previous line
(() => {
  const bar = $("#fsBar");
  let p0 = null;
  bar.addEventListener("pointerdown", (e) => { if (!e.target.closest("button")) p0 = { x: e.clientX, y: e.clientY, t: performance.now() }; });
  bar.addEventListener("pointerup", (e) => {
    if (!p0) return;
    const dx = e.clientX - p0.x, dy = e.clientY - p0.y;
    p0 = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) fsStep(dx < 0 ? 1 : -1);
  });
  bar.addEventListener("pointercancel", () => { p0 = null; });
})();
$("#fsClose").addEventListener("click", () => closeFs());
$("#btnFull").addEventListener("click", () => openFs());
$("#btnMini").addEventListener("click", () => {
  const off = document.body.classList.toggle("mini-off");
  $("#btnMini").setAttribute("aria-expanded", String(!off));
  try { localStorage.setItem("naap.miniOff", off ? "1" : ""); } catch { /* storage off */ }
  if (!off) requestAnimationFrame(() => S.viewer?.resize());
});
try { if (localStorage.getItem("naap.miniOff")) { document.body.classList.add("mini-off"); $("#btnMini").setAttribute("aria-expanded", "false"); } } catch { /* storage off */ }

// ================================================================ phone: overflow menu (job, export, save)
function closeMore() {
  if ($("#moreMenu").hidden) return;
  $("#moreMenu").hidden = true;
  $("#btnMore").setAttribute("aria-expanded", "false");
}
$("#btnMore").addEventListener("click", () => {
  const menu = $("#moreMenu");
  if (!menu.hidden) { closeMore(); return; }
  const item = (src, label) => (src.hidden ? null : el("button", { type: "button", "aria-disabled": src.getAttribute("aria-disabled") || null,
    onclick: () => { closeMore(); src.click(); } }, label));
  const job = S.job ? el("div", { class: "mm-job" }, el("b", {}, S.job.filename || ""), $("#jobName").textContent.replace(S.job.filename || "", "").replace(/^\s*·\s*/, "")) : null;
  menu.replaceChildren(...[job, item($("#btnXlsx"), "Export Excel (.xlsx)"), item($("#btnSaveProject"), "Save project (.naap.json)"),
    el("button", { type: "button", onclick: () => { closeMore(); resetUpload(); setStep("upload"); } }, "Open another drawing")].filter(Boolean));
  menu.hidden = false;
  $("#btnMore").setAttribute("aria-expanded", "true");
  menu.querySelector("button")?.focus();
});
$("#moreMenu").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeMore(); $("#btnMore").focus(); return; }
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const b = $$("button", $("#moreMenu")), i = b.indexOf(document.activeElement);
  b[(i + (e.key === "ArrowDown" ? 1 : -1) + b.length) % b.length]?.focus();
  e.preventDefault();
});
document.addEventListener("pointerdown", (e) => { if (!$("#moreMenu").hidden && !e.target.closest("#moreMenu, #btnMore")) closeMore(); });

// keep a focused input above the on-screen keyboard
document.addEventListener("focusin", (e) => {
  if (!S.compact || !e.target.matches?.("input:not([type=checkbox]):not([type=file]), select, textarea")) return;
  setTimeout(() => e.target.scrollIntoView({ block: "center", behavior: "smooth" }), 320);
});

function setTab(t) {
  S.tab = t;
  renderLedger();
  $("#scroller").scrollTop = 0;
  applySelection(true);
}

function showRows(ids) {
  S.tab = "mb"; S.status = "all"; S.trade = ""; S.q = ""; $("#q").value = "";
  S.linked = new Set(ids);
  renderLedger();
  if (ids[0]) select("row", ids[0], { from: "drawing-list" });
  S.linked = new Set(ids);
  applySelection(true); renderFoot();
}

// ================================================================ flags
async function resolveFlag(f, status, value) {
  const flags = { ...(S.cfg.flags || {}) };
  if (status === "open") delete flags[f.id]; else flags[f.id] = { status, value: value ?? null };
  S.cfg.flags = flags;
  const verb = status === "open" ? "Reopened" : status === "accepted" ? "Accepted" : "Resolved";
  const li = $(`#scroller li.flag[data-key="${CSS.escape(f.id)}"]`);
  li?.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
  const sb = li?.querySelector("button[type=submit]");
  if (sb) sb.textContent = "Re-measuring…";
  $("#foot").prepend(el("span", { class: "busy" }, "Re-measuring with the new value…"));
  // row ids are stable across engine runs
  const rowKey = (r) => r.id;
  const before = new Map(S.mb.rows.map((r) => [rowKey(r), r.qty]));
  const items = new Set(S.mb.rows.filter((r) => r.flag_ids.includes(f.id)).map((r) => r.item));
  const absBefore = new Map(S.mb.abstract.map((a) => [a.item, a.qty]));
  const prevSel = S.sel;
  try {
    if (S.offline) {
      setMB(mockEngine(S.mb), "mock");
    } else {
      await saveConfig();
      S.dirtyAt = Date.now();
      // GET /mb re-runs the engine because the saved config is newer than its cache
      const res = await loadMB(S.jobId, S.cfg);
      let mb = res.data;
      if (MOCK) mb = mockEngine(await withLiveFlags(mb));
      setMB(mb, res.source);
    }
    if (status !== "open" && S.flagStatus === "open") S.flagStatus = "all"; // keep the resolved flag in view
    renderLedger();
    if (prevSel?.kind === "flag" && prevSel.key !== f.id) reselect(prevSel);
    else select("flag", f.id, { from: "table", fly: false });
    const changed = S.mb.rows.filter((r) => !before.has(rowKey(r)) || Math.abs(before.get(rowKey(r)) - r.qty) > 5e-4).length;
    for (const r of S.mb.rows) if (r.flag_ids.includes(f.id)) items.add(r.item);
    const totals = S.mb.abstract.filter((a) => items.has(a.item) && Math.abs((absBefore.get(a.item) ?? 0) - a.qty) > 5e-4).slice(0, 2)
      .map((a) => `${a.item}: ${fq(absBefore.get(a.item) ?? 0)} → ${fq(a.qty)} ${unit(a.unit)}`);
    S.lastChange = { flag: f.id, changed, totals };
    toast(`${verb}: ${f.title}.${changed ? ` ${changed} MB line${changed > 1 ? "s" : ""} changed.` : " Quantities unchanged."}${totals.length ? ` ${totals.join("; ")}.` : ""}`);
  } catch (e) {
    toast(`Could not save: ${e.message}`, true);
    renderLedger();
  }
}

// ================================================================ config autosave (review)
let saveT = null;
async function saveConfig() {
  clearTimeout(saveT);
  if (S.offline || !S.jobId || !S.cfg) return;
  $("#saveState").textContent = "Saving…";
  try {
    await putConfig(S.jobId, S.cfg);
    S.savedAt = Date.now();
    $("#saveState").textContent = "All changes saved";
  } catch (e) { $("#saveState").textContent = `Not saved: ${e.message}`; throw e; }
}
function cfgChanged() {
  S.dirtyAt = Date.now();
  $("#saveState").textContent = "Unsaved changes";
  clearTimeout(saveT);
  saveT = setTimeout(() => saveConfig().catch(() => {}), 700);
  updateXlsx();
}

// ================================================================ Excel export
function updateXlsx() {
  const a = $("#btnXlsx");
  if (S.offline) { a.setAttribute("aria-disabled", "true"); a.title = "Upload a drawing to export its workbook"; a.removeAttribute("href"); return; }
  a.removeAttribute("aria-disabled");
  a.href = xlsxHref(S.jobId);
  a.title = MOCK ? "Downloads the engine's workbook for this job (not the sample quantities)" : "Download the MB workbook (.xlsx)";
}
$("#btnXlsx").addEventListener("click", async (e) => {
  e.preventDefault();
  if (S.offline) { toast("The sample book has no workbook. Upload a drawing to export one."); return; }
  try {
    if (!S.builtAt || S.dirtyAt > S.builtAt) { toast("Building the workbook…"); await runBuild(S.jobId, S.cfg); S.builtAt = Date.now(); }
    try { await downloadXlsx(S.jobId); } catch (err) {
      if (err.status !== 404) throw err;
      toast("Building the workbook…");
      await runBuild(S.jobId, S.cfg); S.builtAt = Date.now();
      await downloadXlsx(S.jobId);
    }
  } catch (err) { toast(`Export failed: ${err.message}`, true); }
});

// ================================================================ projects (.naap.json, in-browser version)
$("#btnSaveProject").addEventListener("click", async () => {
  if (!S.jobId) return;
  const b = $("#btnSaveProject");
  b.disabled = true;
  try {
    if (!S.offline && S.dirtyAt > (S.savedAt || 0)) await saveConfig();
    // the book goes in the file when it matches the answers, so it opens on a phone without the engine
    if (S.mb && !S.offline && S.dirtyAt > S.builtAt) { await loadMB(S.jobId, S.cfg); S.builtAt = Date.now(); }
    await saveProject(S.jobId);
    toast("Project saved. Open it here or in another browser to carry on (Open a saved project, on the upload page).");
  } catch (e) { toast(`Could not save the project: ${e.message}`, true); } finally { b.disabled = false; }
});

async function openProjectFile(file) {
  $("#upErr").hidden = true;
  $("#dropzone").classList.add("busy");
  setTB({ name: file.name, size: `${(file.size / 1e6).toFixed(1)} MB`, units: "—", ents: "—", layers: "—", views: "—", time: "—" });
  renderStages("uploading", "dxf", { msg: "Opening the project…" });
  try {
    const { id, has_result: book } = await openProject(file, (p) => renderStages("uploading", "dxf", p));
    $("#stages").hidden = true;
    $("#dropzone").classList.remove("busy");
    await openJob(id, book ? "mb" : "review");
  } catch (e) { renderStages("error", "dxf"); upErr(e.message, { oom: e.oom }); }
}
$("#projectInput").addEventListener("change", (e) => { if (e.target.files[0]) openProjectFile(e.target.files[0]); e.target.value = ""; });
$("#btnOpenProject").addEventListener("click", (e) => { e.preventDefault(); $("#projectInput").click(); });

// ================================================================ toolbar, layers, splitter
$("#tFit").addEventListener("click", () => S.viewer?.fit(homeBox() || S.viewer.home));
const toggle = (btn, fn) => btn.addEventListener("click", () => { const on = btn.getAttribute("aria-pressed") !== "true"; btn.setAttribute("aria-pressed", String(on)); fn(on); S.viewer?.draw(); });
toggle($("#tText"), (on) => { S.viewer.showText = on; });
toggle($("#tColour"), (on) => { S.viewer.colourMode = on ? "cad" : "mono"; S.viewer._miniDirty = true; });
toggle($("#tFlags"), (on) => { S.viewer.showMarkers = on; });
$("#tLayers").addEventListener("click", () => {
  const pop = $("#layerPop"), open = pop.hidden;
  pop.hidden = !open;
  $("#tLayers").setAttribute("aria-expanded", String(open));
  if (open) { renderLayerPop(); $("#lpFilter").focus(); }
});

function renderLayerPop() {
  if (!S.viewer || $("#layerPop").hidden) return;
  const q = $("#lpFilter").value.trim().toLowerCase();
  const cad = S.viewer.colourMode === "cad";
  $("#lpList").replaceChildren(...S.viewer.layers.filter((L) => L.n && (!q || L.name.toLowerCase().includes(q))).map((L) => el("li", {},
    el("label", { onmouseenter: () => previewLayer(L.i), onmouseleave: () => previewLayer(null) },
      el("input", { type: "checkbox", checked: L.visible, onchange: (e) => setLayerVisible(L, e.target.checked) }),
      el("span", { class: "sw", style: { background: cad ? L.color : "#c9dcea", opacity: cad ? 1 : L.role === "ignore" ? 0.4 : 1 } }),
      el("span", { class: "nm", title: `${L.name} (${S.cfg?.layer_roles?.[L.name] || "ignore"})` }, L.name),
      el("span", { class: "ct" }, L.n)))));
}
$("#lpFilter").addEventListener("input", renderLayerPop);
$("#lpAll").addEventListener("click", () => { S.viewer.layers.forEach((L) => setLayerVisible(L, true, false)); afterLayers(); });
$("#lpNone").addEventListener("click", () => { S.viewer.layers.forEach((L) => setLayerVisible(L, false, false)); afterLayers(); });

function setLayerVisible(L, on, redraw = true) {
  L.visible = on;
  if (S.cfg) { S.cfg.layer_style[L.name] = { ...(S.cfg.layer_style[L.name] || {}), visible: on }; }
  if (redraw) afterLayers();
}
function afterLayers() {
  S.viewer._miniDirty = true; S.viewer.draw(); renderLayerPop();
  if (S.step === "review") renderReview();
  if (!S.offline) cfgChanged();
}
function previewLayer(i) {
  if (i == null) { S.viewer.setPreview(null); return; }
  const v = S.viewer, ents = [];
  for (let k = 0; k < v.E.length; k++) if (v.E[k].l === i && "LPACHT".includes(v.E[k].t)) ents.push(k);
  v.setPreview({ color: SIGNAL, entities: ents, refs: [] });
}

function focusRegion(id) {
  const r = S.cfg.regions.find((x) => x.id === id);
  if (!r) return;
  S.viewer.selectedRegion = id;
  if (S.isPdf && r.sheet != null) { setSheet(r.sheet); renderReview(); return; }
  S.viewer.fit(r.bbox);
  renderReview();
}

// splitter
(() => {
  const sp = $("#splitter"), work = $("#work");
  const saved = Number(localStorage.getItem("dwgmb.split"));
  if (saved >= 25 && saved <= 75) work.style.setProperty("--split", `${saved}%`);
  const set = (pct) => {
    pct = Math.max(25, Math.min(75, pct));
    work.style.setProperty("--split", `${pct}%`);
    sp.setAttribute("aria-valuenow", String(Math.round(pct)));
    localStorage.setItem("dwgmb.split", String(pct));
  };
  sp.addEventListener("pointerdown", (e) => {
    sp.setPointerCapture(e.pointerId); sp.classList.add("drag");
    const r = work.getBoundingClientRect();
    const move = (ev) => set(((ev.clientX - r.left) / r.width) * 100);
    const up = () => { sp.classList.remove("drag"); sp.removeEventListener("pointermove", move); sp.removeEventListener("pointerup", up); };
    sp.addEventListener("pointermove", move); sp.addEventListener("pointerup", up);
  });
  sp.addEventListener("keydown", (e) => {
    const cur = parseFloat(getComputedStyle(work).getPropertyValue("--split")) || 52;
    if (e.key === "ArrowLeft") set(cur - 3); else if (e.key === "ArrowRight") set(cur + 3); else return;
    e.preventDefault();
  });
})();

// ================================================================ ledger controls + keyboard
$("#q").addEventListener("input", debounce((e) => { S.q = e.target.value.trim(); renderLedger(); }, 120));
$("#tradeSel").addEventListener("change", (e) => { S.trade = e.target.value; renderLedger(); });
$$("#rvTabs button").forEach((b) => b.addEventListener("click", () => { S.rvTab = b.dataset.tab; renderReview(); }));

function tablistKeys(container, get, set) {
  container.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const tabs = $$("[role=tab]", container), i = tabs.findIndex((t) => t.dataset.tab === get());
    const n = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    set(n.dataset.tab); container.querySelector(`[data-tab="${n.dataset.tab}"]`)?.focus();
    e.preventDefault();
  });
}
tablistKeys($("#mbTabs"), () => S.tab, setTab);
tablistKeys($("#rvTabs"), () => S.rvTab, (t) => { S.rvTab = t; renderReview(); });

window.addEventListener("keydown", (e) => {
  if (S.step === "upload") return;
  const ae = document.activeElement;
  const typing = ae && (ae.matches("input, select, textarea") || ae.isContentEditable);
  if (e.key === "Escape") {
    if (!$("#guard").hidden) return;
    if (!$("#moreMenu").hidden) { closeMore(); $("#btnMore").focus(); return; }
    if (S.viewer?.measure) { cancelCalibrate(); return; }
    if (!$("#pickpop").hidden && isFs()) { hidePick(); return; }
    if (isFs()) { closeFs(); return; }
    if (!$("#layerPop").hidden) { $("#layerPop").hidden = true; $("#tLayers").setAttribute("aria-expanded", "false"); return; }
    hidePick();
    if (typing) { ae.blur(); return; }
    S.linked = new Set(); select(null); return;
  }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/" && S.step === "mb") { e.preventDefault(); $("#q").focus(); return; }
  if (e.key === "f" || e.key === "F") { S.viewer?.fit(homeBox() || S.viewer.home); return; }
  if (S.isPdf && (e.key === "[" || e.key === "]")) {
    const ns = S.geo.sheets.map((x) => x.sheet), i = ns.indexOf(S.sheet);
    setSheet(ns[(i + (e.key === "]" ? 1 : -1) + ns.length) % ns.length]);
    return;
  }
  if (isFs() && (e.key === "ArrowLeft" || e.key === "ArrowRight") && ae !== $("#canvas")) { e.preventDefault(); fsStep(e.key === "ArrowRight" ? 1 : -1); return; }
  if (S.step !== "mb" || ae === $("#canvas") || ae === $("#splitter") || ae?.getAttribute("role") === "tab") return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "j" || e.key === "k") {
    const vis = S.visible || [];
    if (!vis.length) return;
    e.preventDefault();
    let i = S.sel ? vis.findIndex((x) => x.kind === S.sel.kind && x.key === S.sel.key) : -1;
    i = e.key === "ArrowDown" || e.key === "j" ? Math.min(vis.length - 1, i + 1) : Math.max(0, i - 1);
    const t = vis[i];
    select(t.kind, t.key, { from: "keys" });
    const node = $(`#scroller [data-kind="${t.kind}"][data-key="${CSS.escape(t.key)}"]`);
    node?.focus({ preventScroll: true });
  }
});

// compact table layout when the ledger is narrow
PHONE.addEventListener("change", () => {
  S.compact = PHONE.matches;
  if (!S.compact) closeFs({ silent: true });
  if (S.step === "mb" && S.mb) { renderLedger(); applySelection(true); }
  S.viewer?.resize();
});

new ResizeObserver(() => {
  const w = $("#ledger").clientWidth;
  if (!w) return;
  const n = w < 880;
  if (n !== S.narrow) {
    S.narrow = n;
    $("#table").classList.toggle("narrow", n);
    if (S.step === "mb" && S.mb) { renderLedger(); applySelection(true); }
  }
}).observe($("#ledger"));

window.__dwgmb = S; // for debugging in the console
registerSW();   // the built site (local mode) caches the runtime for offline and repeat visits

// ================================================================ actions + boot
Object.assign(A, {
  select, preview, setTab, resolveFlag, showRows, cfgChanged, focusRegion, previewLayer, setLayerVisible, tapCard, closeFs,
  setSheet, sheetName, startCalibrate, applyCalibration,
  rolesChanged: () => { S.viewer.setRoles(S.cfg.layer_roles); cfgChanged(); },
});

async function bootOffline() {
  S.offline = true;
  ensureViewer();
  setStep("mb");
  $("#stageMsg").hidden = false; $("#stageMsg").textContent = "Loading the sample drawing…";
  const [geo, cfg] = await Promise.all([api("mock/geometry.json"), api("mock/config.json")]);
  await useDrawing({ filename: "sample-house.dxf (sample)", id: null }, geo, cfg);
  await buildAndShow({ rebuild: false });
}

(async function boot() {
  try {
    S.meta = await api("/api/meta");
    $("#maxMb").textContent = S.meta.max_upload_mb;
    if (LOCAL) $("#upPrivacy").textContent = "Naap runs in this browser: the drawing is read here and never uploaded.";
    $("#projectOpen").hidden = !CAN_PROJECTS;
  } catch { /* no server: upload disabled, mock still works */ }
  if (MOCK) $("#mockHint").hidden = true;
  resetUpload();
  const m = location.hash.match(/job=([0-9a-f]{32})(?:&step=(\w+))?/);
  const crashed = LOCAL ? lastReading() : null;
  try {
    if (m && S.meta) { await resume(m[1], m[2] || "review"); if (crashed) clearReading(); return; }
    if (MOCK) { await bootOffline(); return; }
  } catch (e) { upErr(e.status === 404 ? "That job is not in this browser any more (or the link is from another browser). Open the drawing or a saved project again." : e.message); setStep("upload", { nav: false }); return; }
  setStep("upload", { nav: false });
  if (!S.meta) upErr(UNREACHABLE);
  else if (crashed) {
    clearReading();
    upErr(`Naap was reading “${crashed.name}” (${(crashed.size / 1e6).toFixed(1)} MB) when this page stopped or was reloaded. If the page crashed, the drawing is too big for this device's memory.`, { oom: true });
  }
})();

window.addEventListener("popstate", () => {
  if (isFs()) { closeFs({ fromHistory: true }); return; }
  const m = location.hash.match(/job=([0-9a-f]{32})(?:&step=(\w+))?/);
  const step = m?.[2] || "upload";
  if (m && m[1] === S.jobId && step === S.step) return;   // e.g. the full-screen entry was popped
  if (m && S.polling === m[1]) return;                    // still reading that drawing
  if (!m || step === "upload") { if (S.step !== "upload") { resetUpload(); setStep("upload", { nav: false }); } return; }
  if (m[1] !== S.jobId || !S.geo) { resume(m[1], step).catch((e) => upErr(e.message)); return; }
  if (step === "mb" && S.mb) setStep("mb", { nav: false });
  else if (step === "mb" && S.geo) buildAndShow({ rebuild: false, save: false, fallback: "review" });
  else setStep("review", { nav: false });
});
