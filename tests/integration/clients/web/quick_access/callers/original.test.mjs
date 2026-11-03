import { afterAll, beforeAll, expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/callers/original.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'original.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/callers/original.fixture.mjs')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  loader: { '.ttf': 'dataurl', '.png': 'dataurl', '.svg': 'dataurl' },
  tsconfig: join(workbench, 'tsconfig.jsonc'),
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))], logLevel: 'silent' });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  if (new URL(request.url).pathname === '/original.mjs') return new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
  if (new URL(request.url).pathname === '/original.css') return new Response(Bun.file(bundle.replace(/\.mjs$/, '.css')), { headers: { 'content-type': 'text/css' } });
  return new Response('<!doctype html><title>原调用方合同装具</title><link rel="stylesheet" href="/original.css">', { headers: { 'content-type': 'text/html' } });
} });
run.addCleanup('静态组件装具', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(() => run.close());

async function exercise(name, argument) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage(), errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(server.url.href);
    const result = await page.evaluate(async ({ name, argument }) => (await import('/original.mjs'))[name](argument), { name, argument });
    expect(errors).toEqual([]);
    return result;
  } finally { await context.close(); }
}

test('原空文件编辑器经真实 Controller／Provider 调用 delegate，隐藏、切换和释放清理原 cache', async () => {
  const result = await exercise('emptyEditorContract');
  expect(result.patterns).toEqual(['', 'needle', '', '']);
  expect(result.roots).toHaveLength(1);
  expect(result.roots[0]).toContain('boxteam-file://');
  expect(result.filesOnly).toBe(true);
  expect(result.sideButton).toBe(false);
  expect(result.labels).toEqual(['needle.txt']);
  expect(result.description).toBe('dir');
  expect(result.buttons).toEqual([]);
  expect(result.cancelledPrevious).toBe(true);
  expect(result.cacheKeys.every(key => key.startsWith('emptyFileEditorSearch-'))).toBe(true);
  expect(result.cacheKeys[0]).toBe(result.cacheKeys[1]);
  expect(new Set(result.cacheKeys).size).toBe(3);
  expect(result.hiddenCleared).toEqual([result.cacheKeys[0]]);
  expect(result.hiddenCancelled).toBe(true);
  expect(result.cleared).toEqual([result.cacheKeys[0], result.cacheKeys[2], result.cacheKeys[3]]);
  expect(result.switchedCancelled).toBe(true);
  expect(result.disposedCancelled).toBe(true);
  expect(result.input).toBe('');
});

for (const mode of ['value', 'back', 'cancel', 'child', 'prefix']) {
  test(`原 Chat 附加调用方保留 filter／前缀与 widget，二级动作 ${mode}`, async () => {
    const result = await exercise('chatCallerContract', mode);
    expect(result.prefixes).toEqual(['', '#', '@']);
    expect(result.visible).toContain('原附件');
    expect(result.visible).toContain('history.txt');
    expect(result.visible).not.toContain('hidden.txt');
    expect(result.otherAttachments).toEqual([]);
    if (mode === 'prefix') {
      expect(result.blocked).toEqual([]);
      expect(result.attachments).toEqual([]);
    } else {
      expect(result.sameReceiver).toBe(true);
      expect(result.shows).toBe(mode === 'back' ? 2 : 1);
      expect(result.configDisposed).toBe(mode === 'value' ? 0 : 1);
      expect(result.attachments.map(item => item.id)).toEqual(mode === 'cancel' ? [] : ['native-chat-value']);
    }
  });
}
