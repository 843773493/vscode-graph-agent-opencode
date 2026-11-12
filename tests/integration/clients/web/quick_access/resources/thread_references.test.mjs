import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/resources/thread_references.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'thread-references.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/resources/thread_references.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>Thread 引用合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('Thread 引用合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => { browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] }); run.addCleanup('Thread 引用合同 Chromium', () => browser.close()); });
afterAll(() => run.close());

async function exercise(name, mode) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(({ name, mode }) => import('/thread-references.mjs').then(module => module[name](mode)), { name, mode });
    expect(errors).toEqual([]);
    return result;
  } finally { await page.close(); }
}

test('main 与 child 使用精确 Session／Thread 身份，经不同 Gateway 返回显式 owner URI', async () => {
  const result = await exercise('threadReferenceContract', 'identities');
  const requests = result.requests.filter(item => item.path === '/api/gateway/resource-addresses/thread');
  expect(requests).toHaveLength(4);
  expect(requests.map(item => item.body)).toEqual([
    { local_gateway_id: `gateway_${'a'.repeat(32)}`, resource_gateway_id: `gateway_${'a'.repeat(32)}`, workspace_id: '019a0000-0000-7000-8000-000000000001', session_id: expect.any(String), thread_id: result.mainThreadId },
    { local_gateway_id: `gateway_${'a'.repeat(32)}`, resource_gateway_id: `gateway_${'a'.repeat(32)}`, workspace_id: '019a0000-0000-7000-8000-000000000001', session_id: expect.any(String), thread_id: result.childThreadId },
    { local_gateway_id: `gateway_${'a'.repeat(32)}`, resource_gateway_id: `gateway_${'b'.repeat(32)}`, workspace_id: '019a0000-0000-7000-8000-000000000001', session_id: expect.any(String), thread_id: result.mainThreadId },
    { local_gateway_id: `gateway_${'a'.repeat(32)}`, resource_gateway_id: `gateway_${'b'.repeat(32)}`, workspace_id: '019a0000-0000-7000-8000-000000000001', session_id: expect.any(String), thread_id: result.childThreadId },
  ]);
  expect(new Set(requests.map(item => item.body.session_id)).size).toBe(1);
  expect(requests.every(item => item.body.workspace_id === '019a0000-0000-7000-8000-000000000001')).toBe(true);
  expect(result.results.map(item => item.reference)).toEqual(result.expected);
  expect(result.results[0].reference).not.toBe(result.results[2].reference);
  expect(result.requests.some(item => item.path !== '/api/gateway/resource-addresses/thread')).toBe(false);
});

for (const [mode, reason] of [
  ['wrong-gateway', '归属不匹配'], ['wrong-scope', '归属不匹配'], ['wrong-kind', '不属于指定的 Session／Thread'],
  ['wrong-session', '不属于指定的 Session／Thread'], ['wrong-thread', '不属于指定的 Session／Thread'],
]) test(`${mode}回执 owner 不匹配时拒绝引用`, async () => {
  const result = await exercise('threadReferenceContract', mode);
  expect(result.results).toHaveLength(1); expect(result.results[0].reference).toBeUndefined();
  expect(result.results[0].error).toContain(reason);
  expect(result.requests).toHaveLength(1); expect(result.requests[0].path).toBe('/api/gateway/resource-addresses/thread');
});

test('地址响应期间 Gateway 身份改变时丢弃迟到结果', async () => {
  const result = await exercise('threadReferenceContract', 'identity-changed');
  expect(result.requests).toHaveLength(1); expect(result.error).toContain('Gateway 或访问凭据已改变');
  expect(result.outcome.value).toBeUndefined();
});

test('最近打开 Thread 沿既有“复制资源 URI”按钮，只请求地址且不打开 Thread', async () => {
  const result = await exercise('threadReferenceContract', 'quick-access-copy');
  expect(result.commandIds).toEqual(['boxteam.resource.copyUri']);
  expect(result.clipboard).toEqual([result.expected]); expect(result.errors).toEqual([]);
  expect(result.requests).toHaveLength(1); expect(result.requests[0].path).toBe('/api/gateway/resource-addresses/thread');
  expect(result.requests[0].body.thread_id).toBe(`thr_${'e'.repeat(32)}`);
  expect(result.buttonLabels).toContain('复制资源 URI');
});

test('活动 Thread 的 Composer 仍用原文件选择器，不解析 Thread 引用或触发读取', async () => {
  const result = await exercise('composerResourceSelectionContract', 'success');
  expect(result.canPickMany).toBe(true);
  expect(result.events.slice(0, 2)).toEqual(['capture:0', 'pick']);
  expect(result.requests.map(item => item.uri.split('/').at(-1))).toEqual(['alpha.txt', 'beta.txt']);
  expect(result.writes[0]).toEqual([[
    `boxteam://gateway_${'a'.repeat(32)}/workspace/019a0000-0000-7000-8000-000000000001/resources/files/root/alpha.txt`,
    `boxteam://gateway_${'a'.repeat(32)}/workspace/019a0000-0000-7000-8000-000000000001/resources/files/root/beta.txt`,
  ].join('\n')]);
  expect(result.writes[1]).toEqual([]);
  expect(result.outcome).toEqual({});
});
