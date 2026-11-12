import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/configuration_sources.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'configuration-sources.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/configuration_sources.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>来源合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('来源合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Chromium', () => browser.close()); });
afterAll(() => run.close());
async function exercise(mode, contract = 'configurationSourcesContract') {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(({ mode, contract }) => import('/configuration-sources.mjs').then(module => module[contract](mode)), { mode, contract });
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const lists = result => result.requests.filter(item => item.path.endsWith('/config/sources'));
test('配置发现只读目录，同Gateway只查一次，逻辑层无URI仍可查看，逐键零新增读取', async () => {
  const result = await exercise('browse');
  expect(result.errors).toEqual([]); expect(result.commands).toEqual([]);
  expect(lists(result)).toHaveLength(3); expect(lists(result).filter(item => item.path.startsWith('/api/gateway'))).toHaveLength(1);
  expect(result.rows).toHaveLength(3); expect(new Set(result.rows.map(item => item.id)).size).toBe(3);
  expect(result.rows.every(item => item.selectable && item.label === 'mutable.jsonc')).toBe(true);
  expect(result.requests.every(item => item.method === 'GET')).toBe(true); expect(result.maximum).toBeLessThanOrEqual(3);
});
for (const mode of ['open', 'side', 'pinned', 'background', 'no-preview']) test(`${mode}配置打开交原命令并冻结目标和发起组`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toEqual([]); expect(result.commands).toHaveLength(1);
  expect(result.commands[0]).toEqual({ id: 'boxteam.settings.openSource', request: {
    location: { gateway_id: result.owners[1].gatewayId, workspace_id: result.owners[1].workspaceId, version: 2,
      source_key: 'mutable', name: '配置目标 2' }, groupId: 7, openToSide: mode === 'side',
    editorOptions: { pinned: ['pinned', 'background', 'no-preview'].includes(mode), preserveFocus: mode === 'background' },
  } });
  expect(result.requests.every(item => item.method === 'GET' && !/\/config\/sources\//.test(item.path))).toBe(true);
});
test('配置权限失败独立显示原request_id，其它owner仍可打开', async () => {
  const result = await exercise('failure');
  expect(result.rows.filter(item => item.selectable)).toHaveLength(2);
  const failed = result.rows.find(item => item.label.includes('权限已撤销'));
  expect(failed.detail).toContain('source-denied'); expect(failed.selectable).toBe(false); expect(result.commands).toEqual([]);
});
for (const mode of ['close', 'identity']) test(`${mode}结束配置发现实际读取，丢弃迟到候选`, async () => {
  const result = await exercise(mode); expect(result.aborted).toBe(true); expect(result.commands).toEqual([]);
  expect(result.rows.map(item => item.id)).toEqual(mode === 'close' ? [] : ['source-access-invalidated']);
});

for (const mode of ['workspace', 'gateway', 'side', 'pinned', 'background', 'freeze']) test(`${mode}配置URI由正式目录定位来源后复用原JSONC命令`, async () => {
  const result = await exercise(mode, 'configurationUriContract');
  expect(result.errors).toEqual([]); expect(result.references).toEqual([]); expect(result.addedGroups).toBe(0);
  expect(result.commands).toEqual([{ id: 'boxteam.settings.openSource', request: {
    location: { gateway_id: result.owner.gatewayId, ...(mode === 'gateway' ? {} : { workspace_id: result.owner.workspaceId }),
      version: 2, source_key: mode === 'gateway' ? 'gateway-formal-owner-key' : 'workspace-formal-owner-key',
      name: mode === 'gateway' ? 'Gateway' : result.owner.workspaceId },
    groupId: 7, openToSide: mode === 'side', editorOptions: { pinned: mode === 'pinned', preserveFocus: mode === 'background' },
  } }]);
  expect(result.requests.every(request => request.path.endsWith('/parse') || request.path.endsWith('/sources'))).toBe(true);
});
test('远端reference URI由同一grammar按目标Gateway归一后匹配正式来源', async () => {
  const result = await exercise('remote', 'configurationUriContract');
  const { gatewayId: remoteGatewayId, sourceUri, referenceUri } = result.remoteFixture;
  expect(result.errors).toEqual([]);
  const parses = result.requests.filter(request => request.path.endsWith('/parse'));
  expect(parses).toHaveLength(2);
  expect(parses.map(request => request.body)).toEqual([
    { uri: referenceUri, local_gateway_id: result.owner.gatewayId },
    { uri: referenceUri, local_gateway_id: result.owner.gatewayId, resource_gateway_id: remoteGatewayId },
  ]);
  expect(result.requests.filter(request => request.path.endsWith('/config/sources')).map(request => request.gatewayId)).toEqual([remoteGatewayId]);
  expect(result.requests.find(request => request.path.endsWith('/config/sources'))?.gatewayId).toBe(remoteGatewayId);
  expect(result.requests.filter(request => request.path.endsWith('/config/sources')).every(request => !request.workspaceId)).toBe(true);
  expect(result.requests.filter(request => request.path.endsWith('/config/sources')).map(request => request.body)).toEqual([undefined]);
  expect(result.commands).toEqual([{ id: 'boxteam.settings.openSource', request: {
    location: { gateway_id: remoteGatewayId, version: 2, source_key: 'remote-gateway-formal-owner-key', name: 'Gateway' },
    groupId: 7, openToSide: false, editorOptions: { pinned: false, preserveFocus: false },
  } }]);
  expect(sourceUri).toBe('boxteam://inline/source-development-0_0_2/resources/config/gateway_inline');
});
for (const mode of ['missing', 'ambiguous', 'denied', 'wrong-owner', 'identity', 'parse-identity']) test(`${mode}配置URI拒绝改投或退回普通文件读取`, async () => {
  const result = await exercise(mode, 'configurationUriContract');
  expect(result.commands).toEqual([]); expect(result.references).toEqual([]); expect(result.addedGroups).toBe(0);
  expect(result.errors).toHaveLength(1);
  expect(result.errors[0]).toContain({ missing: '没有公布', ambiguous: '多个正式来源', denied: '权限已撤销',
    'wrong-owner': '归属不匹配', identity: '凭据已改变', 'parse-identity': '凭据已改变' }[mode]);
  if (mode === 'parse-identity') expect(result.requests.map(request => request.path)).toEqual(['/api/gateway/resource-addresses/parse']);
});
