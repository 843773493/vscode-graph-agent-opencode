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
async function exercise(mode, kind = 'changeset') {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(({ mode, kind }) => import('/changesets.mjs').then(module => module.changesetContract(mode, kind)), { mode, kind });
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

const attachmentLists = result => result.requests.filter(item => item.path.includes('/attachments?'));
test('附件只读精确Thread元数据，浏览筛选不读取或重建原件', async () => {
  const result = await exercise('browse', 'attachment');
  expect(attachmentLists(result)).toHaveLength(1); expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
  expect(result.requests.every(item => item.method === 'GET' && !item.path.endsWith('/content'))).toBe(true);
  expect(result.rows).toHaveLength(1); expect(result.rows[0].label).toBe('原图片.png');
});
for (const mode of ['open', 'side', 'pinned', 'background', 'no-preview', 'freeze']) test(`${mode}附件交原Thread预览并冻结child和组`, async () => {
  const result = await exercise(mode, 'attachment');
  expect(result.errors).toEqual([]); expect(result.opened).toEqual([{ owner: { ...result.owner,
    workspaceKey: JSON.stringify([result.owner.gatewayId, result.owner.workspaceId]), title: result.owner.threadId },
    group: mode === 'side' ? 8 : 7, options: { pinned: ['pinned', 'background', 'no-preview'].includes(mode),
      preserveFocus: mode === 'background', viewState: { resourcesVisible: true, resourceTab: 'files', selectedAttachment: result.attachment } } }]);
  expect(attachmentLists(result)).toHaveLength(1);
});
for (const mode of ['no-thread', 'excluded']) test(`${mode}附件没有明确入选Thread时零扫描`, async () => {
  const result = await exercise(mode, 'attachment'); expect(attachmentLists(result)).toEqual([]); expect(result.opened).toEqual([]);
});
for (const mode of ['denied', 'wrong-owner', 'unavailable']) test(`${mode}附件不发布可接受的错误来源`, async () => {
  const result = await exercise(mode, 'attachment'); expect(result.opened).toEqual([]);
  expect(result.rows.every(item => !item.selectable)).toBe(true);
  expect(result.rows.every(item => !item.copyable)).toBe(true);
});
test('附件候选直接复制原URI，保持来源Gateway而不打开媒体或读取原件', async () => {
  const result = await exercise('copy', 'attachment');
  expect(result.errors).toEqual([]); expect(result.opened).toEqual([]); expect(result.groupsAdded).toBe(0);
  expect(result.copied).toEqual([`boxteam://${result.owner.gatewayId}/workspace/${result.owner.workspaceId}/resources/attachments/att_${'a'.repeat(32)}/original/content`]);
  expect(result.requests.filter(item => item.method !== 'GET')).toEqual([{
    path: '/api/gateway/resource-addresses/parse', method: 'POST', gatewayId: null, workspaceId: null,
    body: { uri: result.attachment.file_id, local_gateway_id: `gateway_${'a'.repeat(32)}`, resource_gateway_id: result.owner.gatewayId },
  }]);
  expect(result.requests.some(item => item.path.endsWith('/content') || item.path === '/api/v1/resources/navigation')).toBe(false);
});
for (const mode of ['copy-mismatch', 'copy-identity']) test(`${mode}附件错误或迟到地址不写剪贴板`, async () => {
  const result = await exercise(mode, 'attachment');
  expect(result.copied).toEqual([]); expect(result.opened).toEqual([]);
  expect(result.errors[0]).toContain(mode === 'copy-mismatch' ? '归属不匹配' : 'Gateway 或访问凭据已改变');
  expect(result.requests.filter(item => item.method === 'POST')).toHaveLength(1);
});
for (const mode of ['close', 'identity']) test(`${mode}附件取消真实目录请求并丢弃迟到结果`, async () => {
  const result = await exercise(mode, 'attachment'); expect(result.aborted).toBe(true); expect(result.opened).toEqual([]);
  expect(result.rows.every(item => !item.selectable)).toBe(true);
});
test('附件下一页由明确按钮读取，筛选不重查', async () => {
  const result = await exercise('page', 'attachment'); expect(attachmentLists(result)).toHaveLength(2);
  expect(new URLSearchParams(attachmentLists(result)[1].path.split('?')[1]).get('after')).toBe('att_' + 'a'.repeat(32));
  expect(result.rows.map(item => item.label)).toEqual(['原图片.png', '原图片2.png']);
  expect(new Set(result.rows.map(item => item.id)).size).toBe(2);
  expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
});
for (const mode of ['page-close', 'page-identity']) test(`${mode}取消附件续页且不发布迟到结果或通知`, async () => {
  const result = await exercise(mode, 'attachment'); expect(attachmentLists(result)).toHaveLength(2);
  expect(result.aborted).toBe(true); expect(result.rows.some(item => item.label === '原图片2.png')).toBe(false);
  expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
});
