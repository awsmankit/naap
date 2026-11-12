// The Convert panel: PDF -> DXF and DXF/DWG -> PDF, reached from the Upload page. It makes a file, not a
// measurement book. Works through the same adapter as everything else (api.js: server or in-browser).

import { S } from "./state.js";
import { $, $$, el } from "./util.js";
import { LOCAL, convertFile, downloadConverted, warmup } from "./api.js";

const KINDS = {
  pdf2dxf: { accept: ".pdf", ext: ["pdf"], pick: "Choose a PDF", note: "A vector PDF (not a scan). Output is a DXF in millimetres.", out: "DXF" },
  dxf2pdf: { accept: ".dxf,.dwg", ext: ["dxf", "dwg"], pick: "Choose a DXF or DWG", note: "", out: "PDF" },
};
let kind = "pdf2dxf", file = null, result = null, busy = false, hooks = {};

const val = (id) => $(`#${id}`).value;
const showErr = (msg) => { const b = $("#cvErr"); b.textContent = msg || ""; b.hidden = !msg; };

function setKind(k, focus = false) {
  kind = k;
  for (const t of $$("#cvTabs [role=tab]")) {
    const on = t.dataset.kind === k;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    if (on && focus) t.focus();
  }
  $("#cvPane").setAttribute("aria-labelledby", `cvTab-${k}`);
  $("#cvOptsPdf2dxf").hidden = k !== "pdf2dxf";
  $("#cvOptsDxf2pdf").hidden = k !== "dxf2pdf";
  $("#cvFile").accept = KINDS[k].accept;
  $("#cvFile").value = "";
  $("#cvPick").textContent = KINDS[k].pick;
  $("#cvKindNote").textContent = k === "dxf2pdf"
    ? (LOCAL ? "DXF, or a DWG (read in this browser with LibreDWG). Output is a PDF on the paper size you choose." : "A DXF or DWG. Output is a PDF on the paper size you choose.")
    : KINDS[k].note;
  pickFile(null);
}

function clearResult() { result = null; $("#cvResult").hidden = true; showErr(""); }

function pickFile(f) {
  file = f;
  $("#cvName").textContent = f ? `${f.name} (${(f.size / 1e6).toFixed(1)} MB)` : "No file chosen";
  $("#cvName").classList.toggle("has", !!f);
  $("#cvGo").disabled = !f || busy;
  clearResult();
}

/** The form fields for POST /api/convert/...; throws Error with a plain message when a field is wrong. */
function options() {
  const need = (id, label, test) => { const v = parseFloat(val(id)); if (!test(v)) throw new Error(`${label} is not a valid number.`); return v; };
  if (kind === "dxf2pdf") {
    return { paper: val("cvPaper"), orientation: val("cvOrient"), mono: val("cvColour") === "mono", layouts: val("cvLayout") };
  }
  const o = { tolerance: need("cvTol", "Curve tolerance", (v) => v >= 0.01 && v <= 10) };
  if (val("cvPages") === "one") o.page = need("cvPage", "Page number", (v) => Number.isInteger(v) && v >= 1);
  const sc = val("cvScale");
  if (sc === "auto") o.scale = "auto";
  else if (sc === "n") o.scale = `1:${need("cvN", "The scale", (v) => v > 0)}`;
  else {
    const [x1, y1, x2, y2] = ["cvX1", "cvY1", "cvX2", "cvY2"].map((id) => need(id, "Each point coordinate", Number.isFinite));
    const mm = need("cvMm", "The real distance", (v) => v > 0);
    if (x1 === x2 && y1 === y2) throw new Error("The two points must be different.");
    o.calib = [x1, y1, x2, y2, mm].join(",");
  }
  return o;
}

async function run(e) {
  e.preventDefault();
  if (!file || busy) return;
  const ext = file.name.toLowerCase().split(".").pop();
  if (!KINDS[kind].ext.includes(ext)) return showErr(kind === "pdf2dxf" ? "This tab takes a .pdf file. For a DXF or DWG use the other tab." : "This tab takes a .dxf or .dwg file. For a PDF use the other tab.");
  if (!file.size) return showErr(`“${file.name}” is empty (0 bytes).`);
  const max = ext === "pdf" ? S.meta?.max_pdf_mb || 100 : S.meta?.max_upload_mb || 300;
  if (file.size > max * 1024 * 1024) return showErr(`The file is ${(file.size / 1e6).toFixed(0)} MB; the limit is ${max} MB.`);
  let opts;
  try { opts = options(); } catch (err) { return showErr(err.message); }
  clearResult();
  if (LOCAL && hooks.guard && !(await hooks.guard(file))) return showErr("Not converted. Open Naap on a laptop or desktop with 8 GB of memory or more, or use the server version, for a file this big.");
  busy = true;
  $("#cvGo").disabled = true;
  $("#cvProg").hidden = false;
  $("#cvProgMsg").textContent = LOCAL ? "Starting the in-browser engine…" : "Uploading…";
  try {
    result = await convertFile(kind, file, opts, (p) => { $("#cvProgMsg").textContent = p.msg || "Converting…"; });
    renderResult(result);
  } catch (err) {
    showErr(err.message);
  } finally {
    busy = false;
    $("#cvProg").hidden = true;
    $("#cvGo").disabled = !file;
  }
}

function renderResult(r) {
  $("#cvSum").replaceChildren(...(r.summary || []).map((t) => el("li", {}, t)));
  $("#cvDwg").hidden = kind !== "pdf2dxf";
  $("#cvDownload").textContent = `Download ${KINDS[kind].out}`;
  $("#cvResult").hidden = false;
  $("#cvResH").tabIndex = -1;
  $("#cvResH").focus();
}

function show(on) {
  $("#view-convert").hidden = !on;
  $("#view-upload").hidden = on;
  document.title = on ? "Convert a drawing · Naap" : "Naap · Drawing to Measurement Book";
  if (on) warmup();
}

export function openConvert() { show(true); $("#cvTitle").tabIndex = -1; $("#cvTitle").focus({ preventScroll: true }); }
export function closeConvert() { if (!$("#view-convert").hidden) show(false); }

/** hooks.guard(file) -> Promise<boolean>: the device-memory check for big files in the browser. */
export function initConvert(h = {}) {
  hooks = h;
  $("#cvPrivacy").textContent = LOCAL ? "It runs in this browser; the file does not leave this computer." : "";
  $("#btnConvert").addEventListener("click", openConvert);
  $("#cvBack").addEventListener("click", () => { closeConvert(); $("#btnConvert").focus(); });
  $("#cvFile").addEventListener("change", (e) => { pickFile(e.target.files[0] || null); });
  $("#cvForm").addEventListener("submit", run);
  $("#cvDownload").addEventListener("click", () => result && downloadConverted(result));
  $("#cvPages").addEventListener("change", () => { $("#cvPage").hidden = val("cvPages") !== "one"; });
  $("#cvScale").addEventListener("change", () => {
    $("#cvScaleN").hidden = val("cvScale") !== "n";
    $("#cvCal").hidden = val("cvScale") !== "cal";
  });
  // tabs: roving focus, arrows switch
  const tabs = $$("#cvTabs [role=tab]");
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => setKind(t.dataset.kind));
    t.addEventListener("keydown", (e) => {
      const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      setKind(tabs[(to + tabs.length) % tabs.length].dataset.kind, true);
    });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#view-convert").hidden && $("#guard").hidden) { closeConvert(); $("#btnConvert").focus(); }
  });
  setKind("pdf2dxf");
}
