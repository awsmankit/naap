// Review step: what each view is used for, floors per plan, heights per block, and the questions
// the engine would flag before an accurate book. Everything shown comes from POST /review
// (meta.review of an engine run, app/service.py); answers go into the config the way resolving a
// flag on the MB page does (config.flags[id] = {status, value}), plus the region fields the engine reads.

import { S, A } from "./state.js";
import { $, $$, el, fmt } from "./util.js";
import { ROLE_SWATCH } from "./theme.js";
import { runReview } from "./api.js";

const PURPOSE = { measured: "Measured", heights: "Used for heights", finishes: "Used for finishes", sizes: "Sizes", legend: "Legend", unused: "Not used" };
const mm = (v) => (v == null || v === "" ? "" : `${fmt(v, 0)} mm`);
const lvl = (v) => (v == null ? "" : `${v > 0 ? "+" : v < 0 ? "−" : "±"}${fmt(Math.abs(v), 0)}`);
// stable element ids (a re-render keeps keyboard focus on the same field)
const nid = (p, key) => `${p}-${String(key).replace(/[^A-Za-z0-9_-]/g, "_")}`;

// ---------------------------------------------------------------- the review summary
let reqN = 0;
/** Ask the engine what it uses (or reuse the result of the last build). */
export async function loadReview({ force = false } = {}) {
  if (!S.cfg) return;
  if (S.offline || !S.jobId) {
    S.review = S.mb?.meta?.review || null;
    S.reviewState = S.review ? "ok" : "none";
    renderReview();
    return;
  }
  if (!force && S.mb?.meta?.review && S.builtAt && S.dirtyAt <= S.builtAt) {
    S.review = S.mb.meta.review; S.reviewState = "ok"; S.reviewStale = false;
    renderReview();
    return;
  }
  const n = ++reqN;
  S.reviewState = "busy";
  renderAsk();
  try {
    const t0 = performance.now();
    const rv = await runReview(S.jobId, S.cfg);
    if (n !== reqN) return;
    S.review = rv; S.reviewState = rv.error ? "error" : "ok"; S.reviewErr = rv.error || "";
    S.reviewStale = false; S.reviewMs = performance.now() - t0;
  } catch (e) {
    if (n !== reqN) return;
    S.reviewState = "error"; S.reviewErr = e.message;
  }
  if (S.step === "review") renderReview();
}

let staleT = null;
/** Views or floors changed: the purposes and questions are re-read (automatically when that is quick). */
function structural() {
  S.reviewStale = true;
  A.cfgChanged();
  clearTimeout(staleT);
  if (!S.offline && (S.reviewMs ?? 99999) < 4000) staleT = setTimeout(() => loadReview({ force: true }), 1500);
  renderAsk();
}

const RV = () => S.review && !S.review.error ? S.review : null;
const viewInfo = (r) => RV()?.views?.find((v) => (S.isPdf ? v.sheet === r.sheet : v.id === r.id));
const planInfo = (r) => RV()?.plans?.find((p) => (S.isPdf ? p.sheet === r.sheet : p.id === r.id));
const measuredNow = (r) => !!r.include && (r.kind === "plan" || !!r.measure);
const answer = (id) => S.cfg.flags?.[id];
// the config is the source of truth for answers (the engine reads the same config.flags)
const isOpen = (q) => { const a = answer(q.id); return !a || a.status === "open"; };

function setFlag(id, status, value = null) {
  const flags = { ...(S.cfg.flags || {}) };
  if (status === "open") delete flags[id]; else flags[id] = { status, value };
  S.cfg.flags = flags;
}

export function blocked() {
  return !S.isPdf && S.cfg && !S.cfg.regions.some((r) => measuredNow(r));
}

// ---------------------------------------------------------------- render
export function renderReview() {
  if (!S.cfg) return;
  $$("#rvTabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === S.rvTab)));
  renderReviewCounts();
  const assigned = Object.values(S.cfg.layer_roles || {}).filter((r) => r && r !== "ignore").length;
  $("#nLayers").textContent = assigned;
  if (S.viewer && !S.isPdf) {
    S.viewer.regionMeasured = measuredNow;
    S.viewer.regionLabel = (r) => {
      const p = purposeNow(r).purpose;
      return p === "measured" ? r.name : `${r.name} (${PURPOSE[p].toLowerCase()})`;
    };
    S.viewer.draw();
  } else if (S.viewer) { S.viewer.regionMeasured = null; S.viewer.regionLabel = null; }
  const body = $("#rvBody");
  const keep = S.rvBodyTab === S.rvTab ? body.scrollTop : 0;
  S.rvBodyTab = S.rvTab;
  const act = document.activeElement;
  const inPanel = !!act?.closest?.("#reviewPanel") || act === document.body;
  const focusId = inPanel ? act.id : null;
  body.replaceChildren(...({ views, layers, heights })[S.rvTab]());
  renderAsk();
  body.scrollTop = keep;
  const again = focusId && document.getElementById(focusId);
  if (again) again.focus({ preventScroll: true });
  else if (inPanel && S.lastEdited) {
    // the focused control went away (e.g. a Confirm button once answered): go on from the edited field
    const all = $$("#reviewPanel input:not([disabled]), #reviewPanel button:not([disabled]), #reviewPanel select, #reviewPanel summary");
    const k = all.findIndex((n) => n.id === S.lastEdited);
    if (k >= 0) (all[k + 1] || all[k]).focus({ preventScroll: true });
  }
  S.lastEdited = null;
}

function renderReviewCounts() {
  const inc = S.isPdf ? S.cfg.regions.filter((r) => r.include).length : S.cfg.regions.filter(measuredNow).length;
  $("#nViews").textContent = `${inc}/${S.cfg.regions.length}`;
  const b = $("#btnBuild");
  const stop = blocked();
  b.setAttribute("aria-disabled", String(stop));
  b.title = stop ? "Tick at least one plan to measure" : "";
}

// ---------------------------------------------------------------- "Before you build"
function questionList() {
  const rv = RV();
  return rv?.questions || [];
}

function renderAsk() {
  const box = $("#rvAsk");
  if (!box || !S.cfg) return;
  const qs = questionList();
  const open = qs.filter(isOpen);
  const st = S.reviewState;
  const head = el("div", { class: "ask-head" });
  const listId = "askList";
  const expanded = S.askOpen ?? (open.length > 0 && !S.compact);   // phones: a one-line summary until tapped
  if (blocked()) {
    const regs = S.cfg.regions || [];
    const msg = !regs.length
      ? [el("b", {}, "Nothing to measure."), " Naap found no views in this drawing (no plan, section or schedule). Check that it is the right file."]
      : !regs.some((r) => r.kind === "plan" || r.measure)
        ? [el("b", {}, "No plan found."), " None of the views is a plan, so the book would be empty. If one is a plan, set its kind to plan on the Views tab."]
        : [el("b", {}, "No plan is ticked."), " The book would be empty: tick at least one plan on the Views tab to measure it."];
    box.replaceChildren(el("div", { class: "ask-block", role: "alert" }, ...msg));
    return;
  }
  if (st === "busy" && !RV()) {
    box.replaceChildren(el("div", { class: "ask-head" }, el("span", { class: "ask-busy", role: "status" }, "Reading what the engine uses and what it would ask…")));
    return;
  }
  if (st === "error" && !RV()) {
    box.replaceChildren(el("div", { class: "ask-head" }, el("span", { class: "muted small", role: "status" }, `Could not read the questions: ${S.reviewErr}. `),
      el("button", { class: "link", type: "button", onclick: () => loadReview({ force: true }) }, "Try again")));
    return;
  }
  if (!RV()) { box.replaceChildren(); return; }
  const title = open.length
    ? `${open.length} question${open.length === 1 ? "" : "s"} before an accurate book`
    : qs.length ? "All questions answered" : "No open questions";
  head.append(...[
    el("button", { type: "button", class: `ask-toggle${open.length ? "" : " done"}`, "aria-expanded": String(expanded), "aria-controls": listId,
      onclick: () => { S.askOpen = !expanded; renderAsk(); } },
    el("span", { class: "ask-n", "aria-hidden": "true" }, String(open.length)), el("span", {}, title)),
    st === "busy" ? el("span", { class: "ask-busy small", role: "status" }, "Re-checking…")
      : S.reviewStale ? el("button", { type: "button", class: "link small", onclick: () => loadReview({ force: true }) }, "Re-check") : null].filter(Boolean));
  const kids = [head];
  if (expanded && qs.length) {
    kids.push(el("p", { class: "ask-note" }, "You can build with open questions: the rows they affect stay assumed or unresolved, as on the Flags tab."));
    const order = [...qs].sort((a, b) => Number(!isOpen(a)) - Number(!isOpen(b)));
    kids.push(el("ol", { class: "ask-list", id: listId }, order.map(questionItem)));
  } else if (expanded) kids.push(el("p", { class: "ask-note", id: listId }, "Nothing the engine would ask: heights, floors and sizes were all read or answered."));
  box.replaceChildren(...kids);
}

const TOPIC = { floors: "Floors", heights: "Heights", structure: "Sizes", choice: "Choose" };

function suggestion(q) {
  const h = RV()?.heights;
  const row = h?.rows?.find((r) => r.flag_ids?.includes(q.id));
  if (row?.to_slab_top_mm) return String(Math.round(row.to_slab_top_mm - (slabNow() || 0)));
  if (row?.height_mm != null && row.kind !== "block") return String(Math.round(row.height_mm));
  const p = RV()?.plans?.find((x) => x.flag_floors === q.id);
  if (p) return floorsText(p.floors);
  return q.input?.placeholder || "";
}

function questionItem(q) {
  const a = answer(q.id);
  const open = isOpen(q);
  const id = nid("q", q.id);
  const sev = el("span", { class: `sev ${q.severity}`, "aria-hidden": "true" });
  const li = el("li", { class: `ask-q${open ? "" : " done"}` }, sev,
    el("div", { class: "ask-t" }, el("span", { class: "ask-topic" }, TOPIC[q.topic] || ""), el("span", { id: `${id}t` }, q.title)));
  if (!open) {
    li.append(el("div", { class: "ask-res" }, el("span", {}, a.status === "accepted" ? "Accepted as drawn" : `Answered: ${a.value ?? ""}`),
      el("button", { class: "link", type: "button", "aria-label": `Change the answer to: ${q.title}`, onclick: () => { answerQ(q, "open"); } }, "Change")));
    return li;
  }
  if (q.input) {
    const num = q.input.type === "number";
    const inp = el("input", { id, type: num ? "text" : "text", inputmode: num ? "decimal" : "text", enterkeyhint: "done", autocomplete: "off",
      value: suggestion(q), "aria-labelledby": `${id}l ${id}t` });
    const form = el("form", { class: "ask-form", onsubmit: (e) => {
      e.preventDefault();
      const v = inp.value.trim();
      const n = Number(v.replace(/,/g, ""));
      if (!v || (num && !Number.isFinite(n))) { inp.setAttribute("aria-invalid", "true"); inp.focus(); return; }
      answerQ(q, "resolved", num ? n : v);
    } },
      el("label", { for: id, id: `${id}l` }, `${q.input.label || "Value"}${q.input.unit && !/\(/.test(q.input.label || "") ? ` (${q.input.unit})` : ""}`), inp,
      el("button", { class: "btn", type: "submit" }, "Use"),
      el("button", { class: "link", type: "button", onclick: () => answerQ(q, "accepted") }, "Accept as drawn"));
    li.append(form);
  } else {
    li.append(el("div", { class: "ask-form" }, el("button", { class: "btn", type: "button", "aria-describedby": `${id}t`, onclick: () => answerQ(q, "accepted") }, "Confirm")));
  }
  if (q.evidence?.length || q.detail) {
    li.append(el("details", { class: "ask-more" }, el("summary", {}, "Why"), q.detail ? el("p", {}, q.detail) : null,
      q.evidence?.length ? el("ul", {}, q.evidence.map((e) => el("li", {}, e))) : null));
  }
  return li;
}

/** One answer, applied where the engine reads it (the same flag on the MB page shows it as resolved). */
function answerQ(q, status, value = null) {
  const h = RV()?.heights || {};
  const plan = RV()?.plans?.find((p) => p.flag_floors === q.id || p.flag_ftf === q.id);
  if (status === "resolved" && plan && plan.flag_floors === q.id) {
    const fl = parseFloors(String(value));
    if (fl) { applyFloors(plan, fl.floors, fl.mult); rerender(); return; }
  }
  if (status === "resolved" && plan && plan.flag_ftf === q.id) { applyFtf(plan, Number(value)); rerender(); return; }
  if (h.slab?.flag_ids?.includes(q.id) && status !== "accepted") { setGroup(h.slab.flag_ids, status, value); rerender(); return; }
  if (h.beam?.flag_ids?.includes(q.id) && status !== "accepted") { setGroup(h.beam.flag_ids, status, value); rerender(); return; }
  setFlag(q.id, status, value);
  rerender();
}

function setGroup(ids, status, value) { for (const id of ids) setFlag(id, status, value); }

/** Save and redraw after the change event has finished (so Tab has already moved focus on). */
function rerender() {
  A.cfgChanged();
  setTimeout(renderReview, 0);
}

// ---------------------------------------------------------------- floors
/** "6-9", "6 to 9", "3,4,5", "G", "GF", "ground", "1" -> {floors, mult}; "6-9 x3" overrides the count. */
export function parseFloors(s) {
  let t = String(s || "").trim().toUpperCase();
  if (!t) return null;
  let mult = null;
  const mx = t.match(/\s*[X×*]\s*(\d{1,2})\s*$/);
  if (mx && t.slice(0, mx.index).trim()) { mult = Number(mx[1]); t = t.slice(0, mx.index).trim(); }
  t = t.replace(/\bGROUND( FLOOR)?\b|\bG\.?F\.?\b|\bG\b/g, "0").replace(/(\d+)\s*(ST|ND|RD|TH)\b/g, "$1").replace(/\bFLOORS?\b/g, "");
  const out = new Set();
  for (const part of t.split(/[,;&]|\bAND\b/)) {
    const p = part.trim();
    if (!p) continue;
    const m = p.match(/^(-?\d{1,2})\s*(?:-|–|—|TO)\s*(-?\d{1,2})$/);
    if (m) {
      const a = Number(m[1]), b = Number(m[2]);
      if (b < a || b - a > 80) return null;
      for (let k = a; k <= b; k++) out.add(k);
    } else if (/^-?\d{1,2}$/.test(p)) out.add(Number(p));
    else return null;
  }
  if (!out.size) return null;
  const floors = [...out].sort((a, b) => a - b);
  return { floors, mult: mult || floors.length };
}

function ordinal(n) {
  if (n === 0) return "Ground floor";
  if (n < 0) return n === -1 ? "Basement" : `Basement ${-n}`;
  const s = n % 100 >= 10 && n % 100 <= 20 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" })[n % 10] || "th";
  return `${n}${s} floor`;
}

function floorsText(fl) {
  if (!fl?.length) return "";
  const contiguous = fl.every((v, i) => i === 0 || v === fl[i - 1] + 1);
  if (fl.length === 1) return fl[0] === 0 ? "G" : String(fl[0]);
  return contiguous ? `${fl[0]}-${fl[fl.length - 1]}` : fl.join(",");
}

/** "Floors 6–9 · ×4", "Ground floor · ×1", "Terrace". */
function floorsLabel(fl, mult, special) {
  if (special) return special.charAt(0).toUpperCase() + special.slice(1);
  if (!fl?.length) return mult > 1 ? `Floors not identified · ×${mult}` : "Floor not identified";
  const contiguous = fl.every((v, i) => i === 0 || v === fl[i - 1] + 1);
  const what = fl.length === 1 ? ordinal(fl[0]) : contiguous ? `Floors ${fl[0]}–${fl[fl.length - 1]}` : `Floors ${fl.join(", ")}`;
  return `${what} · ×${mult}`;
}

/** The engine's flag answer for a plan's floors: '6-9', '3,5,7', 'Ground floor', '6-9 x3'. */
function floorsValue(fl, mult) {
  const contiguous = fl.every((v, i) => i === 0 || v === fl[i - 1] + 1);
  const base = fl.length === 1 ? ordinal(fl[0]) : contiguous ? `${fl[0]}-${fl[fl.length - 1]}` : fl.join(",");
  return mult !== fl.length ? `${base} x${mult}` : base;
}

function applyFloors(p, floors, mult) {
  const r = S.cfg.regions.find((x) => x.id === p.id);
  if (!r) return;
  r.floors = floors; r.multiplier = mult; r.floors_source = "user (Review)";
  if (p.flag_floors) {
    const same = JSON.stringify(floors) === JSON.stringify(p.floors) && mult === p.multiplier && !p.user;
    setFlag(p.flag_floors, same ? "accepted" : "resolved", same ? null : floorsValue(floors, mult));
  }
}

function applyFtf(p, v) {
  const r = S.cfg.regions.find((x) => x.id === p.id);
  if (!r) return;
  if (!(v > 0)) {
    delete r.floor_to_floor_mm; delete r.floor_to_floor_source;
    if (p.flag_ftf) setFlag(p.flag_ftf, "open");
    return;
  }
  r.floor_to_floor_mm = v; r.floor_to_floor_source = "user (Review)";
  if (p.flag_ftf) setFlag(p.flag_ftf, "resolved", v);
}

/** Current values for a plan: what the user set on this page, else what the engine read. */
function planNow(p) {
  const r = S.cfg.regions.find((x) => x.id === p.id) || {};
  const userF = String(r.floors_source || "").startsWith("user");
  const floors = userF && Array.isArray(r.floors) ? r.floors : p.floors;
  const mult = userF && r.multiplier ? r.multiplier : p.multiplier;
  const ftfUser = String(r.floor_to_floor_source || "").startsWith("user");
  const ftf = ftfUser ? r.floor_to_floor_mm : p.ftf_mm;
  const fa = p.flag_floors ? answer(p.flag_floors) : null;
  const confirmed = !p.flag_floors || userF || (fa && fa.status !== "open");
  return { floors, mult, ftf, ftfUser, confirmed };
}

function floorsLine(r, p) {
  const now = planNow(p);
  const multi = RV()?.building?.multi;
  const edId = nid("fe", p.id);
  const open = S.rvFloorEdit === p.id;
  const bits = [floorsLabel(now.floors, now.mult, p.special)];
  if (multi && !p.special) bits.push(now.ftf ? `storey ${mm(now.ftf)}` : "storey height not found");
  if (p.level_mm != null) bits.push(lvl(p.level_mm));
  const line = el("div", { class: "fl-line" },
    el("span", { class: `fl-sum${now.confirmed ? "" : " todo"}` }, bits.join(" · ")),
    !now.confirmed ? el("span", { class: "fl-tag" }, "confirm") : null,
    p.special ? null : el("button", { class: "link", type: "button", "aria-expanded": String(open), "aria-controls": edId,
      "aria-label": `Edit floors of ${r.name}`, onclick: () => { S.rvFloorEdit = open ? null : p.id; renderReview(); } }, open ? "Done" : "Edit"),
    !now.confirmed && !open ? el("button", { class: "link", type: "button", "aria-label": `Confirm floors of ${r.name}`,
      onclick: () => { setFlag(p.flag_floors, "accepted"); rerender(); } }, "Looks right") : null);
  const src = p.source ? el("div", { class: "fl-src" }, `Read from ${p.source}`, now.ftf && p.ftf_source && !now.ftfUser ? `; storey height from ${p.ftf_source.replace(/^level labels: /, "level labels ")}` : "") : null;
  if (!open) return [line, src];
  const fid = nid("ff", p.id), mid = nid("fm", p.id), hid = nid("fh", p.id);
  const fIn = el("input", { id: fid, type: "text", value: floorsText(now.floors), autocomplete: "off", enterkeyhint: "done", "aria-describedby": `${fid}h` });
  const mIn = el("input", { id: mid, type: "text", inputmode: "numeric", value: now.mult, autocomplete: "off", enterkeyhint: "done" });
  const hIn = el("input", { id: hid, type: "text", inputmode: "decimal", value: now.ftf ?? "", placeholder: "3000", autocomplete: "off", enterkeyhint: "done" });
  const err = el("span", { class: "fl-err", role: "alert" });
  let multTouched = now.mult !== now.floors.length;
  fIn.addEventListener("input", () => {
    const f = parseFloors(fIn.value);
    if (f && !multTouched) mIn.value = f.mult;
  });
  mIn.addEventListener("input", () => { multTouched = true; });
  const commit = () => {
    const f = parseFloors(fIn.value);
    const m = Number(mIn.value);
    if (!f) { err.textContent = "Floors: write a range like 6-9, a list like 3,4,5, or G for ground."; return; }
    if (!(m >= 1 && m <= 80)) { err.textContent = "Multiplier: a whole number from 1."; return; }
    err.textContent = "";
    applyFloors(p, f.floors, Math.round(m));
    if (multi) applyFtf(p, Number(String(hIn.value).replace(/,/g, "")));
    structural();
    setTimeout(renderReview, 0);
  };
  for (const i of [fIn, mIn, hIn]) i.addEventListener("change", (e) => { S.lastEdited = e.target.id; commit(); });
  return [line, el("div", { class: "fl-edit", id: edId },
    el("label", { for: fid }, "Floors", fIn),
    el("label", { for: mid }, "Multiplier ×", mIn),
    multi ? el("label", { for: hid }, "Storey height, mm", hIn) : null,
    el("p", { class: "fl-hint", id: `${fid}h` }, "A range (6-9), a list (3,4,5) or G for ground. The multiplier follows the number of floors unless you change it."),
    err)];
}

// ---------------------------------------------------------------- views
function purposeNow(r) {
  const v = viewInfo(r);
  const m = measuredNow(r);
  if (S.isPdf) return v || { purpose: "measured", reason: "" };
  if (!v) {
    // not read yet: a provisional purpose from the view type
    const guess = m ? "measured" : r.kind === "schedule" ? "sizes" : r.kind === "section" ? "heights" : r.kind === "elevation" ? "finishes" : "unused";
    return { purpose: guess, reason: S.reviewState === "busy" ? "Checking what the engine reads from it…" : "", provisional: true };
  }
  if (m && v.purpose !== "measured") {
    return { ...v, purpose: "measured", reason: "Ticked: measured as a plan (Re-check to see what it gives)",
      warn: r.kind === "plan" ? "" : v.overlaps?.length ? `This will double-count walls already measured in ${listNames(v.overlaps)}` : "A non-plan view measured as a plan" };
  }
  if (!m && v.purpose === "measured") return { ...v, purpose: "unused", reason: "Unticked: not measured", warn: "" };
  return v;
}

const listNames = (n) => (n.length <= 2 ? n.join(" and ") : `${n.slice(0, 2).join(", ")} and ${n.length - 2} more`);

function views() {
  const kinds = S.meta?.kinds || ["plan", "section", "elevation", "schedule", "detail", "other"];
  const levels = S.cfg.levels || [];
  const rv = RV();
  const head = [];
  const intro = S.isPdf
    ? "Sheets in the PDF set and what the structural engine takes from each. Click Show to look at one, or pick it in the strip above the drawing."
    : "What each view is used for. Plans are measured (tick or untick them); sections and elevations are read for heights and finishes without a tick; details and key plans are not used.";
  head.push(el("div", { class: "rv-section" }, el("p", {}, intro),
    rv?.building?.text ? el("p", { class: "rv-building" }, el("b", {}, "Building: "), rv.building.text) : null));
  const list = el("ul", { class: "rv-list" }, S.cfg.regions.map((r) => viewItem(r, kinds, levels)));
  return [...head, list];
}

function viewItem(r, kinds, levels) {
  const pv = purposeNow(r);
  const m = S.isPdf ? !!r.include : measuredNow(r);
  const p = planInfo(r);
  const reasonId = `vr${r.id}`;
  const li = el("li", { class: `${m ? "" : "off"}${S.viewer.selectedRegion === r.id ? " sel" : ""}`, "data-id": r.id },
    el("input", { type: "checkbox", id: `vc${r.id}`, checked: m, "aria-label": `Measure ${r.name}`, "aria-describedby": reasonId,
      onchange: (e) => {
        if (S.isPdf || r.kind === "plan") r.include = e.target.checked;
        else { r.measure = e.target.checked; r.include = e.target.checked; }
        S.viewer.draw(); structural(); renderReview();
      } }),
    el("input", { type: "text", class: "nm", value: r.name, "aria-label": "View name",
      oninput: (e) => { r.name = e.target.value; S.viewer.draw(); A.cfgChanged(); } }),
    el("button", { class: "link", type: "button", "aria-label": `Show ${r.name} on the drawing`, onclick: () => A.focusRegion(r.id) }, "Show"),
    el("div", { class: "why", id: reasonId },
      el("span", { class: `pp ${pv.purpose}${pv.provisional ? " prov" : ""}` }, PURPOSE[pv.purpose]),
      el("span", { class: "why-t" }, pv.reason || "")));
  if (pv.warn) li.append(el("p", { class: "rv-warn" }, pv.warn));
  if (!S.isPdf && p && m && r.kind === "plan") li.append(el("div", { class: "fl" }, floorsLine(r, p)));
  if (S.isPdf && p) li.append(el("div", { class: "fl" }, el("div", { class: "fl-line" }, el("span", { class: "fl-sum" }, `${p.group} · ×${p.multiplier}`))));
  li.append(r.kind === "sheet" ? el("div", { class: "sub" }, el("span", {}, r.title || ""), el("span", {}, r.count == null ? "" : `${r.count} item${r.count === 1 ? "" : "s"}`)) : el("div", { class: "sub" },
    el("select", { "aria-label": `View type of ${r.name}`, onchange: (e) => { r.kind = e.target.value; structural(); renderReview(); } },
      kinds.map((k) => el("option", { value: k, selected: k === r.kind }, k))),
    levels.length ? el("select", { "aria-label": `Level of ${r.name}`, onchange: (e) => { r.level = e.target.value; A.cfgChanged(); } },
      el("option", { value: "" }, "No level"), levels.map((lv) => el("option", { value: lv.name, selected: lv.name === r.level }, lv.name))) : null,
    el("span", {}, `${r.count} entities`)));
  return li;
}

// ---------------------------------------------------------------- layers (unchanged)
function layers() {
  const roles = S.meta?.roles || ["wall-brick", "wall-AAC", "parapet", "column", "door", "window", "room text", "dimension", "ignore"];
  const filter = el("input", { type: "search", placeholder: "Filter layers", "aria-label": "Filter layers", value: S.layerQ || "" });
  const onlyRoles = el("input", { type: "checkbox", checked: !!S.layerOnlyRoles });
  const tbody = el("tbody");
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    S.layerQ = filter.value; S.layerOnlyRoles = onlyRoles.checked;
    tbody.replaceChildren(...S.viewer.layers.filter((L) => L.n && (!q || L.name.toLowerCase().includes(q) || (S.cfg.layer_roles[L.name] || "").includes(q))
      && (!onlyRoles.checked || (S.cfg.layer_roles[L.name] || "ignore") !== "ignore")).map((L) => {
      const role = S.cfg.layer_roles[L.name] || "ignore";
      const tr = el("tr", {
        onmouseenter: () => A.previewLayer(L.i), onmouseleave: () => A.previewLayer(null),
      },
      el("td", {}, el("input", { type: "checkbox", checked: L.visible, "aria-label": `Show ${L.name}`,
        onchange: (e) => { A.setLayerVisible(L, e.target.checked); } })),
      el("td", { class: "nm", title: L.name }, L.name),
      el("td", { class: "num" }, L.n),
      el("td", {}, el("span", { class: "swatch-role", style: { background: ROLE_SWATCH[role] } }),
        el("select", { "aria-label": `Role of ${L.name}`, onchange: (e) => { S.cfg.layer_roles[L.name] = e.target.value; A.rolesChanged(); S.reviewStale = true; draw(); renderAsk(); } },
          roles.map((r) => el("option", { value: r, selected: r === role }, r)))));
      return tr;
    }));
  };
  filter.addEventListener("input", draw);
  onlyRoles.addEventListener("change", draw);
  draw();
  return [
    el("div", { class: "rv-tools" }, filter, el("label", {}, onlyRoles, "Measured only")),
    el("table", { class: "plain" }, el("thead", {}, el("tr", {}, el("th", {}, el("span", { class: "sr-only" }, "Show")), el("th", {}, "Layer"), el("th", {}, "Items"), el("th", {}, "Role"))), tbody),
  ];
}

// ---------------------------------------------------------------- heights
const num = (v) => { const n = Number(String(v ?? "").replace(/,/g, "").trim()); return String(v ?? "").trim() !== "" && Number.isFinite(n) ? n : null; };

/** A labelled number field (inputmode=decimal) that commits on change. */
function field(label, value, onCommit, { id, placeholder = "", describedby, text = false, disabled = false } = {}) {
  const inp = el("input", { id, type: "text", disabled, inputmode: text ? "text" : "decimal", value: value ?? "", placeholder, autocomplete: "off", enterkeyhint: "done",
    "aria-describedby": describedby,
    onchange: (e) => { S.lastEdited = id; onCommit(text ? e.target.value.trim() : num(e.target.value)); } });
  return [el("label", { for: id, class: "sr-only" }, label), inp];
}

function srcChip(text, assumed, confirmed) {
  const cls = confirmed ? "you" : assumed ? "assumed" : "read";
  return el("span", { class: `src ${cls}` }, text || (assumed ? "assumed" : ""));
}

function slabNow() {
  const h = RV()?.heights;
  if (!h?.slab) return null;
  const a = h.slab.flag_ids.map(answer).find((x) => x && x.status === "resolved");
  return a ? num(a.value) : h.slab.value;
}

function heights() {
  const h = RV()?.heights;
  if (!h) {
    return [el("div", { class: "rv-section" }, el("p", { role: "status" },
      S.reviewState === "busy" ? "Reading heights from sections, levels and notes…" : "Heights appear once the drawing has been read."))];
  }
  if (S.isPdf) return structHeights(h);
  const out = [];
  const slab = slabNow();
  // ---- blocks / storeys
  const rows = h.rows.filter((r) => r.kind !== "default" || r.used || !h.rows.some((x) => x.kind !== "default"));
  const intro = h.multi
    ? `Wall height of each floor = its storey height minus the slab${slab ? ` (${mm(slab)})` : " (not given yet)"}. Brick and AAC walls on a floor share its height.`
    : "Wall height (floor to the underside of the slab) for each building or wing under one roof level. Brick and AAC walls in it share that height.";
  const list = el("ul", { class: "hlist" }, rows.map((r) => heightRow(r, h, slab)));
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hWalls" }, el("h3", { id: "hWalls" }, h.multi ? "Storeys and wall heights" : "Wall heights"),
    el("p", {}, intro), list));
  // ---- wall types
  const walls = S.cfg.walls = S.cfg.walls || {};
  const typeList = el("ul", { class: "hlist" }, h.wall_types.map((t) => {
    const w = walls[t.role] = walls[t.role] || {};
    const name = t.role === "wall-AAC" ? "AAC wall" : t.role === "wall-brick" ? "Brick wall" : "Parapet";
    const par = t.role === "parapet";
    // the parapet: what the book uses (the user's value, else the note, else the default), entered as a
    // user value in config.roof (it wins over the note in the engine)
    const roofCfg = () => (S.cfg.roof = S.cfg.roof || {});
    const setRoof = (k, v) => { if (v == null) delete roofCfg()[k]; else roofCfg()[k] = v; A.cfgChanged(); rerender(); };
    const ins = [el("span", { class: "h-lab", "aria-hidden": "true" }, "thick"),
      field(`${name} thickness, mm`, par ? (S.cfg.roof?.parapet_thickness_mm ?? t.thickness_mm) : (w.thickness_mm ?? t.thickness_mm),
        par ? (v) => setRoof("parapet_thickness_mm", v) : (v) => { w.thickness_mm = v; A.cfgChanged(); }, { id: nid("wt", t.role) }), el("span", { class: "u" }, "mm")];
    if (par) {
      ins.push(el("span", { class: "h-lab", "aria-hidden": "true" }, "high"),
        field("Parapet height including coping, mm", S.cfg.roof?.parapet_height_mm ?? t.height_mm, (v) => setRoof("parapet_height_mm", v),
          { id: "wt-parapet-h", describedby: "wt-parapet-src" }), el("span", { class: "u" }, "mm"));
    }
    const user = par && S.cfg.roof?.parapet_height_mm != null;
    const brick = par && t.brick_height_mm != null && !user ? ` · brickwork ${fmt(t.brick_height_mm, 0)} = ${fmt(t.height_mm, 0)} − ${fmt(t.coping_mm, 0)} coping` : "";
    return el("li", { class: "h-row" },
      el("div", { class: "h-name" }, el("b", {}, el("span", { class: "swatch-role", style: { background: ROLE_SWATCH[t.role] } }), name)),
      el("div", { class: "h-in" }, ins),
      par ? el("div", { class: "h-src", id: "wt-parapet-src" }, srcChip(user ? "your value (applies on the next build)" : `${t.height_source || ""}${brick}`,
        !user && (t.height_assumed || t.used === false), user)) : null);
  }));
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hTypes" }, el("h3", { id: "hTypes" }, "Wall types"), typeList));
  // ---- slab, beam, lintel
  const sb = el("ul", { class: "hlist" },
    sizeRow("Slab thickness", h.slab, (v) => setGroup(h.slab.flag_ids, v == null ? "open" : "resolved", v), { unit: "mm",
      note: h.multi ? "Taken off each storey height to give the wall height." : "Used for the roof slab and taken off section heights." }),
    sizeRow("Beam depth below the slab", h.beam, (v) => {
      if (h.beam.flag_ids.length) setGroup(h.beam.flag_ids, v == null ? "open" : "resolved", v);
      else { S.cfg.brickwork = { ...(S.cfg.brickwork || {}), beam_depth_mm: v }; if (v == null) delete S.cfg.brickwork.beam_depth_mm; }
    }, { unit: "mm", note: "Brickwork goes up to the beam soffit (IS 1200 Part 3): this depth is taken off the brick height under beams." }),
    sizeRow("Lintel (optional)", h.lintel, (v) => {
      if (h.lintel.flag_ids.length) setGroup(h.lintel.flag_ids, v ? "resolved" : "open", v || null);
      else { const d = num(String(v).split(/[xX×]/).pop()); S.cfg.brickwork = { ...(S.cfg.brickwork || {}), lintel_depth_mm: d }; if (d == null) delete S.cfg.brickwork.lintel_depth_mm; }
    }, { text: true, placeholder: "230X150", note: "B×D or depth in mm; lintels over openings are deducted from brickwork." }));
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hSizes" }, el("h3", { id: "hSizes" }, "Slab, beams and lintels"), sb));
  out.push(levelsSection());
  return out;
}

function heightRow(r, h, slab) {
  const a = r.flag_ids?.map(answer).find((x) => x && x.status !== "open");
  const id = nid("hr", r.key);
  const nameId = `${id}n`;
  const kids = [el("div", { class: "h-name", id: nameId }, el("b", {}, r.name), r.plan && r.kind !== "storey" && !r.name.includes(r.plan) ? el("span", { class: "h-plan" }, r.plan) : null)];
  if (r.kind === "storey") {
    const p = RV()?.plans?.find((x) => x.id === r.rid);
    const now = p ? planNow(p) : { ftf: r.ftf_mm, ftfUser: false };
    const wall = now.ftf ? now.ftf - (slab || 0) : null;
    kids.push(el("div", { class: "h-in" }, el("span", { class: "h-lab", "aria-hidden": "true" }, "storey"),
      field(`Storey height of ${r.name}, mm`, now.ftf, (v) => { if (p) applyFtf(p, v); rerender(); }, { id, placeholder: "3000" }), el("span", { class: "u" }, "mm")),
    el("div", { class: "h-calc" }, wall ? `wall ${fmt(wall, 0)} mm` : "wall —", slab ? "" : el("span", { class: "muted" }, " (slab not taken off)")),
    el("div", { class: "h-src" }, srcChip(now.ftfUser ? "your answer" : r.source, !now.ftf, now.ftfUser)));
    return el("li", { class: "h-row" }, kids);
  }
  let value = a?.status === "resolved" ? num(a.value) : r.height_mm;
  let src = r.source, assumed = r.assumed, confirmed = !!a || r.confirmed;
  if (!a && r.to_slab_top_mm) { value = r.to_slab_top_mm - (slab || 0); src = r.source.replace(/ - slab \d+|; slab thickness not given, not deducted/, "") + (slab ? ` − slab ${fmt(slab, 0)}` : "; slab not given, not taken off"); }
  if (a?.status === "resolved") src = "your answer";
  if (a?.status === "accepted") src = "accepted as drawn";
  const commit = (v) => {
    if (r.kind === "zone") { S.cfg.zones[r.zone].wall_height_mm = v; A.cfgChanged(); renderReview(); return; }
    if (r.kind === "default") {
      const w = S.cfg.walls = S.cfg.walls || {};
      for (const role of ["wall-brick", "wall-AAC"]) w[role] = { ...(w[role] || {}), height_mm: v ?? 3000, height_confirmed: v != null };
      rerender(); return;
    }
    setGroup(r.flag_ids, v == null ? "open" : "resolved", v); rerender();
  };
  kids.push(el("div", { class: "h-in" }, el("span", { class: "h-lab", "aria-hidden": "true" }, "height"),
    field(`Wall height of ${r.name}, mm`, value != null ? Math.round(value) : "", commit, { id, placeholder: "3000" }), el("span", { class: "u" }, "mm")),
  el("div", { class: "h-src" }, srcChip(src, assumed && !confirmed, confirmed && !/please/.test(src)),
    !confirmed && value != null && r.flag_ids?.length ? el("button", { class: "link", type: "button", "aria-describedby": nameId,
      onclick: () => { setGroup(r.flag_ids, "resolved", Math.round(value)); rerender(); } }, "Confirm") : null));
  return el("li", { class: `h-row${assumed && !confirmed ? " todo" : ""}` }, kids);
}

function sizeRow(label, q, commit, { unit = "", note = "", text = false, placeholder = "" } = {}) {
  const a = q.flag_ids?.map(answer).find((x) => x && x.status !== "open");
  const value = a?.status === "resolved" ? a.value : q.value;
  const src = a?.status === "resolved" ? "your answer" : a?.status === "accepted" ? "accepted as drawn" : q.source;
  const id = nid("sz", label);
  return el("li", { class: `h-row${q.assumed && !a ? " todo" : ""}` },
    el("div", { class: "h-name" }, el("b", {}, label)),
    el("div", { class: "h-in" }, field(`${label}${unit ? `, ${unit}` : ""}`, value ?? "", (v) => { commit(v); rerender(); }, { id, text, placeholder, describedby: note ? `${id}d` : undefined, disabled: q.disabled }), unit ? el("span", { class: "u" }, unit) : null),
    el("div", { class: "h-src" }, srcChip(src, q.assumed && !a, !!a)),
    note ? el("p", { class: "h-note", id: `${id}d` }, note) : null);
}

function levelsSection() {
  const lv = S.cfg.levels = S.cfg.levels || [];
  const levelBody = el("tbody");
  const numIn = (obj, key, label) => el("input", { type: "text", inputmode: "decimal", value: obj[key] ?? "", "aria-label": label, enterkeyhint: "done",
    onchange: (e) => { obj[key] = num(e.target.value); A.cfgChanged(); } });
  const drawLevels = () => levelBody.replaceChildren(...lv.map((l, i) => el("tr", {},
    el("td", {}, el("input", { type: "text", value: l.name, "aria-label": "Level name", onchange: (e) => { l.name = e.target.value; A.cfgChanged(); } })),
    el("td", {}, numIn(l, "level_mm", `${l.name} level, mm`)),
    el("td", {}, el("button", { class: "link", type: "button", "aria-label": `Remove ${l.name}`, onclick: () => { lv.splice(i, 1); drawLevels(); A.cfgChanged(); } }, "Remove")))));
  drawLevels();
  return el("details", { class: "rv-section rv-more" }, el("summary", {}, "Named levels (for labelling views)"),
    el("table", { class: "plain" }, el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Level mm"), el("th", {}, el("span", { class: "sr-only" }, "Remove")))), levelBody),
    el("p", {}, el("button", { class: "link", type: "button", onclick: () => {
      const last = lv.length ? lv[lv.length - 1].level_mm || 0 : 0;
      lv.push({ name: `Level ${lv.length + 1}`, level_mm: last + 3300 }); drawLevels(); A.cfgChanged();
    } }, "Add a level")));
}

function structHeights(h) {
  const out = [];
  const rows = el("ul", { class: "hlist" }, h.rows.map((r) => {
    const a = r.flag_ids.map(answer).find((x) => x && x.status !== "open");
    const v = a?.status === "resolved" ? a.value : r.height_mm;
    const id = nid("ch", r.key);
    return el("li", { class: `h-row${!a && r.assumed ? " todo" : ""}` },
      el("div", { class: "h-name" }, el("b", {}, r.name), el("span", { class: "h-plan" }, r.plan)),
      el("div", { class: "h-in" }, field(`${r.name}, mm`, v ?? "", (x) => { setGroup(r.flag_ids, x == null ? "open" : "resolved", x); rerender(); }, { id, placeholder: "3300" }), el("span", { class: "u" }, "mm")),
      el("div", { class: "h-src" }, srcChip(a?.status === "resolved" ? "your answer" : r.source, !a && r.assumed, !!a)));
  }));
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hCols" }, el("h3", { id: "hCols" }, "Storey heights per floor group"),
    el("p", {}, "Column height below each slab (floor to floor). The structural set writes no levels, so column concrete and bars are 0 until these are entered."),
    h.rows.length ? rows : el("p", {}, "No floor plans with columns were found.")));
  // foundation
  const f = h.foundation;
  const fa = f.flag_ids.map(answer).find((x) => x && x.status !== "open");
  const cur = fa?.status === "resolved" ? fa.value : f.value;
  const fid = "fd-foundation";
  const LAB = { piled_raft: "Piled raft (piles and raft)", piles_only: "Piles only", raft_only: "Raft only" };
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hFound" }, el("h3", { id: "hFound" }, "Foundation"),
    el("ul", { class: "hlist" }, el("li", { class: `h-row${!fa && f.assumed ? " todo" : ""}` },
      el("div", { class: "h-name" }, el("label", { for: fid }, el("b", {}, "Pile or raft"))),
      el("div", { class: "h-in" }, el("select", { id: fid, disabled: !f.flag_ids.length, onchange: (e) => { setGroup(f.flag_ids, "resolved", e.target.value); rerender(); } },
        f.options.map((o) => el("option", { value: o, selected: o === cur }, LAB[o] || o)))),
      el("div", { class: "h-src" }, srcChip(fa ? "your answer" : f.source, !fa && f.assumed, !!fa))))));
  // sunken slab
  out.push(el("section", { class: "rv-section", "aria-labelledby": "hSunk" }, el("h3", { id: "hSunk" }, "Sunken slabs"),
    el("ul", { class: "hlist" }, sizeRow("Sunken slab depth", { ...h.sunken, disabled: !h.sunken.flag_ids.length }, (v) => { setGroup(h.sunken.flag_ids, v == null ? "open" : "resolved", v); }, { unit: "mm",
      note: h.sunken.flag_ids.length ? "The drop below the floor slab in toilets and kitchens." : "No sunken panels were found on the slab plans." }))));
  return out;
}
