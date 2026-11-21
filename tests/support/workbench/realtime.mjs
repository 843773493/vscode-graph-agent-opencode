/** 直接观察产品 WS 的真实控制帧，不把订阅误计为 HTTP 连接。 */
export function observeRealtime(page, { subscribe = () => {}, headers = () => {}, data = () => {}, end = () => {} } = {}) {
  const active = new Map();
  const listen = socket => {
    if (new URL(socket.url()).pathname !== '/api/gateway/realtime') return;
    const subscriptions = new Map();
    socket.on('framesent', ({ payload }) => {
      if (typeof payload !== 'string') return;
      const command = JSON.parse(payload);
      if (command.type === 'subscribe') {
        const request = { ...command, transport: 'websocket', url: new URL(command.path, socket.url().replace(/^ws/, 'http')).href,
          headers: Object.fromEntries(Object.entries(command.headers ?? {}).map(([key, value]) => [key.toLowerCase(), value])) };
        subscriptions.set(command.subscription_id, request); active.set(request, socket); subscribe(request);
      } else if (command.type === 'unsubscribe') {
        const request = subscriptions.get(command.subscription_id);
        if (request) { active.delete(request); end(request); subscriptions.delete(command.subscription_id); }
      }
    });
    socket.on('framereceived', ({ payload }) => {
      if (typeof payload !== 'string') {
        const bytes = Buffer.from(payload);
        const request = subscriptions.get(bytes.readUInt32BE(0));
        if (request) data(request, bytes.subarray(4));
        return;
      }
      const frame = JSON.parse(payload), request = subscriptions.get(frame.subscription_id);
      if (!request) return;
      if (frame.type === 'headers') headers(request, frame);
      if (frame.type === 'end' || frame.type === 'error') { active.delete(request); subscriptions.delete(frame.subscription_id); end(request, frame); }
    });
    socket.on('close', () => { for (const request of subscriptions.values()) { active.delete(request); end(request, { type: 'close' }); } });
  };
  page.on('websocket', listen);
  return { active, dispose() { page.off('websocket', listen); } };
}
