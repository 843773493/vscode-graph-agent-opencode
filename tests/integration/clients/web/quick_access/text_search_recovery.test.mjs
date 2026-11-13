import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/text_search_recovery.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'recovery.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/text_search_recovery.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  loader: { '.ttf': 'dataurl', '.png': 'dataurl', '.svg': 'dataurl' }, tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const path = new URL(request.url).pathname;
  if (path === '/recovery.mjs') return new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
  if (path === '/recovery.css') return new Response(Bun.file(bundle.replace(/\.mjs$/, '.css')), { headers: { 'content-type': 'text/css' } });
  return new Response('<!doctype html><title>原文本搜索恢复合同装具</title><link rel="stylesheet" href="/recovery.css">', { headers: { 'content-type': 'text/html' } });
} });
run.addCleanup('静态组件装具', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(name, ...args) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    let result;
    try { result = await page.evaluate(async ({ name, args }) => (await import('/recovery.mjs'))[name](...args), { name, args }); }
    catch (error) { throw new Error(`${error}\npageErrors: ${JSON.stringify(errors)}`); }
    expect(errors).toEqual([]);
    await writeFile(join(run.artifactsDir, `${name}-${args.join('-') || 'default'}.json`), JSON.stringify(result, null, 2));
    return result;
  } finally { await context.close(); }
}

for (const [mode, phase] of [['credential', 'initial'], ['credential', 'retry'], ['credential', 'settled'], ['gateway', 'settled']]) {
  test(`原%在${phase}的${mode}失效撤销旧候选／接受／重试及迟到结果，重开可用新代`, async () => {
    const result = await exercise('identityChange', mode, phase);
    expect(result.invalidated.labels).toEqual(['Gateway 或访问凭据已改变，请重新打开快速访问']);
    expect(result.invalidated.matches).toEqual([]); expect(result.invalidated.selected).toBe(0); expect(result.invalidated.active).toBe(0);
    expect(result.invalidated.buttons).toBe(0); expect(result.invalidated.busy).toBe(false);
    if (phase !== 'settled') expect(result.invalidated.aborted).toBe(true);
    expect(result.unchanged).toBe(true); expect(result.staleRequests).toBe(0); expect(result.opened).toBe(0);
    expect(result.reopened).toHaveLength(2); expect(result.reopened.every(resource => resource.endsWith('/new-identity.txt'))).toBe(true);
  }, 12000);
}

test('原%同Gateway切换活动工作区不中止查询或改投冻结范围', async () => {
  const result = await exercise('activeWorkspaceSwitch');
  expect(result.aborted).toBe(false); expect(result.matches).toHaveLength(2); expect(result.requests).toHaveLength(1);
  expect(result.requests[0].scope.owners).toEqual([{ gateway_id: 'gateway-a', workspace_id: 'workspace-a' }, { gateway_id: 'gateway-b', workspace_id: 'workspace-a' }]);
});

for (const state of ['failed', 'cancelled']) test(`原%将${state}与成功候选同时展示，按钮只重试失败根且双击不重复分发`, async () => {
  const result = await exercise('recovery', state);
  expect(result.before.matches).toHaveLength(1); expect(result.before.matches[0]).toContain('gateway-b');
  const status = result.before.labels.find(label => label?.includes('request_id:'));
  expect(status).toContain(state === 'cancelled' ? '文件搜索已取消：' : '文件搜索失败：');
  expect(status).toContain('code: execution_failed'); expect(status).toContain('error_owner: workspace_files');
  expect(status).toContain(state === 'cancelled' ? 'reason: request_cancelled' : 'reason: read_failed');
  expect(status).toContain('http_status: 403'); expect(status).toContain('retryable: true'); expect(status).toContain(result.selectedRoot);
  expect(status).toContain('[不执行](command:close) <b>正文</b>');
  expect(result.before.buttons).toEqual([1]); expect(result.busy).toBe(true);
  expect(result.requests).toHaveLength(2);
  const [initial, retry] = result.requests;
  expect(retry.scope.mode).toBe('resume'); expect(retry.scope.scope.owners).toEqual(initial.scope.owners);
  expect(retry.queries[0].file_targets).toEqual([initial.queries[0].file_targets[0]]);
  expect(retry.queries[0].limit).toBe(initial.queries[0].limit);
  expect(retry.continuations).toEqual([{ branch: { owner: initial.scope.owners[0], category: 'file', operation: 'content', resource_uri: result.selectedRoot } }]);
  expect(result.matches).toHaveLength(2); expect(result.matches.filter(resource => resource.endsWith('/recovered.txt'))).toHaveLength(1);
  expect(result.labels.some(label => label?.includes('request_id:'))).toBe(false);
});

test('原%多分支错误不合并状态；重试成功后仍保留另一不可重试的远端取消', async () => {
  const result = await exercise('recovery', 'failed', true);
  expect(result.before.matches).toEqual([]); expect(result.before.buttons).toEqual([1, 0]);
  expect(result.before.labels.filter(label => label?.includes('request_id:'))).toHaveLength(2);
  const remaining = result.labels.filter(label => label?.includes('request_id:'));
  expect(remaining).toHaveLength(1); expect(remaining[0]).toContain('文件搜索已取消：');
  expect(remaining[0]).toContain(result.successfulRoot); expect(remaining[0]).toContain('retryable: false');
  expect(result.matches).toHaveLength(1); expect(result.requests).toHaveLength(2);
});

for (const mode of ['hide', 'value']) test(`原%重试中${mode}取消实际请求，迟到匹配不改关闭浮层或新查询`, async () => {
  const result = await exercise('cancelRetry', mode);
  expect(result.aborted).toBe(true); expect(result.unchanged).toBe(true);
  expect(result.matches.some(resource => resource.endsWith('/late.txt'))).toBe(false);
  expect(result.requests).toHaveLength(mode === 'hide' ? 2 : 3);
  if (mode === 'value') expect(result.matches.every(resource => resource.endsWith('/current.txt'))).toBe(true);
});

for (const kind of ['http', 'incomplete', 'empty-failure']) test(`原%零匹配${kind}保持具名失败且无未处理拒绝`, async () => {
  const result = await exercise('requestFailure', kind);
  expect(result.requests).toBe(1); expect(result.labels.some(label => label?.includes('无匹配结果'))).toBe(false);
  if (kind === 'http') {
    expect(result.labels.join('\n')).toContain('http_status: 409'); expect(result.labels.join('\n')).toContain('source_changed');
    expect(result.labels.join('\n')).toContain('request_id: picker-request-0');
  } else if (kind === 'incomplete') {
    expect(result.labels.join('\n')).toContain('code: incomplete_stream'); expect(result.labels.join('\n')).toContain('request_id: picker-request-0');
    expect(result.labels.join('\n')).not.toContain('http_status:');
  } else expect(result.labels.filter(label => label?.includes('retryable: false'))).toHaveLength(2);
});

test('原%超过同步展示上限仍消费同一次异步错误，原30文件候选不重复', async () => {
  const result = await exercise('synchronousLimit');
  expect(result.requests).toBe(0); expect(result.files).toBe(30); expect(result.matches).toHaveLength(30);
  expect(new Set(result.matches).size).toBe(30);
  expect(result.labels.filter(label => label?.includes('sync-limit-request'))).toHaveLength(1);
});
