import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/target_settings.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'target-settings.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/target_settings.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>默认 Agent 来源命令合同</title>', { headers: { 'content-type': 'text/html' } })
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('默认 Agent 命令合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('默认 Agent 命令合同 Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(mode) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    const result = await page.evaluate(mode => import('/target-settings.mjs').then(module => module.targetSettingsContract(mode)), mode);
    expect(errors).toEqual([]);
    expect(result.commandError).toBeUndefined();
    return result;
  } finally { await context.close(); }
}
const writes = result => result.requests.filter(item => item.method !== 'GET');

for (const mode of ['cancel-target', 'cancel-source']) test(`${mode} 原命令取消选择且零业务写入`, async () => {
  const result = await exercise(mode);
  expect(writes(result)).toEqual([]);
  expect(result.commandCalls).toEqual([]);
});

test('原命令选 owner 和来源后进入 Agent 定义表单，不编辑 default_agent 文本', async () => {
  const result = await exercise('open-form');
  expect(result.commandCalls).toHaveLength(1);
  expect(result.commandCalls[0]).toMatchObject({ id: 'boxteam.settings.agents', args: [{ gateway_id: result.ownerA.gateway_id,
    workspace_id: result.ownerA.workspace_id, source_key: result.sourceKey, version: 2, name: '原目标 A' }] });
  expect(writes(result)).toEqual([]);
  expect(result.requests.every(item => item.owner.gateway_id === result.ownerA.gateway_id
    && item.owner.workspace_id === result.ownerA.workspace_id)).toBe(true);
  expect(result.choices.map(item => item.title)).toEqual(['选择业务配置目标', '选择默认 Agent 配置来源']);
});

for (const mode of ['retry-pending', 'discard-pending', 'discard-conflict']) test(`${mode} 原来源状态命令保持 owner、candidate_ref 和 active CAS`, async () => {
  const result = await exercise(mode), [write] = writes(result);
  expect(result.requests.filter(item => item.method === 'GET').every(item => item.owner.gateway_id === result.ownerA.gateway_id
    && item.owner.workspace_id === result.ownerA.workspace_id)).toBe(true);
  expect(write).toMatchObject({ owner: result.ownerA, params: { candidate_ref: 'original+/=candidate' } });
  expect(write.path).toBe(`/api/v1/config/pending/${mode === 'retry-pending' ? 'retry' : 'discard'}`);
  if (mode !== 'retry-pending') expect(write.body).toEqual({ expected_active_revision: 4, expected_active_digest: 'active-old' });
  if (mode === 'discard-conflict') expect(result.notifications.at(-1)).toMatchObject({ kind: 'error',
    message: expect.stringContaining('request_id=pending-conflict') });
});

test('读取配置状态只展示原后端事实，不进行来源写入', async () => {
  const result = await exercise('inspect-pending');
  expect(writes(result)).toEqual([]);
  expect(result.choices.find(item => item.actions.every(action => action === undefined))).toMatchObject({ title: '默认 Agent 来源与应用状态',
    labels: expect.arrayContaining(['运行中 default_agent', '来源合并 default_agent', '原候选与应用']) });
});
