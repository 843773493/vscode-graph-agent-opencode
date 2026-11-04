import { afterAll, test, spyOn } from 'bun:test';
import { expect } from 'playwright/test';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/file_watch.test.mjs').prepare();
const bundle = join(run.artifactsDir, 'file_watch.fixture.mjs'), root = resolve('src/clients/workbench');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/file_watch.fixture.ts')], outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', logLevel: 'silent',
  tsconfig: join(root, 'tsconfig.jsonc'), plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))] });
const { fileProvider, FileChangeType, FileSystemProviderErrorCode, toFileSystemProviderErrorCode,
  gateway, workspaceFileUri, installSessionApiFetchMock, unwrapSessionApiFetch } = await import(pathToFileURL(bundle).href);
const owner = workspaceId => ({ gatewayId: 'gateway-test', workspaceId });
const rootUri = id => `boxteam://workspace/${id}/resources/files/root`;
const uri = (id, name, root = rootUri(id)) => workspaceFileUri(owner(id), root + '/' + name, root);
const encoder = new TextEncoder();
const receipt = { boundary: '真实注册入口、Provider及观察者SSE解码；仅路由解析和HTTP响应为显式资产', checks: [] };

async function withTransport(callback) {
  const connections = [], handles = [], provider = fileProvider(), events = [], errors = [];
  const routes = spyOn(gateway, 'resolveWorkspace').mockImplementation(async target => target.workspaceId);
  handles.push(provider.onDidChangeFile(changes => events.push(changes.map(change => ({ uri: change.resource.toString(), type: change.type })))),
    provider.onDidWatchError(error => errors.push(error)));
  installSessionApiFetchMock(async ({ path, init }) => {
    expect(path).toBe('/api/v1/workspace/observations/stream');
    const connection = { subscriptions: JSON.parse(init.body).subscriptions,
      owner: new Headers(init.headers).get('X-BoxTeam-Workspace-Id'), cancelled: false };
    connections.push(connection);
    return new Response(new ReadableStream({
      start(controller) { connection.controller = controller; },
      cancel() { connection.cancelled = true; },
    }), { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': `watch-${connections.length}` } });
  });
  const watch = resource => { const handle = provider.watch(resource, { recursive: false, excludes: [] }); handles.push(handle); return handle; };
  const emit = (connection, subscription, value) => connection.controller.enqueue(encoder.encode(
    `event: observation\ndata: ${JSON.stringify({ subscription_id: subscription.subscription_id, ...value })}\n\n`));
  const connect = connection => { for (const subscription of connection.subscriptions) emit(connection, subscription, { state: 'connected', cursor: '0' }); };
  let cursor = 0;
  const changes = (connection, subscription, items, overflow = false) => emit(connection, subscription, {
    state: 'frame', frame: `event: changes\ndata: ${JSON.stringify({ event_id: ++cursor, overflow, changes: items,
      directories: [], snapshots: [], invalidated_roots: [] })}\n\n`,
  });
  let failure;
  try {
    await callback({ connections, events, errors, watch, connect, changes });
    expect(errors).toEqual([]);
  } catch (error) { failure = error;
  } finally {
    try {
      for (const handle of handles.reverse()) handle.dispose();
      await expect.poll(() => connections.every(item => item.cancelled)).toBe(true);
    } catch (error) { failure = failure ? new AggregateError([failure, error], '监听验收与清理均失败') : error;
    } finally { unwrapSessionApiFetch(); routes.mockRestore(); }
  }
  if (failure) throw failure;
}

test('初次连接不报更新，真实删除／编辑仍转发，同根监听按引用共享', async () => {
  await withTransport(async ({ connections, events, watch, connect, changes }) => {
    const a = uri('A', 'a.txt'), b = uri('A', 'b.txt');
    const first = watch(a), second = watch(a); watch(b);
    await expect.poll(() => connections.length).toBe(1);
    const connection = connections[0], subscription = connection.subscriptions[0];
    expect(connection.subscriptions).toHaveLength(1);
    connect(connection);
    changes(connection, subscription, [{ kind: 'delete', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBeGreaterThan(0);
    expect(events).toEqual([[{ uri: a.toString(), type: FileChangeType.DELETED }]]);
    first.dispose();
    changes(connection, subscription, [{ kind: 'edit', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(2);
    expect(events[1]).toEqual([{ uri: a.toString(), type: FileChangeType.UPDATED }]);
    second.dispose();
    changes(connection, subscription, [{ kind: 'edit', path: rootUri('A') + '/b.txt' }]);
    await expect.poll(() => events.length).toBe(3);
    expect(events[2]).toEqual([{ uri: b.toString(), type: FileChangeType.UPDATED }]);
    expect(connections).toHaveLength(1);
    receipt.checks.push('首次0伪更新；真实删除/编辑；同根单连接与引用释放');
  });
});

test('共享流重建只使已连接根重新核实，溢出保留失效且不污染其它owner', async () => {
  await withTransport(async ({ connections, events, watch, connect, changes }) => {
    const a = uri('A', 'a.txt'), b = uri('B', 'a.txt'), nestedRoot = rootUri('A') + '/nested';
    const nested = uri('A', 'b.txt', nestedRoot);
    watch(a);
    await expect.poll(() => connections.length).toBe(1);
    connect(connections[0]);
    changes(connections[0], connections[0].subscriptions[0], [{ kind: 'edit', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(1);
    watch(b);
    await expect.poll(() => connections.length).toBe(2);
    const independent = connections[1]; connect(independent);
    changes(independent, independent.subscriptions[0], [{ kind: 'delete', path: rootUri('B') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(2);
    expect(events[1]).toEqual([{ uri: b.toString(), type: FileChangeType.DELETED }]);
    watch(nested);
    await expect.poll(() => connections.length).toBe(3);
    expect(connections[0].cancelled).toBe(true); expect(independent.cancelled).toBe(false);
    const rebuilt = connections[2]; connect(rebuilt);
    const child = rebuilt.subscriptions.find(item => item.paths[0] === nestedRoot);
    changes(rebuilt, child, [{ kind: 'delete', path: nestedRoot + '/b.txt' }]);
    await expect.poll(() => events.length).toBe(4);
    expect(events.slice(2)).toEqual([[{ uri: a.toString(), type: FileChangeType.UPDATED }],
      [{ uri: nested.toString(), type: FileChangeType.DELETED }]]);
    changes(rebuilt, rebuilt.subscriptions.find(item => item.paths[0] === rootUri('A')), [], true);
    await expect.poll(() => events.length).toBe(5);
    expect(events.at(-1)).toEqual([{ uri: a.toString(), type: FileChangeType.UPDATED }]);
    expect(connections).toHaveLength(3);
    receipt.checks.push('共享流重建/溢出保留失效；新根无伪更新；B独立连接保持');
  });
});

/** 元数据和 raw 使用真实请求链；只有 HTTP 数据、错误及路由解析为具名装具。 */
async function withMetadata(callback) {
  const provider = fileProvider(), calls = [], state = { status: 200, body: 'old A', size: 5, mtime: 1 };
  const routes = spyOn(gateway, 'resolveWorkspace').mockImplementation(async target => target.workspaceId);
  const hash = value => createHash('sha256').update(value).digest('hex');
  installSessionApiFetchMock(async ({ path, init, url, method }) => {
    const owner = new Headers(init.headers).get('X-BoxTeam-Workspace-Id');
    const body = method === 'POST' || method === 'PUT' ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, owner, body });
    const requestId = 'metadata-' + calls.length;
    const response = (data, status = 200) => Response.json({ code: status === 200 ? 0 : status,
      message: status === 200 ? 'ok' : '明确的元数据拒绝', request_id: requestId, data },
    { status, headers: { 'X-Request-ID': requestId } });
    if (path === '/api/v1/resources/navigation') {
      expect(body.operation).toBe('describe');
      expect(body.uri).toBe(rootUri(owner) + '/same.txt');
      if (state.status !== 200) return response(null, state.status);
      return response({ node: { uri: body.uri, resource_shape: 'file', operations: ['read'],
        stat: { size: state.size, modified_at_ms: state.mtime, changed_at_ms: state.mtime } } });
    }
    if (path === '/api/v1/workspace/files/raw') return new Response(state.body);
    if (path === '/api/v1/workspace/files/content' && method === 'PUT') {
      return response({ path: new URL(url, 'http://127.0.0.1').searchParams.get('path'), content: body.content, revision: hash(body.content) });
    }
    throw new Error(`元数据装具收到未声明请求: ${method} ${path}`);
  });
  try { await callback({ provider, state, calls, hash }); }
  finally { unwrapSessionApiFetch(); routes.mockRestore(); }
}

test('stat 保持轻量实时描述，not-found／权限错误恢复后按原身份重新读取', async () => {
  await withMetadata(async ({ provider, state, calls }) => {
    const a = uri('A', 'same.txt'), b = uri('B', 'same.txt');
    expect((await provider.stat(a)).size).toBe(5);
    state.size = 17; state.mtime = 2;
    expect((await provider.stat(a)).size).toBe(17);
    for (const [status, code] of [[404, FileSystemProviderErrorCode.FileNotFound], [403, FileSystemProviderErrorCode.NoPermissions]]) {
      state.status = status;
      let rejection;
      try { await provider.stat(a); } catch (error) { rejection = error; }
      expect(rejection).toBeDefined();
      expect(toFileSystemProviderErrorCode(rejection)).toBe(code);
      state.status = 200;
      expect((await provider.stat(a)).mtime).toBe(2);
    }
    expect((await provider.stat(b)).size).toBe(17);
    expect(calls.map(call => call.owner)).toEqual(['A', 'A', 'A', 'A', 'A', 'A', 'B']);
    expect(calls.every(call => call.path === '/api/v1/resources/navigation' && call.body.operation === 'describe')).toBe(true);
    receipt.checks.push('stat无缓存且只describe；404/403恢复；A/B按完整URI隔离');
  });
});

test('外部更新后的 stat 不读取正文或推进原保存 revision', async () => {
  await withMetadata(async ({ provider, state, calls, hash }) => {
    const a = uri('A', 'same.txt');
    expect(new TextDecoder().decode(await provider.readFile(a))).toBe('old A');
    state.body = 'new authoritative A'; state.size = state.body.length; state.mtime = 3;
    expect((await provider.stat(a)).mtime).toBe(3);
    await provider.writeFile(a, encoder.encode('user unsaved A'), { create: true, overwrite: true });
    expect(calls.map(call => call.path)).toEqual(['/api/v1/workspace/files/raw', '/api/v1/resources/navigation', '/api/v1/workspace/files/content']);
    expect(calls.at(-1).body.expected_revision).toBe(hash('old A'));
    expect(calls.at(-1).body.expected_revision).not.toBe(hash(state.body));
    receipt.checks.push('实际raw建立基线；更新后的describe零正文且不推进PUT expected_revision');
  });
});

afterAll(() => writeFile(join(run.artifactsDir, 'watch-boundaries.json'), JSON.stringify(receipt, null, 2) + '\n'));
