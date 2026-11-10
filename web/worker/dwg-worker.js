// DWG -> DXF in the browser with GNU LibreDWG compiled to WebAssembly (GPL-3.0-or-later; the
// build is @mlightcad/libredwg-web, see THIRD_PARTY_NOTICES.md). The same conversion the server
// runs with `dwg2dxf`. A short-lived worker: it is terminated after one file, which frees the
// converter's memory before the Python engine reads the DXF.
//
// In: {buf: ArrayBuffer (the DWG)}. Out: {ok: true, buf: ArrayBuffer (the DXF), code} | {ok: false, error}.

import createModule from "../vendor/libredwg/libredwg-web.js";

self.onmessage = async (e) => {
  const log = [];
  const keep = (s) => { log.push(String(s)); if (log.length > 40) log.shift(); };
  try {
    const M = await createModule({ print: keep, printErr: keep });
    M.FS.writeFile("/in.dwg", new Uint8Array(e.data.buf));
    const code = M.dwg_write_dxf("/in.dwg", "/out.dxf");
    const made = M.FS.analyzePath("/out.dxf").exists && M.FS.stat("/out.dxf").size > 0;
    if (!made) {  // as on the server: a non-empty DXF counts, whatever the (warning) code
      const tail = log.filter((l) => /error|fail/i.test(l)).slice(-3).join(" ").slice(0, 400);
      self.postMessage({ ok: false, error: `LibreDWG could not read this DWG (code ${code})${tail ? `: ${tail}` : ""}` });
      return;
    }
    const out = M.FS.readFile("/out.dxf");
    self.postMessage({ ok: true, code, buf: out.buffer }, [out.buffer]);
  } catch (err) {
    self.postMessage({ ok: false, error: String(err?.message || err) });
  }
};
