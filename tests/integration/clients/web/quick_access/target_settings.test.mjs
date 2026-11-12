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
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>目标配置来源合同</title>', { headers: { 'content-type': 'text/html' } })
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('设置合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('设置合同 Chromium', () => browser.close());
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
    if (!['identity', 'gateway', 'offline', 'duplicate'].includes(mode)) expect(result.commandError).toBeUndefined();
    expect(result.requests.filter(item => item.path.startsWith('/api/v1/'))
      .every(item => item.owner.gateway_id === result.target.gateway_id && item.owner.workspace_id === result.target.workspace_id)).toBe(true);
    return result;
  } finally { await context.close(); }
}
const writes = result => result.requests.filter(item => item.path.startsWith('/api/v1/') && item.method !== 'GET');
const puts = result => result.requests.filter(item => item.method === 'PUT');

for (const mode of ['cancel-target', 'cancel-source', 'cancel-input', 'cancel-confirm', 'readonly']) test(`${mode} 原命令取消或只读来源零保存`, async () => {
  const result = await exercise(mode);
  expect(puts(result)).toEqual([]);
  expect(result.requests.filter(item => item.path.includes('/pending/'))).toEqual([]);
  expect(result.document.jsonc).toBe(result.original);
  if (mode === 'readonly') {
    const menu = result.choices.find(item => item.title === '目标工作区默认 Agent 来源');
    expect(menu.actions.includes('set') || menu.actions.includes('remove')).toBe(false);
    expect(menu.prompt).toContain('只读');
  }
  if (mode === 'cancel-confirm') expect(writes(result).map(item => item.method)).toEqual(['POST']);
  else expect(writes(result)).toEqual([]);
});

for (const mode of ['save', 'focus', 'delete', 'pending']) test(`${mode} AST单键编辑、完整CAS和同意图保存，导航不改投`, async () => {
  const result = await exercise(mode), [preflight, saved] = writes(result);
  expect(writes(result).map(item => item.method)).toEqual(['POST', 'PUT']);
  expect(preflight.body).toEqual(saved.body);
  expect(preflight.syntaxErrors).toEqual([]);
  expect(saved.body).toMatchObject({ ...result.target, source_key: result.sourceKey, base_file_digest: 'file-old',
    base_layer_revision: 7, base_layer_digest: 'layer-old', expected_active_revision: 4, expected_active_digest: 'active-old' });
  expect(saved.body.idempotency_key).toBeTruthy();
  expect(saved.body.jsonc).toContain('// 顶部注释\r\n');
  expect(saved.body.jsonc).toContain('"ignored_paths": ["cache/"], // 保留此注释');
  expect(saved.body.jsonc).toContain('"ui": {}');
  expect(saved.body.jsonc.includes('"default_agent"')).toBe(mode !== 'delete');
  if (mode !== 'delete') expect(saved.body.jsonc).toContain('"default_agent": "reviewer"');
  expect(result.notifications.at(-1).message).toContain('source_saved=true · layer_imported=true');
  if (mode === 'pending') {
    expect(result.document.effective.default_agent).toBe('default');
    expect(result.notifications.at(-1).message).toContain('active=4 · pending=5 · restart_required=true');
  }
});

test('默认配置目标是页面原生Gateway，不取活动远端工作区', async () => {
  const result = await exercise('save');
  expect(result.choices.find(item => item.title === '选择业务配置目标').labels).toEqual(['原目标 A']);
  expect(result.requests.some(item => item.path === '/api/gateway/management/gateways')).toBe(false);
});

for (const mode of ['delete-first', 'delete-last', 'delete-only', 'insert', 'insert-inline']) test(`${mode} 原AST删除或插入只改所选字段，注释和其它值保留`, async () => {
  const result = await exercise(mode), [preflight, saved] = writes(result);
  expect(writes(result).map(item => item.method)).toEqual(['POST', 'PUT']);
  expect(preflight.syntaxErrors).toEqual([]);
  expect(preflight.body).toEqual(saved.body);
  expect(preflight.payload.default_agent).toBe(mode.startsWith('delete') ? undefined : 'reviewer');
  for (const comment of result.original.matchAll(/\/\/[^\r\n]*|\/\*.*?\*\//g)) expect(saved.body.jsonc).toContain(comment[0]);
  if (mode !== 'delete-only') expect(saved.body.jsonc).toContain(mode === 'insert-inline' ? '"ignored_paths":["cache/"]' : '"ignored_paths": ["cache/"]');
});

test('删除本层不存在的覆盖保持原文，零预检与保存', async () => {
  const result = await exercise('delete-absent');
  expect(writes(result)).toEqual([]);
  expect(result.document.jsonc).toBe(result.original);
});

test('重复的default_agent明确拒绝编辑，保留原件且零写入', async () => {
  const result = await exercise('duplicate');
  expect(result.commandError).toContain('重复 default_agent');
  expect(writes(result)).toEqual([]);
  expect(result.document.jsonc).toBe(result.original);
});

test('409保留输入并回读原目标，仅用户明确重新预检才生成新意图', async () => {
  const result = await exercise('conflict'), saved = puts(result), preflights = result.requests.filter(item => item.path.endsWith('/preflight'));
  expect(saved).toHaveLength(2); expect(preflights).toHaveLength(2);
  expect(result.choices.find(item => item.title === '来源修改未完成：保留本次输入')).toMatchObject({ puts: 1, placeHolder: expect.stringContaining('保留输入 reviewer'), prompt: expect.stringContaining('request_id=original-conflict') });
  expect(result.requests.find(item => item.params.idempotency_key).params.idempotency_key).toBe(saved[0].body.idempotency_key);
  expect(saved[1].body.idempotency_key).not.toBe(saved[0].body.idempotency_key);
  expect(saved[1].body).toMatchObject({ base_file_digest: 'file-latest', base_layer_revision: 8, base_layer_digest: 'layer-latest', expected_active_revision: 5, expected_active_digest: 'active-latest' });
  expect(saved[1].body.jsonc).toContain('// 并发作者注释');
  expect(saved[1].body).toEqual(preflights[1].body);
});

test('409取消重新预检不自动覆盖最新来源', async () => {
  const result = await exercise('conflict-cancel');
  expect(puts(result)).toHaveLength(1);
  expect(result.document.jsonc).toContain('// 并发作者注释');
  expect(result.document.jsonc).toContain('"default_agent": "default"');
});

test('保存回执未知只查原幂等键，期间不开放新写入，零PUT重放', async () => {
  const result = await exercise('unknown'), [saved] = puts(result), queries = result.requests.filter(item => item.params.idempotency_key);
  expect(puts(result)).toHaveLength(1); expect(queries).toHaveLength(3);
  expect(queries.every(item => item.params.idempotency_key === saved.body.idempotency_key)).toBe(true);
  const menus = result.choices.filter(item => item.title === '目标工作区默认 Agent 来源');
  expect(menus[1].actions).not.toContain('set'); expect(menus[1].actions).not.toContain('remove');
  expect(result.document.edit.idempotency_key).toBe(saved.body.idempotency_key);
});

for (const mode of ['retry', 'discard', 'discard-conflict']) test(`${mode} 操作原pending身份并重新获取完整来源，不调用重启／resolve`, async () => {
  const result = await exercise(mode), [write] = writes(result);
  expect(writes(result)).toHaveLength(1);
  expect(write.path).toBe(`/api/v1/config/pending/${mode === 'retry' ? 'retry' : 'discard'}`);
  expect(write.params.candidate_ref).toBe('original+/=candidate');
  if (mode !== 'retry') expect(write.body).toEqual({ expected_active_revision: 4, expected_active_digest: 'active-old' });
  expect(result.requests.at(-1).method).toBe('GET');
  if (mode === 'discard-conflict') expect(result.notifications.at(-1)).toMatchObject({ kind: 'error', message: expect.stringContaining('request_id=original-conflict') });
});

for (const mode of ['identity', 'gateway', 'offline']) test(`${mode} 原目标失效拒绝执行，不请求替代工作区`, async () => {
  const result = await exercise(mode);
  expect(result.commandError).toContain(mode === 'offline' ? '离线' : '访问凭据');
  expect(writes(result)).toEqual([]);
  if (mode !== 'offline') expect(result.inputs[0].cancelled).toBe(true);
});
