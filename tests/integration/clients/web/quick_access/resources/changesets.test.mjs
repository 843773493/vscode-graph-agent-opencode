import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/changesets.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'changesets.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/changesets.fixture.mjs')],
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
    const result = await page.evaluate(mode => import('/changesets.mjs').then(module => module.changesetContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const lists = result => result.requests.filter(item => item.path.endsWith('/changesets'));
test('只发现发起child所属Session摘要，跨owner不扫描会话，筛选取消不读正文或写入', async () => {
  const result = await exercise('browse');
  expect(lists(result)).toEqual([{ path: '/api/v1/sessions/ses_origin/changesets', method: 'GET',
    gatewayId: result.owner.gatewayId, workspaceId: result.owner.workspaceId }]);
  expect(result.rows).toHaveLength(1); expect(result.rows[0].detail).toContain('ses_origin');
  expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
  expect(result.requests.every(item => item.method === 'GET' && !item.path.includes('/changesets/'))).toBe(true);
});
for (const mode of ['open', 'side', 'pinned', 'background', 'no-preview', 'freeze']) test(`${mode}将精确changeset与发起child交原Thread编辑器`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toEqual([]); expect(result.opened).toEqual([{ owner: { ...result.owner,
    workspaceKey: JSON.stringify([result.owner.gatewayId, result.owner.workspaceId]), title: result.owner.threadId },
    group: mode === 'side' ? 8 : 7, options: { pinned: ['pinned', 'background', 'no-preview'].includes(mode),
      preserveFocus: mode === 'background', viewState: { resourcesVisible: true, resourceTab: 'changes', selectedChangesetId: 'turn:chosen' } } }]);
  expect(lists(result)).toHaveLength(1); expect(result.groupsAdded).toBe(mode === 'side' ? 1 : 0);
  expect(result.requests.every(item => item.method === 'GET' && !item.path.includes('/changesets/'))).toBe(true);
});
for (const mode of ['no-thread', 'excluded']) test(`${mode}没有明确且入选的Session时零变更发现`, async () => {
  const result = await exercise(mode); expect(lists(result)).toEqual([]); expect(result.opened).toEqual([]);
});
for (const mode of ['denied', 'wrong-owner']) test(`${mode}保留原拒绝，不能发布错误会话候选`, async () => {
  const result = await exercise(mode); expect(result.opened).toEqual([]);
  expect(result.rows.some(item => item.label.includes(mode === 'denied' ? '原变更读取权限已撤销' : '变更目录响应不属于发起会话'))).toBe(true);
  if (mode === 'denied') expect(result.rows.some(item => item.detail === 'request_id: changeset-denied')).toBe(true);
});
for (const mode of ['close', 'identity']) test(`${mode}取消实际目录请求，不发布迟到候选`, async () => {
  const result = await exercise(mode); expect(result.aborted).toBe(true); expect(result.opened).toEqual([]);
  expect(result.rows.every(item => !item.selectable)).toBe(true);
});
test('发起组已关闭时不创建空分屏或改投活动组', async () => {
  const result = await exercise('closed-group'); expect(result.opened).toEqual([]); expect(result.groupsAdded).toBe(0);
  expect(result.errors[0]).toContain('发起打开的编辑器组已关闭');
});
