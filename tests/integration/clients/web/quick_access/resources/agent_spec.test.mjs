import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/agent_spec.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'agent-spec.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/agent_spec.fixture.mjs')],
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
async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/agent-spec.mjs').then(module => module.agentSpecContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const parses = result => result.requests.filter(item => item.path.endsWith('/parse'));
test('同名来源按完整owner区分，候选／高亮零正文读取与激活', async () => {
  const result = await exercise('browse');
  expect(result.rows).toHaveLength(2); expect(result.rows[0].id).not.toBe(result.rows[1].id);
  expect(result.rows.every(row => row.selectable && row.highlights.label.length)).toBe(true);
  expect(result.requests.every(item => item.method === 'GET')).toBe(true);
  expect(result.references).toEqual([]); expect(result.opened).toEqual([]);
});
for (const mode of ['close', 'identity']) test(`${mode}取消真实读取且不发布迟到来源`, async () => {
  const result = await exercise(mode);
  expect(result.aborted).toBe(true); expect(parses(result)).toEqual([]); expect(result.references).toEqual([]);
  if (mode === 'close') expect(result.rows).toEqual([]);
  else expect(result.rows.map(item => item.id)).toEqual(['source-access-invalidated']);
});
test('范围取消保留原owner与发起组，且零打开', async () => {
  const result = await exercise('scope-cancel');
  expect(result.shows).toHaveLength(1); expect(result.shows[0].options.providerOptions.resourceGroup).toBe(7);
  expect(result.shows[0].options.providerOptions.searchOwners).toHaveLength(2);
  expect(parses(result)).toEqual([]); expect(result.references).toEqual([]);
});
for (const mode of ['stopped', 'unsupported', 'no-owner']) test(`${mode}明确能力错误且不伪造可打开候选`, async () => {
  const result = await exercise(mode); expect(parses(result)).toEqual([]); expect(result.opened).toEqual([]);
  if (mode === 'stopped') { expect(result.rows[1].selectable).toBe(false); expect(result.rows[1].label).toContain('stopped'); }
  else expect(result.rows.every(item => !item.selectable)).toBe(true);
  if (mode === 'no-owner') expect(result.requests).toEqual([]);
});
for (const mode of ['open', 'side', 'pinned', 'background', 'no-preview', 'freeze']) test(`${mode}复用原Opener并固定远端来源、编辑器组及修饰键`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toEqual([]); expect(parses(result)).toHaveLength(1);
  expect(parses(result)[0].body.resource_gateway_id).toBe(`gateway_${'b'.repeat(32)}`);
  expect(result.references).toHaveLength(1); expect(result.released).toBe(1); expect(result.opened).toHaveLength(1);
  expect(result.opened[0].source.gatewayId).toBe(`gateway_${'b'.repeat(32)}`);
  expect(result.opened[0].group).toBe(mode === 'side' ? 8 : 7);
  expect(result.opened[0].options.preserveFocus).toBe(mode === 'background');
  expect(result.opened[0].options.pinned).toBe(['pinned', 'background', 'no-preview'].includes(mode));
});
test('parse归属不匹配时不读取、不打开替代来源', async () => {
  const result = await exercise('mismatch');
  expect(parses(result)).toHaveLength(1); expect(result.errors.join('')).toContain('归属不匹配');
  expect(result.references).toEqual([]); expect(result.opened).toEqual([]);
});
test('发起编辑器组关闭时释放原引用且不创建空分屏', async () => {
  const result = await exercise('closed-group');
  expect(result.errors.join('')).toContain('编辑器组已关闭'); expect(result.released).toBe(1);
  expect(result.opened).toEqual([]); expect(result.addedGroups).toBe(0);
});
