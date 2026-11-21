import { afterAll, test, spyOn } from 'bun:test';
import { useRealtimeSocket } from '../../../../harness/js/realtime-socket.ts';
useRealtimeSocket();
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
  gateway, workspaceFileUri, gatewayWorkspaceKey, installSessionApiFetchMock, unwrapSessionApiFetch,
  invalidateGatewayToken, workspaceContext } = await import(pathToFileURL(bundle).href);
const owner = workspaceId => ({ gatewayId: 'gateway-test', workspaceId });
const rootUri = id => `boxteam://gateway-test/workspace/${id}/resources/files/root`;
const uri = (id, name, root = rootUri(id)) => workspaceFileUri(owner(id), root + '/' + name, root);
const encoder = new TextEncoder();
const receipt = { boundary: '真实注册入口、Provider及共享WS内的领域SSE解码；仅路由解析和HTTP响应为显式资产', checks: [] };

async function withTransport(callback) {
  const connections = [], handles = [], provider = fileProvider(), events = [], errors = [];
  const routes = spyOn(gateway, 'resolveWorkspace').mockImplementation(async target => gatewayWorkspaceKey(target));
  handles.push(provider.onDidChangeFile(changes => events.push(changes.map(change => ({ uri: change.resource.toString(), type: change.type })))),
    provider.onDidWatchError(error => errors.push(error)));
  installSessionApiFetchMock(async ({ path, init }) => {
    expect(path).toBe('/api/v1/workspace/files/events');
    const connection = { request: JSON.parse(init.body),
      owner: new Headers(init.headers).get('X-BoxTeam-Workspace-Id'), cancelled: false };
    connections.push(connection);
    return new Response(new ReadableStream({
      start(controller) { connection.controller = controller; },
      cancel() { connection.cancelled = true; },
    }), { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': `watch-${connections.length}`, 'X-Workspace-File-Cursor': '0' } });
  });
  const watch = resource => { const handle = provider.watch(resource, { recursive: false, excludes: [] }); handles.push(handle); return handle; };
  let cursor = 0;
  const changes = (connection, items, overflow = false) => connection.controller.enqueue(encoder.encode(
    `event: changes\ndata: ${JSON.stringify({ event_id: ++cursor, overflow, changes: items,
      directories: [], snapshots: [], invalidated_roots: [] })}\n\n`));
  let failure;
  try {
    await callback({ connections, events, errors, watch, changes });
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
  await withTransport(async ({ connections, events, watch, changes }) => {
    const a = uri('A', 'a.txt'), b = uri('A', 'b.txt');
    const first = watch(a), second = watch(a); watch(b);
    await expect.poll(() => connections.length).toBe(1);
    const connection = connections[0];
    expect(connection.request.paths).toEqual([rootUri('A')]);
    changes(connection, [{ kind: 'delete', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBeGreaterThan(0);
    expect(events).toEqual([[{ uri: a.toString(), type: FileChangeType.DELETED }]]);
    first.dispose();
    changes(connection, [{ kind: 'edit', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(2);
    expect(events[1]).toEqual([{ uri: a.toString(), type: FileChangeType.UPDATED }]);
    second.dispose();
    changes(connection, [{ kind: 'edit', path: rootUri('A') + '/b.txt' }]);
    await expect.poll(() => events.length).toBe(3);
    expect(events[2]).toEqual([{ uri: b.toString(), type: FileChangeType.UPDATED }]);
    expect(connections).toHaveLength(1);
    receipt.checks.push('首次0伪更新；真实删除/编辑；同根单连接与引用释放');
  });
});

test('新增根不重建已有订阅，溢出保留失效且不污染其它owner', async () => {
  await withTransport(async ({ connections, events, watch, changes }) => {
    const a = uri('A', 'a.txt'), b = uri('B', 'a.txt'), nestedRoot = rootUri('A') + '/nested';
    const nested = uri('A', 'b.txt', nestedRoot);
    watch(a);
    await expect.poll(() => connections.length).toBe(1);
    changes(connections[0], [{ kind: 'edit', path: rootUri('A') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(1);
    watch(b);
    await expect.poll(() => connections.length).toBe(2);
    const independent = connections[1];
    changes(independent, [{ kind: 'delete', path: rootUri('B') + '/a.txt' }]);
    await expect.poll(() => events.length).toBe(2);
    expect(events[1]).toEqual([{ uri: b.toString(), type: FileChangeType.DELETED }]);
    watch(nested);
    await expect.poll(() => connections.length).toBe(3);
    expect(connections[0].cancelled).toBe(false); expect(independent.cancelled).toBe(false);
    const added = connections[2];
    expect(added.request.paths).toEqual([nestedRoot]);
    changes(added, [{ kind: 'delete', path: nestedRoot + '/b.txt' }]);
    await expect.poll(() => events.length).toBe(3);
    expect(events[2]).toEqual([{ uri: nested.toString(), type: FileChangeType.DELETED }]);
    changes(connections[0], [], true);
    await expect.poll(() => events.length).toBe(4);
    expect(events.at(-1)).toEqual([{ uri: a.toString(), type: FileChangeType.UPDATED }]);
    expect(connections).toHaveLength(3);
    receipt.checks.push('新增根不重建旧订阅；溢出保留失效；新根无伪更新；B订阅保持');
  });
});

/** 元数据和 raw 使用真实请求链；只有 HTTP 数据、错误及路由解析为具名装具。 */
async function withMetadata(callback) {
  const provider = fileProvider(), calls = [], state = { status: 200, body: 'old A', size: 5, mtime: 1 };
  const routes = spyOn(gateway, 'resolveWorkspace').mockImplementation(async target => gatewayWorkspaceKey(target));
  const hash = value => createHash('sha256').update(value).digest('hex');
  installSessionApiFetchMock(async ({ path, init, url, method }) => {
    const owner = new Headers(init.headers).get('X-BoxTeam-Workspace-Id');
    const body = method === 'POST' || method === 'PUT' ? JSON.parse(init.body) : undefined;
    calls.push({ path, method, owner, body });
    const requestId = 'metadata-' + calls.length;
    const response = (data, status = 200) => Response.json({ code: status === 200 ? 0 : status,
      message: status === 200 ? 'ok' : '明确的元数据拒绝', request_id: requestId, data },
    { status, headers: { 'X-Request-ID': requestId } });
    if (path === `/api/v1/workspaces/${owner}/resources/navigation`) {
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
    expect(calls.every(call => call.path === `/api/v1/workspaces/${call.owner}/resources/navigation` && call.body.operation === 'describe')).toBe(true);
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
    expect(calls.map(call => call.path)).toEqual(['/api/v1/workspace/files/raw', '/api/v1/workspaces/A/resources/navigation', '/api/v1/workspace/files/content']);
    expect(calls.at(-1).body.expected_revision).toBe(hash('old A'));
    expect(calls.at(-1).body.expected_revision).not.toBe(hash(state.body));
    receipt.checks.push('实际raw建立基线；更新后的describe零正文且不推进PUT expected_revision');
  });
});

for (const phase of ['response', 'body', 'eof']) {
  for (const change of ['gateway-roundtrip', 'credential', 'active-workspace', 'initial-gateway']) {
    test(`原文件${phase}等待期间${change}遵守身份且不接受迟到保存基线`, async () => {
      const previous = workspaceContext.getSnapshot(), provider = fileProvider(), resource = uri('A', 'same.txt');
      workspaceContext.publish({ ...previous, gatewayId: change === 'initial-gateway' ? null : 'gateway-test', activeId: 'A' });
      const entered = Promise.withResolvers(), release = Promise.withResolvers(), calls = [];
      const hash = value => createHash('sha256').update(value).digest('hex');
      let body = 'initial A', readingSignal;
      installSessionApiFetchMock(async ({ path, init }) => {
        calls.push(path);
        if (path !== '/api/v1/workspace/files/raw') throw new Error(`读取出现额外请求：${path}`);
        readingSignal = init.signal;
        if (body !== 'initial A') {
          const wait = async () => { entered.resolve(); await release.promise; };
          if (phase === 'response') await wait();
          let sent = false;
          // 原 Provider 已流式消费正文；分别阻塞响应头、首块和 EOF，不拦截废弃的 Blob 路径。
          return new Response(new ReadableStream({ async pull(controller) {
            if (!sent) {
              if (phase === 'body') await wait();
              sent = true;
              controller.enqueue(encoder.encode(body));
            } else {
              if (phase === 'eof') await wait();
              controller.close();
            }
          } }, { highWaterMark: 0 }));
        }
        return new Response(body);
      });
      try {
        expect(new TextDecoder().decode(await provider.readFile(resource))).toBe('initial A');
        expect(provider.revisionForBackup(resource)).toBe(hash('initial A'));
        body = 'late A';
        const pending = provider.readFile(resource).then(bytes => ({ bytes }), error => ({ error }));
        await entered.promise;
        if (change === 'credential') invalidateGatewayToken(0);
        else if (change === 'gateway-roundtrip') {
          workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway-other' });
          workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway-test' });
        } else workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway-test', activeId: 'B' });
        const aborted = readingSignal.aborted;
        release.resolve();
        const result = await pending;
        if (change === 'active-workspace' || change === 'initial-gateway') {
          expect(result.error).toBeUndefined();
          expect(new TextDecoder().decode(result.bytes)).toBe(body);
          expect(provider.revisionForBackup(resource)).toBe(hash(body));
          expect(aborted).toBe(false);
        } else {
          expect(result.error).toBeInstanceOf(Error);
          expect(result.error.message).toContain('Gateway');
          expect(result.bytes).toBeUndefined();
          expect(provider.revisionForBackup(resource)).toBe(hash('initial A'));
          expect(aborted).toBe(true);
        }
        expect(calls).toEqual(['/api/v1/workspace/files/raw', '/api/v1/workspace/files/raw']);
        if (phase === 'eof') {
          // 旧读取结清后，新代仍可读取；完成的信号不再保留身份订阅。
          expect(new TextDecoder().decode(await provider.readFile(resource))).toBe(body);
          expect(provider.revisionForBackup(resource)).toBe(hash(body));
          workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway-other' });
          workspaceContext.publish({ ...workspaceContext.getSnapshot(), gatewayId: 'gateway-test' });
          invalidateGatewayToken(0);
          expect(readingSignal.aborted).toBe(false);
        }
        receipt.checks.push({ phase, change, aborted, acceptedBytes: result.bytes?.byteLength ?? 0 });
      } finally { release.resolve(); unwrapSessionApiFetch(); workspaceContext.publish(previous); }
    });
  }
}

afterAll(() => writeFile(join(run.artifactsDir, 'watch-boundaries.json'), JSON.stringify(receipt, null, 2) + '\n'));
