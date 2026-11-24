/* CIRQ service worker. Generated for deploy {{ version }}; a new deploy produces a new worker and phones pick it up on their own.

   What it does:
     - saves the app's own small static files (stylesheets, main script, icons) and an offline page when the app is installed;
     - opens pages from the network as normal, and shows the offline page if the server can't be reached;
     - serves stylesheets/scripts network-first (always fresh when online, cached copy when not) and images/icons from cache.

   What it never does (this is deliberate, it keeps people's accounts private on shared phones):
     - cache a page, an API answer, a chat, a form or anything that belongs to a logged-in person;
     - touch requests to other sites (photos, videos, fonts, live streams) or anything that isn't a plain GET. */
"use strict";

const VERSION = {{ version|tojson }};
const STATIC_CACHE = `cirq-static-${VERSION}`;
const OFFLINE_URL = {{ offline_url|tojson }};
const PRECACHE = {{ precache|tojson }};
const NETWORK_TIMEOUT_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    await Promise.all(PRECACHE.map(async (url) => {
      const res = await fetch(new Request(url, { cache: "reload" }));       // "reload" skips the browser's HTTP cache, so a new version never saves stale files
      if (!res.ok) throw new Error(`${url} returned ${res.status}`);        // fail the whole install rather than run half-installed
      await cache.put(url, res);
    }));
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("cirq-") && key !== STATIC_CACHE) await caches.delete(key);   // drop the previous version's files
    }
    if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();   // start the page request while the worker wakes up
    await self.clients.claim();
  })());
});

self.addEventListener("message", (event) => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") self.skipWaiting();              // the person tapped "Update"
  if (event.data.type === "CLEAR_CACHES") event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("cirq-")).map((k) => caches.delete(k)))));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || req.headers.has("range")) return;             // posts, uploads and video ranges go straight to the network
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;                          // other sites are never touched

  if (req.mode === "navigate") {
    event.respondWith(page(event));
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith(/\.(css|js)$/.test(url.pathname) ? networkFirst(req) : staleWhileRevalidate(event, req));
  }
  // everything else (API calls, polling, chat, uploads, auth) is not handled here at all: it behaves exactly as without a service worker
});

async function page(event) {
  try {
    const preloaded = await event.preloadResponse;
    const res = preloaded || await fetch(event.request);
    if ([502, 503, 504].includes(res.status)) return (await offlinePage()) || res;      // the server is down or still starting up
    return res;
  } catch (_) {
    return (await offlinePage()) || new Response("CIRQ can't be reached right now.", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function offlinePage() {
  return (await caches.open(STATIC_CACHE)).match(OFFLINE_URL);
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(timer); resolve(v); }, (e) => { clearTimeout(timer); reject(e); });
  });
}

async function networkFirst(req) {
  const cache = await caches.open(STATIC_CACHE);
  try {
    const res = await withTimeout(fetch(req), NETWORK_TIMEOUT_MS);
    if (res.status === 200) cache.put(req, res.clone());
    return res;
  } catch (error) {
    const saved = await cache.match(req, { ignoreSearch: true });
    if (saved) return saved;
    throw error;
  }
}

async function staleWhileRevalidate(event, req) {
  const cache = await caches.open(STATIC_CACHE);
  const saved = await cache.match(req);
  const refresh = fetch(req).then((res) => { if (res.status === 200) cache.put(req, res.clone()); return res; }).catch(() => null);
  event.waitUntil(refresh);
  return saved || (await refresh) || Response.error();
}
