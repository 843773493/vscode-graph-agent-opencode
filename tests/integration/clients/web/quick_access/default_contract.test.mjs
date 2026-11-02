import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/default_contract.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'default-contract.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/default_contract.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
// 本地静态装具避免每次经浏览器控制通道传输数 MB 的 base64 源码。
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const path = new URL(request.url).pathname;
  if (path === '/') return new Response('<!doctype html><title>Quick Access 合同装具</title>', { headers: { 'content-type': 'text/html' } });
  if (path === '/default-contract.mjs') return new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
  return new Response('Not Found', { status: 404 });
} });
run.addCleanup('合同静态服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(name) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    // 显式组件装具：浏览器运行真实 Provider，宿主及 Picker 事件替身不证明产品页面接线。
    const result = await page.evaluate(async name => (await import('/default-contract.mjs'))[name](), name);
    expect(errors).toEqual([]);
    return result;
  } finally { await context.close(); }
}

test('delegate 接收空查询及 URI 查询，换查询和释放取消旧调用且忽略迟到结果', async () => {
  const result = await exercise('delegateContract');
  expect(result.initial).toEqual(['']);
  expect(result.patterns).toEqual(['', 'https://example.test/file']);
  expect(result.cancelledPrevious).toBe(true);
  expect(result.picks).toEqual([{ label: 'delegate.txt', description: '调用方目录', buttons: [] }]);
  expect(result.afterLate).toEqual(['delegate.txt']);
  expect(result.cancelledOnDispose).toBe(true);
});

test('补充项仍经 filter，handleAccept 保留原接收者与后台标志，历史不混入 Thread', async () => {
  const result = await exercise('selectContract');
  expect(result.labels).toEqual(['附件来源', 'history.txt']);
  expect(result.backgroundKeptOpen).toBe(true);
  expect(result.foregroundClosed).toBe(true);
  expect(result.received).toEqual([{ label: '附件来源', kind: 'contextPick', background: true },
    { label: '附件来源', kind: 'contextPick', background: false }]);
});

test('原帮助的确认和精确前缀转接都保留归属与接收者，返回默认时只关闭本次递归帮助', async () => {
  const result = await exercise('helpContract');
  expect(result.values).toEqual(['', '_contract ']);
  expect(result.preserve).toBe(true);
  expect(result.sameOwner).toBe(true);
  expect(result.sameReceiver).toBe(true);
  expect(result.includeHelp).toEqual([false, true]);
  expect(result.sourceHelpUnchanged).toBe(true);
});

test('业务符号在调用全局 Provider 前明确拒绝，身份错误不改投，原生调用保留原符号服务', async () => {
  const result = await exercise('symbolContract');
  expect(result.scopedLabel).toContain('工作区符号未启用');
  expect(result.errorLabel).toContain('原 Gateway 身份失效');
  expect(result.scopedCalls).toEqual([]);
  expect(result.nativeCalls).toEqual(['needle']);
}, 10000);
