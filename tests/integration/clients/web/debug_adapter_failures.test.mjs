import { afterEach, expect, spyOn, test } from 'bun:test';
import { PluginDebugAdapter } from '../../../../src/clients/workbench/src/debug/adapter.ts';
import { gateway } from '../../../../src/clients/workbench/src/agentApi.ts';
import * as http from '../../../../src/clients/web/src/api/http.ts';
import { errorDiagnostic } from '../../../../src/clients/web/src/utils/errorMessage.ts';

// 真实 Adapter／错误格式化，仅启动回执、凭据和 WebSocket 事件为显式服务边界。
const originalSocket = Object.getOwnPropertyDescriptor(globalThis, 'WebSocket');
const originalLocation = Object.getOwnPropertyDescriptor(globalThis, 'location');
const release = [];
afterEach(() => {
  release.splice(0).reverse().forEach(dispose => dispose());
  for (const [name, descriptor] of [['WebSocket', originalSocket], ['location', originalLocation]]) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

function fixture() {
  const created = Promise.withResolvers();
  class Socket extends EventTarget {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = 0;
    constructor(url) { super(); this.url = String(url); created.resolve(this); }
    open() { this.readyState = 1; this.dispatchEvent(new Event('open')); }
    close(code = 1000, reason = '') {
      this.readyState = 3;
      this.dispatchEvent(Object.assign(new Event('close'), { code, reason }));
    }
  }
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: Socket });
  Object.defineProperty(globalThis, 'location', { configurable: true, value: new URL('http://127.0.0.1:8027/') });
  const owner = { gatewayId: 'gateway-B', workspaceId: 'workspace-A', workspaceKey: 'owner-key', sessionId: 'session-A', threadId: 'thread-A' };
  const binding = { registration_id: 'debugger-A', gateway_id: owner.gatewayId, workspace_id: owner.workspaceId };
  const start = { debugger_registration_id: binding.registration_id, session_id: owner.sessionId, thread_id: owner.threadId };
  const request = spyOn(gateway, 'request').mockResolvedValue({ ...start, run_id: 'run-A', debugger_binding: binding });
  const token = spyOn(http, 'getGatewayToken').mockResolvedValue('private-token');
  const invalidation = spyOn(http, 'onGatewayTokenInvalidated').mockReturnValue(() => {});
  release.push(() => request.mockRestore(), () => token.mockRestore(), () => invalidation.mockRestore());
  const adapter = new PluginDebugAdapter({ configuration: { pluginDebuggerRegistration: binding.registration_id,
    pluginDebug: { owner, debugger: binding, start, rootUris: [] } } });
  release.push(() => adapter.dispose());
  return { adapter, created: created.promise, request, token };
}

test('握手失败保留精确目标，不捏造 HTTP 状态／request_id，不泄漏 token 或追加探测', async () => {
  const f = fixture();
  const failed = f.adapter.startSession().then(() => { throw new Error('应当拒绝握手'); }, error => error);
  const socket = await f.created;
  socket.dispatchEvent(new Event('error'));
  const error = await failed;
  expect(error.context).toEqual({ source: { component: 'workbench' }, stage: 'websocket_connect', target: {
    gateway_id: 'gateway-B', workspace_id: 'workspace-A', session_id: 'session-A', thread_id: 'thread-A', debugger_registration_id: 'debugger-A',
  } });
  expect(error.message).not.toContain('websocket_connect');
  expect(errorDiagnostic(error)).toContain('stage: websocket_connect');
  expect(errorDiagnostic(error)).toContain('target.gateway_id: gateway-B');
  expect(error.message).not.toContain('private-token');
  expect(error.requestId).toBeUndefined();
  expect(error.status).toBeUndefined();
  expect(f.request).toHaveBeenCalledTimes(1);
  expect(f.token).toHaveBeenCalledTimes(1);
});

test('已连接的关闭事件保留退出信号、关闭原因和目标归属', async () => {
  const f = fixture(), errors = [], exits = [];
  f.adapter.onError(error => errors.push(error));
  f.adapter.onExit(code => exits.push(code));
  const started = f.adapter.startSession(), socket = await f.created;
  socket.open();
  await started;
  socket.close(1006, 'upstream closed');
  expect(errors).toHaveLength(1);
  expect(errors[0].context.stage).toBe('websocket_closed');
  expect(errors[0].context.target.gateway_id).toBe('gateway-B');
  expect(errors[0].message).toContain('upstream closed');
  expect(errors[0].message).not.toContain('private-token');
  expect(exits).toEqual([null]);
});
