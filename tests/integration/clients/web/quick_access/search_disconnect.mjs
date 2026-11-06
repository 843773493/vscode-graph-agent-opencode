import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';

const [url, suffix, artifacts] = process.argv.slice(2);
await build({ stdin: { contents: `
  import { streamFederatedSearch } from './src/clients/web/src/api/stream/federatedSearch.ts';
  import { FederatedSearchRequest } from './src/clients/web/src/types/protocol_generated/boxteam/search/v1/public.ts';
  export async function exercise(suffix) {
    const originalFetch = globalThis.fetch, events = [];
    const release = () => originalFetch('/release/browser');
    let releasing;
    // 只观察真实响应头；不伪造响应，不读取副本，不替换正文。
    globalThis.fetch = async (...args) => {
      const response = await originalFetch(...args);
      if (response.headers.has('X-Request-ID') && suffix.startsWith('headers:')) releasing = release();
      return response;
    };
    try {
      await streamFederatedSearch(0, FederatedSearchRequest.create({
        scope: { mode: 'origin', origin: { gateway_id: 'gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', workspace_id: '019a0000-0000-7000-8000-000000000001' } },
        queries: [{ category: 'catalog', operation: 'name', contract_version: 1, match_mode: 'fuzzy', query: 'browser:' + suffix, limit: 1 }],
      }), { signal: new AbortController().signal, onEvent(event) { events.push(event.type); releasing = release(); } });
      throw new Error('截断或无终态响应不应成功');
    } catch (error) {
      await releasing;
      return { events, error: { name: error.name, code: error.code, requestId: error.requestId, message: error.message,
        causeName: error.cause?.name, causeMessage: error.cause?.message } };
    } finally { globalThis.fetch = originalFetch; }
  }`, resolveDir: resolve('.'), sourcefile: 'search-disconnect-fixture.ts', loader: 'ts' },
  outfile: join(artifacts, 'fixture.mjs'), bundle: true, format: 'esm', platform: 'browser', target: 'es2024', logLevel: 'silent' });

const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage(), pageErrors = [];
  page.on('pageerror', error => pageErrors.push(String(error)));
  await page.goto(url);
  const result = await page.evaluate(async suffix => (await import('/fixture.mjs')).exercise(suffix), suffix);
  await writeFile(join(artifacts, 'browser.json'), JSON.stringify({ ...result, pageErrors }, null, 2));
  assert.equal(result.error.requestId, 'browser');
} finally { await browser.close(); }
