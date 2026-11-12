import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/mcp_localization.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'mcp-localization.mjs');
await build({ stdin: { resolveDir: resolve('.'), contents: `
import { setLocaleResources } from './src/clients/shared/core/i18n/index.js';
import en from './src/clients/shared/core/i18n/messages/en.json';
import zh from './src/clients/shared/core/i18n/messages/zh-cn.json';
const locale = document.documentElement.lang;
setLocaleResources(locale, locale === 'en' ? en : zh);
export const { WorkspaceMcpServer } = await import('./src/clients/workbench/src/mcp/server.js');
export { CancellationToken } from './src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
export { renderMarkdown } from './src/clients/workbench/upstream/src/vs/base/browser/markdownRenderer.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))] });
const evidence = [];
const server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
  const url = new URL(request.url);
  return url.pathname === '/' ? new Response(`<!doctype html><html lang="${url.searchParams.get('locale') === 'en' ? 'en' : 'zh-cn'}"><title>MCP</title></html>`,
    { headers: { 'content-type': 'text/html; charset=utf-8' } })
    : new Response(Bun.file(bundle), { headers: { 'content-type': 'text/javascript' } });
} });
run.addCleanup('MCP 文案合同服务', () => server.stop(true));
let browser;
beforeAll(async () => {
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('MCP 文案合同 Chromium', () => browser.close());
});
afterAll(async () => {
  const paths = ['src/mcp/server.ts', 'src/mcp/catalog.ts', '../shared/core/i18n/messages/en.json', '../shared/core/i18n/messages/zh-cn.json'];
  const fingerprints = Object.fromEntries(await Promise.all(paths.map(async path =>
    [path, createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
  await writeFile(join(run.artifactsDir, 'mcp-localization.json'), JSON.stringify({
    boundary: '真实 WorkspaceMcpServer 和 MarkdownString；仅目录数据及动作 owner 是具名装具，不代替 Gateway、OAuth 或执行验收',
    fingerprints, cases: evidence,
  }, null, 2) + '\n');
  await run.close();
});

for (const locale of ['zh-cn', 'en']) test(`${locale} 首次求值及详情使用启动语言，原工具和机器操作不变`, async () => {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', cause => errors.push(String(cause)));
  try {
    await page.goto(`${server.url}?locale=${locale}`);
    const result = await page.evaluate(async () => {
      const { WorkspaceMcpServer, CancellationToken, renderMarkdown } = await import('/mcp-localization.mjs');
      const data = { server_id: '用户服务 {0}', enabled: true, status: 'ready', transport: 'streamable_http',
        tools: [{ remote_name: '原工具 {0}', description: "Cannot find module '/home/alice/中文.js' {0}" }] };
      const original = JSON.stringify(data), calls = [];
      let operation;
      const catalog = { key: '原目录', label: '用户工作区 {0}', isBusy: false,
        target: { gateway_id: 'gateway-original', workspace_id: 'workspace-original' },
        snapshot: { catalog: { catalog_revision: 'revision-original', catalog_generation: 1 }, operations: [] },
        server: () => data, operation: () => operation,
        async act(name, action) { calls.push([name, action]); } };
      const model = new WorkspaceMcpServer(catalog, data.server_id, async () => {}, () => ({ dispose() {} }));
      const ready = model.statusLabel, description = model.description;
      const readme = await model.getReadme(CancellationToken.None);
      const rendered = renderMarkdown({ value: readme });
      const readmeText = rendered.element.textContent;
      rendered.dispose();
      await model.stop();
      data.status = 'unavailable'; await model.start();
      operation = { status: 'waiting_user' }; await model.stop(); await model.authorize();
      data.status = 'ready'; operation = undefined;
      const unchanged = JSON.stringify(data) === original;
      catalog.snapshot.error = '原始服务错误 /home/alice {0}';
      const rawError = model.statusLabel;
      delete catalog.snapshot.error;
      data.status = 'unknown-original-status';
      return { language: document.documentElement.lang, ready, description, readme, readmeText, calls, unchanged, rawError, unknown: model.statusLabel };
    });
    evidence.push({ locale, result, errors });
    expect(errors).toEqual([]);
    expect(result.language).toBe(locale);
    expect(result.ready).toBe(locale === 'en' ? 'Connected' : '连接可用');
    expect(result.description).toContain(locale === 'en' ? 'Known tools: 1' : '1 个已知工具');
    expect(result.readme).toContain(locale === 'en' ? '## MCP Service' : '## MCP 服务');
    expect(result.readme).toContain(locale === 'en' ? 'Target Tool Summary' : '目标工具摘要');
    expect(result.readmeText).toContain('用户服务 {0}');
    expect(result.readmeText).toContain('/home/alice/中文.js');
    expect(result.readmeText).toContain('{0}');
    expect(result.calls).toEqual(['disconnect', 'connect', 'cancel', 'authorize'].map(action => ['用户服务 {0}', action]));
    expect(result.unchanged).toBe(true);
    expect(result.rawError).toBe('原始服务错误 /home/alice {0}');
    expect(result.unknown).toBe('unknown-original-status');
  } finally { await page.close(); }
});
