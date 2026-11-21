import { t } from '../../../../src/clients/shared/core/i18n/index.js';
import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { VSBuffer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/buffer.js';
import { Emitter, Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationError, getErrorMessage } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { parse } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { dirname, resolvePath } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError } from '../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { HttpRequestError, invalidateGatewayToken } from '../../../../src/clients/web/src/api/http.js';
import { getConfigSourceDocument, listConfigSources, preflightConfigSource, saveConfigSource } from '../../../../src/clients/web/src/api/configSources.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

// 实际声明依赖同一产品翻译入口。
const localizedFunction = (...parts) => new Function('t', ...parts).bind(undefined, t);

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_source_transport.test.mjs').prepare();
const fingerprints = [];
async function declarations(path, names) {
  const original = await readFile(path, 'utf8');
  fingerprints.push({ path, sha256: createHash('sha256').update(original).digest('hex') });
  const parsed = ts.createSourceFile(path, original, ts.ScriptTarget.Latest, true);
  const printer = ts.createPrinter();
  const selected = names.map(name => {
    const node = parsed.statements.find(node => node.name?.text === name || ts.isVariableStatement(node)
      && node.declarationList.declarations.some(item => item.name.getText(parsed) === name));
    if (!node) throw new Error(`实际源码缺少 ${path}#${name}`);
    return printer.printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  });
  return ts.transpile(selected.join('\n'), { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}
const ownerCode = await declarations('src/clients/workbench/src/settings/sourceOwner.ts', ['sourceBaseline', 'sourceStatus', 'sourceFanoutStatus', 'ConfigurationSourceOwner']);
const filesCode = await declarations('src/clients/workbench/src/settings/sourceEditor.ts', ['configSourceScheme', 'configSourceEditorUri', 'sourceLocation', 'ConfigurationSourceFiles']);
afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprints.json'), JSON.stringify(fingerprints, null, 2) + '\n'));

function fixture({ confirm, respond } = { confirm: async () => ({ confirmed: true }) }) {
  const calls = [], sourceKey = 'gateway_mutable_override';
  const document = { gateway_id: 'gateway-a', source_key: sourceKey, writable: true, jsonc: '{}\n',
    file_digest: 'file-1', layer_revision: 1, layer_digest: 'layer-1', active_revision: 1, active_digest: 'active-1',
    json_schema: {}, layers: [], origins: [], effective: {}, source_effective: {}, status: {} };
  const transport = async (path, options = {}) => {
    const call = { path, method: options.method ?? 'GET', intent: options.body && JSON.parse(options.body), headers: options.headers };
    calls.push(call);
    if (respond) return respond(call, document);
    if (call.method === 'POST') return { document, changed_paths: [] };
    return call.method === 'PUT' ? { ...document, jsonc: call.intent.jsonc,
      edit: { idempotency_key: call.intent.idempotency_key, source_saved: true, layer_imported: true, file_digest: document.file_digest, state: 'active' } } : document;
  };
  const { ConfigurationSourceOwner, sourceBaseline, sourceStatus, sourceFanoutStatus } = localizedFunction('gateway',
    `${ownerCode}\nreturn { ConfigurationSourceOwner, sourceBaseline, sourceStatus, sourceFanoutStatus };`)({ request: transport });
  const environment = { VSBuffer, Emitter, Event, StorageScope: { WORKSPACE: 1 }, StorageTarget: { MACHINE: 1 }, CancellationError, getErrorMessage, Disposable, parse, dirname, resolvePath, URI,
    FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError,
    HttpRequestError, ConfigurationSourceOwner, sourceBaseline, sourceStatus, sourceFanoutStatus };
  const { ConfigurationSourceFiles } = localizedFunction(...Object.keys(environment), `${filesCode}\nreturn { ConfigurationSourceFiles };`)(...Object.values(environment));
  const stored = new Map(), notices = [];
  const storage = { getObject: key => stored.get(key), store(key, value) { if (value === undefined) stored.delete(key); else stored.set(key, structuredClone(value)); }, async flush() {} };
  const createFiles = () => new ConfigurationSourceFiles({ confirm }, { info: message => notices.push(message), error: message => notices.push(message), warn: message => notices.push(message) }, storage);
  const files = createFiles();
  const target = { gateway_id: document.gateway_id, gateway_connection_id: 'original-route' };
  const resource = files.prepareEditor(target, sourceKey, document);
  return { files, resource, calls, target, sourceKey, document, stored, notices, createFiles };
}
const options = { create: false, overwrite: true, unlock: false };

test('备份恢复先取得来源和相对 schema，保留草稿的旧 CAS 而非采用当前服务端基线', async () => {
  const f = fixture();
  f.document.jsonc = '{"$schema":"gateway_schema.jsonc"}';
  f.document.json_schema = { type: 'object', properties: { enabled: { type: 'boolean' } } };
  const restored = f.createFiles();
  const baseline = { base_file_digest: 'before-external-change', base_layer_revision: 0, base_layer_digest: 'old-layer', expected_active_revision: 0, expected_active_digest: 'old-active' };
  const backup = { baseline };
  await restored.restore(f.resource, backup);
  expect(restored.currentDocument(f.resource)).toBe(f.document);
  expect(restored.backup(f.resource)).toEqual(backup);
  const schema = resolvePath(dirname(f.resource), 'gateway_schema.jsonc');
  expect(JSON.parse(VSBuffer.wrap(await restored.readFile(schema)).toString())).toEqual(f.document.json_schema);
  expect((await restored.stat(schema)).permissions).toBe(FilePermission.Readonly);
  restored.prepareEditor(f.target, f.sourceKey, { ...f.document, file_digest: 'later-document' });
  expect(restored.currentDocument(f.resource)).toBe(f.document);
  expect(restored.backup(f.resource)).toEqual(backup);
  expect(f.calls.map(call => call.path)).toEqual(['/api/gateway/config/sources/' + f.sourceKey]);
  expect(f.calls[0].headers['X-BoxTeam-Target-Gateway-Id']).toBe(f.target.gateway_id);
  restored.dispose(); f.files.dispose();
});

test('先登记的来源在打开表单时补齐文档，保留待查询提交身份', () => {
  const f = fixture(), restored = f.createFiles();
  f.stored.set('boxteam.configurationSource.submission:' + f.resource.toString(), { key: 'pending-save', gatewayConnectionId: 'saved-route' });
  expect(restored.pendingSubmission(f.resource)).toBe('pending-save');
  restored.prepareEditor(f.target, f.sourceKey, f.document);
  expect(restored.currentDocument(f.resource)).toBe(f.document);
  expect(restored.pendingSubmission(f.resource)).toBe('pending-save');
  expect(f.calls).toEqual([]);
  restored.dispose(); f.files.dispose();
});

test('工作区配置列表、读取、预检和保存向 HTTP 层传递选定连接', async () => {
  const target = { gateway_id: 'gateway-b', workspace_id: 'workspace-b', gateway_connection_id: 'selected-route-b' };
  const document = { ...target, source_key: 'workspace_user', jsonc: '{}', edit: { idempotency_key: 'original-edit' } };
  const requests = [], originalFetch = globalThis.fetch;
  const Owner = localizedFunction('getConfigSourceDocument', 'listConfigSources', 'preflightConfigSource', 'saveConfigSource',
    `${ownerCode}\nreturn ConfigurationSourceOwner;`)(getConfigSourceDocument, listConfigSources, preflightConfigSource, saveConfigSource);
  invalidateGatewayToken(0);
  globalThis.fetch = async (url, init) => {
    if (String(url).endsWith('/api/gateway/auth/local-credential')) return Response.json({ data: { token: 'test-token' } });
    requests.push({ url: String(url), method: init.method ?? 'GET', headers: new Headers(init.headers) });
    return Response.json({ code: 0, message: 'ok', request_id: 'route-test-request',
      data: String(url).endsWith('/preflight') ? { document } : String(url).endsWith('/sources') ? { sources: [] } : document });
  };
  try {
    const owner = new Owner(target), intent = { ...target, source_key: document.source_key, idempotency_key: 'original-edit', jsonc: '{}' };
    await owner.list();
    await owner.read(document.source_key);
    await owner.preflight(intent);
    await owner.save(intent);
    expect(requests.map(request => request.method)).toEqual(['GET', 'GET', 'POST', 'PUT']);
    for (const request of requests) {
      expect(request.headers.get('X-BoxTeam-Target-Gateway-Id')).toBe(target.gateway_id);
      expect(request.headers.get('X-BoxTeam-Workspace-Id')).toBe(target.workspace_id);
      expect(request.headers.get('X-BoxTeam-Gateway-Connection-Id')).toBe(target.gateway_connection_id);
    }
  } finally { globalThis.fetch = originalFetch; invalidateGatewayToken(0); }
});

test('同一 URI 重选连接时，在途保存结清原路由，后续读取使用新路由', async () => {
  const entered = Promise.withResolvers(), accepted = Promise.withResolvers();
  const f = fixture({ confirm: () => { entered.resolve(); return accepted.promise; } });
  await f.files.readFile(f.resource);
  const saved = f.files.writeFile(f.resource, VSBuffer.fromString('{"changed":true}').buffer, options);
  await entered.promise;
  expect(f.files.prepareEditor({ ...f.target, gateway_connection_id: 'new-route' }, f.sourceKey, f.document).toString()).toBe(f.resource.toString());
  accepted.resolve({ confirmed: true });
  await saved;
  const writes = f.calls.filter(call => call.method === 'PUT');
  expect(writes).toHaveLength(1);
  expect(new URL(writes[0].path, 'http://fixture').searchParams.get('gateway_connection_id')).toBe('original-route');
  await f.files.query(f.resource);
  expect(new URL(f.calls.at(-1).path, 'http://fixture').searchParams.get('gateway_connection_id')).toBe('new-route');
  expect(writes[0].intent.gateway_id).toBe('gateway-a');
});

test('共享来源预览说明观察范围，未上报结果不伪报成功', () => {
  const { sourceFanoutStatus } = localizedFunction(`${ownerCode}\nreturn { sourceFanoutStatus };`)();
  const document = { workspace_id: 'workspace-a', source_key: 'workspace_mutable_override',
    fanout: { coverage: 'observed_only', source_generation: 2, fanout_id: 'original-fanout', result: 'fanout_partial', workspaces: [
      { workspace_id: 'workspace-a', status: 'applied', result: 'unchanged' },
      { workspace_id: 'workspace-b', status: 'conflict', result: 'conflict', error: '候选基线冲突' },
      { workspace_id: 'workspace-c', status: 'unobserved' },
    ] } };
  const status = sourceFanoutStatus(document);
  expect(status).toContain('不受 Gateway 登记限制');
  expect(status).toContain('并非完整名单');
  expect(status).toContain('workspace-a: unchanged');
  expect(status).toContain('workspace-b: conflict · 候选基线冲突');
  expect(status).toContain('workspace-c: 本代尚未观察');
  expect(status).toContain('original-fanout');
  expect(sourceFanoutStatus({ ...document, fanout: undefined })).toContain('应用结果未知');
});


test('来源保存确认前取消不提交，也不留下提交身份', async () => {
  const f = fixture({ confirm: async () => ({ confirmed: false }) });
  await f.files.readFile(f.resource);
  await expect(f.files.writeFile(f.resource, VSBuffer.fromString('{ "changed": true }').buffer, options)).rejects.toBeInstanceOf(CancellationError);
  expect(f.calls.map(call => call.method)).toEqual(['POST']);
  expect([...f.stored]).toEqual([]);
  expect(f.files.pendingSubmission(f.resource)).toBeUndefined();
});

test('来源保存响应丢失按原 edit 和文件版本确认，格式不同仍只写一次', async () => {
  let saved;
  const f = fixture({ confirm: async () => ({ confirmed: true }), respond(call, document) {
    if (call.method === 'POST') return { document, changed_paths: ['changed'] };
    if (call.method === 'PUT') {
      saved = { ...document, jsonc: '{"changed":true}', file_digest: 'original-file-2',
        edit: { idempotency_key: call.intent.idempotency_key, source_saved: true, layer_imported: true, file_digest: 'original-file-2', state: 'active' } };
      throw new Error('模拟已提交后的网络断开');
    }
    return saved;
  } });
  await f.files.readFile(f.resource);
  await f.files.writeFile(f.resource, VSBuffer.fromString('{ "changed": true }').buffer, options);
  expect(f.calls.map(call => call.method)).toEqual(['POST', 'PUT', 'GET']);
  expect(new URL(f.calls.at(-1).path, 'http://fixture').searchParams.get('idempotency_key')).toBe(saved.edit.idempotency_key);
  expect(f.files.currentDocument(f.resource).jsonc).toBe('{"changed":true}');
  expect(f.files.pendingSubmission(f.resource)).toBeUndefined();
  expect([...f.stored]).toEqual([]);
});

test('未知保存重开和重选路由后只沿原连接查询，恢复信息只有非秘密提交身份', async () => {
  let saved, unavailable = true;
  const f = fixture({ confirm: async () => ({ confirmed: true }), respond(call, document) {
    if (call.method === 'POST') return { document, changed_paths: [] };
    if (call.method === 'PUT') {
      saved = { ...document, jsonc: '{"changed":true}', file_digest: 'file-2',
        edit: { idempotency_key: call.intent.idempotency_key, source_saved: true, layer_imported: true, file_digest: 'file-2', state: 'active' } };
      throw new Error('模拟响应丢失');
    }
    if (unavailable) throw new Error('模拟查询断开');
    return saved;
  } });
  await f.files.readFile(f.resource);
  await expect(f.files.writeFile(f.resource, VSBuffer.fromString('{ "changed": true }').buffer, options)).rejects.toBeInstanceOf(AggregateError);
  expect([...f.stored.values()]).toEqual([{ key: saved.edit.idempotency_key, gatewayConnectionId: 'original-route' }]);
  expect(f.files.backup(f.resource).submissionKey).toBeUndefined();
  f.files.prepareEditor({ ...f.target, gateway_connection_id: 'new-route' }, f.sourceKey, f.document);
  await expect(f.files.query(f.resource)).rejects.toThrow('模拟查询断开');
  expect(new URL(f.calls.at(-1).path, 'http://fixture').searchParams.get('gateway_connection_id')).toBe('original-route');
  f.files.release(f.resource);
  const restored = f.createFiles();
  restored.prepareEditor({ ...f.target, gateway_connection_id: 'new-route' }, f.sourceKey, f.document);
  await expect(restored.writeFile(f.resource, VSBuffer.fromString('{"changed":false}').buffer, options)).rejects.toThrow('模拟查询断开');
  unavailable = false;
  await restored.readFile(f.resource);
  const query = new URL(f.calls.at(-1).path, 'http://fixture');
  expect(query.searchParams.get('gateway_connection_id')).toBe('original-route');
  expect(query.searchParams.get('idempotency_key')).toBe(saved.edit.idempotency_key);
  expect(restored.pendingSubmission(f.resource)).toBeUndefined();
  expect(f.calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  expect([...f.stored]).toEqual([]);
  restored.dispose(); f.files.dispose();
});

test('同一来源的两个保存不会并行提交', async () => {
  const entered = Promise.withResolvers(), accepted = Promise.withResolvers();
  const f = fixture({ confirm: () => { entered.resolve(); return accepted.promise; } });
  await f.files.readFile(f.resource);
  const first = f.files.writeFile(f.resource, VSBuffer.fromString('{ "changed": true }').buffer, options);
  await entered.promise;
  await expect(f.files.writeFile(f.resource, VSBuffer.fromString('{"changed":true}').buffer, options)).rejects.toThrow('保存进行中');
  accepted.resolve({ confirmed: true }); await first;
  expect(f.calls.filter(call => call.method === 'PUT')).toHaveLength(1);
});

test('保存直接返回仍在 applying 的未写入回执时保留原键，恢复后继续只查询', async () => {
  let receipt;
  const f = fixture({ confirm: async () => ({ confirmed: true }), respond(call, document) {
    if (call.method === 'POST') return { document, changed_paths: [] };
    if (call.method === 'PUT') receipt = { ...document,
      edit: { idempotency_key: call.intent.idempotency_key, source_saved: false, layer_imported: false, file_digest: 'candidate-file', state: 'applying' } };
    return receipt;
  } });
  await f.files.readFile(f.resource);
  await expect(f.files.writeFile(f.resource, VSBuffer.fromString('{ "changed": true }').buffer, options)).rejects.toThrow();
  expect(f.files.pendingSubmission(f.resource)).toBe(receipt.edit.idempotency_key);
  await f.files.query(f.resource);
  expect(f.files.pendingSubmission(f.resource)).toBe(receipt.edit.idempotency_key);
  f.files.release(f.resource);
  const reopened = f.createFiles();
  await reopened.readFile(f.resource);
  expect(reopened.pendingSubmission(f.resource)).toBe(receipt.edit.idempotency_key);
  await expect(reopened.writeFile(f.resource, VSBuffer.fromString('{"changed":false}').buffer, options)).rejects.toThrow('仍未知');
  expect(f.calls.filter(call => call.method === 'PUT')).toHaveLength(1);
  receipt = { ...receipt, file_digest: 'candidate-file', edit: { ...receipt.edit, source_saved: true, layer_imported: true, state: 'active' } };
  await reopened.query(f.resource);
  expect(reopened.pendingSubmission(f.resource)).toBeUndefined();
  expect([...f.stored]).toEqual([]);
  reopened.dispose(); f.files.dispose();
});
