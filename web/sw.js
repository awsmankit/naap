// Naap's service worker: the in-browser site (local mode) works offline and opens fast on repeat
// visits. js/pwa.js registers it on the built site only (never in server mode or ?mock=1).
//
// scripts/build_site.py stamps the placeholders below: BUILD is a hash of the whole site (any
// change gives a new sw.js, so the browser installs it and the old caches are deleted on
// activate), VENDOR a hash of vendor/, PRECACHE the app shell. Two caches:
//   naap-app-<BUILD>     the app shell, fetched network-first (cache when offline or slow), so a
//                        new release is picked up on the next visit; fonts and icons cache-first.
//   naap-vendor-<VENDOR> Pyodide, the wheels and LibreDWG (pinned, immutable): cache-first, and
//                        only cached when the engine first loads them (never on install: the
//                        ~27 MB runtime downloads only when a drawing is opened).
// Never cached: non-GET, range requests, other origins and anything under /api/.

const BUILD = "__NAAP_BUILD__";
const VENDOR = "__NAAP_VENDOR__";
const PRECACHE = ["__NAAP_PRECACHE__"];
const APP = `naap-app-${BUILD}`;
const VEND = `naap-vendor-${VENDOR}`;
const SCOPE = new URL("./", self.location).href;
const SLOW_MS = 4000;          // network-first gives up and serves the cached copy after this
const STAMPED = !BUILD.startsWith("__");   // web/sw.js itself (not built): cache nothing, remove itself

self.addEventListener("install", (e) => {
  e.waitUntil((async () => {
    if (!STAMPED) { await self.skipWaiting(); return; }
    const c = await caches.open(APP);
    // one by one: a missing file must not stop the rest being cached
    await Promise.all(PRECACHE.map((p) =>
      c.add(new Request(new URL(p, SCOPE), { cache: "reload" })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) {
      if (k.startsWith("naap-") && (!STAMPED || (k !== APP && k !== VEND))) await caches.delete(k);
    }
    if (!STAMPED) { await self.registration.unregister(); return; }
    await self.clients.claim();
  })());
});

const cacheable = (res) => res && res.status === 200 && res.type === "basic";

// cache.put runs in the background (waitUntil): the page streams the body as it arrives, so the
// worker's download progress still counts bytes as they come
async function cacheFirst(e, name) {
  const c = await caches.open(name);
  const hit = await c.match(e.request);
  if (hit) return hit;
  const res = await fetch(e.request);
  if (cacheable(res)) e.waitUntil(c.put(e.request, res.clone()).catch(() => {}));
  return res;
}

function networkFirst(e, key) {
  const net = fetch(e.request);
  // registered now, while the event is live (the cached copy may answer before the network does)
  e.waitUntil(net.then(async (res) => {
    if (cacheable(res)) await (await caches.open(APP)).put(key, res.clone());
  }).catch(() => {}));
  const cached = () => caches.match(key, { cacheName: APP });
  let timer;
  const slow = new Promise((resolve) => { timer = setTimeout(resolve, SLOW_MS); });
  return (async () => {
    try {
      const first = await Promise.race([net, slow]);
      if (first) return first;
      return (await cached()) || await net;       // the network is slow: use the cached copy
    } catch (err) {
      const hit = await cached();                 // offline
      if (hit) return hit;
      throw err;
    } finally {
      clearTimeout(timer);
    }
  })();
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (!STAMPED || req.method !== "GET" || req.headers.has("range")) return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.href.startsWith(SCOPE)) return;
  const path = url.href.slice(SCOPE.length).split(/[?#]/)[0];
  if (path.startsWith("api/") || path.includes("/api/") || path === "openapi.json" || path === "sw.js") return;
  if (path.startsWith("vendor/")) e.respondWith(cacheFirst(e, VEND));
  else if (/^(fonts|icons)\//.test(path)) e.respondWith(cacheFirst(e, APP));
  // the page under any ?query (?mode=, ?mock=1) is the same file: keep one copy, as "./"
  else if (path === "" || path === "index.html") e.respondWith(networkFirst(e, SCOPE));
  else e.respondWith(networkFirst(e, req));
});
