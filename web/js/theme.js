// Trade palette: one hue per trade, used for the table swatches AND the drawing
// highlights. `d` is tuned for the dark canvas, `p` for the paper ledger.

export const TRADES = [
  { name: "Earthwork & PCC", d: "#d2a86e", p: "#8a6532", re: /earth|excavat|pcc|foundation|footing|filling|plinth/i },
  { name: "Masonry", d: "#ff8a5c", p: "#c2410c", re: /brick|masonry|aac|block work|parapet wall|half brick/i },
  { name: "Shuttering", d: "#ffc36b", p: "#a35a00", re: /shuttering|formwork|centering/i },
  { name: "Steel", d: "#c3e86b", p: "#56730f", re: /\bsteel\b|reinforc|\btmt\b/i },
  { name: "RCC", d: "#9fb7ff", p: "#4458b8", re: /\brcc\b|slab|beam|column|lintel|coping|concrete(?! flooring)/i },
  { name: "Plastering", d: "#f2d14b", p: "#8f7300", re: /plaster|groove|render|pointing|ceiling/i },
  { name: "Flooring", d: "#3fd1b8", p: "#0f7a69", re: /floor|tile|kota|skirting|dado|vitrified/i },
  { name: "Roof & terrace", d: "#5cc8ff", p: "#0b6fa4", re: /coba|terrace|khurra|waterproof|roof|expansion joint/i },
  { name: "Ramp, steps & railings", d: "#d39bff", p: "#7c3aed", re: /ramp|step|stair|rail/i },
  { name: "Doors & windows", d: "#7ee08a", p: "#2d7a3a", re: /door|window|ventilator|shutter|opening/i },
  { name: "Water tanks", d: "#62e3ff", p: "#0e7490", re: /tank/i },
  { name: "Painting & finishes", d: "#ff9ec7", p: "#b0306e", re: /paint|distemper|emulsion|finish/i },
  { name: "General", d: "#c9dcea", p: "#44535f", re: /.*/ },
];
const BY_NAME = new Map(TRADES.map((t) => [t.name.toLowerCase(), t]));

export function tradeOf(name) {
  if (!name) return TRADES[TRADES.length - 1];
  const t = BY_NAME.get(String(name).toLowerCase());
  if (t) return t;
  // unknown trade name from the engine: give it a stable colour from the palette
  let h = 0;
  for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const base = TRADES[h % (TRADES.length - 1)];
  const nt = { ...base, name };
  BY_NAME.set(String(name).toLowerCase(), nt);
  return nt;
}

export function guessTrade(item) {
  const t = TRADES.find((x) => x.re.test(item || ""));
  return t ? t.name : "General";
}

export const SEV = {
  error: { d: "#ff6b57", label: "Error", glyph: "!" },
  warning: { d: "#f5b83d", label: "Warning", glyph: "!" },
  missing: { d: "#a995ff", label: "Missing", glyph: "?" },
};

// Linework hierarchy for the mono "print" rendering, by layer role.
export const ROLE_INK = {
  "wall-brick": [222, 236, 247, 1],
  "wall-AAC": [222, 236, 247, 1],
  parapet: [210, 228, 242, 0.95],
  column: [222, 236, 247, 1],
  door: [160, 205, 232, 0.9],
  window: [160, 205, 232, 0.9],
  "room text": [196, 216, 232, 0.9],
  dimension: [120, 156, 184, 0.75],
  groove: [150, 186, 212, 0.8],
  railing: [176, 206, 228, 0.9],
  ignore: [138, 170, 196, 0.62],
};

export const ROLE_SWATCH = {
  "wall-brick": "#c2410c", "wall-AAC": "#b45309", parapet: "#7c3aed", column: "#0e7490",
  door: "#2d7a3a", window: "#0b6fa4", "room text": "#8f7300", dimension: "#64748b", groove: "#8f7300", railing: "#7c3aed", ignore: "#b4c3cb",
};

// Which layer roles a ref kind should light up when highlighting by bbox.
export const KIND_ROLES = {
  wall: ["wall-brick", "wall-AAC", "parapet", "column"],
  room: ["wall-brick", "wall-AAC", "column", "room text", "door", "window"],
  opening: ["door", "window"],
  dimension: ["dimension"],
  slab: ["parapet", "wall-brick", "wall-AAC", "column"],
  roof: ["parapet", "wall-brick", "wall-AAC"],
  parapet: ["parapet"],
  khurra: ["parapet"],
  text: ["room text", "door", "window"],
  tank: null, stair: null, flag: null, region: null, entity: null,
};
