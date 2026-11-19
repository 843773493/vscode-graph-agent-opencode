import { afterAll, expect, spyOn, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/item_documents.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'item-documents.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
import './tests/harness/js/workbench-locale.ts';
export { ItemDocuments, contextContentUri, itemContentUri } from './src/clients/workbench/src/itemDocuments.js';
export { gateway } from './src/clients/workbench/src/agentApi.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { Emitter } from '${upstream}base/common/event.js';
export { consumeStream } from '${upstream}base/common/stream.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspaceId: '019a0000-0000-7000-8000-000000000001',
  sessionId: 'session-frozen', threadId: 'thread-frozen' };
const reference = { kind: 'sealed_contribution', session_id: owner.sessionId, thread_id: owner.threadId,
  assembly_id: 'assembly-original', contribution_id: 'source-original' };
const uri = m.contextContentUri(owner, reference);
const body = '冻结正文😀\n保留完整原文';

async function fixture(callback) {
  const closed = new m.Emitter(), opened = new Set([uri.toString()]);
  const files = new m.FileService(new m.NullLogService()), provider = new m.ItemDocuments(files, {
    onDidCloseEditor: closed.event, findEditors: resource => opened.has(resource.toString()) ? [{}] : [],
  });
  const route = spyOn(m.gateway, 'resolveWorkspaceTarget').mockResolvedValue({ gateway_id: owner.gatewayId,
    workspace_id: owner.workspaceId, status: 'ready', connection_kind: 'local' });
  const request = spyOn(m.gateway, 'request').mockResolvedValue({ location: null, content_ref: reference,
    contents: [{ name: 'context', language: 'plaintext', content: body, encoding: 'utf-8' }] });
  try { await callback({ files, provider, route, request, closed, opened }); }
  finally { provider.dispose(); files.dispose(); closed.dispose(); request.mockRestore(); route.mockRestore(); }
}

// 真正经过上游 FileService 的 token→Provider→HTTP signal 接线；HTTP 边界受控，不代替页面关闭验收。
test('FileService 取消正在读取的冻结正文，会终止同一次目标 HTTP 请求', async () => {
  await fixture(async ({ files, request }) => {
    let started;
    const ready = new Promise(resolve => { started = resolve; });
    let signal;
    request.mockImplementation((_path, options) => new Promise((_resolve, reject) => {
      signal = options.signal;
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      started();
    }));
    const cancellation = new m.CancellationTokenSource();
    try {
      const outcome = files.readFile(uri, undefined, cancellation.token).then(() => null, error => error);
      await ready;
      expect(request).toHaveBeenCalledTimes(1);
      cancellation.cancel();
      expect(await outcome).toBeInstanceOf(Error);
      expect(signal.aborted).toBe(true);
    } finally { cancellation.dispose(); }
  });
});

test('Provider 收到已取消的令牌不解析目标、不发送正文请求', async () => {
  await fixture(async ({ provider, route, request }) => {
    const cancellation = new m.CancellationTokenSource();
    cancellation.cancel();
    try {
      await expect(m.consumeStream(provider.readFileStream(uri, {}, cancellation.token), chunks => chunks)).rejects.toThrow();
      expect(route).not.toHaveBeenCalled();
      expect(request).not.toHaveBeenCalled();
    } finally { cancellation.dispose(); }
  });
});

test('最后一个正文编辑器关闭终止在途请求，另一组仍打开时保留原读取', async () => {
  await fixture(async ({ files, request, closed, opened }) => {
    let started;
    const ready = new Promise(resolve => { started = resolve; });
    let signal;
    request.mockImplementation((_path, options) => new Promise((_resolve, reject) => {
      signal = options.signal;
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      started();
    }));
    const outcome = files.readFile(uri).then(() => null, error => error);
    await ready;
    closed.fire({ editor: { resource: uri } });
    expect(signal.aborted).toBe(false);
    opened.clear();
    closed.fire({ editor: { resource: uri } });
    expect(signal.aborted).toBe(true);
    expect(await outcome).toBeInstanceOf(Error);
    expect(request).toHaveBeenCalledTimes(1);
  });
});

test('目标离开后切回仍拒绝迟到的路由结果，不再读取旧正文', async () => {
  const snapshot = m.workspaceContext.getSnapshot();
  try {
    m.workspaceContext.publish({ ...snapshot, gatewayId: owner.gatewayId });
    await fixture(async ({ files, route, request }) => {
      let started, resolved;
      const ready = new Promise(resolve => { started = resolve; });
      route.mockImplementation(() => new Promise(resolve => { resolved = resolve; started(); }));
      const outcome = files.readFile(uri).then(() => null, error => error);
      await ready;
      m.workspaceContext.publish({ ...snapshot, gatewayId: 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' });
      m.workspaceContext.publish({ ...snapshot, gatewayId: owner.gatewayId });
      resolved({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, status: 'ready' });
      expect(await outcome).toBeInstanceOf(Error);
      expect(request).not.toHaveBeenCalled();
    });
  } finally { m.workspaceContext.publish(snapshot); }
});

test('原文件服务完整读取与字节范围使用同一冻结引用，stat 不预读', async () => {
  await fixture(async ({ files, request }) => {
    expect((await files.stat(uri)).size).toBe(0);
    expect(request).not.toHaveBeenCalled();
    expect((await files.readFile(uri)).value.toString()).toBe(body);
    const range = await files.readFile(uri, { position: 3, length: 12 });
    expect([...range.value.buffer]).toEqual([...new TextEncoder().encode(body).slice(3, 15)]);
    expect(request).toHaveBeenCalledTimes(2);
    for (const call of request.mock.calls) {
      expect(call[0]).toBe(`/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/items/inspect`);
      expect(JSON.parse(call[1].body)).toEqual({ operation: 'content', content_ref: reference });
      expect(call[2]).toBe(JSON.stringify([owner.gatewayId, owner.workspaceId]));
    }
  });
});

test('普通 Item 内联媒体保留原始字节与只读文件名', async () => {
  await fixture(async ({ files, request }) => {
    const content = { name: 'inline', language: '', mime_type: 'image/png', content: 'AAECA/8=', encoding: 'base64' };
    const resource = m.itemContentUri(owner, 'item-image', 0, content);
    request.mockResolvedValue({ location: { item_id: 'item-image' }, contents: [content] });
    expect(resource.path.endsWith('inline.png')).toBe(true);
    expect([...(await files.readFile(resource)).value.buffer]).toEqual([0, 1, 2, 3, 255]);
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ operation: 'content', item_id: 'item-image' });
  });
});

test('正文失败明确传播，下一次读取重试原引用', async () => {
  await fixture(async ({ files, request }) => {
    request.mockRejectedValueOnce(new Error('冻结正文原件缺失'));
    await expect(files.readFile(uri)).rejects.toThrow('冻结正文原件缺失');
    expect((await files.readFile(uri)).value.toString()).toBe(body);
    expect(request).toHaveBeenCalledTimes(2);
  });
});

afterAll(() => run.close());
