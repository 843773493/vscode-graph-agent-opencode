import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/tool_results.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'tool-results-contract.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/tool_results.fixture.mjs')], outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>工具结果合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('工具结果合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Chromium', () => browser.close()); });
afterAll(() => run.close());

async function exercise(mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/tool-results-contract.mjs').then(module => module.toolResultsContract(mode)), mode);
    expect(errors).toEqual([]);
    return result;
  } finally { await page.close(); }
}

const lists = result => result.requests.filter(item => item.path.endsWith('/tool-results'));
test('浏览只读取精确 child Thread 的工具结果 metadata，筛选不重查或读取正文', async () => {
  const result = await exercise('browse');
  expect(lists(result)).toHaveLength(1);
  expect(lists(result)[0]).toMatchObject({ gatewayId: result.owner.gatewayId, workspaceId: result.owner.workspaceId,
    method: 'GET', query: '?limit=100' });
  expect(lists(result)[0].path).toBe(`/api/v1/sessions/${result.sessionId}/threads/${result.threadId}/tool-results`);
  expect(result.rows.map(row => row.resourceId)).toEqual([result.items[0].item_id]);
  expect(result.rows[0].detail).toContain(result.threadId);
  expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
  expect(Object.keys(result.items[0]).sort()).toEqual(['item_id', 'status', 'tool_call_id', 'tool_name', 'turn_id']);
  expect(result.requests.every(item => item.method === 'GET')).toBe(true);
});

test('续页按钮只发送一次请求，并固定首屏返回的 view 与 revision', async () => {
  const result = await exercise('page'), requests = lists(result);
  expect(requests).toHaveLength(2);
  expect(new URLSearchParams(requests[1].query)).toEqual(new URLSearchParams('limit=100&after=2&view_id=view-child&view_revision=12'));
  expect(result.rows.map(row => row.resourceId)).toEqual(result.items.map(item => item.item_id));
  expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
});

for (const mode of ['cancel', 'page-cancel']) test(`${mode}实际取消请求并丢弃迟到的工具 metadata`, async () => {
  const result = await exercise(mode);
  expect(result.aborted).toBe(true); expect(result.opened).toEqual([]); expect(result.errors).toEqual([]);
  expect(result.rows.every(row => !row.selectable)).toBe(true);
});

test('目录响应属于其他 Thread 时拒绝发布可接受候选', async () => {
  const result = await exercise('wrong-thread');
  expect(lists(result)).toHaveLength(1); expect(result.opened).toEqual([]);
  expect(result.rows.some(row => row.label.includes('工具结果目录响应不属于发起 Thread'))).toBe(true);
  expect(result.rows.every(row => !row.selectable)).toBe(true);
});

test('接受冻结工具结果身份并沿原 Thread 传递 typed boxteamMessage', async () => {
  const result = await exercise('freeze');
  expect(result.errors).toEqual([]); expect(lists(result)).toHaveLength(1);
  expect(result.opened).toEqual([{ owner: { ...result.owner, workspaceKey: JSON.stringify([result.owner.gatewayId, result.owner.workspaceId]),
    sessionId: result.sessionId, threadId: result.threadId, title: result.threadId }, group: 7,
    options: { pinned: false, preserveFocus: false, boxteamMessage: result.items[0], viewState: { resourcesVisible: false } } }]);
});

test('冷 Thread 允许 null source view 的空目录，且没有明确 Thread 时不扫描', async () => {
  const empty = await exercise('empty'), noThread = await exercise('no-thread');
  expect(lists(empty)).toHaveLength(1); expect(empty.rows.every(row => !row.resourceId)).toBe(true);
  expect(lists(noThread)).toEqual([]); expect(noThread.opened).toEqual([]);
});
