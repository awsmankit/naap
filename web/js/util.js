// Small DOM + formatting helpers.

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(n.dataset, v);
    else if (k in n && typeof v !== "string") n[k] = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) if (k != null && k !== false) n.append(k.nodeType ? k : document.createTextNode(String(k)));
  return n;
}

export function svg(markup) {
  const t = document.createElement("template");
  t.innerHTML = markup.trim();
  return t.content.firstChild;
}

const nf = new Map();
function fmtr(d, min) {
  const k = `${d}:${min}`;
  if (!nf.has(k)) nf.set(k, new Intl.NumberFormat("en-IN", { maximumFractionDigits: d, minimumFractionDigits: min }));
  return nf.get(k);
}
/** Number with Indian grouping and a real minus sign. */
export function fmt(v, d = 3, min = 0) {
  if (v == null || v === "" || Number.isNaN(Number(v))) return "";
  const s = fmtr(d, min).format(Math.abs(Number(v)));
  return Number(v) < 0 && s !== "0" ? `−${s}` : s;
}
export const fq = (v) => fmt(v, 3, 3);           // quantities: always 3 decimals
export const fd = (v) => fmt(v, 3, 0);           // dimensions: trim zeros

const UNIT = { m3: "m³", cum: "m³", "cu.m": "m³", m2: "m²", sqm: "m²", "sq.m": "m²", nos: "nos", no: "nos", rm: "m", rmt: "m" };
export const unit = (u) => UNIT[String(u || "").toLowerCase()] || u || "";

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

let toastT;
export function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", err);
  t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; }, err ? 6000 : 2600);
}

export const unionBox = (boxes) => boxes.reduce((b, r) => r ? [Math.min(b[0], r[0]), Math.min(b[1], r[1]), Math.max(b[2], r[2]), Math.max(b[3], r[3])] : b,
  [Infinity, Infinity, -Infinity, -Infinity]);
export const validBox = (b) => b && Number.isFinite(b[0]) && Number.isFinite(b[3]);

export const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
