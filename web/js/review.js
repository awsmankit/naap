// Review step: which views are measured, layer roles, and heights.

import { S, A } from "./state.js";
import { $, $$, el } from "./util.js";
import { ROLE_SWATCH } from "./theme.js";

export function renderReview() {
  if (!S.cfg) return;
  $$("#rvTabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === S.rvTab)));
  const inc = S.cfg.regions.filter((r) => r.include).length;
  $("#nViews").textContent = `${inc}/${S.cfg.regions.length}`;
  const assigned = Object.values(S.cfg.layer_roles || {}).filter((r) => r && r !== "ignore").length;
  $("#nLayers").textContent = assigned;
  const body = $("#rvBody");
  if (S.isPdf && S.rvTab === "heights") S.rvTab = "views";
  body.replaceChildren(...({ views, layers, heights })[S.rvTab]());
}

function views() {
  const kinds = S.meta?.kinds || ["plan", "section", "elevation", "schedule", "detail", "other"];
  const levels = S.cfg.levels || [];
  const list = el("ul", { class: "rv-list" }, S.cfg.regions.map((r) => {
    const li = el("li", { class: `${r.include ? "" : "off"}${S.viewer.selectedRegion === r.id ? " sel" : ""}`, "data-id": r.id },
      el("input", { type: "checkbox", checked: !!r.include, "aria-label": `Measure ${r.name}`,
        onchange: (e) => { r.include = e.target.checked; li.classList.toggle("off", !r.include); A.cfgChanged(); S.viewer.draw(); renderReviewCounts(); } }),
      el("input", { type: "text", class: "nm", value: r.name, "aria-label": "View name",
        oninput: (e) => { r.name = e.target.value; S.viewer.draw(); A.cfgChanged(); } }),
      el("button", { class: "link", type: "button", onclick: () => A.focusRegion(r.id) }, "Show"),
      r.kind === "sheet" ? el("div", { class: "sub" }, el("span", {}, r.title || ""), el("span", {}, `${r.count ?? ""} items`)) : el("div", { class: "sub" },
        el("select", { "aria-label": "View type", onchange: (e) => { r.kind = e.target.value; A.cfgChanged(); } },
          kinds.map((k) => el("option", { value: k, selected: k === r.kind }, k))),
        levels.length ? el("select", { "aria-label": "Level", onchange: (e) => { r.level = e.target.value; A.cfgChanged(); } },
          el("option", { value: "" }, "No level"), levels.map((lv) => el("option", { value: lv.name, selected: lv.name === r.level }, lv.name))) : null,
        el("span", {}, `${r.count} entities`)));
    return li;
  }));
  const intro = S.isPdf
    ? "Sheets in the PDF set. The structural engine reads every sheet; click Show to look at one, or pick it in the strip above the drawing."
    : "Views found on the sheet. Tick the ones to measure from: usually the plans, sections and the door/window schedule.";
  return [el("div", { class: "rv-section" }, el("p", {}, intro)), list];
}

function renderReviewCounts() {
  const inc = S.cfg.regions.filter((r) => r.include).length;
  $("#nViews").textContent = `${inc}/${S.cfg.regions.length}`;
}

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
        el("select", { "aria-label": `Role of ${L.name}`, onchange: (e) => { S.cfg.layer_roles[L.name] = e.target.value; A.rolesChanged(); draw(); } },
          roles.map((r) => el("option", { value: r, selected: r === role }, r)))));
      return tr;
    }));
  };
  filter.addEventListener("input", draw);
  onlyRoles.addEventListener("change", draw);
  draw();
  return [
    el("div", { class: "rv-tools" }, filter, el("label", {}, onlyRoles, "Measured only")),
    el("table", { class: "plain" }, el("thead", {}, el("tr", {}, el("th", {}, el("span", { class: "sr" }, "")), el("th", {}, "Layer"), el("th", {}, "Items"), el("th", {}, "Role"))), tbody),
  ];
}

function numIn(obj, key, step = 10) {
  return el("input", { type: "number", min: 0, step, value: obj[key] ?? "", inputmode: "decimal",
    oninput: (e) => { obj[key] = e.target.value === "" ? null : Number(e.target.value); A.cfgChanged(); } });
}

function heights() {
  const c = S.cfg;
  c.walls = c.walls || {};
  const walls = ["wall-brick", "wall-AAC", "parapet"].map((role) => {
    c.walls[role] = c.walls[role] || { thickness_mm: 230, height_mm: 3000 };
    return el("tr", {}, el("td", {}, el("span", { class: "swatch-role", style: { background: ROLE_SWATCH[role] } }), role),
      el("td", {}, numIn(c.walls[role], "thickness_mm", 5)), el("td", {}, numIn(c.walls[role], "height_mm", 50)));
  });
  c.column = c.column || { height_mm: 3000 };
  const lv = c.levels = c.levels || [];
  const levelBody = el("tbody");
  const drawLevels = () => levelBody.replaceChildren(...lv.map((l, i) => el("tr", {},
    el("td", {}, el("input", { type: "text", value: l.name, "aria-label": "Level name", onchange: (e) => { l.name = e.target.value; A.cfgChanged(); } })),
    el("td", {}, numIn(l, "level_mm", 50)),
    el("td", {}, el("button", { class: "link", type: "button", "aria-label": `Remove ${l.name}`, onclick: () => { lv.splice(i, 1); drawLevels(); A.cfgChanged(); } }, "Remove")))));
  drawLevels();
  return [
    el("div", { class: "rv-section" }, el("h3", {}, "Walls"),
      el("p", {}, "Used where the drawing has no section through a wall. Flags in the book tell you which blocks rely on these."),
      el("table", { class: "plain" }, el("thead", {}, el("tr", {}, el("th", {}, "Wall type"), el("th", {}, "Thickness mm"), el("th", {}, "Height mm"))), el("tbody", {}, walls))),
    el("div", { class: "rv-section" }, el("h3", {}, "Columns and slab"),
      el("table", { class: "plain" }, el("tbody", {},
        el("tr", {}, el("td", {}, "Column height mm"), el("td", {}, numIn(c.column, "height_mm", 50))),
        el("tr", {}, el("td", {}, "Slab thickness mm"), el("td", {}, numIn(c, "slab_thickness_mm", 5)))))),
    el("div", { class: "rv-section" }, el("h3", {}, "Levels"),
      el("table", { class: "plain" }, el("thead", {}, el("tr", {}, el("th", {}, "Name"), el("th", {}, "Level mm"), el("th", {}, ""))), levelBody),
      el("p", {}, el("button", { class: "link", type: "button", onclick: () => {
        const last = lv.length ? lv[lv.length - 1].level_mm || 0 : 0;
        lv.push({ name: `Level ${lv.length + 1}`, level_mm: last + 3300 }); drawLevels(); A.cfgChanged();
      } }, "Add a level"))),
  ];
}
