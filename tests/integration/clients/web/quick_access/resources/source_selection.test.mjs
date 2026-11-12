import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/source_selection.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'source-selection.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/source_selection.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>来源选择合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('来源选择静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('来源选择 Chromium', () => browser.close());
});
afterAll(() => run.close());
async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/source-selection.mjs').then(module => module.sourceReferenceSelectionContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const parses = result => result.requests.filter(item => item.path.endsWith('/parse'));
test('来源多选保留发起child，跨Gateway来源与Gateway配置仅解析引用，全部成功才一次追加', async () => {
  const result = await exercise('success');
  expect(result.errors).toEqual([]); expect(result.outcome).toEqual({});
  expect(result.events).toEqual(['capture:0', 'scope', 'sources']);
  expect(result.beforeWrites).toEqual([[], []]); expect(result.drafts[1]).toEqual([]);
  expect(result.drafts[0]).toHaveLength(1);
  expect(result.drafts[0][0].split('\n')).toEqual([
    `boxteam://${result.owners[1].gatewayId}/workspace/${result.owners[1].workspaceId}/resources/agent-spec/root/AGENTS.md`,
    `boxteam://${result.owners[1].gatewayId}/inline/probe/resources/config/gateway_inline`,
    `boxteam://${result.owners[0].gatewayId}/workspace/${result.owners[0].workspaceId}/resources/attachments/att_${'c'.repeat(32)}/original/content`,
  ]);
  expect(parses(result)).toHaveLength(3);
  expect(parses(result).map(item => item.body.resource_gateway_id)).toEqual([result.owners[1].gatewayId, result.owners[1].gatewayId, result.owners[0].gatewayId]);
  expect(result.scopeOptions.canPickMany).toBe(true); expect(result.pickerOptions.canPickMany).toBe(true);
  expect(result.pickerOptions.enabledProviderPrefixes).toEqual(['resource ']); expect(result.scopeButtons).toBe(1);
  expect(result.selectedItems.every(item => item.buttons === 0 && !item.disabled)).toBe(true);
  expect(result.rows.filter(item => item.label === 'logical.jsonc').every(item => item.disabled)).toBe(true);
  expect(result.requests.some(item => /\/(tool-results|changesets|mcp\/servers)$/.test(item.path) || item.path === '/api/v1/resources')).toBe(false);
});
test('只选择B时A业务metadata零读取，不把B资源改为B接收者', async () => {
  const result = await exercise('only-b');
  expect(result.outcome).toEqual({}); expect(result.drafts[0]).toHaveLength(1); expect(result.drafts[1]).toEqual([]);
  const domain = result.requests.filter(item => item.path.startsWith('/api/v1/'));
  expect(domain.length).toBeGreaterThan(0); expect(domain.every(item => item.gatewayId === result.owners[1].gatewayId)).toBe(true);
  expect(domain.some(item => item.path.endsWith('/attachments'))).toBe(false);
});
for (const mode of ['scope-cancel', 'source-cancel', 'back-cancel', 'scope-identity', 'source-identity', 'source-closed', 'unsupported', 'failure', 'identity-parse']) {
  test(`${mode}保持草稿不变，不追加成功前缀或读取来源正文`, async () => {
    const result = await exercise(mode);
    expect(result.drafts).toEqual([[], []]); expect(result.errors).toEqual([]);
    expect(parses(result)).toHaveLength(['failure', 'identity-parse'].includes(mode) ? 2 : 0);
    if (mode.endsWith('cancel')) expect(result.outcome).toEqual({});
    else expect(result.outcome.error).toBeTruthy();
    if (mode.startsWith('scope-')) expect(result.requests).toEqual([]);
    if (mode === 'back-cancel') expect(result.events).toEqual(['capture:0', 'scope', 'sources', 'scope']);
  });
}
