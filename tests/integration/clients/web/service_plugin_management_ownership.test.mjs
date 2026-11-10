import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/service_plugin_management_ownership.test.mjs').prepare();
const sourcePath = 'src/clients/workbench/src/extensions/servicePluginManagement.ts';
const source = await readFile(sourcePath, 'utf8');
const sourceFingerprint = createHash('sha256').update(source).digest('hex');

function declaration(name) {
  const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name);
  if (!node) throw new Error(`实际源码缺少 ${sourcePath}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

function createInstaller(request, picks, inputs, observed) {
  const install = new Function('gateway', 'generateUuid', 'observeInstallation', 'errorsText',
    ['requireInstallationOwner', 'readInstallation', 'installPackageFromView'].map(declaration).join('\n')
      + '\nreturn installPackageFromView;')(
    { async request(path, options = {}) { return request(path, JSON.parse(options.body ?? '{}')); } },
    () => 'operation-original',
    async (_progress, _notifications, _target, operationId, operation) => observed.push({ operationId, operation }),
    error => error instanceof Error ? error.message : String(error));
  return install({
    pick: async () => picks.shift(),
    input: async () => inputs.shift(),
  }, { info() {} }, {}, { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' });
}

function operation(gatewayId = 'gateway-original', operationId = 'operation-original') {
  return { gateway_id: gatewayId, operation_id: operationId, status: 'succeeded', artifact: undefined,
    cancel_requested: false };
}

afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprint.json'), `${JSON.stringify({
  path: sourcePath, sha256: sourceFingerprint,
}, null, 2)}\n`));

test('安装回执 owner 不匹配时只读取目标 Gateway 的原 operation_id', async () => {
  for (const invalid of [operation('gateway-other'), operation('gateway-original', 'operation-other')]) {
    const calls = [], observed = [];
    await createInstaller(async (path, body) => {
      calls.push({ path, body });
      if (path.endsWith('/install')) return invalid;
      if (path.endsWith('/install-operation')) return operation();
      throw new Error(`不应请求 ${path}`);
    }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed);

    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install', 'install-operation']);
    expect(calls[0].body.operation_id).toBe('operation-original');
    expect(calls[1].body.operation_id).toBe('operation-original');
    expect(observed).toEqual([{ operationId: 'operation-original', operation: operation() }]);
  }
});

test('安装响应未知时只查询一次原 operation_id，不重放安装', async () => {
  const calls = [], observed = [];
  await createInstaller(async (path, body) => {
    calls.push({ path, body });
    if (path.endsWith('/install')) throw new Error('安装响应丢失');
    if (path.endsWith('/install-operation')) return operation();
    throw new Error(`不应请求 ${path}`);
  }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed);

  expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install', 'install-operation']);
  expect(calls.every(call => call.body.operation_id === 'operation-original')).toBe(true);
  expect(observed).toEqual([{ operationId: 'operation-original', operation: operation() }]);
});

test('插件目录 owner 错误或分页 revision 变化时不提交安装', async () => {
  for (const secondPage of [
    { gateway_id: 'gateway-other', source_id: 'source-original', catalog_revision: 'revision-1', items: [] },
    { gateway_id: 'gateway-original', source_id: 'source-original', catalog_revision: 'revision-2', items: [] },
  ]) {
    const calls = [], observed = [];
    const pages = [
      { gateway_id: 'gateway-original', source_id: 'source-original', catalog_revision: 'revision-1',
        items: [], next_cursor: 'next-page' },
      secondPage,
    ];
    await expect(createInstaller(async (path, body) => {
      calls.push({ path, body });
      if (path.endsWith('/inventory')) return { sources: { entries: [{ source_id: 'source-original', enabled: true }] } };
      if (path.endsWith('/source-catalog')) return pages.shift();
      throw new Error(`目录不一致时不应请求 ${path}`);
    }, [{ kind: 'source' }, { source: { source_id: 'source-original' } }, { artifact: undefined }], ['plugin'], observed))
      .rejects.toThrow(/不属于原 Gateway|版本已变化/);

    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['inventory', 'source-catalog', 'source-catalog']);
    expect(calls[1].body.cursor).toBeUndefined();
    expect(calls[2].body.cursor).toBe('next-page');
    expect(observed).toEqual([]);
  }
});
