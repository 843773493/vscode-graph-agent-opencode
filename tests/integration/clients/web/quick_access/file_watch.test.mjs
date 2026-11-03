import { test, spyOn } from 'bun:test';
import { expect } from 'playwright/test';
import { writeFile } from 'node:fs/promises';
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
const { fileProvider, FileChangeType, gateway, workspaceFileUri, installSessionApiFetchMock, unwrapSessionApiFetch } = await import(pathToFileURL(bundle).href);
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

await writeFile(join(run.artifactsDir, 'watch-boundaries.json'), JSON.stringify(receipt, null, 2) + '\n');
