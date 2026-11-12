import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/locale_service.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'locale-service.mjs');
const sources = ['src/vs/workbench/services/localization/browser/localeService.ts',
  'src/vs/workbench/services/host/browser/browserHostService.ts'].map(path => join(root, 'upstream', path));
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path =>
  [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
fingerprints.sourceTransform = createHash('sha256').update(await readFile(join(root, 'scripts/sourceTransform.mjs'))).digest('hex');
await build({ stdin: { resolveDir: root, contents: `
  export { WebLocaleService } from './upstream/src/vs/workbench/services/localization/browser/localeService.js';
  export { BrowserHostService } from './upstream/src/vs/workbench/services/host/browser/browserHostService.js';` },
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))] });
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  return new URL(request.url).pathname === '/' ? new Response('<!doctype html><title>显示语言服务合同</title>')
    : new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } });
} });
run.addCleanup('显示语言服务合同HTTP', () => server.stop(true));
let browser;
const evidence = [];
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('显示语言服务合同Chromium', () => browser.close());
});
afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'locale-service-evidence.json'), JSON.stringify({
    boundary: '实际转换后WebLocaleService与BrowserHostService方法，真实浏览器Storage；仅对话框、展示提交与宿主依赖为具名替身，不替代完整页面或Electron验收',
    fingerprints, cases: evidence,
  }, null, 2) + '\n');
  await run.close();
});

async function exercise(mode) {
  const context = await browser.newContext(), page = await context.newPage();
  try {
    await page.goto(server.url.href);
    const result = await page.evaluate(async mode => {
      globalThis._VSCODE_NLS_LANGUAGE = 'en'; globalThis._VSCODE_NLS_MESSAGES = {};
      const { WebLocaleService, BrowserHostService } = await import('/locale-service.mjs');
      const key = 'vscode.nls.locale', calls = [];
      localStorage.setItem(key, 'en');
      const storageWrite = Storage.prototype.setItem;
      Storage.prototype.setItem = function(name, value) {
        if (name === key) {
          calls.push(['write', value]);
          if (mode === 'write-failed') throw new Error('storage denied /home/alice {0}');
        }
        return storageWrite.call(this, name, value);
      };
      globalThis.boxteamWorkbenchLifecycle = { async prepareToClose() {
        calls.push(['prepare']); throw new Error('draft commit failed /home/alice {0}');
      } };
      const host = { async restart() {
        calls.push(['restart']);
        if (mode === 'commit-failed') {
          const owner = { reload: () => BrowserHostService.prototype.reload.call({
            async handleExpectedShutdown() { calls.push(['shutdown']); },
          }) };
          await BrowserHostService.prototype.restart.call(owner);
        }
      } };
      const dialogs = { async confirm(options) { calls.push(['confirm', options.detail]); return { confirmed: mode !== 'cancel-reload' }; } };
      const service = new WebLocaleService(dialogs, host, { nameLong: 'BoxTeam' });
      let error;
      try {
        if (mode === 'clear') await service.clearLocalePreference();
        else await service.setLocale({ id: mode === 'same' ? 'en' : 'zh-cn', label: 'Chinese (Simplified)' });
      } catch (cause) { error = String(cause); }
      return { calls, stored: localStorage.getItem(key), language: globalThis._VSCODE_NLS_LANGUAGE, error };
    }, mode);
    evidence.push({ mode, result }); return result;
  } finally { await context.close(); }
}

test('选择当前语言不写入也不重载，取消即时重载只保存下一次语言', async () => {
  expect(await exercise('same')).toEqual({ calls: [], stored: 'en', language: 'en', error: undefined });
  const result = await exercise('cancel-reload');
  expect(result.stored).toBe('zh-cn'); expect(result.language).toBe('en');
  expect(result.calls.map(call => call[0])).toEqual(['write', 'confirm']);
});

test('真实语言偏好写入失败保留当前语言，原始错误向调用方传播且零重载', async () => {
  const result = await exercise('write-failed');
  expect(result.stored).toBe('en'); expect(result.language).toBe('en');
  expect(result.error).toContain('storage denied /home/alice {0}');
  expect(result.calls.map(call => call[0])).toEqual(['write']);
});

test('清除入口提示固定中文并调用原宿主，不查询浏览器语言', async () => {
  const result = await exercise('clear');
  expect(result.stored).toBeNull(); expect(result.error).toBeUndefined();
  expect(result.calls).toEqual([['confirm', 'Press the reload button to use Simplified Chinese.'], ['restart']]);
});

test('展示提交失败沿真实restart与reload返回原错，零shutdown且当前页面语言保持', async () => {
  const result = await exercise('commit-failed');
  expect(result.error).toContain('draft commit failed /home/alice {0}');
  expect(result.calls.map(call => call[0])).toEqual(['write', 'confirm', 'restart', 'prepare']);
  expect(result.language).toBe('en'); expect(result.stored).toBe('zh-cn');
});
