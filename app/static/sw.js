// bare minimum so phones offer "add to home screen". everything still goes to the network,
// the app is useless offline anyway since stock lives on the server.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
