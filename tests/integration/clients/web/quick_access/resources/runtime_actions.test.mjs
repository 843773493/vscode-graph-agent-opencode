import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/runtime_actions.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'runtime-actions.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/runtime_actions.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>资源动作合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('资源动作合同服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Chromium', () => browser.close()); });
afterAll(() => run.close());
async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/runtime-actions.mjs').then(module => module.runtimeActionContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const writes = result => result.requests.filter(item => item.method === 'POST');
const ownerB = `gateway_${'b'.repeat(32)}`;
for (const mode of ['mcp-open', 'mcp-options']) test(`${mode}复用原目录和命令，保持同名服务的精确owner`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toEqual([]); expect(writes(result)).toEqual([]);
  expect(result.rows.filter(row => row.id.endsWith('/mcp/Needle MCP'))).toHaveLength(2);
  expect(result.requests.filter(item => item.path === '/api/v1/mcp/servers').every(item => item.query === '?refresh=false')).toBe(true);
  if (mode === 'mcp-open') {
    expect(result.mcpOpened).toEqual([{ name: 'Needle MCP', target: { gateway_id: ownerB, workspace_id: '019a0000-0000-7000-8000-000000000001' } }]);
    expect(result.mcpOptions).toEqual([]);
  } else {
    expect(result.mcpOpened).toEqual([]);
    expect(JSON.parse(result.mcpOptions[0])[0]).toContain(ownerB);
  }
});
test('MCP目录归属错误只使该分支不可用，不能打开错误owner对象', async () => {
  const result = await exercise('mcp-mismatch');
  expect(result.rows.some(row => row.label.includes('MCP 管理响应不属于请求'))).toBe(true);
  expect(result.rows.filter(row => row.id.endsWith('/mcp/Needle MCP'))).toHaveLength(1);
  expect(result.mcpOpened).toEqual([]); expect(writes(result)).toEqual([]);
});
test('凭据失效后旧MCP候选不能打开，关闭picker取消其目录实际读取', async () => {
  const invalidated = await exercise('mcp-identity');
  expect(invalidated.errors).toHaveLength(1); expect(invalidated.mcpOpened).toEqual([]); expect(writes(invalidated)).toEqual([]);
  const closed = await exercise('mcp-pending-close');
  expect(closed.aborted).toBe(true); expect(closed.rows).toEqual([]); expect(writes(closed)).toEqual([]);
});
for (const mode of ['focus', 'focus-browser']) test(`${mode}默认回车只聚焦所选owner与资源，不打开管理菜单或写入`, async () => {
  const result = await exercise(mode);
  expect(result.errors).toEqual([]); expect(result.choices).toEqual([]); expect(writes(result)).toEqual([]);
  expect(result.focuses).toHaveLength(1);
  expect(result.focuses[0].owner.gatewayId).toBe(ownerB);
  expect(result.focuses[0].resource.resource_id).toBe(mode === 'focus' ? 'terminal_same' : 'browser_same');
  expect(result.focuses[0].resource.owner_revision).toBe(7);
  expect(result.requests).toHaveLength(result.discoveryCount);
});
test('同名终端和浏览器按owner保留；发现／逐键过滤／取消没有写入或正文读取', async () => {
  const result = await exercise('browse');
  expect(result.rows).toHaveLength(4); expect(new Set(result.rows.map(row => row.id)).size).toBe(4);
  expect(result.requests).toHaveLength(result.discoveryCount); expect(writes(result)).toEqual([]);
  expect(result.requests.filter(item => item.path === '/api/v1/resources')).toHaveLength(4);
  expect(result.requests.every(item => item.method === 'GET')).toBe(true);
});
test('真实Browser空页面标题使用资源ID，仍按owner分别发现', async () => {
  const result = await exercise('empty-title');
  expect(result.rows).toHaveLength(2); expect(new Set(result.rows.map(row => row.id)).size).toBe(2);
  expect(result.rows.every(row => row.label.includes('browser_same'))).toBe(true);
  expect(result.result.every(row => row.id.endsWith('/browser/browser_same'))).toBe(true);
  expect(result.requests).toHaveLength(result.discoveryCount); expect(writes(result)).toEqual([]);
});
test('有标题的资源也可按精确ID筛选，不把ID命中高亮投射到名称', async () => {
  const result = await exercise('id-search');
  expect(result.result).toHaveLength(2);
  expect(result.result.every(row => row.id.endsWith('/browser/browser_same') && row.label.includes('同名资源'))).toBe(true);
  expect(result.result.map(row => row.highlights?.label)).toEqual([undefined, undefined]);
  expect(result.requests).toHaveLength(result.discoveryCount); expect(writes(result)).toEqual([]);
});
test('来源及运行资源发现共用三并发，不按种类另开队列', async () => {
  const result = await exercise('parallel'); expect(result.maximum).toBe(3); expect(writes(result)).toEqual([]);
});
for (const mode of ['pending-close', 'pending-identity']) test(`${mode}取消运行资源实际读取并丢弃迟到清单`, async () => {
  const result = await exercise(mode); expect(result.aborted).toBe(true); expect(writes(result)).toEqual([]);
  expect(result.rows.map(item => item.id)).toEqual(mode === 'pending-close' ? [] : ['source-access-invalidated']);
});
test('错误owner运行清单拒绝为可操作目标，其余来源保留', async () => {
  const result = await exercise('mismatch'); expect(writes(result)).toEqual([]);
  expect(result.rows.some(row => row.label.includes('工作区或资源种类不匹配'))).toBe(true);
  expect(result.rows.filter(row => row.id.endsWith('terminal/terminal_same'))).toHaveLength(1);
});
for (const action of ['stop', 'promote', 'attach', 'detach', 'delete']) test(`${action}使用独立原端点、冻结revision及明确owner`, async () => {
  const result = await exercise(action), post = writes(result);
  expect(result.errors).toEqual([]); expect(post).toHaveLength(1); expect(post[0].gateway).toBe(ownerB);
  expect(post[0].path).toBe(`/api/v1/resources/terminal_same/${action}`); expect(post[0].body.expected_revision).toBe(7);
  expect(result.result.owner_revision).toBe(8); expect(result.notices.at(-1)).toBe('pending-closed');
  if (action === 'stop') expect(result.result.status).toBe('terminated');
  if (action === 'attach' || action === 'detach') expect(post[0].body.thread).toEqual({ session_id: 'ses_source', thread_id: 'thr_child' });
  else expect(post[0].body.thread).toBeUndefined();
  if (action === 'delete') expect(result.confirmations).toHaveLength(1);
});
test('picker attach固定发起child，不把导航A或来源Thread当接收者', async () => {
  const result = await exercise('picker-attach'), post = writes(result);
  expect(result.errors).toEqual([]); expect(post).toHaveLength(1); expect(post[0].gateway).toBe(ownerB);
  expect(post[0].body.thread).toEqual({ session_id: 'ses_source', thread_id: 'thr_child' });
});
test('资源picker二级取消返回原范围，零写入', async () => {
  const result = await exercise('action-cancel');
  expect(writes(result)).toEqual([]); expect(result.shows).toHaveLength(1);
  expect(result.shows[0].options.providerOptions.searchOwners).toHaveLength(2);
});
for (const mode of ['identity', 'delete-cancel', 'illegal-attach', 'source-mismatch']) test(`${mode}在副作用前拒绝或取消`, async () => {
  const result = await exercise(mode); expect(writes(result)).toEqual([]); expect(result.sourceCalls).toEqual([]);
  if (mode !== 'delete-cancel') expect(result.errors).toHaveLength(1);
});
test('等待路由期间改变参数与焦点，原资源和revision仍冻结', async () => {
  const result = await exercise('freeze'), post = writes(result);
  expect(result.errors).toEqual([]); expect(post).toHaveLength(1); expect(post[0].gateway).toBe(ownerB);
  expect(post[0].body.expected_revision).toBe(7);
});
for (const mode of ['conflict', 'unknown']) test(`${mode}保留原因、只回读不重放`, async () => {
  const result = await exercise(mode);
  expect(writes(result)).toHaveLength(1); expect(result.errors.join('')).toContain(mode === 'conflict' ? 'revision 冲突' : '写入后断开');
  expect(result.requests.filter(item => item.path === '/api/v1/resources')).toHaveLength(5);
  expect(result.requests.at(-1)).toMatchObject({ path: '/api/v1/resources', gateway: ownerB, method: 'GET' });
});
test('已受理后关闭picker不会取消或重放原写入', async () => {
  const result = await exercise('accepted-close');
  expect(result.errors).toEqual([]); expect(writes(result)).toHaveLength(1); expect(result.aborted).toBe(false);
  expect(result.notices.some(value => value.includes('已确认'))).toBe(true);
});
test('原组件临时句柄执行同一Action，命令不另发写入或建立镜像', async () => {
  const result = await exercise('source');
  expect(result.errors).toEqual([]); expect(writes(result)).toEqual([]);
  expect(result.sourceCalls).toEqual([{ action: 'stop', ref: undefined }]); expect(result.result.owner_revision).toBe(8);
});
