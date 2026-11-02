import { describe, expect, test, spyOn } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/search_service.test.mjs').prepare();
const bundle = join(run.artifactsDir, 'search.fixture.mjs');
// Bun 的直接 TS loader 不支持这里的上游 legacy accessor decorators；与生产共用 tsconfig 编译。
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/search.fixture.ts')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  tsconfig: resolve('src/clients/workbench/tsconfig.jsonc'), logLevel: 'silent' });
const { URI, gateway, fixture, model, WorkspaceSearchService, WorkspaceSearchProvider,
  workspaceFileUri, workspaceFileSource, textMatch, CancellationTokenSource, resourceInQuery } = await import(pathToFileURL(bundle).href);
const ownerA = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const ownerB = { gatewayId: 'gateway-b', workspaceId: 'workspace-a' };
const root = owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
const uri = (owner, name = '') => workspaceFileUri(owner, root(owner) + (name ? '/' + name : ''), root(owner));
const query = (extra = {}) => ({ type: 2, folderQueries: [{ folder: uri(ownerA) }], contentPattern: { pattern: 'needle', isCaseSensitive: true }, ...extra });
const hit = (path, column = 1) => ({ uri: path, line: 1, column, end_line: 1, end_column: column + 6,
  utf16_column: column, utf16_end_column: column + 6, snippet_start_utf16_column: 1, text: 'needle', snippet_start_column: 1, truncated: false });
const page = (path, hits = [], extra = {}) => ({ node: { uri: path }, entries: [], matches: hits, contexts: [], truncated: false, ...extra });

async function withTransport(callback, requestHandler = async (_path, options) => page(JSON.parse(options.body).uri)) {
  const routes = spyOn(gateway, 'resolveWorkspace').mockImplementation(async owner => 'route-' + owner.gatewayId);
  const requests = spyOn(gateway, 'request').mockImplementation(requestHandler);
  try { return await callback({ routes, requests }); }
  finally { routes.mockRestore(); requests.mockRestore(); }
}

describe('业务 SearchService 真实注册及完整身份隔离（Gateway 传输为显式替身）', () => {
  test('respect 交目标 owner 处理；已打开模型不额外读取磁盘规则且保留 owner 过滤', async () => {
    const a = model(uri(ownerA, 'ignored.txt'), 'needle unsaved'), b = model(uri(ownerB, 'ignored.txt'), 'needle foreign');
    const f = fixture([a, b]);
    try {
      await withTransport(async ({ requests }) => {
        const options = query({ folderQueries: [{ folder: uri(ownerA), disregardIgnoreFiles: false }] });
        const result = await f.search.textSearch(options);
        expect(result.results.map(item => item.resource.toString())).toEqual([a.uri.toString()]);
        expect(b.calls).toBe(0);
        expect(requests.mock.calls).toHaveLength(1);
        const body = JSON.parse(requests.mock.calls[0][1].body);
        expect(body.uri).toBe(root(ownerA));
        expect(body.search_options.ignore_mode).toBe('respect');
        const count = requests.mock.calls.length;
        const onlyOpen = await f.search.textSearch({ ...options, onlyOpenEditors: true });
        expect(onlyOpen.results.map(item => item.resource.toString())).toEqual([a.uri.toString()]);
        expect(requests.mock.calls).toHaveLength(count);
      });
    } finally { f.instantiation.dispose(); }
  });

  test('未支持的父目录和全局规则在模型 progress 及 HTTP 之前明确拒绝', () => {
    const local = model(uri(ownerA, 'same.txt'), 'needle');
    const f = fixture([local]);
    try {
      const progress = [];
      for (const option of [{ disregardParentIgnoreFiles: false }, { disregardGlobalIgnoreFiles: false }]) {
        expect(() => f.search.textSearchSplitSyncAsync(query({ folderQueries: [{ folder: uri(ownerA), ...option }] }),
          undefined, item => progress.push(item))).toThrow('全局或父目录 ignore');
      }
      expect(local.calls).toBe(0);
      expect(progress).toEqual([]);
    } finally { f.instantiation.dispose(); }
  });

  test('最终 ServiceCollection 唯一实现，首次及异步 progress 不扫描 B 或 IndexedDB，同文件模型覆盖磁盘', async () => {
    const a = model(uri(ownerA, 'same.txt'), '🧪 needle A'), b = model(uri(ownerB, 'same.txt'), 'needle B');
    const native = model(URI.parse('workbench:/same.txt'), 'needle IndexedDB');
    const f = fixture([a, b, native]);
    try {
      expect(f.descriptor.ctor).toBe(WorkspaceSearchService);
      expect(f.search.schemeHasFileSearchProvider('boxteam-file')).toBe(true);
      await withTransport(async ({ routes, requests }) => {
        const progress = [];
        const split = f.search.textSearchSplitSyncAsync(query(), undefined, item => progress.push(item));
        expect(split.syncResults.results.map(item => item.resource.toString())).toEqual([a.uri.toString()]);
        expect(progress.length).toBe(1);
        expect(progress[0].results[0].rangeLocations[0].source.startColumn).toBe(3);
        const remote = await split.asyncResults;
        expect(remote.results.map(item => item.resource.toString())).toEqual([uri(ownerA, 'disk.txt').toString()]);
        expect(progress.map(item => item.resource.toString())).toEqual([a.uri.toString(), uri(ownerA, 'disk.txt').toString()]);
        expect(a.calls).toBe(1); expect(b.calls).toBe(0); expect(native.calls).toBe(0);
        expect(routes.mock.calls).toHaveLength(1);
        expect(requests.mock.calls.map(call => call[2])).toEqual(['route-gateway-a']);
      }, async (_path, options) => page(JSON.parse(options.body).uri, [hit(root(ownerA) + '/same.txt'), hit(root(ownerA) + '/disk.txt')]));
    } finally { f.instantiation.dispose(); }
  });

  test('零命中 dirty 模型屏蔽旧磁盘结果，反向 B 查询也不碰 A', async () => {
    const a = model(uri(ownerA, 'same.txt'), '已删除旧词'), b = model(uri(ownerB, 'same.txt'), 'needle B');
    const f = fixture([a, b]);
    try {
      await withTransport(async () => {
        const progress = [];
        expect((await f.search.textSearch(query(), undefined, item => progress.push(item))).results).toEqual([]);
        expect(progress).toEqual([]);
        const reverse = await f.search.textSearch(query({ folderQueries: [{ folder: uri(ownerB) }] }));
        expect(reverse.results.map(item => item.resource.toString())).toEqual([b.uri.toString()]);
        expect(a.calls).toBe(1); expect(b.calls).toBe(1);
      }, async (_path, options) => {
        const target = JSON.parse(options.body).uri;
        return page(target, [hit(target + '/same.txt')]);
      });
    } finally { f.instantiation.dispose(); }
  });

  test('onlyOpenEditors 在查询前选择精确目标，保留 include 目录语义且不读 B', async () => {
    const open = [uri(ownerA, 'src/open.txt'), uri(ownerB, 'src/open.txt'), URI.parse('workbench:/src/open.txt')];
    const f = fixture([], open);
    try {
      await withTransport(async ({ requests }) => {
        const result = await f.search.textSearch(query({ onlyOpenEditors: true, includePattern: { 'src/*.txt': true } }));
        expect(requests.mock.calls.map(call => JSON.parse(call[1].body).uri)).toEqual([root(ownerA) + '/src/open.txt']);
        expect(result.results.map(item => item.resource.toString())).toEqual([open[0].toString()]);
        const empty = fixture([], [open[1], open[2]]);
        try { expect((await empty.search.textSearch(query({ onlyOpenEditors: true }))).results).toEqual([]); }
        finally { empty.instantiation.dispose(); }
        expect(requests.mock.calls).toHaveLength(1);
      }, async (_path, options) => { const target = JSON.parse(options.body).uri; return page(target, [hit(target)]); });
    } finally { f.instantiation.dispose(); }
  });

  test('总结果上限同时约束多份模型和磁盘；AI、混合根和 unsupported 在 progress 之前失败', async () => {
    const models = [model(uri(ownerA, 'first.txt'), 'needle needle'), model(uri(ownerA, 'second.txt'), 'needle')];
    const f = fixture(models);
    try {
      await withTransport(async ({ requests }) => {
        const progress = [];
        const result = await f.search.textSearch(query({ maxResults: 1 }), undefined, item => progress.push(item));
        expect(result.limitHit).toBe(true); expect(result.results).toHaveLength(1);
        expect(progress).toHaveLength(1);
        expect(progress[0].results[0].rangeLocations).toHaveLength(1);
        const before = requests.mock.calls.length;
        await expect(f.search.aiTextSearch({ ...query(), type: 3, contentPattern: 'needle' })).rejects.toThrow('未启用');
        expect(() => f.search.textSearchSplitSyncAsync(query({ extraFileResources: [URI.parse('workbench:/same.txt')] }), undefined, item => progress.push(item))).toThrow('不能混入');
        for (const surroundingContext of [-1, 1.5, 101, NaN]) {
          expect(() => f.search.textSearchSplitSyncAsync(query({ surroundingContext }), undefined, item => progress.push(item))).toThrow('前后文');
        }
        expect(requests.mock.calls.length).toBe(before); expect(progress).toHaveLength(1);
      }, async (_path, options) => page(JSON.parse(options.body).uri, [hit(root(ownerA) + '/disk.txt')]));
    } finally { f.instantiation.dispose(); }
  });

  test('分页复用路由与原查询，选项传给原 owner；file/text 共享 Provider', async () => {
    const f = fixture([]);
    try {
      await withTransport(async ({ requests, routes }) => {
        const provider = new WorkspaceSearchProvider(f.editors);
        try {
          const options = { ...query(), contentPattern: { pattern: 'Need.*le', isRegExp: true, isWordMatch: true, wordSeparators: ' /', isCaseSensitive: false, isMultiline: true },
            includePattern: { '*.txt': true }, excludePattern: { '**/hidden/**': true }, maxResults: 3 };
          const result = await provider.textSearch(options);
          expect(result.results).toHaveLength(2); expect(routes.mock.calls).toHaveLength(1);
          const bodies = requests.mock.calls.map(call => JSON.parse(call[1].body));
          expect(bodies.map(body => body.cursor)).toEqual([undefined, 'next-page']);
          expect(bodies[0].search_options).toEqual({ case_mode: 'insensitive', is_regex: true, whole_word: true,
            word_separators: ' /', is_multiline: true, all_matches: true, context_lines: 0, include_globs: ['*.txt'], exclude_globs: ['**/hidden/**'], ignore_mode: 'default' });
          expect(bodies[1].limit).toBe(bodies[0].limit);
          const files = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'dp' });
          expect(files.results.map(item => item.resource.toString())).toEqual([uri(ownerA, 'deep.txt').toString()]);
          expect(JSON.parse(requests.mock.calls.at(-1)[1].body).search_mode).toBe('fuzzy');
        } finally { provider.dispose(); }
      }, async (_path, options) => {
        const body = JSON.parse(options.body);
        if (body.search_mode === 'fuzzy') return page(body.uri, [], { entries: [{ uri: body.uri + '/deep.txt', resource_shape: 'file' }] });
        return page(body.uri, [hit(body.uri + (body.cursor ? '/deep.txt' : '/first.txt'))], body.cursor ? {} : { truncated: true, next_cursor: 'next-page' });
      });
    } finally { f.instantiation.dispose(); }
  });

  test('显式文件页只读一页且不丢边界项，继续时保持原合同；普通搜索仍聚合原页', async () => {
    const f = fixture([]);
    const options = { type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'page', maxResults: 2 };
    try {
      await withTransport(async ({ requests }) => {
        const first = await f.search.fileSearchPage(options);
        expect(requests.mock.calls).toHaveLength(1);
        expect(first.results.map(item => item.resource.toString())).toEqual(['page-a.txt', 'page-b.txt'].map(name => uri(ownerA, name).toString()));
        expect(first.cursor).toBe('next-file-page');
        expect(first.requestIds).toEqual(['req-first-page']);
        const second = await f.search.fileSearchPage(options, first.cursor);
        expect(second.results.map(item => item.resource.toString())).toEqual([uri(ownerA, 'page-c.txt').toString()]);
        expect(second.cursor).toBeUndefined();
        expect(second.limitHit).toBe(false);
        expect(second.requestIds).toEqual(['req-last-page']);
        const bodies = requests.mock.calls.map(call => JSON.parse(call[1].body));
        expect(bodies.map(body => body.cursor)).toEqual([undefined, 'next-file-page']);
        expect(bodies[0].limit).toBe(2);
        expect({ ...bodies[1], cursor: undefined }).toEqual(bodies[0]);
        const all = await f.search.fileSearch({ ...options, maxResults: 10 });
        expect(all.results).toHaveLength(3);
        expect(requests.mock.calls).toHaveLength(4);
        const deadlines = requests.mock.calls.slice(2).map(call => Number(call[1].headers['X-BoxTeam-Resource-Navigation-Deadline']));
        expect(deadlines[0]).toBe(deadlines[1]);
        expect(deadlines[0]).toBeGreaterThan(Date.now() / 1000);
      }, async (_path, options) => {
        const body = JSON.parse(options.body), names = body.cursor ? ['page-c.txt'] : ['page-a.txt', 'page-b.txt'];
        options.onResponse?.(body.cursor ? 'req-last-page' : 'req-first-page');
        return page(body.uri, [], { entries: names.map(name => ({ uri: body.uri + '/' + name, resource_shape: 'file' })),
          ...(body.cursor ? {} : { truncated: true, next_cursor: 'next-file-page' }) });
      });
    } finally { f.instantiation.dispose(); }
  });

  test('排队已耗尽原总期限的文件页不再解析路由或访问目标', async () => {
    const f = fixture([]);
    try {
      await withTransport(async ({ routes, requests }) => {
        await expect(f.search.fileSearchPage({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'page' },
          undefined, undefined, Date.now() - 1)).rejects.toThrow('文件搜索超过本次总期限');
        expect(routes.mock.calls).toHaveLength(0);
        expect(requests.mock.calls).toHaveLength(0);
      });
    } finally { f.instantiation.dispose(); }
  });

  test('文件页保留受限事实与原拒绝，取消中止 HTTP，单 cursor 不冒充多根分页', async () => {
    const f = fixture([]), token = new CancellationTokenSource();
    const options = { type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'page', maxResults: 2 };
    try {
      await withTransport(async ({ requests }) => {
        const limited = await f.search.fileSearchPage(options);
        expect(limited.limitHit).toBe(true);
        expect(limited.cursor).toBeUndefined();
        expect(limited.messages[0].text).toContain('response_byte_limit');
        expect(() => f.search.fileSearchPage({ ...options, folderQueries: [...options.folderQueries, { folder: uri(ownerB) }] })).toThrow('单个明确目录根');
        expect(requests.mock.calls).toHaveLength(1);
      }, async (_path, options) => page(JSON.parse(options.body).uri, [], { truncated: true, truncation_reason: 'response_byte_limit' }));
      const rejection = new Error('原 owner 拒绝旧 cursor');
      await withTransport(async () => {
        await expect(f.search.fileSearchPage(options, 'expired')).rejects.toBe(rejection);
      }, async () => { throw rejection; });
      const entered = Promise.withResolvers(), delayed = Promise.withResolvers();
      await withTransport(async ({ requests }) => {
        const pending = f.search.fileSearchPage(options, 'next-file-page', token.token);
        await entered.promise;
        token.cancel();
        await expect(pending).rejects.toThrow();
        expect(requests.mock.calls[0][1].signal.aborted).toBe(true);
        delayed.resolve(page(root(ownerA)));
      }, async () => { entered.resolve(); return delayed.promise; });
    } finally { token.dispose(); f.instantiation.dispose(); }
  });

  test('取消和 dispose 中止实际请求，迟到响应不发 progress', async () => {
    for (const mode of ['cancel', 'dispose']) {
      const f = fixture([]), token = new CancellationTokenSource();
      let release, entered;
      const started = new Promise(resolve => { entered = resolve; });
      try {
        await withTransport(async ({ requests }) => {
          const progress = [], pending = f.search.textSearch(query(), token.token, item => progress.push(item));
          await started;
          if (mode === 'cancel') token.cancel(); else f.instantiation.dispose();
          await expect(pending).rejects.toThrow();
          expect(requests.mock.calls[0][1].signal.aborted).toBe(true);
          release(page(root(ownerA), [hit(root(ownerA) + '/late.txt')]));
          await Promise.resolve(); expect(progress).toEqual([]);
        }, async () => { entered(); return await new Promise(resolve => { release = resolve; }); });
      } finally { token.dispose(); if (mode !== 'dispose') f.instantiation.dispose(); }
    }
  });

  test('跨页上下文按最终命中去重，上限哨兵和 dirty 文件的磁盘上下文不进入结果', async () => {
    const dirty = model(uri(ownerA, 'dirty.txt'), 'dirty before\nneedle\ndirty after');
    const f = fixture([dirty]);
    const path = root(ownerA) + '/disk.txt';
    const match = line => ({ ...hit(path), line, end_line: line });
    const context = (line, text = `disk context ${line}`, extra = {}) => ({ uri: path, line, text, truncated: false, ...extra });
    try {
      await withTransport(async ({ requests }) => {
        const progress = [];
        const result = await f.search.textSearch(query({ maxResults: 3, surroundingContext: 1 }), undefined, item => progress.push(item));
        expect(result.limitHit).toBe(true);
        const disk = result.results.find(item => item.resource.toString() === uri(ownerA, 'disk.txt').toString());
        expect(disk.results.filter(item => 'lineNumber' in item)).toEqual([
          { lineNumber: 1, text: 'disk context 1' }, { lineNumber: 4, text: 'clipped…' },
        ]);
        const local = result.results.find(item => item.resource.toString() === dirty.uri.toString());
        expect(local.results.filter(item => 'lineNumber' in item)).toEqual([
          { lineNumber: 1, text: 'dirty before' }, { lineNumber: 3, text: 'dirty after' },
        ]);
        expect(JSON.stringify(progress)).not.toContain('stale disk');
        expect(result.messages.some(message => message.text.includes('上下文行已截断'))).toBe(true);
        expect(requests.mock.calls.map(call => JSON.parse(call[1].body).search_options.context_lines)).toEqual([1, 1]);
        expect(requests.mock.calls.every(call => call[0] === '/api/v1/resources/navigation')).toBe(true);
      }, async (_path, options) => {
        const body = JSON.parse(options.body);
        return body.cursor ? page(body.uri, [match(3), match(10)], { contexts: [context(2), context(4, 'clipped', { truncated: true }), context(9), context(11)] })
          : page(body.uri, [match(2), hit(root(ownerA) + '/dirty.txt')], { truncated: true, next_cursor: 'next', contexts: [
            context(1), context(3), context(1, 'stale disk', { uri: root(ownerA) + '/dirty.txt' }),
          ] });
      });
    } finally { f.instantiation.dispose(); }
  });

  test('只有上下文中的越界 URI 也拒绝；上下文 progress 取消后不再投递', async () => {
    const f = fixture([]), token = new CancellationTokenSource();
    try {
      await withTransport(async () => {
        await expect(f.search.textSearch(query({ surroundingContext: 1 }))).rejects.toThrow();
      }, async (_path, options) => page(JSON.parse(options.body).uri, [hit(root(ownerA) + '/disk.txt')], {
        contexts: [{ uri: root(ownerB) + '/disk.txt', line: 2, text: 'foreign', truncated: false }],
      }));
      await withTransport(async () => {
        const contexts = [];
        await expect(f.search.textSearch(query({ surroundingContext: 2 }), token.token, item => {
          if (item.results?.some(result => 'lineNumber' in result)) { contexts.push(item); token.cancel(); }
        })).rejects.toThrow();
        expect(contexts).toHaveLength(1);
      }, async (_path, options) => page(JSON.parse(options.body).uri, [hit(root(ownerA) + '/disk.txt')], {
        contexts: [2, 3].map(line => ({ uri: root(ownerA) + '/disk.txt', line, text: 'context', truncated: false })),
      }));
    } finally { token.dispose(); f.instantiation.dispose(); }
  });

  test('非业务根沿原服务，失败保留原错误；片段与多行 UTF-16 区间互不混用', async () => {
    const native = model(URI.parse('workbench:/same.txt'), 'needle native');
    const f = fixture([native]);
    try {
      await withTransport(async ({ requests }) => {
        f.search.registerSearchResultProvider('workbench', 1, { textSearch: async () => ({ results: [], messages: [] }) });
        const nativeResult = await f.search.textSearch(query({ folderQueries: [{ folder: URI.parse('workbench:/') }] }));
        expect(nativeResult.results.map(item => item.resource.toString())).toEqual([native.uri.toString()]);
        expect(requests.mock.calls).toHaveLength(0);
        await expect(f.search.textSearch(query())).rejects.toThrow('owner denied: request-123');
      }, async () => { throw new Error('owner denied: request-123'); });
      const projected = textMatch({ ...hit(root(ownerA) + '/emoji.txt'), text: '🧪 needle', utf16_column: 103,
        utf16_end_column: 5, snippet_start_utf16_column: 100, end_line: 3 });
      expect(projected.rangeLocations[0]).toEqual({
        source: { startLineNumber: 0, startColumn: 102, endLineNumber: 2, endColumn: 4 },
        preview: { startLineNumber: 0, startColumn: 3, endLineNumber: 0, endColumn: 9 },
      });
      expect(resourceInQuery(uri(ownerA, 'nested/a.txt'), query({ includePattern: { '*.txt': true } }))).toBe(true);
      expect(resourceInQuery(uri(ownerA, 'nested/hidden/a.txt'), query({ excludePattern: { hidden: true } }))).toBe(false);
      expect(workspaceFileSource(uri(ownerB, 'same.txt')).gatewayId).toBe('gateway-b');
    } finally { f.instantiation.dispose(); }
  });

  test('首条 progress 取消后，同页其它结果不得继续投递', async () => {
    const f = fixture([]), token = new CancellationTokenSource();
    try {
      await withTransport(async () => {
        const progress = [];
        await expect(f.search.textSearch(query(), token.token, item => { progress.push(item); token.cancel(); })).rejects.toThrow();
        expect(progress.map(item => item.resource.toString())).toEqual([uri(ownerA, 'first.txt').toString()]);
      }, async (_path, options) => page(JSON.parse(options.body).uri, [hit(root(ownerA) + '/first.txt'), hit(root(ownerA) + '/late.txt')]));
    } finally { token.dispose(); f.instantiation.dispose(); }
  });

  test('仅已打开文件名搜索沿完整相对路径匹配，不因精确请求丢失目录前缀', async () => {
    const f = fixture([], [uri(ownerA, 'src/open.txt'), uri(ownerA, 'other/no.txt'), uri(ownerB, 'src/open.txt')]);
    try {
      await withTransport(async ({ requests }) => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'src/op', onlyOpenEditors: true });
        expect(result.results.map(item => item.resource.toString())).toEqual([uri(ownerA, 'src/open.txt').toString()]);
        expect(requests.mock.calls.map(call => JSON.parse(call[1].body).uri)).toEqual([root(ownerA) + '/src/open.txt']);
      }, async (_path, options) => {
        const target = JSON.parse(options.body).uri;
        return page(target, [], { entries: [{ uri: target, resource_shape: 'file' }] });
      });
    } finally { f.instantiation.dispose(); }
  });

  test('路由解析尚在途时取消，也中止控制面信号且不发文件请求', async () => {
    const f = fixture([]), token = new CancellationTokenSource();
    const resolve = spyOn(gateway, 'resolveWorkspace').mockImplementation(async (_owner, signal) => {
      await new Promise((_, reject) => signal.addEventListener('abort', () => reject(new Error('lookup aborted')), { once: true }));
      return 'unreachable';
    });
    const requests = spyOn(gateway, 'request');
    try {
      const pending = f.search.textSearch(query(), token.token);
      await Promise.resolve(); await Promise.resolve();
      token.cancel();
      await expect(pending).rejects.toThrow();
      expect(resolve.mock.calls[0][1].aborted).toBe(true);
      expect(requests.mock.calls).toHaveLength(0);
    } finally { resolve.mockRestore(); requests.mockRestore(); token.dispose(); f.instantiation.dispose(); }
  });
});

await writeFile(join(run.artifactsDir, 'boundaries.json'), JSON.stringify({
  compiler: 'production esbuild + workbench tsconfig', gateway: 'explicit transport spy',
  model: 'in-memory text facade with real upstream TextModelSearch',
  notProven: ['Quick Access UI', 'SearchView scope', 'Electron', 'real backend request'],
}, null, 2));
