const CACHE_VER    = 'cfp-v3';
const STATIC_CACHE = CACHE_VER + '-static';

const PRECACHE = [
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/offline',
];

// Install: cache only stable assets (icons, offline page)
// CSS and JS use network-first so deploys are never blocked by cache
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(STATIC_CACHE).then(c => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// Activate: evict ALL old cfp- caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k.startsWith('cfp-') && k !== STATIC_CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Push notifications
self.addEventListener('push', e => {
  let data = { title: '💊 Family Planner', body: 'You have a reminder', url: '/' };
  try { data = Object.assign(data, e.data.json()); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icons/icon-192.png',
      badge: '/static/icons/icon-192.png',
      data: { url: data.url },
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data?.url || '/';
  e.waitUntil(clients.matchAll({ type: 'window' }).then(wins => {
    const w = wins.find(w => w.url === url && 'focus' in w);
    return w ? w.focus() : clients.openWindow(url);
  }));
});

// Fetch strategy:
//   CSS + JS  → network-first (always get fresh on deploy, cache as fallback)
//   Icons     → cache-first   (stable, large)
//   Pages     → network-first, fall back to /offline
self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  if (url.origin !== location.origin) return;

  const path = url.pathname;

  // Icons: cache-first
  if (path.startsWith('/static/icons/') || path.startsWith('/static/images/')) {
    e.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;
        return fetch(request).then(res => {
          const clone = res.clone();
          caches.open(STATIC_CACHE).then(c => c.put(request, clone));
          return res;
        });
      })
    );
    return;
  }

  // CSS, JS, and all other static: network-first, cache as fallback
  if (path.startsWith('/static/')) {
    e.respondWith(
      fetch(request).then(res => {
        const clone = res.clone();
        caches.open(STATIC_CACHE).then(c => c.put(request, clone));
        return res;
      }).catch(() => caches.match(request))
    );
    return;
  }

  // Page navigation: network-first, fall back to offline page
  if (request.mode === 'navigate') {
    e.respondWith(
      fetch(request).catch(() => caches.match('/offline'))
    );
    return;
  }
});
