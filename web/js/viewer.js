// Canvas drawing viewer for the compact geometry JSON (app/extract.py).
//
// Speed: entities are baked once into Path2D chunks keyed by (layer, spatial tile);
// a frame only strokes chunks whose bbox meets the viewport. A uniform grid over
// entity bboxes serves picking and "entities inside this ref bbox" queries.
// Highlights are separate Path2Ds built per selection, so pan/zoom never re-walks
// the geometry.

import { ROLE_INK, KIND_ROLES, SEV } from "./theme.js";
import { reducedMotion, fmt } from "./util.js";

const DRAWN = new Set(["L", "P", "A", "C", "H", "T"]);
const ISO = "Osifont, 'Barlow SC', sans-serif";
const SANS = "'Barlow SC', Barlow, sans-serif";

export class Viewer {
  constructor(canvas, minimap, scalebar) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.mini = minimap;
    this.mctx = minimap.getContext("2d");
    this.scalebar = scalebar;
    this.layers = [];
    this.s = 1; this.tx = 0; this.ty = 0; this.ox = 0; this.oy = 0;
    this.colourMode = "mono";
    this.showText = true;
    this.showMarkers = true;
    this.showRegions = false;
    this.regions = [];
    this.selectedRegion = null;
    this.flags = [];
    this.selectedFlag = null;
    this.groups = [];      // persistent highlight (selection)
    this.preview = null;   // hover highlight
    this.callbacks = {};
    this._raf = 0;
    this._anim = null;
    this.home = [0, 0, 1, 1];
    // PDF sets: sheets side by side in one space; `clip` shows one sheet only
    this.sheets = [];          // [{sheet, bbox, label}]
    this.clip = null;          // bbox of the shown sheet, or null for all
    this.unitMM = null;        // drawing units per real mm (pt sheets: pt/mm of the calibration)
    this.scaleMM = null;       // pt per real mm of the shown sheet, for the scale bar
    this.insetTop = 0;         // screen px covered by overlays at the top (sheet strip)
    this.raster = { on: false, url: null, base: new Map(), detail: null, want: null };
    this.measure = null;       // {p1, p2, hover} while calibrating
    this.marks = [];           // [{x, y, color}] small markers (calibration samples)
    this._bind();
    new ResizeObserver(() => this.resize()).observe(canvas);
  }

  on(name, fn) { this.callbacks[name] = fn; }
  _emit(name, ...a) { this.callbacks[name]?.(...a); }

  // ------------------------------------------------------------ geometry
  setGeometry(geo, roles = {}, home = null) {
    const E = geo.entities, n = E.length;
    this.geo = geo;
    this.E = E;
    const eb = new Float64Array(n * 4);
    const owner = new Int32Array(n);
    const cx = new Float64Array(n), cy = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const e = E[i];
      owner[i] = e.v && e.pi != null ? e.pi : i;
      const b = entityBox(e);
      eb.set(b, i * 4);
      cx[i] = (b[0] + b[2]) / 2; cy[i] = (b[1] + b[3]) / 2;
    }
    this.eb = eb; this.owner = owner;
    this.members = new Map();
    for (let i = 0; i < n; i++) {
      const o = owner[i];
      if (o === i && !E[i].v) continue;
      if (!this.members.has(o)) this.members.set(o, [o]);
      this.members.get(o).push(i);
    }
    // handles -> all entity indices (top-level + exploded children)
    this.handleIdx = new Map();
    for (let i = 0; i < n; i++) {
      // "handle" (current extractor; exploded children carry the parent's) or legacy "hd"
      const hd = E[i].handle ?? E[i].hd ?? E[owner[i]].handle ?? E[owner[i]].hd;
      if (!hd) continue;
      const k = String(hd).toUpperCase();
      if (!this.handleIdx.has(k)) this.handleIdx.set(k, []);
      this.handleIdx.get(k).push(i);
    }
    // robust core extents (drawings often carry a few far-away stray entities)
    const core = home || robustBox(cx, cy, E);
    this.home = core;
    this.ox = (core[0] + core[2]) / 2; this.oy = (core[1] + core[3]) / 2;
    this._buildGrid(core);
    this._buildLayers(geo, roles, core);
    this._miniDirty = true;
    this.fit(core, false);
  }

  _buildGrid(core) {
    const N = 192;
    const w = Math.max(core[2] - core[0], 1), h = Math.max(core[3] - core[1], 1);
    const cs = Math.max(w, h) / N;
    const gx = Math.ceil(w / cs) + 1, gy = Math.ceil(h / cs) + 1;
    const cells = new Array(gx * gy);
    const big = [];
    const eb = this.eb, E = this.E;
    for (let i = 0; i < E.length; i++) {
      const t = E[i].t;
      if (!DRAWN.has(t)) continue;
      const x0 = clamp(Math.floor((eb[i * 4] - core[0]) / cs), 0, gx - 1);
      const y0 = clamp(Math.floor((eb[i * 4 + 1] - core[1]) / cs), 0, gy - 1);
      const x1 = clamp(Math.floor((eb[i * 4 + 2] - core[0]) / cs), 0, gx - 1);
      const y1 = clamp(Math.floor((eb[i * 4 + 3] - core[1]) / cs), 0, gy - 1);
      if ((x1 - x0 + 1) * (y1 - y0 + 1) > 400) { big.push(i); continue; }
      for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
        const k = y * gx + x;
        (cells[k] || (cells[k] = [])).push(i);
      }
    }
    this.grid = { core, cs, gx, gy, cells, big };
  }

  /** Entity indices whose bbox may touch the world box b. */
  query(b) {
    const g = this.grid, out = new Set(g.big);
    const x0 = clamp(Math.floor((b[0] - g.core[0]) / g.cs), 0, g.gx - 1);
    const y0 = clamp(Math.floor((b[1] - g.core[1]) / g.cs), 0, g.gy - 1);
    const x1 = clamp(Math.floor((b[2] - g.core[0]) / g.cs), 0, g.gx - 1);
    const y1 = clamp(Math.floor((b[3] - g.core[1]) / g.cs), 0, g.gy - 1);
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
      const c = g.cells[y * g.gx + x];
      if (c) for (const i of c) out.add(i);
    }
    return out;
  }

  _buildLayers(geo, roles, core) {
    const ox = this.ox, oy = this.oy, E = this.E, eb = this.eb;
    const T = 12, tw = (core[2] - core[0]) / T || 1, th = (core[3] - core[1]) / T || 1;
    const bigHatch = Math.max(core[2] - core[0], core[3] - core[1]) * 0.02;
    this.layers = geo.layers.map((l, i) => ({
      i, name: l.name, color: fixColour(l.color), visible: !l.off, role: roles[l.name] || "ignore",
      n: 0, chunks: new Map(), texts: [],
    }));
    this.texts = [];
    for (let i = 0; i < E.length; i++) {
      const e = E[i];
      if (!DRAWN.has(e.t)) continue;
      const L = this.layers[e.l];
      if (!L) continue;
      L.n++;
      if (e.t === "T") {
        if (e.s && e.s.trim()) { const t = { i, x: e.p[0] - ox, y: e.p[1] - oy, s: e.s, h: e.h || 1, r: e.r || 0, ap: e.ap, l: e.l }; L.texts.push(t); this.texts.push(t); }
        continue;
      }
      const ccx = (eb[i * 4] + eb[i * 4 + 2]) / 2, ccy = (eb[i * 4 + 1] + eb[i * 4 + 3]) / 2;
      const key = clamp(Math.floor((ccx - core[0]) / tw), -1, T) * 100 + clamp(Math.floor((ccy - core[1]) / th), -1, T);
      let ch = L.chunks.get(key);
      if (!ch) { ch = { path: new Path2D(), fill: null, solid: null, bb: [Infinity, Infinity, -Infinity, -Infinity] }; L.chunks.set(key, ch); }
      const bb = ch.bb;
      bb[0] = Math.min(bb[0], eb[i * 4]); bb[1] = Math.min(bb[1], eb[i * 4 + 1]);
      bb[2] = Math.max(bb[2], eb[i * 4 + 2]); bb[3] = Math.max(bb[3], eb[i * 4 + 3]);
      if (e.t === "H" && e.solid) {
        addLoops(ch.solid || (ch.solid = new Path2D()), e.p, ox, oy);
      } else if (e.t === "H") {
        const big = Math.max(eb[i * 4 + 2] - eb[i * 4], eb[i * 4 + 3] - eb[i * 4 + 1]) > bigHatch;
        const target = big ? ch.path : (ch.fill || (ch.fill = new Path2D()));
        addLoops(target, e.p, ox, oy);
      } else {
        addEntity(ch.path, e, ox, oy);
      }
    }
    for (const L of this.layers) L.chunkList = [...L.chunks.values()];
  }

  setRoles(roles) {
    for (const L of this.layers) L.role = roles[L.name] || "ignore";
    this._miniDirty = true;
    this.draw();
  }

  // ------------------------------------------------------------ refs -> entities
  /** Entity indices (drawable, incl. exploded children) that a ref points at. */
  refEntities(ref) {
    const out = [];
    if (ref.handles && ref.handles.length && this.handleIdx.size) {
      for (const h of ref.handles) {
        const ids = this.handleIdx.get(String(h).toUpperCase());
        if (ids) for (const i of ids) if (DRAWN.has(this.E[i].t)) out.push(i);
      }
      if (out.length) return out;
    }
    const b = ref.bbox;
    if (!b) return out;
    const mm = this.mmUnit();
    // rooms and openings are given as clear sizes; take in the wall faces around them
    const reach = ["room", "opening", "wall"].includes(ref.kind) ? 350 * mm : 30 * mm;
    const tol = Math.max((b[2] - b[0]) * 0.01, (b[3] - b[1]) * 0.01, reach);
    const B = [b[0] - tol, b[1] - tol, b[2] + tol, b[3] + tol];
    const roles = KIND_ROLES[ref.kind] ?? null;
    const any = [];
    const eb = this.eb;
    for (const i of this.query(B)) {
      const k = i * 4;
      if (eb[k] < B[0] || eb[k + 1] < B[1] || eb[k + 2] > B[2] || eb[k + 3] > B[3]) continue;
      const L = this.layers[this.E[i].l];
      if (!L || !L.visible) continue;
      if (L.role === "dimension" && ref.kind !== "dimension") continue;
      any.push(i);
      if (!roles || roles.includes(L.role)) out.push(i);
    }
    return out.length ? out : any;
  }

  /** Drawing units per real millimetre. */
  mmUnit() {
    if (this.unitMM) return this.unitMM;
    return { mm: 1, cm: 0.1, m: 0.001, in: 1 / 25.4, ft: 1 / 304.8, pt: 0.025 }[this.geo?.units] ?? 1;
  }

  /** Show one sheet (bbox) or all (null). */
  setClip(b) {
    this.clip = b || null;
    this._miniDirty = true;
    this.raster.detail = null;
    this.draw();
  }

  /** Sheet record whose bbox holds the world point. */
  sheetAt(x, y) {
    return this.sheets.find((s) => x >= s.bbox[0] && x <= s.bbox[2] && y >= s.bbox[1] && y <= s.bbox[3]) || null;
  }

  _inClip(i) {
    const c = this.clip;
    if (!c) return true;
    const k = i * 4, eb = this.eb, x = (eb[k] + eb[k + 2]) / 2, y = (eb[k + 1] + eb[k + 3]) / 2;
    return x >= c[0] && x <= c[2] && y >= c[1] && y <= c[3];
  }

  // ------------------------------------------------------------ raster backdrop (PDF sheets)
  /** url(sheet, dpi, clip) -> image URL; clip in page points (y down). */
  setRaster(on, url) {
    this.raster.on = on;
    if (url) this.raster.url = url;
    this.draw();
  }

  _rasterImage(src, onload) {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      // invert once (white paper -> dark) so it sits on the blueprint with "screen" blending
      const c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      const g = c.getContext("2d");
      g.drawImage(img, 0, 0);
      g.globalCompositeOperation = "difference";
      g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
      onload(c);
      this.draw();
    };
    img.onerror = () => { onload(null); };
    img.src = src;
    return img;
  }

  _rasterFrame(ctx, dpr, vb, dim) {
    const R = this.raster;
    if (!R.on || !R.url) return;
    const vis = this.sheets.filter((s) => (!this.clip || sameBox(s.bbox, this.clip))
      && !(s.bbox[2] < vb[0] || s.bbox[0] > vb[2] || s.bbox[3] < vb[1] || s.bbox[1] > vb[3]));
    ctx.save();
    ctx.globalCompositeOperation = "screen";
    ctx.globalAlpha = Math.min(1, dim * 1.6);
    ctx.imageSmoothingQuality = "high";
    for (const s of vis) {
      let b = R.base.get(s.sheet);
      if (!b) {
        b = { canvas: null };
        R.base.set(s.sheet, b);
        const w = s.bbox[2] - s.bbox[0];
        const dpi = Math.max(24, Math.min(100, Math.round((2600 / w) * 72)));
        this._rasterImage(R.url(s.sheet, dpi, null), (c) => { b.canvas = c; });
      }
      if (b.canvas) this._blit(ctx, dpr, b.canvas, s.bbox);
    }
    // a sharper crop of the view when zoomed past the base image
    const d = R.detail;
    if (d?.canvas && vis.some((s) => s.sheet === d.sheet)) this._blit(ctx, dpr, d.canvas, d.box);
    ctx.restore();
    this._wantDetail(vis);
  }

  _blit(ctx, dpr, img, b) {
    const [x0, y0] = this.toScreen(b[0], b[3]), [x1, y1] = this.toScreen(b[2], b[1]);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.drawImage(img, x0, y0, x1 - x0, y1 - y0);
  }

  _wantDetail(vis) {
    const R = this.raster;
    const s = vis.length === 1 ? vis[0] : null;
    const dpr = window.devicePixelRatio || 1;
    const pxPerPt = this.s * dpr;
    clearTimeout(this._detailT);
    if (!s || pxPerPt < 1.15) return;
    this._detailT = setTimeout(() => {
      const [ax, ay] = this.toWorld(0, this.H), [bx, by] = this.toWorld(this.W, 0);
      const b = [Math.max(ax, s.bbox[0]), Math.max(ay, s.bbox[1]), Math.min(bx, s.bbox[2]), Math.min(by, s.bbox[3])];
      if (b[2] - b[0] < 2 || b[3] - b[1] < 2) return;
      const dpi = Math.min(400, Math.round(pxPerPt * 72 / 12) * 12);
      const d = R.detail;
      if (d && d.sheet === s.sheet && d.dpi >= dpi * 0.9 && d.box[0] <= b[0] && d.box[1] <= b[1] && d.box[2] >= b[2] && d.box[3] >= b[3]) return;
      // pad the crop a little so small pans stay sharp; keep under the server's pixel budget
      const padX = (b[2] - b[0]) * 0.15, padY = (b[3] - b[1]) * 0.15;
      const box = [Math.max(s.bbox[0], b[0] - padX), Math.max(s.bbox[1], b[1] - padY), Math.min(s.bbox[2], b[2] + padX), Math.min(s.bbox[3], b[3] + padY)];
      let q = dpi;
      while ((box[2] - box[0]) * (box[3] - box[1]) * (q / 72) ** 2 > 16e6 && q > 24) q = Math.round(q * 0.8);
      const H = s.bbox[3] - s.bbox[1];
      const clip = [box[0] - s.bbox[0], H - (box[3] - s.bbox[1]), box[2] - s.bbox[0], H - (box[1] - s.bbox[1])].map((v) => v.toFixed(1)).join(",");
      const want = `${s.sheet}|${q}|${clip}`;
      if (R.want === want) return;
      R.want = want;
      this._rasterImage(R.url(s.sheet, q, clip), (c) => {
        if (R.want !== want) return;
        R.detail = c ? { canvas: c, box, sheet: s.sheet, dpi: q } : null;
      });
    }, 220);
  }

  // ------------------------------------------------------------ highlight API
  /** groups: [{color, refs:[{bbox,...}], label:{title, sub}}] */
  setSelection(groups) {
    this.groups = (groups || []).map((g) => this._bake(g));
    this.preview = null;
    this._selT = performance.now();
    this._miniDirty = true;
    this.draw();
  }

  setPreview(group) {
    this.preview = group ? this._bake(group) : null;
    this.draw();
  }

  _bake(g) {
    const path = new Path2D(), fill = new Path2D();
    const texts = new Set();
    const ox = this.ox, oy = this.oy;
    const idx = g.entities || [].concat(...(g.refs || []).map((r) => this.refEntities(r)));
    for (const i of idx) {
      const e = this.E[i];
      if (e.t === "T") texts.add(i);
      else if (e.t === "H") addLoops(path, e.p, ox, oy);
      else addEntity(path, e, ox, oy);
    }
    let boxes = (g.refs || []).map((r) => r.bbox).filter(Boolean);
    if (!boxes.length && g.focus) boxes = g.focus;
    return { ...g, path, fill, texts, count: idx.length, boxes };
  }

  // ------------------------------------------------------------ view
  get W() { return this.canvas.clientWidth || 800; }
  get H() { return this.canvas.clientHeight || 600; }
  toWorld(sx, sy) { return [(sx - this.tx) / this.s + this.ox, -(sy - this.ty) / this.s + this.oy]; }
  toScreen(wx, wy) { return [(wx - this.ox) * this.s + this.tx, -(wy - this.oy) * this.s + this.ty]; }
  _view() { const [cx, cy] = this.toWorld(this.W / 2, this.H / 2); return { cx, cy, w: this.W / this.s }; }
  _setView(cx, cy, w) {
    this.s = this.W / w;
    this.tx = this.W / 2 - (cx - this.ox) * this.s;
    this.ty = this.H / 2 + (cy - this.oy) * this.s;
  }
  _target(b, pad = 1.12, minW = 0) {
    const bw = Math.max(b[2] - b[0], 1), bh = Math.max(b[3] - b[1], 1);
    const top = Math.min(this.insetTop, this.H * 0.4), Hv = this.H - top;
    const w = Math.max(bw, bh * this.W / Hv, minW) * pad;
    // centre the box in the part of the canvas below the top overlay
    return { cx: (b[0] + b[2]) / 2, cy: (b[1] + b[3]) / 2 + (top / 2) * (w / this.W), w };
  }

  fit(b = this.home, animate = true, pad = 1.08) {
    this._lastFit = b;
    if (!this._sized) return;
    const t = this._target(b, pad);
    if (!animate || reducedMotion()) { this._anim = null; this._setView(t.cx, t.cy, t.w); this.draw(); return; }
    this.flyTo(t);
  }

  /** Smooth zoom-and-pan (van Wijk & Nuij) to a world bbox. */
  focus(b, { pad = 1.9, minW = 9000 } = {}) {
    const t = this._target(b, pad, minW);
    if (reducedMotion()) { this._setView(t.cx, t.cy, t.w); this.draw(); return; }
    this.flyTo(t);
  }

  flyTo(t) {
    const v = this._view();
    const rho = 1.42, dx = t.cx - v.cx, dy = t.cy - v.cy, u1 = Math.hypot(dx, dy);
    const w0 = v.w, w1 = t.w;
    let S, at;
    if (u1 < 1e-6 * Math.max(w0, w1)) {
      const k = w1 < w0 ? -1 : 1;
      S = Math.abs(Math.log(w1 / w0)) / rho;
      at = (s) => [0, w0 * Math.exp(k * rho * s)];
    } else {
      const b = (i) => { const wi = i ? w1 : w0; return (w1 * w1 - w0 * w0 + (i ? -1 : 1) * rho ** 4 * u1 * u1) / (2 * wi * rho * rho * u1); };
      const r = (i) => Math.log(Math.sqrt(b(i) ** 2 + 1) - b(i));
      const r0 = r(0), r1 = r(1);
      S = (r1 - r0) / rho;
      at = (s) => [w0 / (rho * rho) * (Math.cosh(r0) * Math.tanh(rho * s + r0) - Math.sinh(r0)), w0 * Math.cosh(r0) / Math.cosh(rho * s + r0)];
    }
    if (!Number.isFinite(S) || S <= 0) { this._setView(t.cx, t.cy, t.w); this.draw(); return; }
    const dur = Math.min(1100, Math.max(380, S * 260));
    const t0 = performance.now();
    this._anim = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      const e = p < 0.5 ? 4 * p * p * p : 1 - (-2 * p + 2) ** 3 / 2;
      const [u, w] = at(e * S);
      const f = u1 ? u / u1 : 0;
      this._setView(v.cx + dx * f, v.cy + dy * f, w);
      if (p >= 1) { this._setView(t.cx, t.cy, t.w); this._anim = null; }
    };
    this.draw();
  }

  resize() {
    const dpr = window.devicePixelRatio || 1;
    const v = this.E ? this._view() : null;
    this.canvas.width = Math.max(1, Math.round(this.W * dpr));
    this.canvas.height = Math.max(1, Math.round(this.H * dpr));
    const m = this.mini;
    m.width = Math.round(m.clientWidth * dpr); m.height = Math.round(m.clientHeight * dpr);
    this._miniDirty = true;
    const real = this.canvas.clientWidth > 10 && this.canvas.clientHeight > 10;
    if (v && this._sized && real) this._setView(v.cx, v.cy, v.w);
    else if (this.E && real) { this._sized = true; this.fit(this._lastFit || this.home, false); }
    if (!real) this._sized = false;
    this.draw();
  }

  // ------------------------------------------------------------ drawing
  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame((now) => {
      this._raf = 0;
      if (this._anim) this._anim(now);
      this._frame(now);
      if (this._anim || (this._selT && now - this._selT < 420)) this.draw();
    });
  }

  _layerInk(L, alpha) {
    if (this.colourMode === "cad") return withAlpha(L.color, alpha * (L.role === "ignore" ? 0.8 : 1));
    const c = ROLE_INK[L.role] || ROLE_INK.ignore;
    return `rgba(${c[0]},${c[1]},${c[2]},${c[3] * alpha})`;
  }

  _frame(now) {
    if (!this.E) return;
    const ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const W = this.W, H = this.H;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#0d2233";
    ctx.fillRect(0, 0, W, H);
    if (this.sheets.length) this._paper(ctx, dpr);
    else this._grid(ctx, W, H);

    let vb = [...this.toWorld(0, H), ...this.toWorld(W, 0)];
    const sel = this.groups.length > 0;
    const dim = sel ? 0.26 : this.preview ? 0.5 : 1;
    const c = this.clip;
    if (c) {
      ctx.save();
      const [x0, y0] = this.toScreen(c[0], c[3]), [x1, y1] = this.toScreen(c[2], c[1]);
      ctx.beginPath(); ctx.rect(x0, y0, x1 - x0, y1 - y0); ctx.clip();
      vb = [Math.max(vb[0], c[0]), Math.max(vb[1], c[1]), Math.min(vb[2], c[2]), Math.min(vb[3], c[3])];
    }
    const raster = this.raster.on && this.raster.url && this.sheets.length;
    if (raster) this._rasterFrame(ctx, dpr, vb, dim);

    ctx.setTransform(dpr * this.s, 0, 0, -dpr * this.s, dpr * this.tx, dpr * this.ty);
    ctx.lineJoin = "round"; ctx.lineCap = "round";
    const lw = 1 / this.s;
    // with the raster backdrop on, the vector linework stays pickable but is not drawn
    if (!raster) for (const L of this.layers) {
      if (!L.visible || !L.n) continue;
      const ink = this._layerInk(L, dim);
      ctx.strokeStyle = ink;
      ctx.lineWidth = (L.role.startsWith("wall") || L.role === "column" ? 1.35 : 0.9) * lw;
      for (const ch of L.chunkList) {
        const b = ch.bb;
        if (b[2] < vb[0] || b[0] > vb[2] || b[3] < vb[1] || b[1] > vb[3]) continue;
        if (ch.fill) { ctx.fillStyle = this._layerInk(L, dim * 0.22); ctx.fill(ch.fill, "evenodd"); }
        if (ch.solid) { ctx.fillStyle = this._layerInk(L, dim * 0.5); ctx.fill(ch.solid, "nonzero"); }
        ctx.stroke(ch.path);
      }
    }
    if (this.showText && !raster) this._texts(ctx, dpr, vb, dim);

    const fade = this._selT ? Math.min(1, (now - this._selT) / 380) : 1;
    if (this.preview) this._group(ctx, dpr, this.preview, 0.75, false);
    this.groups.forEach((g, k) => this._group(ctx, dpr, g, fade, k === 0));

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.marks.length) this._marks(ctx);
    if (c) ctx.restore();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.measure) this._measure(ctx);
    if (this.showRegions && !this.sheets.length) this._regions(ctx);
    if (this.showMarkers) this._flags(ctx);
    if (this.groups[0]?.label) this._label(ctx, this.groups[0], fade);
    this._minimap();
    this._scale();
  }

  _grid(ctx, W, H) {
    // drafting-film grid in world metres, fades in/out with zoom
    const steps = [1000, 5000, 10000, 50000, 100000, 500000];
    for (const st of steps) {
      const px = st * this.s;
      if (px < 14) continue;
      const a = Math.min(0.5, (px - 14) / 120) * 0.35;
      ctx.strokeStyle = `rgba(80,130,170,${a})`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      const [wx0, wy1] = this.toWorld(0, 0), [wx1, wy0] = this.toWorld(W, H);
      if ((wx1 - wx0) / st > 400) continue;
      for (let x = Math.ceil(wx0 / st) * st; x <= wx1; x += st) { const sx = Math.round(this.toScreen(x, 0)[0]) + 0.5; ctx.moveTo(sx, 0); ctx.lineTo(sx, H); }
      for (let y = Math.ceil(wy0 / st) * st; y <= wy1; y += st) { const sy = Math.round(this.toScreen(0, y)[1]) + 0.5; ctx.moveTo(0, sy); ctx.lineTo(W, sy); }
      ctx.stroke();
      break;
    }
  }

  /** Sheets as paper on the drafting table: a frame and the drawing number. */
  _paper(ctx, dpr) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = `14px ${ISO}`; ctx.textBaseline = "bottom"; ctx.textAlign = "left";
    for (const s of this.sheets) {
      if (this.clip && !sameBox(s.bbox, this.clip)) continue;
      const [x0, y0] = this.toScreen(s.bbox[0], s.bbox[3]), [x1, y1] = this.toScreen(s.bbox[2], s.bbox[1]);
      if (x1 < 0 || y1 < 0 || x0 > this.W || y0 > this.H) continue;
      ctx.fillStyle = "#0f2739";
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      ctx.strokeStyle = "#3c6684"; ctx.lineWidth = 1;
      ctx.strokeRect(Math.round(x0) + 0.5, Math.round(y0) + 0.5, Math.round(x1 - x0), Math.round(y1 - y0));
      if (!this.clip && x1 - x0 > 60) { ctx.fillStyle = "#93aec3"; ctx.fillText(s.label, x0, y0 - 5); }
    }
  }

  _marks(ctx) {
    for (const m of this.marks) {
      const [x, y] = this.toScreen(m.x, m.y);
      if (x < -10 || y < -10 || x > this.W + 10 || y > this.H + 10) continue;
      ctx.beginPath();
      ctx.moveTo(x, y - 5); ctx.lineTo(x + 5, y); ctx.lineTo(x, y + 5); ctx.lineTo(x - 5, y); ctx.closePath();
      ctx.fillStyle = m.color || "#e9c46a"; ctx.fill();
      ctx.strokeStyle = "#0d2233"; ctx.lineWidth = 1.5; ctx.stroke();
    }
  }

  /** Two-point calibration: points, the line between them and its length on paper. */
  _measure(ctx) {
    const M = this.measure;
    const a = M.p1, b = M.p2 || M.hover;
    const col = "#e9c46a";
    const dot = (p) => {
      const [x, y] = this.toScreen(p[0], p[1]);
      ctx.strokeStyle = col; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - 12, y); ctx.lineTo(x + 12, y); ctx.moveTo(x, y - 12); ctx.lineTo(x, y + 12); ctx.stroke();
    };
    if (M.snap && !M.p2) { const [x, y] = this.toScreen(M.snap[0], M.snap[1]); ctx.strokeStyle = "#5cc8ff"; ctx.lineWidth = 1.5; ctx.strokeRect(x - 5, y - 5, 10, 10); }
    if (!a) return;
    dot(a);
    if (!b) return;
    const [x0, y0] = this.toScreen(a[0], a[1]), [x1, y1] = this.toScreen(b[0], b[1]);
    ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.setLineDash(M.p2 ? [] : [6, 4]);
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
    if (M.p2) dot(M.p2);
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const mm = this.scaleMM ? len / this.scaleMM : null;
    const label = `${fmt(len, 1)} pt${mm ? `  ≈ ${fmt(mm, 0)} mm now` : ""}`;
    ctx.font = `600 13px ${SANS}`;
    const w = ctx.measureText(label).width + 14;
    const mx = (x0 + x1) / 2, my = (y0 + y1) / 2 - 18;
    ctx.fillStyle = "rgba(9,26,40,.94)"; ctx.beginPath(); ctx.roundRect(mx - w / 2, my - 11, w, 22, 3); ctx.fill();
    ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = "#f4f9fc"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(label, mx, my);
  }

  /** Nearest drawn vertex within 10 px of a screen point (for calibration). */
  snap(sx, sy) {
    const [wx, wy] = this.toWorld(sx, sy), tol = 10 / this.s;
    let best = null, bd = tol;
    for (const i of this.query([wx - tol, wy - tol, wx + tol, wy + tol])) {
      const e = this.E[i];
      if ((e.t !== "L" && e.t !== "P") || !this._inClip(i) || !this.layers[e.l]?.visible) continue;
      const p = e.p;
      for (let k = 0; k + 1 < p.length; k += 2) {
        const d = Math.hypot(p[k] - wx, p[k + 1] - wy);
        if (d < bd) { bd = d; best = [p[k], p[k + 1]]; }
      }
    }
    return best;
  }

  _texts(ctx, dpr, vb, dim) {
    const hiT = new Map();
    for (const g of [this.preview, ...this.groups]) if (g) for (const i of g.texts) hiT.set(i, g.color);
    let drawn = 0;
    for (const t of this.texts) {
      const L = this.layers[t.l];
      if (!L.visible) continue;
      const px = t.h * this.s;
      const hc = hiT.get(t.i);
      if (px < (hc ? 2.5 : 4.5)) continue;
      const x = t.x + this.ox, y = t.y + this.oy, reach = t.h * Math.max(6, t.s.length) * 0.9;
      if (x + reach < vb[0] || x - reach > vb[2] || y + reach < vb[1] || y - reach > vb[3]) continue;
      if (++drawn > 2500) break;
      const sx = t.x * this.s + this.tx, sy = -t.y * this.s + this.ty;
      ctx.setTransform(dpr, 0, 0, dpr, dpr * sx, dpr * sy);
      if (t.r) ctx.rotate(-t.r * Math.PI / 180);
      ctx.fillStyle = hc || this._layerInk(L, dim * 0.95);
      ctx.font = `${px * 1.2}px ${ISO}`;
      const lines = t.s.split("\n"), step = px * 1.6;
      let y0 = 0;
      if (t.ap) {
        const row = Math.floor((t.ap - 1) / 3), col = (t.ap - 1) % 3;
        ctx.textAlign = ["left", "center", "right"][col];
        ctx.textBaseline = "top";
        y0 = row === 0 ? 0 : row === 1 ? -(lines.length * step) / 2 : -(lines.length * step);
      } else { ctx.textAlign = "left"; ctx.textBaseline = "alphabetic"; }
      for (let k = 0; k < lines.length; k++) ctx.fillText(lines[k], 0, y0 + k * step);
    }
    ctx.setTransform(dpr * this.s, 0, 0, -dpr * this.s, dpr * this.tx, dpr * this.ty);
  }

  _group(ctx, dpr, g, fade, primary) {
    const lw = 1 / this.s;
    ctx.setTransform(dpr * this.s, 0, 0, -dpr * this.s, dpr * this.tx, dpr * this.ty);
    ctx.strokeStyle = g.color;
    // glow: wide soft passes under a crisp core line
    ctx.globalAlpha = 0.16 * fade; ctx.lineWidth = 9 * lw; ctx.stroke(g.path);
    ctx.globalAlpha = 0.35 * fade; ctx.lineWidth = 4.5 * lw; ctx.stroke(g.path);
    ctx.globalAlpha = fade; ctx.lineWidth = 1.7 * lw; ctx.stroke(g.path);
    ctx.globalAlpha = 1;
    // ref boxes with drafting corner ticks
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const b of g.boxes) {
      const [x0, y0] = this.toScreen(b[0], b[3]), [x1, y1] = this.toScreen(b[2], b[1]);
      const pad = 6, X0 = x0 - pad, Y0 = y0 - pad, X1 = x1 + pad, Y1 = y1 + pad;
      ctx.strokeStyle = g.color;
      ctx.globalAlpha = (primary ? 0.9 : 0.6) * fade;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.strokeRect(X0 + 0.5, Y0 + 0.5, X1 - X0, Y1 - Y0);
      ctx.setLineDash([]);
      ctx.lineWidth = 2;
      const k = Math.min(12, (X1 - X0) / 3, (Y1 - Y0) / 3);
      ctx.beginPath();
      for (const [cx, cy, sx, sy] of [[X0, Y0, 1, 1], [X1, Y0, -1, 1], [X0, Y1, 1, -1], [X1, Y1, -1, -1]]) {
        ctx.moveTo(cx + sx * k, cy); ctx.lineTo(cx, cy); ctx.lineTo(cx, cy + sy * k);
      }
      ctx.stroke();
      if (!g.count && (X1 - X0) < 30) { // nothing to glow: draw a target ring
        ctx.beginPath(); ctx.arc((X0 + X1) / 2, (Y0 + Y1) / 2, 14, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
  }

  _label(ctx, g, fade) {
    const bs = g.boxes;
    if (!bs.length) return;
    let X1 = -Infinity, Y0 = Infinity;
    for (const b of bs) { const [x1, y0] = this.toScreen(b[2], b[3]); X1 = Math.max(X1, x1); Y0 = Math.min(Y0, y0); }
    const { title, sub } = g.label;
    ctx.font = `20px ${ISO}`;
    const tw = ctx.measureText(title).width;
    ctx.font = `500 13px ${SANS}`;
    const sw = sub ? Math.min(ctx.measureText(sub).width, 300) : 0;
    const w = Math.max(tw, sw) + 26, h = sub ? 52 : 34;
    let x = X1 + 26, y = Y0 - 26 - h;
    x = clamp(x, 8, this.W - w - 240 > 8 ? this.W - w - 8 : this.W - w - 8);
    y = clamp(y, 8, this.H - h - 170);
    ctx.globalAlpha = fade;
    // leader
    ctx.strokeStyle = g.color; ctx.lineWidth = 1.25;
    ctx.beginPath(); ctx.moveTo(X1 + 6, Y0 - 6); ctx.lineTo(x, y + h); ctx.stroke();
    ctx.beginPath(); ctx.arc(X1 + 6, Y0 - 6, 2.5, 0, Math.PI * 2); ctx.fillStyle = g.color; ctx.fill();
    ctx.fillStyle = "rgba(9,26,40,.94)";
    ctx.beginPath(); ctx.roundRect(x, y, w, h, 4); ctx.fill();
    ctx.strokeStyle = g.color; ctx.stroke();
    ctx.fillStyle = g.color; ctx.fillRect(x, y + 6, 3, h - 12);
    ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillStyle = "#f4f9fc"; ctx.font = `20px ${ISO}`;
    ctx.fillText(title, x + 13, y + 25);
    if (sub) {
      ctx.fillStyle = "#a9c2d6"; ctx.font = `500 13px ${SANS}`;
      ctx.fillText(ellipsize(ctx, sub, 300), x + 13, y + 43);
    }
    ctx.globalAlpha = 1;
  }

  _regions(ctx) {
    ctx.font = `14px ${ISO}`; ctx.textBaseline = "bottom"; ctx.textAlign = "left";
    for (const r of this.regions) {
      const [x0, y0] = this.toScreen(r.bbox[0], r.bbox[3]), [x1, y1] = this.toScreen(r.bbox[2], r.bbox[1]);
      if (x1 < 0 || y1 < 0 || x0 > this.W || y0 > this.H) continue;
      const sel = r.id === this.selectedRegion;
      const col = sel ? "#e9c46a" : r.include ? "rgba(233,196,106,.75)" : "rgba(147,174,195,.45)";
      ctx.strokeStyle = col; ctx.lineWidth = sel ? 2 : 1;
      ctx.setLineDash(r.include ? [] : [5, 5]);
      ctx.strokeRect(x0 - 8.5, y0 - 8.5, x1 - x0 + 17, y1 - y0 + 17);
      ctx.setLineDash([]);
      const label = r.include ? r.name : `${r.name} (not measured)`;
      ctx.fillStyle = col;
      ctx.fillText(label, x0 - 8, y0 - 13);
    }
  }

  _flagPoint(f) {
    const L = f.location;
    if (L?.bbox) return [(L.bbox[0] + L.bbox[2]) / 2, (L.bbox[1] + L.bbox[3]) / 2];
    if (L && L.x != null) return [L.x, L.y];
    const b = f.refs?.[0]?.bbox;
    return b ? [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2] : null;
  }

  _markers() {
    const c = this.clip;
    return this.flags.filter((f) => f.status === "open" || f.id === this.selectedFlag)
      .map((f) => ({ f, p: this._flagPoint(f) }))
      .filter((m) => m.p && (!c || (m.p[0] >= c[0] && m.p[0] <= c[2] && m.p[1] >= c[1] && m.p[1] <= c[3])));
  }

  _flags(ctx) {
    for (const { f, p } of this._markers()) {
      const [x, y] = this.toScreen(p[0], p[1]);
      if (x < -20 || y < -20 || x > this.W + 20 || y > this.H + 20) continue;
      const sel = f.id === this.selectedFlag, col = SEV[f.severity]?.d || "#ff6b57";
      const r = sel ? 11 : 8;
      ctx.lineWidth = 2;
      if (sel) { ctx.strokeStyle = col; ctx.globalAlpha = 0.45; ctx.beginPath(); ctx.arc(x, y, r + 7, 0, Math.PI * 2); ctx.stroke(); ctx.globalAlpha = 1; }
      ctx.beginPath();
      if (f.severity === "warning") { ctx.moveTo(x, y - r - 2); ctx.lineTo(x + r + 2, y + r - 1); ctx.lineTo(x - r - 2, y + r - 1); ctx.closePath(); }
      else ctx.arc(x, y, r, 0, Math.PI * 2);
      if (f.severity === "missing") {
        ctx.fillStyle = "#0d2233"; ctx.fill(); ctx.setLineDash([3, 2]); ctx.strokeStyle = col; ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = col;
      } else {
        ctx.fillStyle = col; ctx.fill(); ctx.strokeStyle = "#0d2233"; ctx.lineWidth = 1.5; ctx.stroke();
        ctx.fillStyle = "#0d2233";
      }
      if (f.status !== "open") { ctx.globalAlpha = 0.6; }
      ctx.font = `700 ${sel ? 13 : 11}px ${SANS}`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(f.severity === "missing" ? "?" : "!", x, y + (f.severity === "warning" ? 2 : 0.5));
      ctx.globalAlpha = 1;
    }
  }

  _miniRender() {
    const m = this.mini, c = this.mctx, dpr = window.devicePixelRatio || 1;
    const w = m.width, h = m.height;
    if (!this._miniCanvas) this._miniCanvas = document.createElement("canvas");
    const oc = this._miniCanvas;
    oc.width = w; oc.height = h;
    const o = oc.getContext("2d");
    const b = this.clip || this.home;
    const s = Math.min(w / (b[2] - b[0]), h / (b[3] - b[1])) * 0.92;
    const mx = w / 2 - ((b[0] + b[2]) / 2 - this.ox) * s, my = h / 2 + ((b[1] + b[3]) / 2 - this.oy) * s;
    this._miniT = { s, mx, my };
    o.fillStyle = "#0a1b29"; o.fillRect(0, 0, w, h);
    o.setTransform(s, 0, 0, -s, mx, my);
    if (this.clip) { o.beginPath(); o.rect(b[0] - this.ox, b[1] - this.oy, b[2] - b[0], b[3] - b[1]); o.clip(); }
    o.lineWidth = 1 / s;
    for (const L of this.layers) {
      if (!L.visible || !L.n || L.role === "dimension") continue;
      o.strokeStyle = L.role === "ignore" ? "rgba(120,160,190,.45)" : "rgba(200,222,240,.85)";
      for (const ch of L.chunkList) o.stroke(ch.path);
    }
    this._miniDirty = false;
    void c; void dpr;
  }

  _minimap() {
    if (this._miniDirty) this._miniRender();
    const c = this.mctx, m = this.mini;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.drawImage(this._miniCanvas, 0, 0);
    const { s, mx, my } = this._miniT;
    const ms = (wx, wy) => [(wx - this.ox) * s + mx, -(wy - this.oy) * s + my];
    for (const g of this.groups) for (const b of g.boxes) {
      const [x0, y0] = ms(b[0], b[3]), [x1, y1] = ms(b[2], b[1]);
      c.fillStyle = g.color;
      c.fillRect(Math.min(x0, x1) - 1, Math.min(y0, y1) - 1, Math.max(3, Math.abs(x1 - x0)), Math.max(3, Math.abs(y1 - y0)));
    }
    const [a0, a1] = this.toWorld(0, 0), [b0, b1] = this.toWorld(this.W, this.H);
    const [x0, y0] = ms(a0, a1), [x1, y1] = ms(b0, b1);
    const dpr = window.devicePixelRatio || 1;
    c.strokeStyle = "#e9c46a"; c.lineWidth = 1.5 * dpr;
    c.fillStyle = "rgba(233,196,106,.08)";
    const rx = clamp(x0, -2, m.width + 2), ry = clamp(y0, -2, m.height + 2);
    const rw = Math.max(4, clamp(x1, -2, m.width + 2) - rx), rh = Math.max(4, clamp(y1, -2, m.height + 2) - ry);
    c.fillRect(rx, ry, rw, rh); c.strokeRect(rx, ry, rw, rh);
  }

  _scale() {
    const cv = this.scalebar, c = cv.getContext("2d"), dpr = window.devicePixelRatio || 1;
    if (cv.width !== 180 * dpr) { cv.width = 180 * dpr; cv.height = 28 * dpr; cv.style.width = "180px"; cv.style.height = "28px"; }
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, 180, 28);
    const k = this.scaleMM || 1;   // world units per shown unit (pt sheets: pt per real mm)
    const target = 110 / this.s / k;
    const p = 10 ** Math.floor(Math.log10(target));
    const len = [1, 2, 5, 10].map((m) => m * p).filter((v) => v <= target).pop() || p;
    const px = len * this.s * k;
    const seg = px / 4;
    for (let i = 0; i < 4; i++) {
      c.fillStyle = i % 2 ? "#0d2233" : "#c9dcea";
      c.fillRect(2 + i * seg, 16, seg, 5);
    }
    c.strokeStyle = "#c9dcea"; c.lineWidth = 1; c.strokeRect(2.5, 16.5, px, 5);
    c.fillStyle = "#c9dcea"; c.font = `12px ${ISO}`; c.textBaseline = "alphabetic"; c.textAlign = "left";
    const unit = this.scaleMM ? "mm" : this.geo?.units || "mm";
    const label = unit === "mm" && len >= 1000 ? `${fmt(len / 1000, 2)} m` : `${fmt(len, 2)} ${unit}`;
    c.fillText("0", 0, 12);
    c.textAlign = "right"; c.fillText(label, Math.min(178, px + 14), 12);
  }

  // ------------------------------------------------------------ picking
  pick(sx, sy) {
    const [wx, wy] = this.toWorld(sx, sy);
    const tol = 6 / this.s;
    let best = -1, bd = Infinity, ba = Infinity;
    const eb = this.eb;
    for (const i of this.query([wx - tol, wy - tol, wx + tol, wy + tol])) {
      const k = i * 4;
      if (wx < eb[k] - tol || wx > eb[k + 2] + tol || wy < eb[k + 1] - tol || wy > eb[k + 3] + tol) continue;
      const e = this.E[i], L = this.layers[e.l];
      if (!L?.visible || !this._inClip(i)) continue;
      if (e.t === "T" && !this.showText && !this.raster.on) continue;
      const d = entityDist(e, wx, wy, eb, k);
      if (d > tol) continue;
      const area = (eb[k + 2] - eb[k]) * (eb[k + 3] - eb[k + 1]);
      if (d < bd - tol * 0.2 || (Math.abs(d - bd) <= tol * 0.2 && area < ba)) { best = i; bd = d; ba = area; }
    }
    return best;
  }

  /** All drawable entity indices belonging to the same top-level entity. */
  family(i) {
    const m = this.members.get(this.owner[i]);
    return m ? m.filter((j) => DRAWN.has(this.E[j].t)) : [i];
  }

  // ------------------------------------------------------------ input
  _bind() {
    const c = this.canvas;
    let drag = null;
    c.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 && ev.button !== 1) return;
      c.setPointerCapture(ev.pointerId);
      drag = { x: ev.clientX, y: ev.clientY, tx: this.tx, ty: this.ty, moved: false };
      this._anim = null;
    });
    c.addEventListener("pointermove", (ev) => {
      const r = c.getBoundingClientRect();
      if (drag) {
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; c.classList.add("grab"); }
        if (drag.moved) { this.tx = drag.tx + dx; this.ty = drag.ty + dy; this.draw(); }
      }
      const [wx, wy] = this.toWorld(ev.clientX - r.left, ev.clientY - r.top);
      this._emit("coords", wx, wy);
      if (this.measure && !this.measure.p2 && !drag?.moved) {
        const sn = this.snap(ev.clientX - r.left, ev.clientY - r.top);
        this.measure.snap = sn;
        this.measure.hover = sn || [wx, wy];
        this.draw();
      }
    });
    c.addEventListener("pointerup", (ev) => {
      if (!drag) return;
      c.classList.remove("grab");
      const moved = drag.moved;
      drag = null;
      if (moved) return;
      const r = c.getBoundingClientRect();
      const sx = ev.clientX - r.left, sy = ev.clientY - r.top;
      if (this.measure) {
        const p = this.snap(sx, sy) || this.toWorld(sx, sy);
        this._emit("measure", p, sx, sy);
        return;
      }
      if (this.showMarkers) {
        for (const { f, p } of this._markers().reverse()) {
          const [x, y] = this.toScreen(p[0], p[1]);
          if (Math.hypot(x - sx, y - sy) <= 12) { this._emit("flag", f.id); return; }
        }
      }
      if (this.showRegions && this.callbacks.region) {
        const [wx, wy] = this.toWorld(sx, sy);
        let best = null, area = Infinity;
        for (const g of this.regions) {
          const b = g.bbox;
          if (wx >= b[0] && wx <= b[2] && wy >= b[1] && wy <= b[3] && (b[2] - b[0]) * (b[3] - b[1]) < area) { best = g.id; area = (b[2] - b[0]) * (b[3] - b[1]); }
        }
        this._emit("region", best);
      }
      this._emit("pick", this.pick(sx, sy), sx, sy);
    });
    c.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      this._anim = null;
      const r = c.getBoundingClientRect();
      const mx = ev.clientX - r.left, my = ev.clientY - r.top;
      const k = ev.deltaMode === 1 ? 0.05 : 0.0016;
      const f = Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.012 : k));
      this.tx = mx - (mx - this.tx) * f; this.ty = my - (my - this.ty) * f; this.s *= f;
      this.draw();
    }, { passive: false });
    c.addEventListener("keydown", (ev) => {
      const step = 80;
      const k = ev.key;
      if (k === "+" || k === "=" || k === "-") {
        const f = k === "-" ? 1 / 1.4 : 1.4, mx = this.W / 2, my = this.H / 2;
        this.tx = mx - (mx - this.tx) * f; this.ty = my - (my - this.ty) * f; this.s *= f;
      } else if (k === "ArrowLeft") this.tx += step;
      else if (k === "ArrowRight") this.tx -= step;
      else if (k === "ArrowUp") this.ty += step;
      else if (k === "ArrowDown") this.ty -= step;
      else return;
      ev.preventDefault();
      this.draw();
    });
    // minimap navigation
    const m = this.mini;
    let mdrag = false;
    const go = (ev, animate) => {
      if (!this._miniT) return;
      const r = m.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
      const px = (ev.clientX - r.left) * dpr, py = (ev.clientY - r.top) * dpr;
      const { s, mx, my } = this._miniT;
      const wx = (px - mx) / s + this.ox, wy = -(py - my) / s + this.oy;
      const v = this._view();
      if (animate && !reducedMotion()) this.flyTo({ cx: wx, cy: wy, w: v.w });
      else { this._anim = null; this._setView(wx, wy, v.w); this.draw(); }
    };
    m.addEventListener("pointerdown", (ev) => { mdrag = true; m.setPointerCapture(ev.pointerId); go(ev, true); });
    m.addEventListener("pointermove", (ev) => { if (mdrag) go(ev, false); });
    m.addEventListener("pointerup", () => { mdrag = false; });
  }
}

// ------------------------------------------------------------ geometry helpers
function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
function sameBox(a, b) { return a && b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2] && a[3] === b[3]; }

function fixColour(c) {
  if (!c || c.length !== 7) return "#c9dcea";
  const n = parseInt(c.slice(1), 16), r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  return 0.299 * r + 0.587 * g + 0.114 * b < 70 ? "#8fb3cc" : c;
}

function withAlpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

function ellipsize(ctx, s, max) {
  if (ctx.measureText(s).width <= max) return s;
  while (s.length > 4 && ctx.measureText(`${s}…`).width > max) s = s.slice(0, -1);
  return `${s}…`;
}

function addEntity(path, e, ox, oy) {
  const p = e.p;
  if (!p) return;
  if (e.t === "L") { path.moveTo(p[0] - ox, p[1] - oy); path.lineTo(p[2] - ox, p[3] - oy); return; }
  if (p.length < 4) return;
  path.moveTo(p[0] - ox, p[1] - oy);
  for (let i = 2; i < p.length; i += 2) path.lineTo(p[i] - ox, p[i + 1] - oy);
  if (e.c === true) path.closePath();
}

function addLoops(path, loops, ox, oy) {
  for (const loop of loops || []) {
    if (loop.length < 4) continue;
    path.moveTo(loop[0] - ox, loop[1] - oy);
    for (let i = 2; i < loop.length; i += 2) path.lineTo(loop[i] - ox, loop[i + 1] - oy);
    path.closePath();
  }
}

export function entityBox(e) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const add = (x, y) => { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; };
  switch (e.t) {
    case "L": case "P": case "A": case "C": {
      const p = e.p || [];
      for (let i = 0; i + 1 < p.length; i += 2) add(p[i], p[i + 1]);
      if (e.c && Array.isArray(e.c) && e.r) { add(e.c[0] - e.r, e.c[1] - e.r); add(e.c[0] + e.r, e.c[1] + e.r); }
      break;
    }
    case "H": for (const l of e.p || []) for (let i = 0; i + 1 < l.length; i += 2) add(l[i], l[i + 1]); break;
    case "T": {
      if (e.bb) { add(e.bb[0], e.bb[1]); add(e.bb[2], e.bb[3]); break; }
      const h = e.h || 1, lines = String(e.s || "").split("\n");
      const w = Math.max(...lines.map((s) => s.length)) * h * 0.8, ht = lines.length * h * 1.6;
      const [x, y] = e.p;
      let dx = 0, dy = 0;
      if (e.ap) { const row = Math.floor((e.ap - 1) / 3), col = (e.ap - 1) % 3; dx = -w * col / 2; dy = row === 0 ? -ht : row === 1 ? -ht / 2 : 0; }
      if (e.r && Math.abs(e.r) > 1) { const R = Math.max(w, ht); add(x - R, y - R); add(x + R, y + R); }
      else { add(x + dx, y + dy); add(x + dx + w, y + dy + (e.ap ? ht : h)); }
      break;
    }
    case "D": for (const k of ["p1", "p2", "tp", "dp"]) if (e[k]) add(e[k][0], e[k][1]); break;
    case "I": if (e.p) add(e.p[0], e.p[1]); break;
    default: if (Array.isArray(e.p) && typeof e.p[0] === "number") add(e.p[0], e.p[1]);
  }
  return [x0, y0, x1, y1];
}

function robustBox(cx, cy, E) {
  const xs = [], ys = [];
  for (let i = 0; i < cx.length; i++) if (DRAWN.has(E[i].t) && Number.isFinite(cx[i])) { xs.push(cx[i]); ys.push(cy[i]); }
  if (!xs.length) return [0, 0, 1000, 1000];
  xs.sort((a, b) => a - b); ys.sort((a, b) => a - b);
  const q = (a, f) => a[Math.min(a.length - 1, Math.max(0, Math.round(f * (a.length - 1))))];
  const b = [q(xs, 0.005), q(ys, 0.005), q(xs, 0.995), q(ys, 0.995)];
  const px = (b[2] - b[0]) * 0.04, py = (b[3] - b[1]) * 0.04;
  return [b[0] - px, b[1] - py, b[2] + px, b[3] + py];
}

function segDist(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1, l = dx * dx + dy * dy;
  let t = l ? ((px - x1) * dx + (py - y1) * dy) / l : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

function polyDist(p, x, y, closed) {
  let d = Infinity;
  for (let i = 0; i + 3 < p.length; i += 2) d = Math.min(d, segDist(x, y, p[i], p[i + 1], p[i + 2], p[i + 3]));
  if (closed && p.length >= 4) d = Math.min(d, segDist(x, y, p[p.length - 2], p[p.length - 1], p[0], p[1]));
  return d;
}

function entityDist(e, x, y, eb, k) {
  switch (e.t) {
    case "L": return segDist(x, y, e.p[0], e.p[1], e.p[2], e.p[3]);
    case "P": case "A": case "C": return polyDist(e.p || [], x, y, e.c === true || e.t === "C");
    case "H": {
      let d = Infinity;
      for (const l of e.p || []) d = Math.min(d, polyDist(l, x, y, true));
      return d;
    }
    case "T": return x >= eb[k] && x <= eb[k + 2] && y >= eb[k + 1] && y <= eb[k + 3] ? 0 : Infinity;
    default: return Infinity;
  }
}
