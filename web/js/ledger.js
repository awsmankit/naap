// The MB ledger: tabs, filters and tables for MB, Abstract, Rooms, Doors & windows,
// Dimensions and Flags. Selection is applied by toggling classes, so the scroll
// position survives clicks and keyboard moves.

import { S, A, rowById } from "./state.js";
import { $, el, svg, fq, fd, fmt, unit } from "./util.js";
import { tradeOf, SEV } from "./theme.js";
import { renderSteel, renderSchedules, renderCalibration, calibDetail } from "./struct.js";

const TABS_ARCH = [
  ["mb", "MB"], ["abstract", "Abstract"], ["rooms", "Rooms"],
  ["openings", "Doors & windows"], ["dims", "Dimensions"], ["flags", "Flags"],
];
const TABS_STRUCT = [
  ["mb", "MB"], ["abstract", "Abstract"], ["steel", "Steel"], ["schedules", "Schedules"],
  ["calib", "Calibration"], ["flags", "Flags"],
];
export const tabsFor = (mb) => (mb?.kind === "structural" ? TABS_STRUCT : TABS_ARCH);
const STATUS_LABEL = { ok: "OK", assumed: "Assumed", unresolved: "Unresolved", scaled: "Scaled" };
const STATUS_TITLE = { scaled: "Length scaled from a NOT-TO-SCALE drawing: verify on site or with a written dimension" };
const WARN_SVG = '<svg viewBox="0 0 18 18" aria-hidden="true"><path d="M9 2 17 16H1z" fill="currentColor"/><path d="M9 7v4M9 13v.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>';

export function sevGlyph(sev) {
  const s = el("span", { class: `sev ${sev}`, role: "img", "aria-label": SEV[sev]?.label || sev });
  if (sev === "warning") s.append(svg(WARN_SVG));
  else s.textContent = sev === "missing" ? "?" : "!";
  return s;
}

export function chip(status) {
  return el("span", { class: `chip ${status}`, title: STATUS_TITLE[status] || STATUS_LABEL[status] || status }, STATUS_LABEL[status] || status);
}

// ------------------------------------------------------------------ chrome
export function renderTabs() {
  const mb = S.mb;
  const openFlags = mb.flags.filter((f) => f.status === "open");
  const errs = openFlags.filter((f) => f.severity === "error").length;
  const counts = {
    mb: mb.rows.length, abstract: mb.abstract.length, rooms: mb.rooms.length,
    openings: mb.openings.length, dims: mb.dimensions.length, flags: openFlags.length,
    steel: mb.steel?.total_kg != null ? `${fmt(mb.steel.total_kg / 1000, 1)} t` : "",
    schedules: mb.schedules?.tables?.length ?? 0,
    calib: Object.keys(mb.calibration || {}).length,
  };
  const tabs = tabsFor(mb);
  if (!tabs.some(([k]) => k === S.tab)) S.tab = "mb";
  $("#mbTabs").replaceChildren(...tabs.map(([k, label]) => el("button", {
    role: "tab", "aria-selected": String(S.tab === k), "data-tab": k, id: `tab-${k}`,
    onclick: () => A.setTab(k),
  }, label, el("span", { class: `n${k === "flags" && errs ? " err" : ""}`, title: k === "flags" ? `${openFlags.length} open` : "" }, counts[k]))));
}

function renderFilters() {
  const tradeSel = $("#tradeSel");
  const showTrade = S.tab === "mb" || S.tab === "abstract";
  tradeSel.hidden = !showTrade;
  if (showTrade) {
    const trades = [...new Set(S.mb.rows.map((r) => r.trade).concat(S.mb.abstract.map((a) => a.trade)))];
    tradeSel.replaceChildren(el("option", { value: "" }, "All trades"), ...trades.map((t) => el("option", { value: t, selected: t === S.trade }, t)));
    tradeSel.value = S.trade;
  }
  const seg = $("#statusSeg");
  let opts = null, cur = null;
  if (S.tab === "mb") {
    const c = { all: S.mb.rows.length, ok: 0, assumed: 0, unresolved: 0, scaled: 0 };
    for (const r of S.mb.rows) c[r.chip] = (c[r.chip] || 0) + 1;
    opts = [["all", "All", c.all], ["ok", "OK", c.ok], c.scaled ? ["scaled", "Scaled", c.scaled] : null,
      ["assumed", "Assumed", c.assumed], ["unresolved", "Unresolved", c.unresolved]].filter(Boolean);
    if (!opts.some(([k]) => k === S.status)) S.status = "all";
    cur = S.status;
  } else if (S.tab === "flags") {
    const open = S.mb.flags.filter((f) => f.status === "open").length;
    opts = [["open", "Open", open], ["done", "Resolved", S.mb.flags.length - open], ["all", "All", S.mb.flags.length]];
    cur = S.flagStatus;
  }
  seg.hidden = !opts;
  if (opts) {
    seg.replaceChildren(...opts.map(([k, label, n]) => el("button", {
      type: "button", "aria-pressed": String(cur === k),
      onclick: () => { if (S.tab === "flags") S.flagStatus = k; else S.status = k; renderLedger(); },
    }, label, el("b", {}, n))));
  }
  const ph = { mb: "Search items, rooms, tags", abstract: "Search items", rooms: "Search rooms", openings: "Search tags or rooms", dims: "Search values or views", flags: "Search flags",
    steel: "Search elements", schedules: "Search schedules, tags", calib: "Search sheets" }[S.tab];
  $("#q").placeholder = S.touch ? ph : `${ph}  ( / )`;
}

/** Worst status of a set of lines; "scaled" ranks between assumed and ok. */
function worstOf(rows) {
  if (rows.some((x) => x.status === "unresolved")) return "unresolved";
  if (rows.some((x) => x.status === "assumed" && !x.scaled)) return "assumed";
  if (rows.some((x) => x.scaled)) return "scaled";
  return "ok";
}

// ------------------------------------------------------------------ main render
export function renderLedger() {
  if (!S.mb) return;
  renderTabs();
  renderFilters();
  S.visible = [];
  S.shown = null;
  const table = $("#table");
  const scroller = $("#scroller");
  scroller.querySelectorAll(".flagwrap, .panewrap, .cardwrap").forEach((n) => n.remove());
  const panes = { steel: renderSteel, schedules: renderSchedules, calib: renderCalibration };
  table.hidden = S.compact || S.tab === "flags" || S.tab in panes;
  if (S.compact) table.replaceChildren();
  if (S.tab in panes) { scroller.append(el("div", { class: "panewrap" }, panes[S.tab]())); applySelection(false); renderFoot(); return; }
  if (S.compact && S.tab !== "flags") {
    scroller.append(el("div", { class: "cardwrap" }, ({ mb: cardsMB, abstract: cardsAbstract, rooms: cardsRooms, openings: cardsOpenings, dims: cardsDims })[S.tab]()));
    applySelection(false);
    renderFoot();
    return;
  }
  ({ mb: renderMB, abstract: renderAbstract, rooms: renderRooms, openings: renderOpenings, dims: renderDims, flags: renderFlags })[S.tab]();
  applySelection(false);
  renderFoot();
}

const match = (q, ...xs) => !q || xs.some((x) => x != null && String(x).toLowerCase().includes(q));

function head(cols) {
  return el("thead", {}, el("tr", {}, cols.filter(Boolean).map(([label, cls, title]) => el("th", { class: cls || "", scope: "col", title }, label))));
}

function selectable(tr, kind, key) {
  tr.dataset.kind = kind;
  tr.dataset.key = key;
  tr.tabIndex = -1;
  tr.addEventListener("click", (e) => { if (!e.target.closest("button, a, input, details")) A.select(kind, key, { from: "table" }); });
  tr.addEventListener("mouseenter", () => A.preview(kind, key));
  tr.addEventListener("mouseleave", () => A.preview(null));
  S.visible.push({ kind, key });
  return tr;
}

function tradeRow(trade, cols, info) {
  const t = tradeOf(trade);
  return el("tr", { class: "trade" }, el("th", { colspan: cols, scope: "rowgroup" },
    el("div", { class: "tw" }, el("span", { class: "sw", style: { background: t.p } }), trade, el("span", { class: "tc" }, info))));
}

const n3 = (v) => (v == null ? "" : fd(v));

function renderMB() {
  const q = S.q.toLowerCase();
  const rows = S.mb.rows;
  const table = $("#table");
  if (!rows.length) {
    table.replaceChildren(el("tbody", {}, el("tr", { class: "empty" }, el("td", { colspan: 10 },
      S.source === "build"
        ? "No line-by-line measurements from this server yet. See the Abstract, Rooms and Doors & windows tabs."
        : "No measurements yet. Build the book from the Review step."))));
    return;
  }
  // engines that send no per-line description: show the location in the description slot
  const compact = S.narrow || !rows.some((r) => r.desc);
  const vis = rows.filter((r) => (!S.trade || r.trade === S.trade) && (S.status === "all" || r.chip === S.status)
    && match(q, r.item, r.desc, r.location, r.remarks, r.trade, r.sno, r.floor_group));
  const body = el("tbody");
  let lastTrade = null, lastItem = null;
  const byTrade = new Map();
  for (const r of vis) byTrade.set(r.trade, (byTrade.get(r.trade) || 0) + 1);
  S.shown = vis.length;
  const emit = (r) => {
    const t = tradeOf(r.trade);
    if (r.trade !== lastTrade) {
      const items = new Set(vis.filter((x) => x.trade === r.trade).map((x) => x.item)).size;
      body.append(tradeRow(r.trade, 10, `${items} item${items > 1 ? "s" : ""} · ${byTrade.get(r.trade)} lines`));
      lastTrade = r.trade; lastItem = null;
    }
    if (r.item !== lastItem) {
      const all = S.mb._itemRows.get(r.item) || [];
      const tot = all.reduce((s, x) => s + (x.qty || 0), 0);
      const a = S.mb.abstract.find((x) => x.item === r.item);
      const tr = el("tr", { class: "item", style: { "--tc": t.p } },
        el("td", { class: "c-sno" }, r.item_sno ?? ""),
        el("td", { colspan: compact ? 5 : 6, class: "c-itemd" }, el("div", { class: "it" }, r.item), r.code ? el("small", { class: "muted" }, r.code) : null),
        el("td", { class: "num c-q", title: "Item total (all lines)" }, fq(tot)),
        el("td", { class: "c-unit" }, unit(all[0]?.unit)),
        el("td", { class: "c-st" }, chip(worstOf(all))));
      body.append(a ? selectable(tr, "abstract", String(a.sno)) : tr);
      lastItem = r.item;
    }
    const neg = (r.nos ?? 1) < 0 || r.qty < 0;
    const cols = S.narrow ? 9 : 10;
    // floor group ("2nd to 6th floor slab (S-05) ×5") unless the location already says it
    const fg = r.floor_group && !String(r.location || "").includes(r.floor_group) ? r.floor_group : "";
    const sub = [compact && r.desc ? r.location : "", fg].filter(Boolean).join(" · ");
    const tr = el("tr", { class: `row${neg ? " neg" : ""}`, style: { "--tc": t.p } },
      el("td", { class: "c-sno" }, r.sno),
      el("td", { class: "desc" }, r.desc || (compact ? r.location : el("span", { class: "muted" }, "—")), sub ? el("small", {}, sub) : null),
      compact ? null : el("td", { class: "loc c-loc" }, r.location),
      el("td", { class: "num c-nos" }, r.nos == null ? "" : fmt(r.nos, 3)),
      el("td", { class: "num", title: r.l_formula ? `= ${r.l_formula}` : null }, n3(r.l)),
      el("td", { class: "num" }, n3(r.b)),
      el("td", { class: "num", title: r.h_formula ? `= ${r.h_formula}` : null }, n3(r.h)),
      el("td", { class: "num c-q" }, fq(r.qty)),
      el("td", { class: "c-unit" }, unit(r.unit)),
      el("td", { class: "c-st" }, chip(r.chip || r.status)));
    if (S.linked.has(r.id)) tr.classList.add("linked");
    body.append(selectable(tr, "row", r.id));
  };
  // long books (thousands of lines): the first screenfuls now, the rest in small batches after
  // the page has painted, so typing in the search box and switching tabs stay quick
  const token = (S.renderToken = (S.renderToken || 0) + 1);
  const selAt = S.sel?.kind === "row" ? vis.findIndex((r) => r.id === S.sel.key) : -1;
  let i = 0;
  const first = Math.max(250, selAt + 40);
  for (; i < Math.min(vis.length, first); i++) emit(vis[i]);
  const more = () => {
    if (token !== S.renderToken || S.tab !== "mb" || S.compact) return;
    const end = Math.min(vis.length, i + 400);
    for (; i < end; i++) emit(vis[i]);
    if (i < vis.length) setTimeout(more, 0);
    else applySelection(false);
  };
  if (i < vis.length) setTimeout(more, 0);
  if (!vis.length) body.append(el("tr", { class: "empty" }, el("td", { colspan: 10 }, "No lines match. Clear the search or filters.")));
  table.replaceChildren(head([["S.No", "c-sno"], [compact ? (rows.some((r) => r.desc) ? "Description · location" : "Location") : "Description"], compact ? null : ["Location", "c-loc"], ["Nos", "num c-nos"], ["L", "num", "Length, m"],
    ["B", "num", "Breadth, m"], ["H/D", "num", "Height or depth, m"], ["Qty", "num"], ["Unit", "c-unit"], ["Status", "c-st"]]), body);
}

function renderAbstract() {
  const q = S.q.toLowerCase();
  const vis = S.mb.abstract.filter((a) => (!S.trade || a.trade === S.trade) && match(q, a.item, a.trade, a.note));
  const body = el("tbody");
  let last = null;
  for (const a of vis) {
    if (a.trade !== last) {
      const n = vis.filter((x) => x.trade === a.trade).length;
      body.append(tradeRow(a.trade, 6, `${n} item${n > 1 ? "s" : ""}`));
      last = a.trade;
    }
    const rows = a.row_ids.map(rowById).filter(Boolean);
    const worst = worstOf(rows);
    const tr = el("tr", { class: "row", style: { "--tc": tradeOf(a.trade).p } },
      el("td", { class: "c-sno" }, a.sno),
      el("td", { class: "desc" }, a.item, a.note ? el("small", {}, a.note) : null),
      el("td", { class: "num", title: "MB lines that make up this item" }, rows.length || ""),
      el("td", { class: "num c-q" }, fq(a.qty)),
      el("td", { class: "c-unit" }, unit(a.unit)),
      el("td", { class: "c-st" }, rows.length ? chip(worst) : ""));
    body.append(selectable(tr, "abstract", String(a.sno)));
  }
  if (!vis.length) body.append(el("tr", { class: "empty" }, el("td", { colspan: 6 }, "No abstract items match.")));
  $("#table").replaceChildren(head([["S.No", "c-sno"], ["Item"], ["Lines", "num"], ["Quantity", "num"], ["Unit", "c-unit"], ["Status", "c-st"]]), body);
}

function renderRooms() {
  const q = S.q.toLowerCase();
  const body = el("tbody");
  const vis = S.mb.rooms.map((r, i) => [r, i]).filter(([r]) => match(q, r.name, r.level, r.label_size, r.check, r.remarks));
  let total = 0;
  for (const [r, i] of vis) {
    const chk = String(r.check || "");
    const cls = /mismatch|error/i.test(chk) ? "mismatch" : /ok/i.test(chk) ? "ok" : chk ? "note" : "";
    total += r.area || 0;
    const tr = el("tr", { class: "row", style: { "--tc": "#8f7300" } },
      el("td", { class: "desc" }, r.name, r.remarks ? el("small", {}, r.remarks) : null),
      el("td", { class: "loc" }, r.level || ""),
      el("td", { class: "num" }, r.label_size ? String(r.label_size).replace(/x/i, " × ") : ""),
      el("td", { class: "num" }, r.measured_l != null ? `${fmt(r.measured_l, 0)} × ${fmt(r.measured_b, 0)}` : ""),
      el("td", { class: "num c-q" }, r.area != null ? fq(r.area) : ""),
      el("td", { class: "c-st" }, cls ? el("span", { class: `chip ${cls}` }, chk) : ""));
    body.append(selectable(tr, "room", String(i)));
  }
  if (!vis.length) body.append(el("tr", { class: "empty" }, el("td", { colspan: 6 }, "No rooms found.")));
  else body.append(el("tr", { class: "total" }, el("td", { colspan: 4 }, `${vis.length} rooms (sizes in mm)`), el("td", { class: "num" }, fq(total)), el("td", {}, "m²")));
  $("#table").replaceChildren(head([["Room"], ["Level"], ["Label size", "num"], ["Measured L × B", "num"], ["Area m²", "num"], ["Check", "c-st"]]), body);
}

function renderOpenings() {
  const q = S.q.toLowerCase();
  const body = el("tbody");
  const vis = S.mb.openings.map((o, i) => [o, i]).filter(([o]) => match(q, o.type, o.location, o.description, o.kind));
  let last = null, nos = 0, area = 0;
  for (const [o, i] of vis) {
    if (o.type !== last) {
      const same = vis.filter(([x]) => x.type === o.type).map(([x]) => x);
      const n = same.reduce((s, x) => s + (x.nos || 0), 0);
      body.append(el("tr", { class: "trade" }, el("th", { colspan: 6, scope: "rowgroup" }, el("div", { class: "tw" },
        el("span", { class: "sw", style: { background: tradeOf("Doors & windows").p } }), o.type,
        el("span", { class: "tc" }, `${o.w ? `${fmt(o.w, 0)} × ${fmt(o.h, 0)} mm` : "size unknown"} · ${n} nos`)))));
      last = o.type;
    }
    nos += o.nos || 0; area += o.area || 0;
    const tr = el("tr", { class: "row", style: { "--tc": tradeOf("Doors & windows").p } },
      el("td", { class: "c-sno" }, o.type),
      el("td", { class: "desc" }, o.location || "", o.description ? el("small", {}, o.description) : null),
      el("td", { class: "num" }, o.w ? `${fmt(o.w, 0)} × ${fmt(o.h, 0)}` : "?"),
      el("td", { class: "num" }, o.sill != null ? fmt(o.sill, 0) : ""),
      el("td", { class: "num" }, o.nos ?? ""),
      el("td", { class: "num c-q" }, o.area != null ? fq(o.area) : ""));
    body.append(selectable(tr, "opening", String(i)));
  }
  if (!vis.length) body.append(el("tr", { class: "empty" }, el("td", { colspan: 6 }, "No doors or windows found.")));
  else body.append(el("tr", { class: "total" }, el("td", { colspan: 4 }, "Total doors, windows and ventilators"), el("td", { class: "num" }, nos), el("td", { class: "num" }, `${fq(area)} m²`)));
  $("#table").replaceChildren(head([["Tag", "c-sno"], ["Location"], ["W × H mm", "num"], ["Sill", "num"], ["Nos", "num"], ["Area m²", "num"]]), body);
}

function renderDims() {
  const q = S.q.toLowerCase();
  const body = el("tbody");
  const vis = S.mb.dimensions.map((d, i) => [d, i]).filter(([d]) => match(q, d.value, d.region, d.layer, d.text));
  let last = null;
  const MAX = 3000;
  for (const [d, i] of vis.slice(0, MAX)) {
    if (d.region !== last) {
      const n = vis.filter(([x]) => x.region === d.region).length;
      body.append(el("tr", { class: "trade" }, el("th", { colspan: 4, scope: "rowgroup" }, el("div", { class: "tw" }, d.region || "Unassigned", el("span", { class: "tc" }, `${n} dimensions`)))));
      last = d.region;
    }
    const tr = el("tr", { class: "row" },
      el("td", { class: "num c-q" }, d.value != null ? fmt(d.value, 1) : ""),
      el("td", { class: "loc" }, d.text && d.text !== "<>" ? `“${d.text}”` : ""),
      el("td", { class: "loc" }, d.layer || ""),
      el("td", { class: "loc" }, d.region || ""));
    body.append(selectable(tr, "dim", String(i)));
  }
  if (!vis.length) body.append(el("tr", { class: "empty" }, el("td", { colspan: 4 }, "No dimensions found.")));
  $("#table").replaceChildren(head([["Value mm", "num"], ["Text override"], ["Layer"], ["View"]]), body);
}

// ------------------------------------------------------------------ flags
function renderFlags() {
  const q = S.q.toLowerCase();
  const flags = S.mb.flags;
  const counts = { error: 0, warning: 0, missing: 0 };
  for (const f of flags) if (f.status === "open" && counts[f.severity] != null) counts[f.severity]++;
  const sum = el("div", { class: "sev-sum", role: "group", "aria-label": "Open flags by severity" },
    Object.keys(counts).map((sev) => el("button", {
      type: "button", "aria-pressed": String(S.sevOn[sev]), title: `Show or hide ${sev} flags`,
      onclick: () => { S.sevOn[sev] = !S.sevOn[sev]; renderLedger(); },
    }, sevGlyph(sev), el("b", {}, counts[sev]), el("span", {}, `${SEV[sev].label}${counts[sev] === 1 ? "" : "s"}`, el("br"), "open"))));
  const vis = flags.filter((f) => (S.sevOn[f.severity] ?? true)
    && (S.flagStatus === "all" || (S.flagStatus === "open" ? f.status === "open" : f.status !== "open"))
    && match(q, f.title, f.detail, f.category, ...(f.evidence || [])));
  const list = el("ul", { class: "flags" });
  for (const f of vis) list.append(flagItem(f));
  if (!vis.length) list.append(el("li", { class: "flag" }, el("span"), el("p", { class: "d muted" },
    flags.length ? "Nothing in this filter." : "No flags. The checks found nothing missing or inconsistent.")));
  $("#scroller").append(el("div", { class: "flagwrap" }, sum, list));
}

function flagItem(f) {
  const done = f.status !== "open";
  const rows = S.mb.rows.filter((r) => r.flag_ids.includes(f.id));
  const li = el("li", { class: `flag${done ? " done" : ""}`, "data-kind": "flag", "data-key": f.id, tabindex: -1 });
  li.addEventListener("click", (e) => { if (!e.target.closest("button, input, form, summary, a")) (S.compact ? A.tapCard : A.select)("flag", f.id, { from: "table" }); });
  li.addEventListener("mouseenter", () => A.preview("flag", f.id));
  li.addEventListener("mouseleave", () => A.preview(null));
  S.visible.push({ kind: "flag", key: f.id });
  let action;
  if (done) {
    action = el("div", { class: "res" }, f.status === "resolved" ? `Resolved: ${f.value}${f.input?.unit ? ` ${f.input.unit}` : ""}` : "Accepted as drawn",
      el("button", { class: "link", type: "button", onclick: () => A.resolveFlag(f, "open") }, "Reopen"));
  } else {
    const inp = f.input ? el("input", {
      type: f.input.type === "number" ? "number" : "text", name: "v", placeholder: f.input.placeholder || "",
      inputmode: f.input.type === "number" ? "decimal" : null, step: "any", autocomplete: "off", enterkeyhint: "done",
      autocapitalize: f.input.type === "number" ? null : "characters", "aria-describedby": f.suggested_fix ? `fx-${f.id}` : null,
    }) : null;
    action = el("form", {
      onsubmit: (e) => {
        e.preventDefault();
        if (!inp) return;
        const raw = inp.value.trim() || (f.input.placeholder || "").trim();
        if (!raw) { inp.focus(); return; }
        A.resolveFlag(f, "resolved", f.input.type === "number" ? Number(raw) : raw);
      },
    },
    inp ? el("label", {}, `${f.input.label}${f.input.unit && !f.input.label.includes(f.input.unit) ? ` (${f.input.unit})` : ""}`, inp) : null,
    inp ? el("button", { class: "btn primary", type: "submit" }, "Resolve") : null,
    el("button", { class: "btn", type: "button", onclick: () => A.resolveFlag(f, "accepted") }, "Accept as drawn"));
  }
  li.append(...[sevGlyph(f.severity),
    el("h3", {}, f.title),
    el("div", { class: "meta" }, `${SEV[f.severity]?.label || f.severity}, ${f.category}${f.refs.length ? "" : ". No drawing location"}`),
    f.detail ? el("p", { class: "d" }, f.detail) : null,
    f.evidence?.length ? el("details", {}, el("summary", {}, `Evidence (${f.evidence.length})`), el("ul", {}, f.evidence.map((x) => el("li", {}, x)))) : null,
    f.suggested_fix && !done ? el("p", { class: "fix", id: `fx-${f.id}` }, el("b", {}, "Fix: "), f.suggested_fix) : null,
    rows.length ? el("div", { class: "affects" }, `Affects ${rows.length} MB line${rows.length > 1 ? "s" : ""}: `,
      el("button", { class: "link", type: "button", onclick: () => A.showRows(rows.map((r) => r.id)) }, "show in book")) : null,
    action].filter(Boolean));
  return li;
}

// ------------------------------------------------------------------ selection
export function applySelection(scroll = true) {
  const sel = S.sel;
  const scroller = $("#scroller");
  scroller.querySelectorAll(".sel").forEach((n) => n.classList.remove("sel"));
  scroller.querySelectorAll("tr.detail, .cdetail").forEach((n) => n.remove());
  scroller.querySelectorAll("tr.row[data-kind='row'], .card[data-kind='row']").forEach((n) => n.classList.toggle("linked", S.linked.has(n.dataset.key)));
  if (!sel) return;
  if (sel.kind === "calib" || sel.kind === "sample") {  // calibration row + its sample list
    const n = String(sel.key).split(":")[0];
    const row = scroller.querySelector(`[data-kind="calib"][data-key="${CSS.escape(n)}"]`);
    const det = row && calibDetail(n);
    if (det) row.after(det);
    if (row && sel.kind === "sample") row.classList.add("sel");
  }
  const node = scroller.querySelector(`[data-kind="${sel.kind}"][data-key="${CSS.escape(sel.key)}"]`);
  if (!node) return;
  node.classList.add("sel");
  if (sel.kind === "row") {
    const r = rowById(sel.key);
    if (r) node.after(node.tagName === "TR" ? detailRow(r) : cardDetail(r));
  }
  if (scroll) node.scrollIntoView({ block: "nearest" });
  if (scroll && document.activeElement?.closest?.("#ledger") && !document.activeElement.matches("input, select, textarea")) (node.querySelector?.(".k-main") || node).focus({ preventScroll: true });
}

function detailRow(r) {
  const flags = r.flag_ids.map((id) => S.mb.flags.find((f) => f.id === id)).filter(Boolean);
  const parts = [];
  if (r.l_formula) parts.push(el("span", {}, el("span", { class: "k" }, "L"), el("code", {}, `= ${r.l_formula}`)));
  if (r.h_formula) parts.push(el("span", {}, el("span", { class: "k" }, "H"), el("code", {}, `= ${r.h_formula}`)));
  const prod = [r.nos, r.l, r.b, r.h].filter((v) => v != null).map((v) => fmt(v, 3)).join(" × ");
  return el("tr", { class: "detail" }, el("td", { class: "c-sno" }), el("td", { colspan: 9 },
    el("div", { class: "detail-grid" },
      el("p", {}, el("span", { class: "k" }, "Source"), r.remarks || "No remarks."),
      r.floor_group ? el("p", {}, el("span", { class: "k" }, "Floor"), r.floor_group) : null,
      el("p", { class: "num" }, el("span", { class: "k" }, "Qty"), `${prod} = `, el("b", {}, `${fq(r.qty)} ${unit(r.unit)}`)),
      parts.length ? el("p", {}, parts) : null,
      el("p", { class: "muted small" }, r.refs.length
        ? `${r.refs.length} drawing reference${r.refs.length > 1 ? "s" : ""}${r.refs.some((x) => x.handles?.length) ? " (by entity handle)" : " (by area)"}${sheetsNote(r.refs)}`
        : "No drawing reference for this line."),
      flags.length ? el("div", { class: "flaglinks" }, flags.map((f) => el("button", {
        class: "flaglink", type: "button", onclick: () => { A.setTab("flags"); A.select("flag", f.id, { from: "table" }); },
      }, sevGlyph(f.severity), f.status === "open" ? f.title : `${f.title} (${f.status})`))) : null)));
}

function sheetsNote(refs) {
  const names = [...new Set(refs.map((x) => x.sheet).filter((n) => n != null))].map((n) => A.sheetName?.(n) || `sheet ${n}`);
  return names.length ? ` on ${names.join(", ")}` : "";
}

export function renderFoot() {
  const foot = $("#foot");
  const mb = S.mb;
  const sel = S.sel;
  const bits = [];
  if (S.tab === "mb") {
    const shown = S.shown ?? $("#table").querySelectorAll("tr.row").length;
    bits.push(el("span", {}, el("b", {}, shown), shown === mb.rows.length ? " lines" : ` of ${mb.rows.length} lines`));
  }
  if (sel?.kind === "row") {
    const r = rowById(sel.key);
    if (r) bits.push(el("span", {}, `Line ${r.sno}: `, el("b", {}, `${fq(r.qty)} ${unit(r.unit)}`)));
  } else if (sel?.kind === "abstract") {
    const a = mb.abstract.find((x) => String(x.sno) === sel.key);
    if (a) bits.push(el("span", {}, `Item ${a.sno}: `, el("b", {}, `${fq(a.qty)} ${unit(a.unit)}`), ` from ${a.row_ids.length} lines`));
  } else if (sel?.kind === "entity") {
    bits.push(el("span", {}, el("b", {}, S.linked.size), S.linked.size === 1 ? " line uses the picked entity" : " lines use the picked entity"));
  }
  if (S.linked.size && sel?.kind !== "entity") bits.push(el("span", {}, el("b", {}, S.linked.size), " linked lines"));
  bits.push(el("span", { class: "kbd" }, el("kbd", {}, "↑"), " ", el("kbd", {}, "↓"), " move  ", el("kbd", {}, "F"), " fit  ", el("kbd", {}, "Esc"), " clear"));
  foot.replaceChildren(...bits);
}

// ------------------------------------------------------------------ cards (phones)
// The same content as the tables, one card per line: S.No, item, location, the
// Nos × L × B × H = Qty line, unit, status and flags. Trade groups fold up.
const CARD_PAGE = 400;
const shownLimit = () => S.cardLimit?.[S.tab] ?? CARD_PAGE;

function moreButton(left) {
  return el("button", { class: "btn cmore", type: "button", onclick: () => {
    S.cardLimit = { ...(S.cardLimit || {}), [S.tab]: shownLimit() + CARD_PAGE };
    renderLedger();
  } }, `Show ${Math.min(left, CARD_PAGE)} more (${left} not shown)`);
}

/** A card: a full-width button (select + show on the drawing) and a side column (status, flags). */
function card(kind, key, { cls = "", tc = null, main = [], side = [], neg = false } = {}) {
  S.visible.push({ kind, key });
  return el("div", { class: `card ${cls}${neg ? " neg" : ""}`, style: tc ? { "--tc": tc } : null, "data-kind": kind, "data-key": key, tabindex: "-1" },
    el("button", { class: "k-main", type: "button", onclick: () => A.tapCard(kind, key) }, main, el("span", { class: "sr-only" }, ". Show on the drawing")),
    el("div", { class: "k-side" }, side));
}

const CHEV = '<svg class="chev" viewBox="0 0 20 20" aria-hidden="true"><path d="m5 8 5 5 5-5"/></svg>';

/** A foldable group (a trade, a door type, a view); kids() is only built when it is open. */
function group(id, title, info, color, kids) {
  const key = `${S.tab}:${id}`;
  S.collapsed ??= new Set();
  const open = !S.collapsed.has(key);
  const bodyId = `cg-${S.tab}-${String(id).replace(/[^\w-]/g, "_")}`;
  return el("section", { class: "cgroup" },
    el("h3", { class: "cg-h" }, el("button", {
      class: "cg-t", type: "button", "aria-expanded": String(open), "aria-controls": bodyId, "data-g": key,
      onclick: () => {
        if (S.collapsed.has(key)) S.collapsed.delete(key); else S.collapsed.add(key);
        renderLedger();
        $(`#scroller [data-g="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
      },
    }, color ? el("span", { class: "sw", style: { background: color } }) : null, el("span", {}, title), el("span", { class: "tc" }, info), svg(CHEV))),
    el("div", { class: "cg-body", id: bodyId, hidden: !open }, open ? kids() : null));
}

function flagBadge(r) {
  const open = r.flag_ids.map((id) => S.mb.flags.find((f) => f.id === id)).filter((f) => f && f.status === "open");
  if (!open.length) return null;
  const sev = ["error", "warning", "missing"].find((v) => open.some((f) => f.severity === v)) || open[0].severity;
  return el("button", { class: "fbadge", type: "button", "aria-label": `${open.length} open flag${open.length > 1 ? "s" : ""}: ${open.map((f) => f.title).join("; ")}. Show in Flags`,
    onclick: () => { A.setTab("flags"); A.select("flag", open[0].id, { from: "table" }); } }, sevGlyph(sev), String(open.length));
}

/** Σ of a set of lines when they share a unit ("" otherwise). */
function sumLine(rows, get = (x) => x.qty) {
  const units = new Set(rows.map((x) => unit(x.unit)));
  if (units.size !== 1) return "";
  return ` · Σ ${fq(rows.reduce((a, x) => a + (get(x) || 0), 0))} ${[...units][0]}`;
}

function formula(r) {
  const prod = [r.nos, r.l, r.b, r.h].filter((v) => v != null).map((v) => fmt(v, 3)).join(" × ");
  return el("span", { class: "k-f" }, prod ? `${prod} = ` : "", el("b", {}, fq(r.qty)), ` ${unit(r.unit)}`);
}

function cardsMB() {
  const q = S.q.toLowerCase();
  const rows = S.mb.rows;
  if (!rows.length) return [el("p", { class: "cempty" }, S.source === "build"
    ? "No line-by-line measurements from this server yet. See the Abstract, Rooms and Doors & windows tabs."
    : "No measurements yet. Build the book from the Review step.")];
  const vis = rows.filter((r) => (!S.trade || r.trade === S.trade) && (S.status === "all" || r.chip === S.status)
    && match(q, r.item, r.desc, r.location, r.remarks, r.trade, r.sno, r.floor_group));
  S.shown = vis.length;
  if (!vis.length) return [el("p", { class: "cempty" }, "No lines match. Clear the search or filters.")];
  const trades = [...new Set(vis.map((r) => r.trade))];
  let budget = shownLimit(), left = 0;
  const out = trades.map((trade) => {
    const tr = vis.filter((r) => r.trade === trade);
    const items = [...new Set(tr.map((r) => r.item))];
    const t = tradeOf(trade);
    return group(trade, trade, `${items.length} item${items.length > 1 ? "s" : ""} · ${tr.length} lines${sumLine(tr)}`, t.p, () => {
      const kids = [];
      for (const item of items) {
        const lines = tr.filter((r) => r.item === item);
        if (budget <= 0) { left += lines.length; continue; }
        const all = S.mb._itemRows.get(item) || lines;
        const a = S.mb.abstract.find((x) => x.item === item);
        const total = all.reduce((acc, x) => acc + (x.qty || 0), 0);
        const head = [el("span", { class: "k-head" }, el("span", { class: "k-sno" }, lines[0].item_sno ?? ""), el("span", { class: "k-desc" }, item)),
          lines[0].code ? el("span", { class: "k-loc" }, lines[0].code) : null,
          el("span", { class: "k-f" }, "Item total ", el("b", {}, fq(total)), ` ${unit(all[0]?.unit)} · ${all.length} line${all.length > 1 ? "s" : ""}`)];
        kids.push(a ? card("abstract", String(a.sno), { cls: "item", tc: t.p, main: head, side: [chip(worstOf(all))] })
          : el("div", { class: "card item", style: { "--tc": t.p } }, el("div", { class: "k-main" }, head), el("div", { class: "k-side" }, chip(worstOf(all)))));
        for (const r of lines) {
          if (budget-- <= 0) { left++; continue; }
          const fg = r.floor_group && !String(r.location || "").includes(r.floor_group) ? r.floor_group : "";
          const loc = [r.desc ? r.location : "", fg].filter(Boolean).join(" · ");
          const c = card("row", r.id, { cls: "row", tc: t.p, neg: (r.nos ?? 1) < 0 || r.qty < 0,
            main: [el("span", { class: "k-head" }, el("span", { class: "k-sno" }, r.sno), el("span", { class: "k-desc" }, r.desc || r.location || "—")),
              loc ? el("span", { class: "k-loc" }, loc) : null, formula(r)],
            side: [chip(r.chip || r.status), flagBadge(r)] });
          if (S.linked.has(r.id)) c.classList.add("linked");
          kids.push(c);
        }
      }
      return kids;
    });
  });
  if (left) out.push(moreButton(left));
  return out;
}

function cardsAbstract() {
  const q = S.q.toLowerCase();
  const vis = S.mb.abstract.filter((a) => (!S.trade || a.trade === S.trade) && match(q, a.item, a.trade, a.note));
  if (!vis.length) return [el("p", { class: "cempty" }, "No abstract items match.")];
  return [...new Set(vis.map((a) => a.trade))].map((trade) => {
    const items = vis.filter((a) => a.trade === trade);
    const t = tradeOf(trade);
    return group(trade, trade, `${items.length} item${items.length > 1 ? "s" : ""}${sumLine(items)}`, t.p, () => items.map((a) => {
      const rows = a.row_ids.map(rowById).filter(Boolean);
      return card("abstract", String(a.sno), { cls: "row", tc: t.p,
        main: [el("span", { class: "k-head" }, el("span", { class: "k-sno" }, a.sno), el("span", { class: "k-desc" }, a.item)),
          a.note ? el("span", { class: "k-loc" }, a.note) : null,
          el("span", { class: "k-f" }, el("b", {}, fq(a.qty)), ` ${unit(a.unit)}${rows.length ? ` · ${rows.length} line${rows.length > 1 ? "s" : ""}` : ""}`)],
        side: [rows.length ? chip(worstOf(rows)) : null] });
    }));
  });
}

function cardsRooms() {
  const q = S.q.toLowerCase();
  const vis = S.mb.rooms.map((r, i) => [r, i]).filter(([r]) => match(q, r.name, r.level, r.label_size, r.check, r.remarks));
  if (!vis.length) return [el("p", { class: "cempty" }, "No rooms found.")];
  let total = 0;
  const out = vis.map(([r, i]) => {
    const chk = String(r.check || "");
    const cls = /mismatch|error/i.test(chk) ? "mismatch" : /ok/i.test(chk) ? "ok" : chk ? "note" : "";
    total += r.area || 0;
    return card("room", String(i), { cls: "row", tc: "#8f7300",
      main: [el("span", { class: "k-head" }, el("span", { class: "k-desc" }, r.name)),
        r.level || r.remarks ? el("span", { class: "k-loc" }, [r.level, r.remarks].filter(Boolean).join(" · ")) : null,
        el("span", { class: "k-kv" },
          r.label_size ? el("span", {}, "Label ", el("b", {}, String(r.label_size).replace(/x/i, " × "))) : null,
          r.measured_l != null ? el("span", {}, "Measured ", el("b", {}, `${fmt(r.measured_l, 0)} × ${fmt(r.measured_b, 0)}`)) : null),
        r.area != null ? el("span", { class: "k-f" }, "Area ", el("b", {}, fq(r.area)), " m²") : null],
      side: [cls ? el("span", { class: `chip ${cls}` }, chk) : null] });
  });
  out.push(el("div", { class: "ctotal" }, el("span", {}, `${vis.length} rooms (sizes in mm)`), el("span", {}, `${fq(total)} m²`)));
  return out;
}

function cardsOpenings() {
  const q = S.q.toLowerCase();
  const vis = S.mb.openings.map((o, i) => [o, i]).filter(([o]) => match(q, o.type, o.location, o.description, o.kind));
  if (!vis.length) return [el("p", { class: "cempty" }, "No doors or windows found.")];
  const col = tradeOf("Doors & windows").p;
  let nos = 0, area = 0;
  for (const [o] of vis) { nos += o.nos || 0; area += o.area || 0; }
  const out = [...new Set(vis.map(([o]) => o.type))].map((type) => {
    const same = vis.filter(([o]) => o.type === type);
    const o0 = same[0][0], n = same.reduce((a, [o]) => a + (o.nos || 0), 0);
    return group(type, type, `${o0.w ? `${fmt(o0.w, 0)} × ${fmt(o0.h, 0)} mm` : "size unknown"} · ${n} nos`, col, () => same.map(([o, i]) => card("opening", String(i), { cls: "row", tc: col,
      main: [el("span", { class: "k-head" }, el("span", { class: "k-sno" }, o.type), el("span", { class: "k-desc" }, o.location || "—")),
        o.description ? el("span", { class: "k-loc" }, o.description) : null,
        el("span", { class: "k-kv" }, el("span", {}, "W × H ", el("b", {}, o.w ? `${fmt(o.w, 0)} × ${fmt(o.h, 0)}` : "?")),
          o.sill != null ? el("span", {}, "Sill ", el("b", {}, fmt(o.sill, 0))) : null,
          el("span", {}, "Nos ", el("b", {}, o.nos ?? "")),
          o.area != null ? el("span", {}, "Area ", el("b", {}, `${fq(o.area)} m²`)) : null)] })));
  });
  out.push(el("div", { class: "ctotal" }, el("span", {}, `${nos} doors, windows and ventilators`), el("span", {}, `${fq(area)} m²`)));
  return out;
}

function cardsDims() {
  const q = S.q.toLowerCase();
  const vis = S.mb.dimensions.map((d, i) => [d, i]).filter(([d]) => match(q, d.value, d.region, d.layer, d.text));
  if (!vis.length) return [el("p", { class: "cempty" }, "No dimensions found.")];
  let budget = shownLimit(), left = 0;
  const out = [...new Set(vis.map(([d]) => d.region))].map((region) => {
    const same = vis.filter(([d]) => d.region === region);
    return group(region || "-", region || "Unassigned", `${same.length} dimensions`, null, () => same.flatMap(([d, i]) => {
      if (budget-- <= 0) { left++; return []; }
      return [card("dim", String(i), { cls: "row",
        main: [el("span", { class: "k-head" }, el("span", { class: "k-desc" }, d.value != null ? `${fmt(d.value, 1)} mm` : "—")),
          el("span", { class: "k-kv" }, d.text && d.text !== "<>" ? el("span", {}, "Text ", el("b", {}, `“${d.text}”`)) : null, d.layer ? el("span", {}, "Layer ", el("b", {}, d.layer)) : null)] })];
    }));
  });
  if (left) out.push(moreButton(left));
  return out;
}

function cardDetail(r) {
  const flags = r.flag_ids.map((id) => S.mb.flags.find((f) => f.id === id)).filter(Boolean);
  return el("div", { class: "cdetail" },
    el("p", {}, el("span", { class: "k" }, "Source"), r.remarks || "No remarks."),
    r.floor_group ? el("p", {}, el("span", { class: "k" }, "Floor"), r.floor_group) : null,
    r.l_formula ? el("p", {}, el("span", { class: "k" }, "L"), el("code", {}, `= ${r.l_formula}`)) : null,
    r.h_formula ? el("p", {}, el("span", { class: "k" }, "H"), el("code", {}, `= ${r.h_formula}`)) : null,
    el("p", { class: "muted small" }, r.refs.length
      ? `${r.refs.length} drawing reference${r.refs.length > 1 ? "s" : ""}${r.refs.some((x) => x.handles?.length) ? " (by entity handle)" : " (by area)"}${sheetsNote(r.refs)}`
      : "No drawing reference for this line."),
    flags.length ? el("div", { class: "flaglinks" }, flags.map((f) => el("button", {
      class: "flaglink", type: "button", onclick: () => { A.setTab("flags"); A.select("flag", f.id, { from: "table" }); },
    }, sevGlyph(f.severity), f.status === "open" ? f.title : `${f.title} (${f.status})`))) : null);
}

export { card, group };
