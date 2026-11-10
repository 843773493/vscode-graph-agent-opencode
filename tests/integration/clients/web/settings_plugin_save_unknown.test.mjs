import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { HttpRequestError } from '../../../../src/clients/web/src/api/http.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_plugin_save_unknown.test.mjs').prepare();
const sourcePath = 'src/clients/workbench/src/settings/pluginConfiguration.ts';
const source = await readFile(sourcePath, 'utf8');
const sourceOwnerPath = 'src/clients/workbench/src/settings/sourceOwner.ts';
const sourceOwnerSource = await readFile(sourceOwnerPath, 'utf8');
const fingerprints = [sourcePath, sourceOwnerPath].map(path => ({
  path,
  sha256: createHash('sha256').update(path === sourcePath ? source : sourceOwnerSource).digest('hex'),
}));

function declaration(path, text, name) {
  const parsed = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name);
  if (!node) throw new Error(`实际源码缺少 ${path}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

const sourceOwnerCode = ['sourceBaseline', 'sourceStatus', 'ConfigurationSourceOwner']
  .map(name => declaration(sourceOwnerPath, sourceOwnerSource, name)).join('\n');
const saveCode = declaration(sourcePath, source, 'saveSourceEdit');
const sourceOwnerFactory = new Function('gateway',
  `${sourceOwnerCode}\nreturn { ConfigurationSourceOwner, sourceBaseline, sourceStatus };`);
const saveSourceEdit = new Function(`${saveCode}\nreturn saveSourceEdit;`)();
afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprints.json'), `${JSON.stringify(fingerprints, null, 2)}\n`));

function fixture({ read, saveError = new HttpRequestError(403, 'Forbidden', 'permission denied after commit', '/config/sources', 'request-save') }) {
  const key = 'gateway_mutable_override';
  const document = { gateway_id: 'gateway-a', source_key: key, writable: true, jsonc: '{}\n', file_digest: 'file-1',
    layer_revision: 1, layer_digest: 'layer-1', active_revision: 1, active_digest: 'active-1', json_schema: {},
    effective: {}, source_effective: {}, layers: [], origins: [], status: {} };
  const calls = [];
  const gateway = { async request(path, options = {}) {
    calls.push({ path, method: options.method ?? 'GET' });
    if (options.method === 'PUT') throw saveError;
    return read({ path, document, options });
  } };
  const { ConfigurationSourceOwner, sourceBaseline, sourceStatus } = sourceOwnerFactory(gateway);
  const owner = new ConfigurationSourceOwner({ gateway_id: document.gateway_id, gateway_connection_id: 'connection-a' });
  const intent = owner.intent(key, '{"plugins":{}}\n', sourceBaseline(document));
  return { calls, intent, owner, sourceKey: key, document, sourceStatus, saveSourceEdit };
}

test('保存返回 403 后只查询原 key，并显示原来源 owner 的 edit 状态', async () => {
  const f = fixture({ read: ({ path, document }) => {
    const query = new URL(path, 'http://fixture').searchParams;
    return { ...document, edit: { idempotency_key: query.get('idempotency_key'), candidate_id: 'candidate-a',
      state: 'candidate_validated', file_digest: 'file-2', source_saved: true, layer_imported: true },
      status: { pending_revision: 2, restart_required: true, reason: '重启后应用' } };
  } });

  const result = await f.saveSourceEdit(f.owner, f.intent);

  expect(f.calls.map(call => call.method)).toEqual(['PUT', 'GET']);
  const readUrl = new URL(f.calls[1].path, 'http://fixture');
  expect(readUrl.pathname).toBe('/api/gateway/config/sources/gateway_mutable_override');
  expect(readUrl.searchParams.get('gateway_connection_id')).toBe('connection-a');
  expect(readUrl.searchParams.get('idempotency_key')).toBe(f.intent.idempotency_key);
  expect(result.document).toBeDefined();
  expect(f.sourceStatus(result.document)).toContain(`${f.intent.idempotency_key} / candidate_validated`);
});

test('原 key 查询没有 edit 时保留原错误且不重放', async () => {
  const f = fixture({ read: ({ document }) => document });

  const result = await f.saveSourceEdit(f.owner, f.intent);

  expect(f.calls.map(call => call.method)).toEqual(['PUT', 'GET']);
  const readUrl = new URL(f.calls[1].path, 'http://fixture');
  expect(readUrl.searchParams.get('idempotency_key')).toBe(f.intent.idempotency_key);
  expect(result.error).toContain('403 Forbidden');
  expect(result.error).toContain(f.intent.idempotency_key);
  expect(result.error).toContain('不会重放');
});

test('原 key 查询失败时同时保留查询错误、原错误和原 key', async () => {
  const f = fixture({ read: () => { throw new Error('原提交查询也断开'); }, saveError: new Error('保存连接断开') });

  const result = await f.saveSourceEdit(f.owner, f.intent);

  expect(f.calls.map(call => call.method)).toEqual(['PUT', 'GET']);
  const readUrl = new URL(f.calls[1].path, 'http://fixture');
  expect(readUrl.searchParams.get('idempotency_key')).toBe(f.intent.idempotency_key);
  expect(result.error).toContain('保存连接断开');
  expect(result.error).toContain('原提交查询也断开');
  expect(result.error).toContain(f.intent.idempotency_key);
  expect(result.error).toContain('不会重放');
});
