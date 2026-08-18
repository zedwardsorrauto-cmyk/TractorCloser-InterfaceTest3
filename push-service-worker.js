self.addEventListener('push', event => {
  const payload = event.data ? event.data.json() : {};
  const title = payload.title || 'TractorCloser';
  const options = {
    body: payload.body || 'You have a new TractorCloser update.',
    icon: 'tractorcloser-icon.svg',
    badge: 'tractorcloser-icon.svg',
    data: { destination: payload.destination || '#today' },
    tag: `tractorcloser-${payload.destination || 'update'}`,
    renotify: true,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const destination = event.notification.data?.destination || '#today';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windows => {
    const existing = windows[0];
    if (existing) return existing.focus().then(() => existing.navigate(`tractorcloser-interface.html${destination}`));
    return clients.openWindow(`tractorcloser-interface.html${destination}`);
  }));
});
