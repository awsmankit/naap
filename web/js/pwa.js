// Offline support for the in-browser site: registers ../sw.js (see the notes there).
//
// Only in local mode (the GitHub Pages build; `?mode=` overrides config.js as in api.js), never
// with ?mock=1, and only on https or localhost (browsers allow service workers nowhere else).
// Server mode (FastAPI, web/dev_server.py) gets no service worker: its /api calls must always go
// to the server, and web/sw.js there is the unstamped source.

import { CONFIG } from "../config.js";

export function registerSW() {
  const params = new URLSearchParams(location.search);
  const asked = params.get("mode");
  const mode = asked === "local" || asked === "server" ? asked : CONFIG.mode === "local" ? "local" : "server";
  if (mode !== "local" || params.get("mock") === "1" || !("serviceWorker" in navigator)) return;
  const secure = location.protocol === "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  if (!secure) return;
  const go = () => navigator.serviceWorker.register(new URL("../sw.js", import.meta.url), { scope: new URL("../", import.meta.url).pathname })
    .catch((e) => console.warn("Naap: offline support is off (service worker not registered)", e));
  // after the page has loaded, so caching the app shell does not compete with first paint
  if (document.readyState === "complete") go();
  else addEventListener("load", go, { once: true });
}
