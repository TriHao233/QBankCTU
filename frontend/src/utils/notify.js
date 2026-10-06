const listeners = new Set();
let nextId = 1;

export function notify(message, tone = 'error') {
  const item = { id: nextId++, message: String(message ?? ''), tone };
  if (listeners.size === 0) {
    window.alert(item.message);
    return;
  }
  listeners.forEach((listener) => listener(item));
}

export function subscribeNotifications(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
