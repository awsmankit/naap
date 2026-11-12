// Shared app state and a tiny action registry so modules can call each other
// without import cycles.

export const S = {
  meta: null,
  jobId: null,
  job: null,
  geo: null,
  cfg: null,
  mb: null,            // normalised MB contract
  source: null,        // "api" | "mock" | "build"
  offline: false,      // mock without a job (no API calls)
  step: "upload",
  tab: "mb",
  rvTab: "views",
  q: "",
  trade: "",
  status: "all",
  flagStatus: "open",
  sevOn: { error: true, warning: true, missing: true },
  sel: null,           // {kind, key}
  linked: new Set(),   // row ids linked to a picked entity
  rowsByOwner: new Map(),
  builtAt: 0,
  dirtyAt: 0,
};

export const A = {};   // actions, filled in by main.js

export const rowById = (id) => S.mb?._rowIdx?.get(id);

/** Where a sheet came from when the drawing set has several PDFs: "plans.pdf, page 3" (else ""). */
export const sheetSource = (n) => {
  const sheets = S.geo?.sheets || [];
  const s = sheets.find((x) => x.sheet === Number(n));
  if (!s?.file || !sheets.some((x) => x.file_index !== s.file_index)) return "";
  return `${s.file}, page ${s.file_page ?? s.sheet}`;
};
