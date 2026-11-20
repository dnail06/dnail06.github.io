/* daiyi-pwa-v3 — network-first for HTML/JSON, cache fallback offline.
   Does not intercept non-GET or cross-origin requests (FormSubmit POSTs must pass through). */
const CACHE = 'daiyi-pwa-v3';
const PRECACHE = [
  '/',
  '/index.html',
  '/loan.html',
  '/closing-cost.html',
  '/sell.html',
  '/thanks.html',
  '/afford.html',
  '/realprice.html',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-512-maskable.png',
  '/icons/apple-touch-icon.png',
  '/icons/favicon-32.png',
  '/icons/favicon.ico',
  '/images/portrait-cutout.webp',
  '/images/portrait.jpg',
  '/images/signature.webp',
  '/images/line-qr.jpg'
];

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function (err) {
          console.warn('[sw] precache skip', url, err);
        });
      }));
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) {
        return k !== CACHE;
      }).map(function (k) {
        return caches.delete(k);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (event) {
  const req = event.request;
  if (req.method !== 'GET') return;

  let url;
  try {
    url = new URL(req.url);
  } catch (e) {
    return;
  }
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/sw.js') return;

  const accept = req.headers.get('accept') || '';
  const isHTML = req.mode === 'navigate' || accept.includes('text/html') ||
    url.pathname === '/' || url.pathname.endsWith('.html');
  const isJSON = url.pathname.endsWith('.json');

  if (isHTML || isJSON) {
    event.respondWith(
      fetch(req).then(function (res) {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(function (cache) {
            cache.put(req, copy);
          });
        }
        return res;
      }).catch(function () {
        return caches.match(req).then(function (cached) {
          if (cached) return cached;
          if (isHTML) return caches.match('/index.html');
          return undefined;
        });
      })
    );
    return;
  }

  event.respondWith(
    caches.match(req).then(function (cached) {
      if (cached) return cached;
      return fetch(req).then(function (res) {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then(function (cache) {
            cache.put(req, copy);
          });
        }
        return res;
      });
    })
  );
});
