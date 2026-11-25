// Minimal service worker (only registers over https). Network-first; the app needs the laptop anyway.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('fetch', () => {});
