import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/plugin_management.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'plugin-management.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/plugin_management.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>插件快速访问合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('插件快速访问合同服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Chromium', () => browser.close()); });
afterAll(() => run.close());
async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/plugin-management.mjs').then(module => module.pluginManagementContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const inventoryRequests = result => result.requests.filter(item => item.path.endsWith('/inventory'));
test('ext过滤复用同次库存，保留同名插件的确切版本和管理目标', async () => {
  const result = await exercise('filter');
  expect(result.errors).toEqual([]);
  expect(result.items.map(item => item.id)).toEqual(['proof.same-package/same-plugin@2.0.0']);
  expect(inventoryRequests(result)).toHaveLength(1);
  expect(inventoryRequests(result)[0].body).toEqual({ gateway_id: 'target-gateway', gateway_connection_id: 'connection-target' });
  expect(result.opened).toEqual([]); expect(result.commands).toEqual([]);
});
test('ext接受精确版本后打开原插件管理详情', async () => {
  const result = await exercise('open');
  expect(result.errors).toEqual([]);
  expect(result.opened).toEqual([{ id: 'boxteam.views.extensionPackages', args: [
    { gateway_id: 'target-gateway', gateway_connection_id: 'connection-target' }, 'proof.same-package', '2.0.0', 'same-plugin', 'workspace',
  ] }]);
  expect(result.commands).toEqual([]);
});
test('ext install无需库存或公网目录，接受后复用带冻结目标的原安装命令', async () => {
  const result = await exercise('install');
  expect(result.errors).toEqual([]); expect(inventoryRequests(result)).toEqual([]);
  expect(result.commands).toEqual([['boxteam.plugins.installPackage', { gateway_id: 'target-gateway', gateway_connection_id: 'connection-target' }]]);
  expect(result.opened).toEqual([]);
});
for (const mode of ['identity', 'install-identity']) test(`${mode}凭据失效后旧候选不能打开或安装`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toHaveLength(1); expect(result.opened).toEqual([]); expect(result.commands).toEqual([]);
});
for (const mode of ['pending-close', 'pending-identity']) test(`${mode}释放库存实际读取并拒绝迟到候选`, async () => {
  const result = await exercise(mode);
  expect(result.aborted).toBe(true); expect(result.opened).toEqual([]); expect(result.commands).toEqual([]);
  expect(result.items.map(item => item.id)).toEqual(mode === 'pending-close' ? [] : ['plugin-target-invalidated']);
});
test('库存失败明确展示原原因，不能变成可执行空结果', async () => {
  const result = await exercise('failed');
  expect(result.items).toHaveLength(1); expect(result.items[0].label).toContain('原安装域不可达'); expect(result.items[0].actionable).toBe(false);
});
