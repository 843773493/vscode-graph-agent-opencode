import { beforeAll, afterAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/automation/actions.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'automation-actions.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/automation/actions.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>持续任务动作合同</title>') : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
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
  const page = await browser.newPage();
  const errors = []; page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/automation-actions.mjs').then(module => module.automationContract(mode)), mode);
    expect(errors).toEqual([]); return result;
  } finally { await page.close(); }
}
const writes = result => result.requests.filter(item => item.method !== 'GET');
test('候选保留跨Gateway同ID，聚焦与取消零写入，Goal不冒用automation控制', async () => {
  const result = await exercise('browse');
  expect(result.rows).toHaveLength(2); expect(result.rows[0].id).not.toBe(result.rows[1].id);
  expect(result.requests).toEqual([]); expect(result.goalActions).toEqual(['details', 'history']);
});
test('身份失效取消原HTTP读取，迟到结果不能覆盖明确的失效提示', async () => {
  const result = await exercise('pending-identity');
  expect(result.aborted).toBe(true); expect(writes(result)).toEqual([]);
  expect(result.rows).toEqual([{ id: 'task-access-invalidated', label: 'Gateway 或访问凭据已改变，请重新查找任务' }]);
});
test('关闭尚未接受的浮层取消实际HTTP读取，迟到结果不发布也不写入', async () => {
  const result = await exercise('pending-close');
  expect(result.aborted).toBe(true); expect(result.rows).toEqual([]); expect(writes(result)).toEqual([]);
});
test('原DTO拒绝不支持的组件版本，保留其它Gateway的合法候选', async () => {
  const result = await exercise('unsupported-edit');
  expect(result.rows[0].id).toBe('task-read-failed-0');
  expect(result.rows[0].label).toContain('持久任务快照无效');
  expect(result.rows[1].label).toBe('Needle 自动化'); expect(writes(result)).toEqual([]);
});
for (const action of ['pause', 'resume', 'check', 'checkOnly', 'terminate']) test(`明确${action}只写一次原revision并读回完整权威快照`, async () => {
  const result = await exercise(action), write = writes(result);
  expect(result.errors).toEqual([]); expect(result.result).toBe(true); expect(write).toHaveLength(1);
  expect(write[0].body.expected_revision).toBe(7);
  expect(result.requests.at(-1).path).toBe('/api/v1/persistent-tasks');
  expect(result.requests.at(-1).method).toBe('GET');
  if (action === 'check' || action === 'checkOnly') { expect(write[0].body.check_only).toBe(action === 'checkOnly'); expect(write[0].body.idempotency_key).toBeTruthy(); }
  else expect(write[0].body.action).toBe(action);
  expect(result.opened).toEqual([]);
});
test('冲突不覆盖revision重试，保留原错误并回读', async () => {
  const result = await exercise('conflict');
  expect(writes(result)).toHaveLength(1); expect(writes(result)[0].body.expected_revision).toBe(7);
  expect(result.requests.at(-1).method).toBe('GET');
  expect(result.errors.join('')).toContain('revision'); expect(result.result).toBe(false);
});
for (const mode of ['owner-cancel', 'action-cancel', 'terminate-cancel', 'identity']) test(`${mode}在未接受步骤零写入`, async () => {
  const result = await exercise(mode); expect(writes(result)).toEqual([]);
  if (mode === 'owner-cancel') expect(result.requests).toEqual([]);
  if (mode === 'identity') expect(result.errors.join('')).toContain('身份');
});
for (const mode of ['accepted-close', 'accepted-terminate-close']) test(`${mode}关闭浮层和切换A/B不撤销、改投或重放已受理动作`, async () => {
  const result = await exercise(mode); expect(result.result).toBe(true);
  expect(writes(result)).toHaveLength(1); expect(result.aborted).toBe(false);
  expect(writes(result)[0].body).toEqual({ expected_revision: 7, action: mode === 'accepted-close' ? 'pause' : 'terminate' });
  expect(writes(result)[0].owner).toBe(`gateway_${'a'.repeat(32)}`);
  expect(result.requests.at(-1)).toMatchObject({ method: 'GET', path: '/api/v1/persistent-tasks', owner: `gateway_${'a'.repeat(32)}` });
});
for (const mode of ['freeze', 'origin']) test(`${mode}固定显式参数或发起编辑器owner`, async () => {
  const result = await exercise(mode), write = writes(result);
  expect(result.errors).toEqual([]); expect(write).toHaveLength(1); expect(write[0].body.expected_revision).toBe(7);
  expect(write[0].owner).toBe(`gateway_${(mode === 'freeze' ? 'a' : 'b').repeat(32)}`);
});
for (const action of ['details', 'history', 'edit']) test(`${action}只打开精确任务的原产品宿主`, async () => {
  const result = await exercise(action);
  expect(writes(result)).toEqual([]); expect(result.opened).toHaveLength(1);
  expect(result.opened[0].options.automationAction).toBe(action); expect(result.opened[0].task.revision).toBe(7);
});
