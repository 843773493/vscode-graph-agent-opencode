import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/session_actions.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'session-actions.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/session_actions.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>会话动作合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('会话动作合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('会话动作合同 Chromium', () => browser.close());
});
afterAll(() => run.close());
async function exercise(mode, name = 'sessionActionContract') {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(({ mode, name }) => import('/session-actions.mjs').then(module => module[name](mode)), { mode, name });
    expect(errors).toEqual([]);
    return result;
  } finally { await page.close(); }
}
const key = letter => JSON.stringify([`gateway_${letter.repeat(32)}`, '019a0000-0000-7000-8000-000000000001']);
for (const mode of ['origin', 'explicit', 'rename', 'mutation']) test(`${mode} 冻结明确 Session，经原导航 writer 结算，不误用 child 标题或旧路由`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([{ action: mode === 'rename' ? 'rename' : 'delete', sessionId: `ses_${'c'.repeat(32)}`,
    title: '权威会话标题', workspaceKey: key(mode === 'origin' ? 'b' : 'a') }]);
  expect(result.picks).toEqual([]); expect(result.errors).toEqual([]);
  expect(result.requests.map(item => [item.path, item.method])).toEqual([
    ['/api/gateway/workspaces', 'GET'], [`/api/v1/sessions/ses_${'c'.repeat(32)}`, 'GET'],
  ]);
  expect(result.requests[0].query).toBe('?check_health=false');
  expect(result.requests[1].gateway).toBe(`gateway_${(mode === 'origin' ? 'b' : 'a').repeat(32)}`);
});
test('没有发起 Session 时沿原有界 session Provider 只选择，接受不打开 Thread', async () => {
  const result = await exercise('pick-select');
  expect(result.picks).toHaveLength(1); expect(result.picks[0].value).toBe('session ');
  expect(result.picks[0].options.providerOptions).toEqual({ sessionSelection: true,
    searchOwners: [{ gatewayId: `gateway_${'a'.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }] });
  expect(result.calls[0].workspaceKey).toBe(key('a')); expect(result.errors).toEqual([]);
});
for (const mode of ['pick-cancel', 'scope-cancel']) test(`${mode} 取消不读取会话或写入业务`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.requests).toEqual([]); expect(result.errors).toEqual([]);
});
for (const [mode, reason] of [['pick-folder', '目录不能'], ['removed', '缺少唯一已登记'], ['deleted', '原会话已删除'],
  ['wrong-session', '归属不一致'], ['wrong-workspace', '归属不一致'], ['corrupt-origin', 'URI malformed'], ['offline', '明确启动或连接']]) test(`${mode} 明确拒绝，不改投同名目标`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain(reason);
  if (mode === 'offline') expect(result.requests.map(item => item.path)).toEqual(['/api/gateway/workspaces']);
});
for (const mode of ['late-token', 'late-gateway', 'late-view']) test(`${mode} 异步读取或挂载期间身份失效，拒绝尚未接受动作`, async () => {
  const result = await exercise(mode);
  expect(result.calls).toEqual([]); expect(result.errors).toHaveLength(1); expect(result.errors[0]).toContain('访问凭据已改变');
});
test('原 writer 已接受后不因凭据变化撤销或自动重放', async () => {
  const result = await exercise('accepted');
  expect(result.completedBeforeOwner).toBe(false); expect(result.calls).toHaveLength(1); expect(result.errors).toEqual([]);
});

for (const mode of ['session-selection', 'session-rename', 'session-delete']) test(`${mode} 沿真实Provider保持同名跨Gateway身份和原按钮`, async () => {
  const result = await exercise(mode, 'sessionPickerContract');
  expect(result.errors).toEqual([]); expect(result.requests).toHaveLength(1);
  expect(result.requests[0].queries.map(query => query.category)).toEqual(['catalog']);
  expect(result.candidates).toHaveLength(2);
  const target = { owner: { gatewayId: `gateway_${'b'.repeat(32)}`, workspaceId: '019a0000-0000-7000-8000-000000000001' }, sessionId: 'same-session' };
  expect(result.candidates[1].target).toEqual(target);
  if (mode === 'session-selection') {
    expect(result.commands).toEqual([]); expect(result.candidates.every(item => item.buttons.length === 0)).toBe(true);
    expect(result.folder).toEqual({ hasAccept: false, pickable: false });
  } else {
    expect(result.commands).toEqual([{ id: `boxteam.session.${mode === 'session-delete' ? 'delete' : 'rename'}`, target }]);
    expect(result.candidates[1].buttons).toEqual(['在侧边打开', '重命名会话', '永久删除会话']);
    expect(result.folder.hasAccept).toBe(true);
  }
});
