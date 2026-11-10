// Structural (PDF) jobs: the Steel, Schedules and Calibration tabs, and the drawing
// highlights behind them (schedule cells, calibration samples, steel by element).

import { S, A } from "./state.js";
import { el, fmt, fq, unionBox, validBox } from "./util.js";
import { tradeOf } from "./theme.js";
import { card } from "./ledger.js";

const match = (q, ...xs) => !q || xs.some((x) => x != null && String(x).toLowerCase().includes(q));
const STEEL_TRADE = "Steel";
const BAR = "#4458b8";      // single-series bar hue (validated: 3:1+ on the paper surface)
const SAMPLE = "#e9c46a";

export const sheetName = (n) => {
  const s = S.geo?.sheets?.find((x) => x.sheet === Number(n));
  return s?.title_block?.drawing_no || `Sheet ${n}`;
};

function pickable(node, kind, key, label) {
  node.dataset.kind = kind;
  node.dataset.key = key;
  node.tabIndex = -1;
  if (label) node.setAttribute("aria-label", label);
  node.addEventListener("click", (e) => { if (!e.target.closest("button, a, input, form, details > summary")) (S.compact ? A.tapCard : A.select)(kind, key, { from: "table" }); });
  node.addEventListener("mouseenter", () => A.preview(kind, key));
  node.addEventListener("mouseleave", () => A.preview(null));
  S.visible.push({ kind, key });
  return node;
}

// ================================================================ Steel
function elementRows(element) {
  return S.mb.rows.filter((r) => r.trade === STEEL_TRADE && new RegExp(`[–-]\\s*${escapeRe(element)}\\s*$`, "i").test(r.item));
}
const escapeRe = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Horizontal bars, one series: value at the tip, tooltip on hover/focus, keyboard reachable. */
function hbars(title, items, total, { kind, unitLabel = "t", scale = 1000 } = {}) {
  const max = Math.max(...items.map((x) => x.v), 1);
  return el("figure", { class: "hbar" },
    el("figcaption", {}, title),
    el("ul", { role: "list" }, items.map((x) => {
      const pct = total ? (x.v / total) * 100 : 0;
      const val = `${fmt(x.v / scale, 1, 1)} ${unitLabel}`;
      const li = el("li", {},
        el("span", { class: "lab" }, x.label),
        el("span", { class: "track" },
          el("span", { class: "bar", style: { width: `${Math.max(0.4, (x.v / max) * 100)}%`, background: BAR } }),
          el("span", { class: "val" }, val)),
        el("span", { class: "tip", role: "tooltip" }, el("b", {}, x.label), ` ${fmt(x.v, 1)} kg · ${fmt(pct, 1, 1)}% of all steel`));
      if (kind) pickable(li, kind, x.key, `${x.label}: ${val}, ${fmt(pct, 1)}%`);
      else li.tabIndex = 0;
      return li;
    })));
}

export function renderSteel() {
  const st = S.mb.steel;
  if (!st || !st.total_kg) return [el("p", { class: "pane-empty" }, "No reinforcement measured yet.")];
  const q = S.q.toLowerCase();
  const dias = Object.keys(st.by_dia_kg).map(Number).sort((a, b) => a - b);
  const byEl = Object.entries(st.by_element_kg).map(([name, m]) => ({ name, m, kg: Object.values(m).reduce((a, b) => a + b, 0) }))
    .filter((e) => match(q, e.name)).sort((a, b) => b.kg - a.kg);
  const assumedPct = st.total_kg ? (st.assumed_kg / st.total_kg) * 100 : 0;
  const tiles = el("div", { class: "tiles" },
    el("div", { class: "tile" }, el("span", {}, "Total steel"), el("b", {}, `${fmt(st.total_kg / 1000, 2, 2)} t`), el("small", {}, `${fmt(st.total_kg, 0)} kg`)),
    el("div", { class: "tile" }, el("span", {}, "Assumed or scaled"), el("b", {}, `${fmt(st.assumed_kg / 1000, 2, 2)} t`),
      el("small", {}, `${fmt(assumedPct, 0)}% of the total rests on a scaled length or an assumption`)),
    el("div", { class: "tile" }, el("span", {}, "Bar sizes"), el("b", {}, String(dias.length)), el("small", {}, dias.map((d) => `Ø${d}`).join(" · "))));

  const charts = el("div", { class: "charts" },
    hbars("By bar diameter", dias.map((d) => ({ label: `Ø${d}`, v: st.by_dia_kg[d], key: String(d) })), st.total_kg),
    hbars("By element", byEl.map((e) => ({ label: e.name, v: e.kg, key: e.name })), st.total_kg, { kind: "steel" }));

  if (S.compact) {
    const cards = byEl.map((e) => {
      const n = elementRows(e.name).length;
      return card("steel", e.name, { cls: "row", tc: tradeOf(STEEL_TRADE).p,
        main: [el("span", { class: "k-head" }, el("span", { class: "k-desc" }, e.name)),
          el("span", { class: "k-kv" }, dias.filter((d) => e.m[d]).map((d) => el("span", {}, `Ø${d} `, el("b", {}, `${fmt(e.m[d], 1)} kg`)))),
          el("span", { class: "k-f" }, "Total ", el("b", {}, fmt(e.kg, 1)), " kg")],
        side: [n ? el("button", { class: "link", type: "button", onclick: () => A.showRows(elementRows(e.name).map((r) => r.id)) }, `${n} MB lines`) : null] });
    });
    return [tiles, charts, el("h3", { class: "pane-h" }, "Steel by element"), ...cards,
      el("div", { class: "ctotal" }, el("span", {}, "Total"), el("span", {}, `${fmt(st.total_kg, 1)} kg`)),
      el("p", { class: "pane-note" }, "Weights use d²/162 kg per metre. Tap an element to see it on the drawing.")];
  }
  // the table view of the same numbers (element x diameter, kg)
  const body = el("tbody");
  for (const e of byEl) {
    const tr = el("tr", { class: "row", style: { "--tc": tradeOf(STEEL_TRADE).p } },
      el("td", { class: "desc" }, e.name, el("small", {}, el("button", { class: "link", type: "button", onclick: () => A.showRows(elementRows(e.name).map((r) => r.id)) }, `${elementRows(e.name).length} MB lines`))),
      ...dias.map((d) => el("td", { class: "num" }, e.m[d] ? fmt(e.m[d], 1) : "")),
      el("td", { class: "num c-q" }, fmt(e.kg, 1)));
    body.append(pickable(tr, "steel", e.name));
  }
  body.append(el("tr", { class: "total" }, el("td", {}, "Total kg"), ...dias.map((d) => el("td", { class: "num" }, fmt(st.by_dia_kg[d], 1))),
    el("td", { class: "num" }, fmt(st.total_kg, 1))));
  const table = el("table", { class: "mb steel-table" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Element"), ...dias.map((d) => el("th", { class: "num", scope: "col" }, `Ø${d} kg`)), el("th", { class: "num", scope: "col" }, "Total kg"))),
    body);
  return [tiles, charts, el("h3", { class: "pane-h" }, "Steel by element and bar size"), table,
    el("p", { class: "pane-note" }, "Weights use d²/162 kg per metre. Click an element to see it on the drawing.")];
}

// ================================================================ Schedules
const norm = (s) => String(s ?? "").toUpperCase().replace(/[\s.,:;_]+/g, "").replace(/[–—]/g, "-");

/** Text entities on a sheet inside a box (y-up world coordinates). */
function textsIn(sheet, box) {
  const v = S.viewer, out = [];
  if (!v?.E) return out;
  for (const i of v.query(box)) {
    const e = v.E[i];
    if (e.t !== "T" || (sheet != null && e.sheet !== sheet)) continue;
    const b = e.bb || [e.p[0], e.p[1], e.p[0], e.p[1]];
    const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
    if (cx >= box[0] && cx <= box[2] && cy >= box[1] && cy <= box[3]) out.push({ i, e, b, cx, cy, n: norm(e.s) });
  }
  return out;
}

/** Best text match for `needle` among texts, nearest to an anchor point. */
function bestText(texts, needle, ax, ay) {
  const n = norm(needle);
  if (!n) return [];
  let best = null, bd = Infinity;
  for (const t of texts) {
    if (!t.n) continue;
    const exact = t.n === n;
    const part = !exact && t.n.length >= Math.min(3, n.length) && n.includes(t.n);
    if (!exact && !part) continue;
    const d = (ax == null ? 0 : Math.abs(t.cx - ax)) + (ay == null ? 0 : Math.abs(t.cy - ay) * 3) + (exact ? 0 : 40);
    if (d < bd) { bd = d; best = t; }
  }
  if (!best) return [];
  // a cell split into several text runs ("#16-2NOS." "+#12-2NOS."): take its neighbours that also belong
  const h = Math.max(best.b[3] - best.b[1], 4);
  const out = [best];
  if (best.n !== n) {
    for (const t of texts) {
      if (t === best || !t.n || !n.includes(t.n) || t.n.length < 2) continue;
      if (Math.abs(t.cy - best.cy) < h * 2.6 && Math.abs(t.cx - best.cx) < h * 12) out.push(t);
    }
  }
  return out;
}

const cellText = (c) => (c && typeof c === "object" ? c.text ?? c.s ?? "" : c);
const cellRefs = (c) => (c && typeof c === "object" && (c.bbox || c.handles) ? [{ bbox: c.bbox || null, handles: c.handles || [], kind: "entity", sheet: c.sheet ?? null }] : null);

/** Locate one schedule cell on its sheet: engine refs when given, else by matching text
 *  between the row-label and column-header anchors inside the table's box. */
function locateCell(t, ri, ci) {
  const row = t.rows[ri], cell = row?.cells?.[ci];
  const given = cellRefs(cell) || (row?.refs?.[ci] ? [row.refs[ci]] : null);
  if (given) return { refs: given.map((r) => ({ ...r, sheet: r.sheet ?? t.sheet })), entities: null };
  const tb = t.bbox;
  if (!tb) return { refs: [], entities: null };
  const pad = Math.max(tb[2] - tb[0], tb[3] - tb[1]) * 0.03;
  const texts = textsIn(t.sheet, [tb[0] - pad, tb[1] - pad, tb[2] + pad, tb[3] + pad]);
  const rowHit = bestText(texts, row.label ?? cellText(row.cells?.[0]))[0];
  const colHit = bestText(texts, (t.columns?.[ci] || "").split(/\s+/).slice(0, 2).join(" "))[0];
  const hits = bestText(texts, cellText(cell), colHit?.cx, rowHit?.cy);
  if (!hits.length) return { refs: [{ bbox: tb, handles: [], kind: "table", sheet: t.sheet }], entities: null, approx: true };
  const box = unionBox(hits.map((h) => h.b));
  return { refs: [{ bbox: box, handles: [], kind: "text", sheet: t.sheet }], entities: hits.map((h) => h.i) };
}

export function renderSchedules() {
  const sc = S.mb.schedules;
  const tables = sc?.tables || [];
  const q = S.q.toLowerCase();
  const out = [];
  if (sc?.pile) out.push(pileCard(sc.pile));
  let shown = 0;
  tables.forEach((t, ti) => {
    const rows = t.rows.map((r, ri) => [r, ri]).filter(([r]) => match(q, t.name, sheetName(t.sheet), r.label, ...(r.cells || []).map(cellText)));
    if (!rows.length) return;
    shown++;
    const ncol = Math.max(t.columns?.length || 0, ...t.rows.map((r) => r.cells?.length || 0));
    if (S.compact) {
      const caption = el("div", { class: "sched-h" },
        el("h3", {}, t.name), el("span", { class: "muted" }, `${sheetName(t.sheet)} · ${t.rows.length} rows`));
      pickable(caption, "table", String(ti));
      const cards = rows.map(([r, ri]) => el("div", { class: "card sched-card" }, el("div", { style: { gridColumn: "1 / -1" } },
        el("dl", {}, Array.from({ length: ncol }, (_, ci) => {
          const v = cellText(r.cells?.[ci]);
          const dd = el("dd", {}, v ?? "");
          if (v != null && String(v).trim() && String(v).trim() !== "-") pickable(dd, "cell", `${ti}:${ri}:${ci}`, `${t.name}, ${r.label}, ${t.columns?.[ci] || ""}: ${v}`);
          return [el("dt", {}, t.columns?.[ci] || (ci ? `Column ${ci + 1}` : "Row")), dd];
        })))));
      out.push(el("section", { class: "sched" }, caption, ...cards));
      return;
    }
    const head = el("tr", {}, Array.from({ length: ncol }, (_, ci) => el("th", { scope: "col" }, t.columns?.[ci] || "")));
    const body = el("tbody", {}, rows.map(([r, ri]) => el("tr", {}, Array.from({ length: ncol }, (_, ci) => {
      const v = cellText(r.cells?.[ci]);
      const td = el(ci === 0 ? "th" : "td", ci === 0 ? { scope: "row" } : {}, v ?? "");
      if (v != null && String(v).trim() && String(v).trim() !== "-") pickable(td, "cell", `${ti}:${ri}:${ci}`, `${t.name}, ${r.label}, ${t.columns?.[ci] || ""}: ${v}`);
      return td;
    }))));
    const caption = el("div", { class: "sched-h" },
      el("h3", {}, t.name), el("span", { class: "muted" }, `${sheetName(t.sheet)} · ${t.rows.length} rows`),
      el("button", { class: "link", type: "button", onclick: () => A.select("table", String(ti), { from: "table" }) }, "Show on sheet"));
    const sec = el("section", { class: "sched" }, caption, el("div", { class: "sched-scroll" }, el("table", { class: "mb sched-t" }, el("thead", {}, head), body)));
    pickable(caption, "table", String(ti));
    out.push(sec);
  });
  if (!shown && !sc?.pile) out.push(el("p", { class: "pane-empty" }, tables.length ? "No schedule matches the search." : "No schedules were read from the drawings."));
  out.unshift(el("p", { class: "pane-note top" }, `Tables read from the drawings. ${S.touch ? "Tap" : "Click"} a cell to find it on its sheet.`));
  return out;
}

function pileCard(p) {
  const rows = [
    ["Pile", `${p.tag || ""} Ø${p.dia} mm${p.bulbs ? `, ${p.bulbs} bulb${p.bulbs > 1 ? "s" : ""} Ø${p.bulb_dia}` : ""}`],
    ["Length", p.length ? `${fmt(p.length, 0)} mm below cut-off` : ""],
    ["Cut-off", p.cutoff_below_gl ? `${fmt(p.cutoff_below_gl, 0)} mm below GL` : ""],
    ["Longitudinal", (p.long || []).map((b) => `${b.n} × Ø${b.dia}`).join(" + ")],
    ["Rings", p.rings ? `Ø${p.rings.dia} @ ${p.rings.s}` : ""],
    ["Bulb spacing", p.bulb_spacing ? `${fmt(p.bulb_spacing, 0)} mm` : ""],
  ].filter(([, v]) => v);
  const sec = el("section", { class: "sched pile" },
    el("div", { class: "sched-h" }, el("h3", {}, "Pile detail"), el("span", { class: "muted" }, `${sheetName(p.sheet)} · ${p.title || ""}`)),
    el("dl", { class: "kv" }, rows.map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])));
  return pickable(sec, "pile", "0");
}

// ================================================================ Calibration
export function renderCalibration() {
  const cal = S.mb.calibration || {};
  const user = S.cfg?.calibration || {};
  const q = S.q.toLowerCase();
  const sheets = S.geo?.sheets || [];
  const body = el("tbody");
  const rest = [], cards = [];
  for (const sh of sheets) {
    const c = cal[sh.sheet] || cal[String(sh.sheet)];
    const name = sheetName(sh.sheet), title = sh.title_block?.title || "";
    if (!match(q, name, title, c?.method)) continue;
    if (!c) { rest.push(el("li", {}, el("b", {}, name), ` ${title}`)); continue; }
    const weak = c.pt_per_mm == null || c.spread_pct > 3 || c.n < 3;
    const u = user[sh.sheet] || user[String(sh.sheet)];
    const scaleIn = el("input", { type: "number", min: 1, step: "any", inputmode: "decimal", placeholder: c.scale_1_to ? String(Math.round(c.scale_1_to)) : "100", "aria-label": `Scale 1:N for ${name}` });
    const status = el("span", { class: `chip ${c.method === "user" || u ? "ok" : weak ? "assumed" : "scaled"}`, title: weak ? "Few samples or wide spread: lengths scaled on this sheet are uncertain" : "" },
      c.method === "user" || u ? "User" : c.pt_per_mm == null ? "No scale" : weak ? "Weak" : "Consensus");
    const acts = () => [el("button", { class: "btn", type: "button", onclick: () => A.startCalibrate(sh.sheet) }, "Calibrate…"),
      el("form", { class: "cal-n", onsubmit: (e) => { e.preventDefault(); const n = Number(scaleIn.value); if (n > 0) A.applyCalibration(sh.sheet, { scale: n }); else scaleIn.focus(); } },
        el("label", {}, "1:", scaleIn), el("button", { class: "btn", type: "submit" }, "Set")),
      u ? el("button", { class: "link", type: "button", onclick: () => A.applyCalibration(sh.sheet, null) }, "Clear override") : null];
    if (S.compact) {
      const cd = card("calib", String(sh.sheet), { cls: "row",
        main: [el("span", { class: "k-head" }, el("span", { class: "k-desc" }, name)), title ? el("span", { class: "k-loc" }, title) : null,
          el("span", { class: "k-kv" }, el("span", {}, "Scale ", el("b", {}, c.scale_1_to ? `1:${fmt(c.scale_1_to, 1)}` : "—")),
            el("span", {}, "Method ", el("b", {}, c.method || "")), c.n != null ? el("span", {}, "Samples ", el("b", {}, c.n)) : null,
            c.pt_per_mm != null ? el("span", {}, "Spread ", el("b", {}, `${fmt(c.spread_pct, 2, 2)}%`)) : null)],
        side: [status] });
      cd.append(el("div", { class: "k-acts" }, acts()));
      cards.push(cd);
      continue;
    }
    const tr = el("tr", { class: "row" },
      el("td", { class: "desc" }, name, el("small", {}, title)),
      el("td", { class: "loc" }, c.method || "", c.user_override ? el("small", {}, `override ${c.user_override}`) : null),
      el("td", { class: "num c-q" }, c.scale_1_to ? `1:${fmt(c.scale_1_to, 1)}` : "—"),
      el("td", { class: "num" }, c.pt_per_mm ? fmt(c.pt_per_mm, 5) : ""),
      el("td", { class: "num" }, c.n ?? ""),
      el("td", { class: "num" }, c.pt_per_mm == null ? "" : `${fmt(c.spread_pct, 2, 2)}%`),
      el("td", { class: "c-st" }, status),
      el("td", { class: "cal-act" }, acts()));
    body.append(pickable(tr, "calib", String(sh.sheet)));
  }
  const table = el("table", { class: "mb cal-t" },
    el("thead", {}, el("tr", {}, ["Sheet", "Method", "Scale", "pt/mm", "Samples", "Spread", "Status", ""].map((h, i) => el("th", { scope: "col", class: [2, 3, 4, 5].includes(i) ? "num" : i === 6 ? "c-st" : "" }, h)))),
    body);
  const out = [
    el("p", { class: "pane-note top" }, "The sheets are NOT TO SCALE, so lengths that are not written are scaled with a per-sheet calibration ",
      "taken from written dimensions and scheduled element sizes. To calibrate a sheet yourself, pick two points on the drawing and enter the real length between them. The book is then rebuilt."),
    ...(S.compact ? cards : [table]),
  ];
  if (rest.length) out.push(el("h3", { class: "pane-h" }, "Sheets with nothing scaled"), el("ul", { class: "pane-list" }, rest));
  return out;
}

/** Detail under a selected calibration row: its samples. */
export function calibDetail(n) {
  const c = (S.mb.calibration || {})[n];
  if (!c?.samples?.length) return null;
  const med = c.pt_per_mm;
  const list = el("ol", { class: "samples" }, c.samples.slice(0, 24).map((s, k) => {
    const dev = med ? (s.ratio / med - 1) * 100 : 0;
    const mm = s.value_mm ?? s.mm, pt = s.len_pt ?? (s.p1 && s.p2 ? Math.hypot(s.p2[0] - s.p1[0], s.p2[1] - s.p1[1]) : null);
    const li = el("li", {}, el("span", {}, s.source === "dimension" ? `“${s.text ?? s.value_mm}”` : s.source === "user" ? (s.scale ? `user 1:${s.scale}` : "user, two points") : s.source),
      el("span", { class: "num" }, mm != null ? `${fmt(mm, 0)} mm` : ""),
      el("span", { class: "num" }, pt != null ? `${fmt(pt, 2)} pt` : ""),
      el("span", { class: "num" }, med ? `${dev >= 0 ? "+" : "−"}${fmt(Math.abs(dev), 2, 2)}%` : ""));
    return pickable(li, "sample", `${n}:${k}`);
  }));
  const note = el("p", { class: "muted small" }, `${c.n} samples by ${c.method}${c.samples.length > 24 ? ` (first 24 of ${c.samples.length} shown)` : ""}. Deviation is from the consensus.`);
  if (S.compact) return el("div", { class: "cdetail" }, note, list);
  return el("tr", { class: "detail" }, el("td", { colspan: 8 }, note, list));
}

// ================================================================ drawing groups for these tabs
function samplePoint(s) {
  return Array.isArray(s.at) ? s.at : s.p1 && s.p2 ? [(s.p1[0] + s.p2[0]) / 2, (s.p1[1] + s.p2[1]) / 2] : null;
}

export function structGroups(kind, key) {
  const mb = S.mb;
  if (kind === "steel") {
    const rows = elementRows(key);
    const kg = Object.values(mb.steel?.by_element_kg?.[key] || {}).reduce((a, b) => a + b, 0);
    return [{ color: tradeOf(STEEL_TRADE).d, refs: rows.flatMap((r) => r.refs), label: { title: `${fmt(kg / 1000, 2, 2)} t`, sub: `${key} · ${rows.length} lines` } }];
  }
  if (kind === "table") {
    const t = mb.schedules?.tables?.[Number(key)];
    return t ? [{ color: "#9fe3ff", refs: [{ bbox: t.bbox, handles: [], kind: "table", sheet: t.sheet }], label: { title: t.name, sub: sheetName(t.sheet) } }] : [];
  }
  if (kind === "cell") {
    const [ti, ri, ci] = key.split(":").map(Number);
    const t = mb.schedules?.tables?.[ti];
    if (!t) return [];
    const loc = locateCell(t, ri, ci);
    const v = cellText(t.rows[ri]?.cells?.[ci]);
    return [{ color: "#9fe3ff", refs: loc.refs, entities: loc.entities || undefined, sheet: t.sheet,
      label: { title: String(v), sub: `${t.name} · ${t.rows[ri]?.label || ""} · ${t.columns?.[ci] || ""}${loc.approx ? " (not found: showing the table)" : ""}` } }];
  }
  if (kind === "pile") {
    const p = mb.schedules?.pile;
    if (!p) return [];
    const sh = S.geo.sheets.find((s) => s.sheet === p.sheet);
    const hit = sh ? bestText(textsIn(p.sheet, sh.bbox), p.title || "PILE")[0] : null;
    const refs = hit ? [{ bbox: hit.b, handles: [], kind: "text", sheet: p.sheet }] : sh ? [{ bbox: sh.bbox, handles: [], kind: "sheet", sheet: p.sheet }] : [];
    return [{ color: "#9fe3ff", refs, entities: hit ? [hit.i] : undefined, sheet: p.sheet, label: { title: `Pile ${p.tag || ""} Ø${p.dia}`, sub: p.title || "" } }];
  }
  if (kind === "calib" || kind === "sample") {
    const [n, k] = key.split(":").map(Number);
    const c = (mb.calibration || {})[n];
    if (!c) return [];
    const samples = kind === "sample" ? [c.samples?.[k]].filter(Boolean) : c.samples || [];
    const pts = samples.map(samplePoint).filter(Boolean);
    const handles = [...new Set(samples.flatMap((s) => s.handles || []))];
    const u = (S.cfg?.calibration || {})[n];
    if (u?.p1 && u?.p2) pts.push(u.p1, u.p2);
    const b = unionBox(pts.map((p) => [p[0], p[1], p[0], p[1]]));
    const s0 = samples[0];
    return [{ color: SAMPLE, refs: handles.length ? [{ handles, bbox: null, kind: "entity", sheet: n }] : [], sheet: n,
      focus: validBox(b) ? [b] : [S.geo.sheets.find((s) => s.sheet === n)?.bbox].filter(Boolean),
      marks: pts.map(([x, y]) => ({ x, y, color: SAMPLE })),
      label: kind === "sample" && s0
        ? { title: `${s0.value_mm != null ? `${fmt(s0.value_mm, 0)} mm` : ""} = ${fmt(s0.len_pt, 2)} pt`, sub: `${s0.source} · ratio ${fmt(s0.ratio, 5)} pt/mm` }
        : { title: c.scale_1_to ? `≈ 1:${fmt(c.scale_1_to, 1)}` : "No scale", sub: `${sheetName(n)} · ${c.n} samples · spread ${fmt(c.spread_pct, 2)}%` } }];
  }
  return null;
}

export { fq };
