/* LifeOS Capture — minimal service worker.
 * Caches the app shell so the page opens offline. Network-first for HTML
 * (so updates are picked up), cache-first for static assets, and never
 * caches /api/ responses.
 */
const CACHE = 'lifeos-capture-v1';
const SHELL = [
    '/capture/',
    '/capture/index.html',
    '/capture/manifest.json',
    '/capture/icon.svg',
    'https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys()
            .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Never cache API calls — uploads + status need fresh round trips.
    if (url.pathname.startsWith('/api/')) return;

    // HTML: network-first, fall back to cache (so the app loads offline).
    if (event.request.mode === 'navigate' || event.request.destination === 'document') {
        event.respondWith(
            fetch(event.request)
                .then((res) => {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put(event.request, copy));
                    return res;
                })
                .catch(() => caches.match(event.request).then((r) => r || caches.match('/capture/')))
        );
        return;
    }

    // Static assets: cache-first.
    event.respondWith(
        caches.match(event.request).then((cached) => {
            if (cached) return cached;
            return fetch(event.request).then((res) => {
                if (res.ok && (url.origin === location.origin || url.host.endsWith('jsdelivr.net'))) {
                    const copy = res.clone();
                    caches.open(CACHE).then((c) => c.put(event.request, copy));
                }
                return res;
            });
        })
    );
});
