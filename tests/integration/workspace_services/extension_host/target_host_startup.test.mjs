import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { once } from 'node:events';
import WebSocket, { WebSocketServer } from 'ws';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/target_host_startup.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'startup.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const paths = ['tests/integration/workspace_services/extension_host/target_host_startup.test.mjs',
  'src/clients/workbench/src/extensions/targetHost.ts', 'src/clients/workbench/src/extensions/targetUris.ts'];
const fingerprint = async () => Object.fromEntries(await Promise.all(paths.map(async path => [path,
  createHash('sha256').update(await readFile(path)).digest('hex')])));
const before = await fingerprint();
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { TargetExtensionHost } from './src/clients/workbench/src/extensions/targetHost.js';
export { PersistentProtocol } from '${upstream}base/parts/ipc/common/ipc.net.js';
export { Emitter, Event } from '${upstream}base/common/event.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
export { isCancellationError } from '${upstream}base/common/errors.js';
export { MessageType, createMessageOfType, isMessageOfType } from '${upstream}workbench/services/extensions/common/extensionHostProtocol.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [{ name: 'explicit-startup-service-boundaries', setup(builder) {
    builder.onLoad({ filter: /\/extensions\/targetHost\.ts$/ }, async args => ({
      contents: (await readFile(args.path, 'utf8')).replace("import { getGatewayToken } from '../../../web/src/api/http.js';",
        'const getGatewayToken = () => globalThis.targetHostStartupBoundary.token();'), loader: 'ts', resolveDir: dirname(args.path) }));
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({
      contents: 'export const gateway = {}; export class GatewayRequestError extends Error {}', loader: 'ts' }));
    builder.onLoad({ filter: /\/extensions\/configuration\.ts$/ }, () => ({
      contents: 'export const targetConfigurationServices = (...args) => globalThis.targetHostStartupBoundary.configure(...args);', loader: 'ts' }));
  } }, buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const checks = [];
afterAll(async () => {
  await run.close();
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({
    boundary: '真实 TargetExtensionHost、GatewaySocket、双端原 PersistentProtocol 和本机 HTTP/WebSocket；认证等待与配置服务为具名装具，服务端模拟宿主握手，不替代真实 Gateway 权限、Node 扩展执行或浏览器页面验收。',
    before, after: await fingerprint(), checks,
  }, null, 2));
});

const deferred = () => Promise.withResolvers();
const turn = () => new Promise(resolve => setTimeout(resolve, 0));

async function scenario(body, { binding = true, initialized = true } = {}) {
  const requestReceived = deferred(), initReceived = deferred(), closed = deferred();
  const connections = [], requests = [], initData = [], notifications = [];
  let configurationCalls = 0, snapshotCalls = 0, tokenCalls = 0;
  const boundary = globalThis.targetHostStartupBoundary = {
    token: async () => { tokenCalls++; return 'fixture-token'; },
    configure: async () => {
      configurationCalls++;
      return { fixtureConfiguration: true };
    },
  };
  const previousLocation = globalThis.location;
  const previousWebSocket = globalThis.WebSocket;
  globalThis.WebSocket = WebSocket;
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0, path: '/api/gateway/extension-hosts/channel' });
  server.on('connection', socket => {
    connections.push(socket); socket.data = {};
    socket.on('message', (message, binary) => {
        if (binary) {
          socket.data.input.fire(m.VSBuffer.wrap(new Uint8Array(message)));
          return;
        }
        const request = JSON.parse(message.toString()); requests.push(request); requestReceived.resolve(socket);
        if (!binding) return;
        const folders = request.workspace_ids.map((workspace_id, index) => ({ workspace_id, name: 'root-' + index,
          resource_uri: `boxteam://workspace/${workspace_id}/resources/files/root`, native_uri: `file:///target/root-${index}` }));
        socket.send(JSON.stringify({ ...request, client_id: 'authenticated-client', generation: 'generation-1', request_id: 'request-1',
          initialization: { os: 'linux', parent_pid: 123, logs_location_uri: 'file:///target/logs',
            environment: { app_root_uri: 'file:///target/app', profile_id: 'default',
              global_storage_home_uri: 'file:///target/global', workspace_storage_home_uri: 'file:///target/workspaces',
              remote_settings: { native_uri: 'file:///target/settings.json',
                resource_uri: 'boxteam://gateway/gateway_a/config/extensions/default/settings.json' } },
            workspace: { id: 'stable-view', name: 'Target view', folders } } }));
        const input = socket.data.input = new m.Emitter();
        const protocol = socket.data.protocol = new m.PersistentProtocol({ socket: { onData: input.event,
          onClose: m.Event.None, onEnd: m.Event.None, write: buffer => socket.send(buffer.buffer),
          end: () => socket.close(), drain: async () => {}, traceSocketEvent() {}, dispose() {} } });
        protocol.onMessage(message => {
          if (m.isMessageOfType(message, m.MessageType.Terminate)) return;
          const value = JSON.parse(message.toString()); initData.push(value); initReceived.resolve(socket);
          if (initialized) protocol.send(m.createMessageOfType(m.MessageType.Initialized));
        });
        protocol.send(m.createMessageOfType(m.MessageType.Ready));
    });
    socket.on('close', () => {
        socket.data.protocol?.dispose(); socket.data.input?.dispose(); closed.resolve();
    });
  });
  await once(server, 'listening');
  globalThis.location = { href: `http://127.0.0.1:${server.address().port}/` };
  const target = { kind: 'backend', gatewayId: 'gateway_a', backendId: '11111111-1111-4111-8111-111111111111' };
  const snapshot = { versionId: 1, extensions: [] };
  let readSnapshot = async () => snapshot;
  const host = new m.TargetExtensionHost({ installationTarget: target, viewId: 'default', workspaceIds: ['22222222-2222-4222-8222-222222222222'] },
    { getHostExtensions: () => [], filterByRunningLocation: () => [] }, () => { snapshotCalls++; return readSnapshot(); },
    {}, { commit: pinnedUpstreamCommit, version: '1.0.0', nameLong: 'Fixture', urlProtocol: 'fixture' }, { getLevel: () => 2 },
    {}, { defaultLogLevels: { extensions: [] } }, { error: error => notifications.push(error.message ?? String(error)) }, {});
  const start = () => {
    const promise = host.start();
    return { promise, outcome: promise.then(value => ({ value }), error => ({ error })) };
  };
  try {
    await body({ host, boundary, snapshot, setSnapshot: value => { readSnapshot = value; }, start,
      requestReceived: requestReceived.promise, initReceived: initReceived.promise, closed: closed.promise,
      connections, requests, initData, notifications,
      counts: () => ({ snapshotCalls, tokenCalls, configurationCalls }) });
  } finally {
    host.dispose();
    for (const socket of connections) socket.terminate();
    if (connections.length) await closed.promise;
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
    globalThis.location = previousLocation;
    globalThis.WebSocket = previousWebSocket;
    delete globalThis.targetHostStartupBoundary;
  }
}

for (const close of ['disconnect', 'dispose']) {
  for (const stage of ['snapshot', 'authentication']) test(`${close}取消${stage}等待，迟到结果不创建通道`, async () => scenario(async p => {
    const gate = deferred(), entered = deferred();
    if (stage === 'snapshot') p.setSnapshot(() => { entered.resolve(); return gate.promise; });
    else p.boundary.token = () => { entered.resolve(); return gate.promise; };
    const started = p.start(); expect(p.host.start()).toBe(started.promise);
    await entered.promise;
    await p.host[close]();
    expect(m.isCancellationError((await started.outcome).error)).toBe(true);
    gate.resolve(stage === 'snapshot' ? p.snapshot : 'late-shared-token'); await turn();
    expect(p.connections).toHaveLength(0); expect(p.notifications).toEqual([]);
    expect(p.host.customerInstantiationService).toBeUndefined();
    checks.push({ close, stage, connections: p.connections.length, counts: p.counts() });
  }));

  test(`${close}取消已发送的绑定等待并结清真实WebSocket`, async () => scenario(async p => {
    const started = p.start(); await Promise.race([p.requestReceived, started.promise]);
    await p.host[close]();
    expect(m.isCancellationError((await started.outcome).error)).toBe(true); await p.closed;
    expect(p.counts().configurationCalls).toBe(0); expect(p.requests).toHaveLength(1); expect(p.notifications).toEqual([]);
    checks.push({ close, stage: 'binding' });
  }, { binding: false }));

  test(`${close}取消配置初始化，迟到服务不能发布或发送原初始化`, async () => scenario(async p => {
    const gate = deferred(), entered = deferred();
    p.boundary.configure = (_parent, targetContext, lifetime) => {
      expect(targetContext.kind).toBe('node'); expect(targetContext.connection).toBe(p.host.binding);
      entered.resolve(lifetime); return gate.promise;
    };
    const started = p.start(), lifetime = await Promise.race([entered.promise, started.promise]);
    await p.host[close]();
    expect(m.isCancellationError((await started.outcome).error)).toBe(true);
    expect(lifetime.isDisposed).toBe(true);
    gate.resolve({ lateConfiguration: true }); await turn(); await p.closed;
    expect(p.host.customerInstantiationService).toBeUndefined(); expect(p.initData).toHaveLength(0); expect(p.notifications).toEqual([]);
    checks.push({ close, stage: 'configuration' });
  }));

  test(`${close}取消原Initialized等待，及时撤销监听与期限`, async () => scenario(async p => {
    const started = p.start(); await Promise.race([p.initReceived, started.promise]);
    await p.host[close]();
    expect(m.isCancellationError((await started.outcome).error)).toBe(true); await p.closed;
    expect(p.initData).toHaveLength(1); expect(p.notifications).toEqual([]);
    checks.push({ close, stage: 'initialized' });
  }, { initialized: false }));
}

test('绑定已到达但尚未续跑时关闭，不创建配置owner', async () => scenario(async p => {
  const started = p.start(); await Promise.race([p.requestReceived, started.promise]);
  p.host.socket.addEventListener('message', event => { if (typeof event.data === 'string') p.host.dispose(); });
  expect(m.isCancellationError((await started.outcome).error)).toBe(true); await p.closed;
  expect(p.counts().configurationCalls).toBe(0); expect(p.host.customerInstantiationService).toBeUndefined();
}));

test('原Initialized已到达但尚未续跑时关闭，不返回成功协议', async () => scenario(async p => {
  const started = p.start(); await Promise.race([p.initReceived, started.promise]);
  p.host.protocol.onMessage(message => { if (m.isMessageOfType(message, m.MessageType.Initialized)) p.host.dispose(); });
  expect(m.isCancellationError((await started.outcome).error)).toBe(true); await p.closed;
  expect(p.connections).toHaveLength(1); expect(p.notifications).toEqual([]);
}));

test('先关闭再start不读快照或认证，不能重新打开宿主', async () => scenario(async p => {
  p.host.dispose();
  expect(m.isCancellationError((await p.start().outcome).error)).toBe(true);
  expect(p.counts()).toEqual({ snapshotCalls: 0, tokenCalls: 0, configurationCalls: 0 }); expect(p.connections).toHaveLength(0);
}));

test('正常原协议初始化保留目标与快照，共享start且关闭不改写已成功结果', async () => scenario(async p => {
  const started = p.start(), result = await started.outcome;
  expect(result.error).toBeUndefined(); expect(result.value).toBeInstanceOf(m.PersistentProtocol);
  expect(p.initData[0].workspace.id).toBe('stable-view'); expect(p.initData[0].extensions.versionId).toBe(1);
  expect(p.host.customerInstantiationService).toEqual({ fixtureConfiguration: true });
  await p.host.disconnect(); await p.closed;
  expect(p.host.start()).toBe(started.promise); expect(p.connections).toHaveLength(1); expect(p.notifications).toEqual([]);
  checks.push({ stage: 'successful-original-protocol', connections: p.connections.length });
}));

test('认证失败仍拒绝并报告原错误，不能当作用户取消吞掉', async () => scenario(async p => {
  const failure = new Error('fixture-authentication-failure');
  p.boundary.token = async () => { throw failure; };
  const { error } = await p.start().outcome;
  expect(error).toBe(failure); expect(m.isCancellationError(error)).toBe(false);
  expect(p.notifications).toEqual([failure.message]); expect(p.connections).toHaveLength(0);
}));

test('原协议排空失败仍关闭真实WebSocket并保留同一异常', async () => scenario(async p => {
  await p.host.start();
  const failure = new Error('fixture-drain-failure');
  p.host.protocol.drain = async () => { throw failure; };
  await expect(p.host.disconnect()).rejects.toBe(failure);
  await p.closed;
  expect(p.notifications).toEqual([]);
  checks.push({ stage: 'drain-failure-closes-socket' });
}));
