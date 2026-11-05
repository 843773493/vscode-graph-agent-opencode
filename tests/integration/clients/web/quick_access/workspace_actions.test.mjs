import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/workspace_actions.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'workspace-actions.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/workspace_actions.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  if (new URL(request.url).pathname === '/') return new Response('<!doctype html><title>工作区动作合同</title>', { headers: { 'content-type': 'text/html' } });
  return new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(mode) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/workspace-actions.mjs').then(module => module.workspaceControlContract(mode)), mode);
    expect(errors).toEqual([]);
    return result;
  } finally { await context.close(); }
}
const keys = ['gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'gateway_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb']
  .map(gateway => JSON.stringify([gateway, '019a0000-0000-7000-8000-000000000001']));

test('只读控制面发现沿原 cursor 续页，同 UUID 跨 Gateway 不合并，保留候选与活动身份', async () => {
  const result = await exercise('browse');
  expect(result.items.map(item => item.id)).toEqual(keys);
  expect(result.items.every(item => item.detail.includes('Gateway 导航/Needle 工作区'))).toBe(true);
  expect(result.requests).toHaveLength(2);
  expect(result.requests[0]).toEqual({ path: '/api/gateway/search/workspaces', method: 'GET', params: { query: 'Needle', include_folders: 'false', limit: '50' } });
  expect(result.requests[1].params).toEqual({ ...result.requests[0].params, cursor: 'original-control-cursor' });
  expect(result.retained).toBe(true); expect(result.activeRetained).toBe(true);
  expect(result.calls).toEqual([]); expect(result.errors).toEqual([]); expect(result.busy).toBe(false);
});

test('原查询改变取消网络，迟到控制面结果不能覆写新候选', async () => {
  const result = await exercise('late');
  expect(result.aborted).toBe(true); expect(result.retained).toBe(true);
  expect(result.items.map(item => item.id)).toEqual([keys[1]]);
  expect(result.requests.map(item => item.params.query)).toEqual(['slow', 'fast']);
  expect(result.calls).toEqual([]); expect(result.errors).toEqual([]);
});

test('发现失败保留原 request_id，仅显式重试原读查询', async () => {
  const result = await exercise('failure');
  expect(result.choices[0].failed.label).toContain('原控制面读取拒绝');
  expect(result.choices[0].failed.detail).toBe('request_id: discovery-rejected');
  expect(result.requests).toHaveLength(2); expect(result.requests[1]).toEqual(result.requests[0]);
  expect(result.items.map(item => item.id)).toEqual([keys[0]]); expect(result.calls).toEqual([]);
});

for (const mode of ['scope-cancel', 'scope-confirm', 'action-cancel', 'action-search']) test(`${mode} 只选择范围或取消，不启动、连接或激活`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.errors).toEqual([]);
  expect(result.requests.every(request => request.method === 'GET' && request.path === '/api/gateway/search/workspaces')).toBe(true);
  expect(result.shows).toHaveLength(1);
  if (mode.endsWith('cancel')) {
    expect(result.shows[0].value).toBe('workspace Needle');
    expect(result.shows[0].options.providerOptions.workspaceRestore).toEqual({ value: 'workspace Needle', id: keys[0] });
  } else {
    expect(result.shows[0].value).toBe('');
    expect(result.shows[0].options.providerOptions.searchOwners).toEqual([{
      gatewayId: mode === 'scope-confirm' ? JSON.parse(keys[1])[0] : JSON.parse(keys[0])[0], workspaceId: JSON.parse(keys[0])[1],
    }]);
  }
});

for (const action of ['activate', 'reconnect', 'start']) test(`明确 ${action} 经同一已注册命令和原视图 writer 边界`, async () => {
  const result = await exercise(`action-${action}`);
  expect(result.calls).toEqual([{ action, workspaceKey: keys[action === 'activate' ? 0 : 1] }]);
  expect(result.requests.map(item => item.path)).toEqual(['/api/gateway/search/workspaces', '/api/gateway/workspaces']);
  expect(result.listUrls).toEqual(['/api/gateway/workspaces?check_health=false']);
  expect(result.requests[1]).toEqual({ path: '/api/gateway/workspaces', method: 'GET', params: { check_health: 'false' } });
  expect(result.errors).toEqual([]);
});

for (const mode of ['command-origin', 'command-priority', 'command-current']) test(`${mode} 沿显式参数、发起编辑器和当前工作区顺序取目标`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([{ action: mode === 'command-origin' ? 'start' : 'activate', workspaceKey: keys[mode === 'command-origin' ? 1 : 0] }]);
  expect(result.errors).toEqual([]);
});

for (const kind of ['file', 'source', 'change']) test(`损坏的 ${kind} 发起身份保留原解析错误，不进入工作区选择`, async () => {
  const result = await exercise(`command-corrupt-${kind}`);
  expect(result.calls).toEqual([]); expect(result.choices).toEqual([]); expect(result.listUrls).toEqual([]);
  expect(result.errors).toHaveLength(1);
  expect(result.errors[0]).toContain(kind === 'change' ? '更改编辑器记录缺少稳定身份' : 'URI malformed');
});

for (const mode of ['command-empty', 'command-empty-editor']) test(`${mode} 确无发起与当前 owner 时才明确选择目标`, async () => {
  const result = await exercise(mode);
  expect(result.choices).toHaveLength(1); expect(result.choices[0].options.title).toBe('选择工作区动作目标');
  expect(result.calls).toEqual([{ action: 'start', workspaceKey: keys[1] }]);
  expect(result.listUrls).toEqual(['/api/gateway/workspaces?check_health=false']);
  expect(result.errors).toEqual([]);
});

test('取消无 owner 的明确选择不会产生控制面预检查或写动作', async () => {
  const result = await exercise('command-empty-cancel');
  expect(result.choices).toHaveLength(1); expect(result.calls).toEqual([]);
  expect(result.listUrls).toEqual([]); expect(result.errors).toEqual([]);
});

for (const mode of ['command-loading', 'command-control-error']) test(`${mode} 直接暴露控制镜像错误，不提供替代目标`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.choices).toEqual([]); expect(result.listUrls).toEqual([]);
  expect(result.errors).toHaveLength(1);
  expect(result.errors[0]).toContain(mode === 'command-loading' ? '工作区登记信息尚未加载' : '原控制面读取失败');
});

test('显式命令目标优先，控制面读取期间参数对象和活动工作区变化也不改投', async () => {
  const result = await exercise('command-mutation');
  expect(result.calls).toEqual([{ action: 'start', workspaceKey: keys[1] }]);
  expect(result.errors).toEqual([]);
});

test('已移除 owner 明确失败，不执行其它同名候选', async () => {
  const result = await exercise('removed');
  expect(result.calls).toEqual([]); expect(result.errors).toHaveLength(1);
  expect(result.errors[0]).toContain('缺少唯一已登记工作区');
});

for (const mode of ['action-identity', 'action-gateway']) test(`${mode} 废弃旧目标执行权`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.errors).toHaveLength(1);
  expect(result.errors[0]).toContain('身份');
  expect(result.requests).toHaveLength(1);
});
