import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/bootstrap_localization.test.mjs').prepare();
const root = resolve('src/clients/workbench'), output = join(run.artifactsDir, 'client');
const entry = join(root, 'src/bootstrap.ts');
const buildId = 'a'.repeat(64), manifestPath = `/out/nls-assets-${buildId}.json`;
// 只替换业务 main 的求值接缝；被测 bootstrap、语言入口、词典及浏览器模块加载均使用实际实现。
const bundle = await build({ entryPoints: [entry], outdir: output, bundle: true, splitting: true,
  format: 'esm', platform: 'browser', target: 'es2024', metafile: true, logLevel: 'silent',
  define: { __BOXTEAM_NLS_MANIFEST__: JSON.stringify(manifestPath) }, plugins: [{
    name: 'bootstrap-main-observer', setup(build) {
      build.onResolve({ filter: /^\.\/main\.js$/ }, args => args.importer === entry ? { path: 'business-main', namespace: 'observer' } : undefined);
      build.onLoad({ filter: /.*/, namespace: 'observer' }, () => ({ loader: 'js', resolveDir: root,
        contents: `import { getLocale, t } from '../shared/core/i18n/index.js';
          globalThis.bootstrapContract = { locale: getLocale(), htmlLang: document.documentElement.lang,
            productText: t('plugins.installPackage'), nlsLanguage: globalThis._VSCODE_NLS_LANGUAGE,
            nlsPrepared: typeof globalThis._VSCODE_NLS_MESSAGES === 'object' };`,
      }));
    },
  }] });
const localeChunks = Object.fromEntries(['en', 'zh-cn'].map(locale => {
  const result = Object.entries(bundle.metafile.outputs).find(([, value]) => Object.keys(value.inputs).some(input => input.endsWith(`/i18n/messages/${locale}.json`)));
  if (!result) throw new Error(`实际 bootstrap 构建未输出 ${locale} catalog`);
  return [locale, `/client/${relative(output, resolve(result[0])).split('\\').join('/')}`];
}));
const nlsChinese = JSON.parse(await readFile(join(root, 'resources/localization/zh-cn-upstream-main.json'), 'utf8'));
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const pathname = new URL(request.url).pathname;
  if (pathname === '/') return new Response('<!doctype html><html><body><p id="startup-status"></p><pre id="startup-error"></pre><script type="module" src="/client/bootstrap.js"></script></body></html>', { headers: { 'Content-Type': 'text/html' } });
  if (pathname === manifestPath) return Response.json({ buildId, assets: { en: '/out/nls-en.json', 'zh-cn': '/out/nls-zh.json' } });
  if (pathname === '/out/nls-en.json') return Response.json({ buildId, contents: {}, extensions: {} });
  if (pathname === '/out/nls-zh.json') return Response.json({ buildId, contents: nlsChinese.contents, extensions: {} });
  if (pathname.startsWith('/client/')) return new Response(Bun.file(join(output, pathname.slice('/client/'.length))), { headers: { 'Content-Type': 'text/javascript' } });
  return new Response('Not Found', { status: 404 });
} });
run.addCleanup('bootstrap 合同静态服务', () => server.stop(true));
let browser;
const evidence = [];
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('bootstrap Chromium', () => browser.close());
});
afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'bootstrap-evidence.json'), JSON.stringify({ boundary: '实际 bootstrap 与浏览器模块加载；main 求值观察器、具名静态资源和 Storage 故障替身，不证明完整 Workbench 或 Gateway',
    source: entry, sha256: createHash('sha256').update(await readFile(entry)).digest('hex'), localeChunks, cases: evidence }, null, 2) + '\n');
  await run.close();
});

async function exercise(name, callback, prepare) {
  const context = await browser.newContext({ locale: 'en-US' });
  const page = await context.newPage(), requests = [], errors = [];
  page.setDefaultTimeout(5000);
  page.on('request', request => requests.push(new URL(request.url()).pathname));
  page.on('pageerror', error => errors.push(String(error)));
  try {
    await prepare?.(context, page);
    await page.goto(server.url.href);
    await callback(page, requests);
    expect(errors).toEqual([]);
    evidence.push({ name, requests, errors, exit: 'passed' });
  } catch (error) {
    evidence.push({ name, requests, errors, exit: 'failed', error: String(error) });
    throw error;
  } finally { await context.close(); }
}

for (const locale of ['zh-cn', 'en']) test(`${locale} bootstrap在业务首次求值前设置资源与HTML语言，仅加载当前全文`, async () => {
  await exercise(locale, async (page, requests) => {
    await page.waitForFunction(() => globalThis.bootstrapContract);
    expect(await page.evaluate(() => globalThis.bootstrapContract)).toEqual({ locale, htmlLang: locale === 'en' ? 'en' : 'zh-CN',
      productText: locale === 'en' ? 'Install Plugin Package' : '安装插件包', nlsLanguage: locale, nlsPrepared: true });
    expect(await page.evaluate(() => navigator.language)).toBe('en-US');
    expect(requests).toContain(localeChunks[locale]);
    expect(requests).toContain(manifestPath);
    expect(requests).not.toContain(localeChunks[locale === 'en' ? 'zh-cn' : 'en']);
    expect(requests.some(path => path.includes('bootstrap-assets') || path.includes('bootstrap-zh') || path.includes('bootstrap-en'))).toBe(false);
  }, locale === 'en' ? context => context.addInitScript(() => localStorage.setItem('vscode.nls.locale', 'en')) : undefined);
});

test('不支持的语言明确阻止业务求值，清除动作回到中文而不删其它存储', async () => {
  await exercise('unsupported', async page => {
    await page.waitForFunction(() => document.getElementById('startup-status')?.textContent === '不支持已保存的显示语言');
    expect(await page.evaluate(() => globalThis.bootstrapContract)).toBeUndefined();
    expect(await page.locator('#startup-error').textContent()).toContain('vscode.nls.locale=fr');
    await page.getByRole('button', { name: '清除显示语言偏好并使用简体中文' }).click();
    await page.waitForFunction(() => globalThis.bootstrapContract?.locale === 'zh-cn');
    expect(await page.evaluate(() => localStorage.getItem('vscode.nls.locale'))).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem('unrelated-draft'))).toBe('用户原文 {0}');
  }, context => context.addInitScript(() => {
    if (!sessionStorage.getItem('prepared')) {
      sessionStorage.setItem('prepared', '1'); localStorage.setItem('vscode.nls.locale', 'fr');
      localStorage.setItem('unrelated-draft', '用户原文 {0}');
    }
  }));
});

test('语言偏好读取被拒绝时展示原始错误，业务与全文加载均不开始', async () => {
  await exercise('storage-read-denied', async (page, requests) => {
    await page.waitForFunction(() => document.getElementById('startup-status')?.textContent === '无法读取显示语言偏好');
    expect(await page.locator('#startup-error').textContent()).toContain('storage denied /home/alice {0}');
    expect(await page.evaluate(() => globalThis.bootstrapContract)).toBeUndefined();
    expect(requests).not.toContain(localeChunks.en); expect(requests).not.toContain(localeChunks['zh-cn']);
    expect(requests).not.toContain(manifestPath);
  }, context => context.addInitScript(() => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = function(key) {
      if (key === 'vscode.nls.locale') throw new Error('storage denied /home/alice {0}');
      return original.call(this, key);
    };
  }));
});

for (const fault of ['batch', 'contents']) test(`${fault}资源错误沿真实bootstrap阻止业务求值`, async () => {
  await exercise(`invalid-${fault}`, async page => {
    await page.waitForFunction(() => document.getElementById('startup-status')?.textContent === 'Unable to load English resources');
    expect(await page.evaluate(() => globalThis.bootstrapContract)).toBeUndefined();
    expect(await page.locator('#startup-error').textContent()).toContain(fault === 'batch' ? 'build mismatch' : 'invalid contents');
  }, async (context, page) => {
    await context.addInitScript(() => localStorage.setItem('vscode.nls.locale', 'en'));
    await page.route('**/out/nls-en.json', route => route.fulfill({ json: fault === 'batch'
      ? { buildId: 'b'.repeat(64), contents: {}, extensions: {} } : { buildId, extensions: {} } }));
  });
});

for (const stage of ['catalog', 'nls']) test(`${stage}加载失败明确阻止业务求值，沿具名恢复动作重新启动`, async () => {
  let failed = false;
  await exercise(`failed-${stage}`, async page => {
    await page.waitForFunction(() => document.getElementById('startup-status')?.textContent === 'Unable to load English resources');
    expect(failed).toBe(true);
    expect(await page.evaluate(() => globalThis.bootstrapContract)).toBeUndefined();
    await page.getByRole('button').click();
    await page.waitForFunction(locale => globalThis.bootstrapContract?.locale === locale, stage === 'nls' ? 'zh-cn' : 'en');
  }, async (context, page) => {
    await context.addInitScript(() => { if (!sessionStorage.getItem('prepared')) { sessionStorage.setItem('prepared', '1'); localStorage.setItem('vscode.nls.locale', 'en'); } });
    await page.route(`**${stage === 'catalog' ? localeChunks.en : '/out/nls-en.json'}`, route => {
      if (!failed) { failed = true; return route.fulfill({ status: 503, body: 'locale asset unavailable' }); }
      return route.continue();
    });
  });
});
