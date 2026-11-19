import { afterEach, expect, test } from 'bun:test';
import { RealtimeTestSocket, useRealtimeSocket } from '../../../../../../tests/harness/js/realtime-socket';
import { consumeRealtimeSubscription } from './realtime';
import { HttpRequestError } from '../http';
import { defineSseEvent } from '../../sse/sseClient';
import { gatewayWorkspaceKey } from '../workspaceIdentity';
import { observeTerminalResources } from '../workspaceResources';
import { openRealtimeResponse } from '../../../../../shared/realtime.js';

useRealtimeSocket();
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
async function until(check: () => boolean): Promise<void> {
  for (let i = 0; i < 100; i++) { if (check()) return; await new Promise(resolve => setTimeout(resolve, 1)); }
  throw new Error('测试条件未收敛');
}
function backend(handler: (url: URL, init?: RequestInit) => Response | Promise<Response>): void {
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    return url.pathname.endsWith('/local-credential') ? Response.json({ data: { token: 'token' } }) : handler(url, init);
  }, { preconnect: originalFetch.preconnect });
}

test('已取消的订阅不创建物理连接，也不占用后续订阅的认证身份', async () => {
  const cancelled = new AbortController();
  cancelled.abort(new DOMException('视图已关闭', 'AbortError'));
  const url = 'ws://127.0.0.1:49958/api/gateway/realtime';
  await expect(openRealtimeResponse(url, 'old-token', { path: '/cancelled' }, cancelled.signal)).rejects.toThrow('视图已关闭');
  await Promise.resolve();
  expect(RealtimeTestSocket.sockets).toHaveLength(0);
  backend(() => new Response(new ReadableStream()));
  const response = await openRealtimeResponse(url, 'new-token', { path: '/active' });
  expect(RealtimeTestSocket.sockets).toHaveLength(1);
  expect(RealtimeTestSocket.sockets[0]!.commands.find(command => command.type === 'auth')!.token).toBe('new-token');
  await response.body!.cancel();
});

test('跨工作区动态订阅只建一个 WS，独立取消、最后关闭并回收 reader', async () => {
  const streams = new Map<string, ReadableStreamDefaultController<Uint8Array>>(), closed: string[] = [];
  backend(url => new Response(new ReadableStream<Uint8Array>({ start(c) { streams.set(url.pathname, c); }, cancel() { closed.push(url.pathname); } }), { headers: { "Content-Type": "text/event-stream" } }));
  const a = new AbortController(), b = new AbortController();
  const values: string[] = [];
  const options = (signal: AbortSignal) => ({ signal, events: { changed: defineSseEvent(data => data, value => values.push(value)) } });
  const first = consumeRealtimeSubscription(49951, () => ({ path: '/first', headers: { 'X-BoxTeam-Workspace-ID': 'one' } }), options(a.signal));
  const second = consumeRealtimeSubscription(49951, () => ({ path: '/second', headers: { 'X-BoxTeam-Workspace-ID': 'two' } }), options(b.signal));
  await until(() => streams.size === 2);
  const socket = RealtimeTestSocket.sockets[0]!;
  expect(RealtimeTestSocket.sockets).toHaveLength(1);
  expect(socket.commands.filter(c => c.type === 'subscribe').map(c => c.headers)).toEqual([
    { 'X-BoxTeam-Workspace-ID': 'one' }, { 'X-BoxTeam-Workspace-ID': 'two' },
  ]);
  a.abort(); await first;
  await until(() => closed.includes('/first'));
  expect(socket.readyState).toBe(1);
  socket.bytes(Number(socket.commands.find(c => c.type === 'subscribe')!.subscription_id), new TextEncoder().encode('event: changed\ndata: late\n\n'));
  streams.get('/second')!.enqueue(new TextEncoder().encode('event: changed\ndata: alive\n\n'));
  await until(() => values.length === 1);
  expect(values).toEqual(['alive']);
  b.abort(); await second;
  expect(socket.readyState).toBe(3);
  await until(() => closed.length === 2);
});

test('HTTP 410 保留 request_id 与 owner detail，协议错误只结束本订阅', async () => {
  let requests = 0;
  backend(() => Response.json({ detail: { code: 'cursor_gone' } }, { status: 410, headers: { 'X-Request-ID': 'request-test' } }));
  await expect(consumeRealtimeSubscription(49952, () => ({ path: ++requests === 1 ? '/gone' : '/different-cursor' }), { events: {} }))
    .rejects.toMatchObject({ status: 410, path: '/gone', requestId: 'request-test', detail: { code: 'cursor_gone' } } satisfies Partial<HttpRequestError>);
  expect(requests).toBe(1);
});

test('重连重新读取已消费游标，不重放历史且仍只共享一个连接', async () => {
  let after = 0, requests = 0;
  backend(() => { requests++; return new Response(new ReadableStream<Uint8Array>({ start(c) {
    c.enqueue(new TextEncoder().encode(`id: ${requests}\nevent: changed\ndata: ${requests}\n\n`));
  } }), { headers: { "Content-Type": "text/event-stream" } }); });
  const controller = new AbortController();
  const pending = consumeRealtimeSubscription(49953, () => ({ path: `/events?after=${after}` }), {
    signal: controller.signal, events: { changed: defineSseEvent(Number, value => { after = value; }) },
  });
  await until(() => after === 1);
  RealtimeTestSocket.sockets[0]!.close();
  await new Promise(resolve => setTimeout(resolve, 1050));
  await until(() => after === 2);
  expect(RealtimeTestSocket.sockets[1]!.commands.find(c => c.type === 'subscribe')!.path).toBe('/events?after=1');
  controller.abort(); await pending;
});

test('终端迟到视图共享一个逻辑订阅，增减视图不重建源', async () => {
  let requests = 0, cancelled = 0;
  backend(() => { requests++; return new Response(new ReadableStream<Uint8Array>({ start(c) {
    c.enqueue(new TextEncoder().encode('event: resources_changed\ndata: {"resource_ids":null}\n\n'));
  }, cancel() { cancelled++; } }), { headers: { "Content-Type": "text/event-stream" } }); });
  const a = new AbortController(), b = new AbortController();
  const seen: unknown[] = [];
  const first = observeTerminalResources(49954, gatewayWorkspaceKey({ gatewayId: 'gateway-test', workspaceId: '00000000-0000-4000-8000-000000000001' }), value => seen.push(value), a.signal);
  await until(() => seen.length === 1);
  const second = observeTerminalResources(49954, gatewayWorkspaceKey({ gatewayId: 'gateway-test', workspaceId: '00000000-0000-4000-8000-000000000001' }), value => seen.push(value), b.signal);
  expect(seen).toEqual([null, null]); expect(requests).toBe(1);
  a.abort(); await first; expect(cancelled).toBe(0);
  b.abort(); await second; await until(() => cancelled === 1);
});

test('其它订阅持续心跳不会掩盖一个 source 的空闲超时', async () => {
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let active = 0;
  backend(url => new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      if (url.pathname === '/healthy') heartbeat = setInterval(() => {
        active++;
        controller.enqueue(new TextEncoder().encode(': heartbeat\n\n'));
      }, 5);
    }, cancel() { if (url.pathname === '/healthy') clearInterval(heartbeat); },
  }), { headers: { 'Content-Type': 'text/event-stream' } }));
  const controller = new AbortController();
  const healthy = consumeRealtimeSubscription(49955, () => ({ path: '/healthy' }), {
    signal: controller.signal, idleTimeoutMs: 100, events: {},
  });
  try {
    await expect(consumeRealtimeSubscription(49955, () => ({ path: '/silent' }), {
      idleTimeoutMs: 35, events: {}, idleTimeoutError: () => new Error('silent-source-idle'),
    })).rejects.toThrow('silent-source-idle');
    expect(active).toBeGreaterThan(1);
    expect(RealtimeTestSocket.sockets).toHaveLength(1);
    expect(RealtimeTestSocket.sockets[0]!.readyState).toBe(1);
  } finally { controller.abort(); await healthy; clearInterval(heartbeat); }
});

test('首帧认证拒绝沿唯一凭据缓存刷新一次，不在 URL 暴露 token', async () => {
  let credentials = 0;
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    const url = new URL(String(input));
    return url.pathname.endsWith('/local-credential')
      ? Response.json({ data: { token: `credential-${++credentials}` } })
      : new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } });
  }, { preconnect: originalFetch.preconnect });
  const socketClass = globalThis.WebSocket;
  class AuthSocket extends RealtimeTestSocket {
    override send(raw: string): void {
      const command = JSON.parse(raw);
      if (command.type === 'auth' && command.token === 'credential-1') {
        this.commands.push(command);
        this.control({ type: 'error', status: 401, message: '401: invalid local token', request_id: 'auth-rejected' });
      } else super.send(raw);
    }
  }
  globalThis.WebSocket = AuthSocket as unknown as typeof WebSocket;
  const controller = new AbortController();
  try {
    await consumeRealtimeSubscription(49956, () => ({ path: '/events' }), {
      signal: controller.signal, events: {}, onConnected: () => controller.abort(),
    });
    expect(credentials).toBe(2);
    expect(RealtimeTestSocket.sockets).toHaveLength(2);
    expect(RealtimeTestSocket.sockets.map(socket => new URL(socket.url).search)).toEqual(['', '']);
    expect(RealtimeTestSocket.sockets[1]!.commands.find(command => command.type === 'auth')!.token).toBe('credential-2');
  } finally { controller.abort(); globalThis.WebSocket = socketClass; }
});

test('同一 WS 中各订阅保留自己的响应头期限', async () => {
  backend(async () => {
    await new Promise(resolve => setTimeout(resolve, 25));
    return new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } });
  });
  const url = 'ws://127.0.0.1:49957/api/gateway/realtime';
  const first = openRealtimeResponse(url, 'token', { path: '/short' }, undefined, 10);
  const second = openRealtimeResponse(url, 'token', { path: '/long' }, undefined, 100);
  await expect(first).rejects.toThrow('/short');
  const response = await second;
  expect(response.status).toBe(200);
  expect(RealtimeTestSocket.sockets).toHaveLength(1);
  await response.body!.cancel();
});

test('六个订阅同时认证失败只刷新一次，不互相撤销新凭据连接', async () => {
  let credentials = 0, connected = 0;
  const ready = Promise.withResolvers<void>();
  globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
    return new URL(String(input)).pathname.endsWith('/local-credential')
      ? Response.json({ data: { token: `credential-${++credentials}` } })
      : new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } });
  }, { preconnect: originalFetch.preconnect });
  const originalSocket = globalThis.WebSocket;
  class AuthSocket extends RealtimeTestSocket {
    override send(raw: string): void {
      const command = JSON.parse(raw);
      if (command.type === 'auth' && command.token === 'credential-1') {
        this.commands.push(command);
        this.control({ type: 'error', status: 401, message: '401: invalid local token', request_id: 'shared-auth-rejected' });
      } else super.send(raw);
    }
  }
  globalThis.WebSocket = AuthSocket as unknown as typeof WebSocket;
  const controllers = Array.from({ length: 6 }, () => new AbortController());
  const streams = controllers.map((controller, index) => consumeRealtimeSubscription(49959, () => ({ path: `/events/${index}` }), {
    signal: controller.signal, events: {}, onConnected: () => { if (++connected === 6) ready.resolve(); },
  }).catch(error => { ready.reject(error); throw error; }));
  const finished = Promise.all(streams);
  try {
    await ready.promise;
    expect(credentials).toBe(2);
    expect(RealtimeTestSocket.sockets).toHaveLength(2);
    expect(RealtimeTestSocket.sockets.filter(socket => socket.readyState === 1)).toHaveLength(1);
  } finally {
    for (const controller of controllers) controller.abort();
    try { await finished; } finally { globalThis.WebSocket = originalSocket; }
  }
});
