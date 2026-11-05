/* The dashboard's service worker. It exists for one thing: to say that a reply
 * is waiting for approval, on a phone with no page open.
 *
 * The pushes this server sends carry no payload at all — nothing about a
 * conversation passes through a third-party push service — so when one
 * arrives this worker asks the server how many replies are waiting. The
 * request carries the session cookie because it is same-origin; if the session
 * has expired the notification falls back to saying only that something is
 * waiting, which is still true and still gets him to the queue.
 *
 * Served by app/notify.py at /sw.js with Service-Worker-Allowed: /, so its
 * scope covers /dashboard. Register it from the page with:
 *   navigator.serviceWorker.register('/sw.js')
 */
const DASHBOARD = '/dashboard';
const HELD = '/dashboard?tab=held';
/* One tag, so a second nudge replaces the first instead of stacking: the count
 * in the newest one is the whole truth. */
const TAG = 'held-queue';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

/* How many replies are waiting, or null when the server would not say. */
async function pendingCount() {
  try {
    const response = await fetch('/api/status', { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) return null;
    const count = Number((await response.json()).held);
    return Number.isFinite(count) ? count : null;
  } catch (err) {
    return null;   /* offline, or the server is down: fall back to the vague line */
  }
}

self.addEventListener('push', (event) => {
  event.waitUntil((async () => {
    const count = await pendingCount();
    /* Nothing waiting any more — he cleared the queue between the send and the
     * delivery. Showing "a reply is waiting" would be a lie, and a notification
     * that turns out to be wrong is how a queue stops being trusted. */
    if (count === 0) return;
    let body = 'A reply is waiting for you.';
    if (count === 1) body = '1 reply is waiting for you.';
    if (count > 1) body = count + ' replies are waiting for you.';
    await self.registration.showNotification('Arianna', {
      body: body,
      tag: TAG,
      renotify: true,
      data: { url: HELD }
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of open) {
      if (new URL(client.url).pathname === DASHBOARD) {
        /* A hint for a page that wants to switch to the Held tab; focusing the
         * dashboard he already has open is the part that must work. */
        client.postMessage({ type: 'show-held' });
        return client.focus();
      }
    }
    const url = (event.notification.data && event.notification.data.url) || HELD;
    await self.clients.openWindow(url);
  })());
});

function applicationServerKey(base64url) {
  const padded = base64url.replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

/* A push service may replace a subscription on its own — and then every
 * notification stops arriving with nothing to see. Re-subscribe and tell the
 * server. */
self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    try {
      const settings = await fetch('/api/notify', { credentials: 'same-origin', cache: 'no-store' });
      if (!settings.ok) return;
      const key = (await settings.json()).push.public_key;
      if (!key) return;
      const subscription = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: applicationServerKey(key)
      });
      await fetch('/api/notify/subscribe', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subscription)
      });
    } catch (err) {
      /* Nothing useful to do from here and nowhere to report it: the page
       * subscribes again on its next visit, which is the real recovery. */
    }
  })());
});
