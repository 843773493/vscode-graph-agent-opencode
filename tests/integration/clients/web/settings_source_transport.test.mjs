import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { VSBuffer } from '../../../../src/clients/workbench/upstream/src/vs/base/common/buffer.js';
import { Event } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationError, getErrorMessage } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { parse } from '../../../../src/clients/workbench/upstream/src/vs/base/common/json.js';
import { dirname, resolvePath } from '../../../../src/clients/workbench/upstream/src/vs/base/common/resources.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError } from '../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { HttpRequestError, invalidateGatewayToken } from '../../../../src/clients/web/src/api/http.js';
import { getConfigSourceDocument, listConfigSources, preflightConfigSource, saveConfigSource } from '../../../../src/clients/web/src/api/configSources.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

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
const ownerCode = await declarations('src/clients/workbench/src/settings/sourceOwner.ts', ['sourceBaseline', 'sourceStatus', 'ConfigurationSourceOwner']);
const filesCode = await declarations('src/clients/workbench/src/settings/sourceEditor.ts', ['configSourceScheme', 'configSourceEditorUri', 'sourceLocation', 'ConfigurationSourceFiles']);
afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprints.json'), JSON.stringify(fingerprints, null, 2) + '\n'));

function fixture({ confirm }) {
  const calls = [], sourceKey = 'gateway_mutable_override';
  const document = { gateway_id: 'gateway-a', source_key: sourceKey, writable: true, jsonc: '{}\n',
    file_digest: 'file-1', layer_revision: 1, layer_digest: 'layer-1', active_revision: 1, active_digest: 'active-1',
    json_schema: {}, layers: [], origins: [], effective: {}, source_effective: {}, status: {} };
  const transport = async (path, options = {}) => {
    const call = { path, method: options.method ?? 'GET', intent: options.body && JSON.parse(options.body), headers: options.headers };
    calls.push(call);
    if (call.method === 'POST') return { document, changed_paths: [] };
    return call.method === 'PUT' ? { ...document, jsonc: call.intent.jsonc,
      edit: { idempotency_key: call.intent.idempotency_key, source_saved: true, layer_imported: true } } : document;
  };
  const { ConfigurationSourceOwner, sourceBaseline, sourceStatus } = new Function('gateway',
    `${ownerCode}\nreturn { ConfigurationSourceOwner, sourceBaseline, sourceStatus };`)({ request: transport });
  const environment = { VSBuffer, Event, CancellationError, getErrorMessage, Disposable, parse, dirname, resolvePath, URI,
    FilePermission, FileType, FileSystemProviderCapabilities, FileSystemProviderErrorCode, createFileSystemProviderError,
    HttpRequestError, ConfigurationSourceOwner, sourceBaseline, sourceStatus };
  const { ConfigurationSourceFiles } = new Function(...Object.keys(environment), `${filesCode}\nreturn { ConfigurationSourceFiles };`)(...Object.values(environment));
  const files = new ConfigurationSourceFiles({ confirm }, { info() {}, error() {} });
  const target = { gateway_id: document.gateway_id, gateway_connection_id: 'original-route' };
  const resource = files.prepareEditor(target, sourceKey, document);
  return { files, resource, calls, target, sourceKey, document };
}
const options = { create: false, overwrite: true, unlock: false };

test('工作区配置列表、读取、预检和保存向 HTTP 层传递选定连接', async () => {
  const target = { gateway_id: 'gateway-b', workspace_id: 'workspace-b', gateway_connection_id: 'selected-route-b' };
  const document = { ...target, source_key: 'workspace_user', jsonc: '{}', edit: { idempotency_key: 'original-edit' } };
  const requests = [], originalFetch = globalThis.fetch;
  const Owner = new Function('getConfigSourceDocument', 'listConfigSources', 'preflightConfigSource', 'saveConfigSource',
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
