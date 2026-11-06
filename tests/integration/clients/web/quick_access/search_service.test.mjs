import { describe, expect, test, spyOn } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/search_service.test.mjs').prepare();
const bundle = join(run.artifactsDir, 'search.fixture.mjs');
await build({ entryPoints: [resolve('tests/integration/clients/web/quick_access/search.fixture.ts')],
  outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(resolve('src/clients/workbench'), sourceTransform(resolve('src/clients/workbench'), { commit: pinnedUpstreamCommit }, []))],
  tsconfig: resolve('src/clients/workbench/tsconfig.jsonc'), logLevel: 'silent' });
const { URI, gateway, fixture, model, WorkspaceSearchService, WorkspaceSearchProvider,
  workspaceFileUri, workspaceFileSource, textMatch, CancellationTokenSource, resourceInQuery,
  ExtUri, extUri, textSearchResultToMatches, installSessionApiFetchMock, unwrapSessionApiFetch } = await import(pathToFileURL(bundle).href);
const ownerA = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const ownerB = { gatewayId: 'gateway-b', workspaceId: 'workspace-a' };
const root = owner => `boxteam://${owner.gatewayId}/workspace/${owner.workspaceId}/resources/files/root`;
const uri = (owner, name = '') => workspaceFileUri(owner, root(owner) + (name ? '/' + name : ''), root(owner));
const query = (extra = {}) => ({ type: 2, folderQueries: [{ folder: uri(ownerA) }], contentPattern: { pattern: 'needle', isCaseSensitive: true }, ...extra });
const hit = (path, column = 1) => ({ uri: path, line: 1, column, end_line: 1, end_column: column + 6,
  utf16_column: column, utf16_end_column: column + 6, snippet_start_utf16_column: 1, text: 'needle', snippet_start_column: 1, truncated: false });
const page = (path, hits = [], extra = {}) => ({ node: { uri: path, resource_shape: 'directory', operations: ['search'] },
  entries: [], matches: hits, contexts: [], truncated: false, view: 'live', ...extra });
const files = (target, names, extra = {}) => page(target.uri, [], { entries: names.map(name => ({ uri: target.uri + '/' + name, resource_shape: 'file', operations: ['read'] })), ...extra });

/** 仅提供显式领域页资产；生产 Provider、共享 SSE 消费及其校验均实际运行。 */
async function withTransport(callback, asset = async target => page(target.uri), diagnostic = {}) {
  const calls = [], paths = [];
  const routes = spyOn(gateway, 'resolveWorkspace');
  const direct = spyOn(gateway, 'request');
  installSessionApiFetchMock(async ({ path, init }) => {
    paths.push(path);
    if (path !== '/api/gateway/search') throw new Error(`文件搜索出现旧请求：${path}`);
    const request = JSON.parse(init.body), id = `file-request-${calls.length}`;
    calls.push({ request, signal: init.signal });
    const scope = request.scope.scope ?? { version: 1, gateway_id: 'gateway-current', resolved_at: '2026-11-03T00:00:00Z', mode: 'selected', owners: request.scope.owners };
    let sequence = 0;
    const frames = [], states = [];
    const emit = (type, payload) => {
      const event = { sequence: ++sequence, request_id: id, type, ...payload };
      frames.push(`id: ${sequence}\nevent: search\ndata: ${JSON.stringify(event)}\n\n`);
    };
    emit('scope', { scope: { scope, branches: scope.owners.map(owner => ({ owner, state: 'registered' })) } });
    for (const q of request.queries) for (const item of q.file_targets) {
      const branch = { owner: item.owner, category: 'file', operation: q.operation, resource_uri: item.target.uri };
      const selected = request.continuations.find(c => JSON.stringify(c.branch) === JSON.stringify(branch));
      if (request.continuations.length && !selected) continue;
      const result = await asset(item.target, selected?.cursor, q, init.signal, request);
      if (result.failure) {
        const state = { branch, state: 'failed', retryable: true, allocated_results: q.limit, returned_results: 0, ...result.failure };
        states.push(state); emit('branch', { branch: state }); continue;
      }
      const count = q.operation === 'content' ? result.matches.length : result.entries.length;
      const limitedReason = ['response_byte_limit', 'file_byte_limit'].includes(result.truncation_reason) ? 'byte_limit' : 'result_limit';
      const state = { branch, state: result.truncated ? 'limited' : 'complete', retryable: false, allocated_results: q.limit,
        returned_results: count, ...(result.truncated ? { code: limitedReason, cursor: result.next_cursor } : {}), ...diagnostic };
      states.push(state);
      emit('page', { branch: state, page: { provider_id: 'workspace-files-' + q.operation, workspace_id: item.owner.workspace_id,
        category: 'file', operation: q.operation, contract_version: 1, state: state.state,
        ...(result.truncated ? { limited_reason: limitedReason } : {}), files: result } });
      emit('branch', { branch: state });
    }
    emit('end', { summary: { state: states.every(s => s.state === 'complete') ? 'complete' : 'incomplete',
      order: 'category-scope-owner-v1', returned_results: states.reduce((count, s) => count + s.returned_results, 0), branches: states } });
    return new Response(frames.join(''), { headers: { 'Content-Type': 'text/event-stream', 'X-Request-ID': id } });
  });
  try {
    const result = await callback(calls);
    expect(paths.every(path => path === '/api/gateway/search')).toBe(true);
    expect(routes).not.toHaveBeenCalled(); expect(direct).not.toHaveBeenCalled();
    return result;
  } finally { unwrapSessionApiFetch(); routes.mockRestore(); direct.mockRestore(); }
}

describe('唯一 SearchService 文件联邦合同（网络页为显式资产）', () => {
  test('真实 singleton 和 file/text Provider，模型首次 progress 限精确 owner；异步一次提交全部根', async () => {
    const a = model(uri(ownerA, 'same.txt'), '🧪 needle A'), b = model(uri(ownerB, 'same.txt'), 'needle B');
    const native = model(URI.parse('workbench:/same.txt'), 'needle IndexedDB'), f = fixture([a, b, native]);
    try {
      expect(f.descriptor.ctor).toBe(WorkspaceSearchService); expect(f.search.schemeHasFileSearchProvider('boxteam-file')).toBe(true);
      await withTransport(async calls => {
        const progress = [], split = f.search.textSearchSplitSyncAsync(query(), undefined, item => progress.push(item));
        expect(split.syncResults.results.map(m => m.resource.toString())).toEqual([a.uri.toString()]);
        expect(progress[0].results[0].rangeLocations[0].source.startColumn).toBe(3);
        const remote = await split.asyncResults;
        expect(remote.results.map(m => m.resource.toString())).toEqual([uri(ownerA, 'disk.txt').toString()]);
        expect(progress.map(m => m.resource.toString())).toEqual([a.uri.toString(), uri(ownerA, 'disk.txt').toString()]);
        expect([a.calls, b.calls, native.calls]).toEqual([1, 0, 0]);
        expect(calls).toHaveLength(1); expect(calls[0].request.queries[0].file_targets[0].target.search_options.excluded_resource_uris).toEqual([root(ownerA) + '/same.txt']);
      }, async target => page(target.uri, [hit(target.uri + '/disk.txt')]));
    } finally { f.instantiation.dispose(); }
  });

  test('A 零命中覆盖和 B 命中按 owner 分区，后端只取得身份而没有模型正文', async () => {
    const a = model(uri(ownerA, 'same.txt'), 'removed in A'), b = model(uri(ownerB, 'same.txt'), 'needle B'), f = fixture([a, b]);
    try {
      await withTransport(async calls => {
        const result = await f.search.textSearch(query({ folderQueries: [{ folder: uri(ownerA) }, { folder: uri(ownerB) }], maxResults: 2 }));
        expect(result.results.map(m => m.resource.toString()).sort()).toEqual([uri(ownerA, 'remaining.txt').toString(), b.uri.toString()].sort());
        expect(calls).toHaveLength(1);
        const targets = calls[0].request.queries[0].file_targets;
        expect(targets.map(t => t.target.search_options.excluded_resource_uris)).toEqual([[root(ownerA) + '/same.txt'], [root(ownerB) + '/same.txt']]);
        expect(calls[0].request.scope.owners).toHaveLength(2);
        expect(JSON.stringify(calls)).not.toContain('removed in A');
      }, async target => page(target.uri, target.uri === root(ownerA) ? [hit(target.uri + '/remaining.txt')] : []));
    } finally { f.instantiation.dispose(); }
  });

  test('本地已确认超过上限时停止后续模型及远端读取，精确上限仍探测远端', async () => {
    for (const [maximum, texts, matchingCalls] of [[2, ['needle needle needle', 'needle'], [1, 0]],
      [2, ['needle needle', 'needle', 'needle'], [1, 1, 0]], [0, ['needle', 'needle'], [1, 0]]]) {
      const models = texts.map((text, index) => model(uri(ownerA, `draft-${index}.txt`), text)), f = fixture(models);
      try {
        await withTransport(async calls => {
          const progress = [], split = f.search.textSearchSplitSyncAsync(query({ maxResults: maximum }), undefined, item => progress.push(item));
          expect(split.syncResults.limitHit).toBe(true);
          expect(split.syncResults.results.flatMap(file => file.results.flatMap(result => result.rangeLocations))).toHaveLength(maximum);
          const remote = await split.asyncResults;
          expect(remote.results).toEqual([]); expect(remote.limitHit).toBe(true);
          expect(calls).toEqual([]); expect(models.map(model => model.calls)).toEqual(matchingCalls);
          expect(models.every(model => !model.watching)).toBe(true);
          expect(progress.flatMap(file => file.results.flatMap(result => result.rangeLocations))).toHaveLength(maximum);
        });
      } finally { f.instantiation.dispose(); models.forEach(model => model.dispose()); }
    }
    for (const more of [false, true]) {
      const local = model(uri(ownerA, 'draft.txt'), 'needle needle'), f = fixture([local]);
      try {
        await withTransport(async calls => {
          const progress = [], result = await f.search.textSearch(query({ maxResults: 2 }), undefined, item => progress.push(item));
          expect(result.limitHit).toBe(more);
          expect(result.results.map(file => file.resource.toString())).toEqual([local.uri.toString()]);
          expect(progress).toHaveLength(1); expect(calls).toHaveLength(1);
          expect(calls[0].request.queries[0].limit).toBe(1);
          expect(calls[0].request.queries[0].file_targets[0].target.search_options.excluded_resource_uris).toEqual([root(ownerA) + '/draft.txt']);
          expect(local.watching).toBe(false);
        }, async target => page(target.uri, more ? [hit(target.uri + '/disk.txt')] : []));
      } finally { f.instantiation.dispose(); local.dispose(); }
    }
  });

  test('onlyOpenEditors 只发送未覆盖的精确文件，先应用相对目录 glob 和名称模式', async () => {
    const dirty = model(uri(ownerA, 'src/draft.txt'), 'removed'), disk = uri(ownerA, 'src/open.txt');
    const f = fixture([dirty], [dirty.uri, disk, uri(ownerB, 'src/open.txt'), URI.parse('workbench:/src/open.txt')]);
    try {
      await withTransport(async calls => {
        const result = await f.search.textSearch(query({ onlyOpenEditors: true, includePattern: { 'src/*.txt': true } }));
        expect(result.results.map(m => m.resource.toString())).toEqual([disk.toString()]);
        const targets = calls[0].request.queries[0].file_targets;
        expect(targets).toHaveLength(1); expect(targets[0].target.uri).toBe(root(ownerA) + '/src/open.txt');
        expect(targets[0].target.search_options.include_globs).toEqual([]); expect(targets[0].target.search_options.excluded_resource_uris).toEqual([]);
        const names = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'src/op', onlyOpenEditors: true });
        expect(names.results.map(m => m.resource.toString())).toEqual([disk.toString()]);
        expect(calls[1].request.queries[0].file_targets).toHaveLength(1);
      }, async (target, _cursor, q) => q.operation === 'content' ? page(target.uri, [hit(target.uri)])
        : page(target.uri, [], { entries: [{ uri: target.uri, resource_shape: 'file', operations: ['read'] }] }));
      const empty = fixture([], [uri(ownerB, 'other.txt')]);
      try { await withTransport(async calls => { expect((await empty.search.textSearch(query({ onlyOpenEditors: true }))).results).toEqual([]); expect(calls).toEqual([]); }); }
      finally { empty.instantiation.dispose(); }
    } finally { f.instantiation.dispose(); }
  });

  test('同 owner 多根保留局部选项，普通查询仅续未完成根且页大小和总期限不变', async () => {
    const f = fixture([]), folders = ['one', 'two'].map((name, index) => ({ folder: uri(ownerA, name), includePattern: { [index ? '*.md' : '*.txt']: true } }));
    try {
      await withTransport(async calls => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: folders, filePattern: 'page', maxResults: 999 });
        expect(result.results).toHaveLength(3); expect(calls).toHaveLength(2);
        const [first, second] = calls.map(c => c.request);
        expect(first.scope.owners).toHaveLength(1); expect(first.queries[0].file_targets.map(t => t.target.search_options.include_globs)).toEqual([['*.txt'], ['*.md']]);
        expect(second.scope.mode).toBe('resume'); expect(second.scope.scope.owners).toEqual(first.scope.owners);
        expect(second.continuations).toEqual([{ branch: { owner: first.scope.owners[0], category: 'file', operation: 'name', resource_uri: root(ownerA) + '/two' }, cursor: 'two-next' }]);
        expect(second.queries).toEqual(first.queries); expect(first.queries[0].limit).toBe(500);
        expect(second.deadline_at).toBe(first.deadline_at);
      }, async (target, cursor) => files(target, [target.uri.endsWith('/one') ? 'page.txt' : cursor ? 'page2.md' : 'page1.md'],
        target.uri.endsWith('/two') && !cursor ? { truncated: true, next_cursor: 'two-next' } : {}));
    } finally { f.instantiation.dispose(); }
  });

  test('重叠根按独立子树分区，重复根去重，不影响 B 同路径；输入对象保持原样', async () => {
    const f = fixture([]);
    try {
      for (const reversed of [false, true]) await withTransport(async calls => {
        const nested = [{ folder: uri(ownerA, 'src/nested') }, { folder: uri(ownerA, 'src') }, { folder: uri(ownerA) }];
        const folders = [...(reversed ? nested.toReversed() : nested), { folder: uri(ownerA) }, { folder: uri(ownerB) }];
        const request = { type: 1, folderQueries: folders, extraFileResources: [uri(ownerA, 'file.txt'), uri(ownerA, 'file.txt')], filePattern: 'file', maxResults: 999 };
        // URI 自带惰性 external 缓存；只比较查询语义和数组身份。
        const snapshot = () => ({ ...request, folderQueries: request.folderQueries.map(folder => ({ ...folder, folder: folder.folder.toString() })),
          extraFileResources: request.extraFileResources.map(resource => resource.toString()) });
        const before = snapshot();
        const result = await f.search.fileSearch(request);
        expect(snapshot()).toEqual(before); expect(request.folderQueries).toBe(folders);
        expect(calls).toHaveLength(1);
        const targets = calls[0].request.queries[0].file_targets.map(item => item.target);
        expect(targets.map(item => item.uri)).toEqual([...(reversed ? nested.toReversed() : nested).map(item => workspaceFileSource(item.folder).path), root(ownerB)]);
        expect(targets.find(item => item.uri === root(ownerA)).search_options.excluded_subtree_uris).toEqual([root(ownerA) + '/src', root(ownerA) + '/src/nested']);
        expect(targets.find(item => item.uri === root(ownerA) + '/src').search_options.excluded_subtree_uris).toEqual([root(ownerA) + '/src/nested']);
        expect(targets.find(item => item.uri === root(ownerB)).search_options.excluded_subtree_uris).toEqual([]);
        expect(result.results.map(item => item.resource.toString()).sort()).toEqual([
          ...['file.txt', 'src/file.txt', 'src/nested/file.txt'].map(name => uri(ownerA, name).toString()), uri(ownerB, 'file.txt').toString(),
        ].sort());
      }, async target => files(target, ['file.txt']));
    } finally { f.instantiation.dispose(); }
  });

  test('相同根的冲突选项在本地匹配与 HTTP 前明确拒绝', async () => {
    const a = model(uri(ownerA, 'src/file.txt'), 'needle'), f = fixture([a]);
    try {
      await withTransport(async calls => {
        const sets = [
          [{ folder: uri(ownerA), includePattern: { '*.txt': true } }, { folder: uri(ownerA), includePattern: { '*.md': true } }],
          [{ folder: uri(ownerA) }, { folder: uri(ownerA), disregardIgnoreFiles: true }],
        ];
        for (const folders of sets) {
          expect(() => f.search.textSearchSplitSyncAsync(query({ folderQueries: folders }))).toThrow('同一搜索目录');
          await expect(f.search.fileSearch({ type: 1, folderQueries: folders, filePattern: 'file' })).rejects.toThrow('同一搜索目录');
        }
        expect(a.calls).toBe(0); expect(calls).toEqual([]);
        const folders = [true, false].map(reverse => ({ folder: uri(ownerA), includePattern: reverse ? { '*.txt': true, '*.md': true } : { '*.md': true, '*.txt': true } }));
        await f.search.fileSearch({ type: 1, folderQueries: folders, filePattern: 'file' });
        expect(calls).toHaveLength(1); expect(calls[0].request.queries[0].file_targets).toHaveLength(1);
      });
    } finally { f.instantiation.dispose(); }
  });

  test('局部 include 与 ignore 保留子根语义，模型覆盖只交所属分支，已打开文件不带兄弟子树', async () => {
    const parent = model(uri(ownerA, 'outside.txt'), 'needle parent');
    const child = model(uri(ownerA, 'src/keep.md'), 'needle child');
    const hidden = model(uri(ownerA, 'src/drop.txt'), 'needle must not match parent include');
    const disk = uri(ownerA, 'src/disk.md');
    const f = fixture([parent, child, hidden], [parent.uri, child.uri, hidden.uri, disk]);
    const folders = [{ folder: uri(ownerA), includePattern: { '*.txt': true }, disregardIgnoreFiles: false },
      { folder: uri(ownerA, 'src'), includePattern: { '*.md': true }, disregardIgnoreFiles: true }];
    try {
      await withTransport(async calls => {
        const result = await f.search.textSearch(query({ folderQueries: folders }));
        expect(result.results.map(item => item.resource.toString())).toEqual([parent.uri.toString(), child.uri.toString()]);
        expect(hidden.calls).toBe(0);
        const targets = calls[0].request.queries[0].file_targets.map(item => item.target);
        expect(targets.map(item => item.search_options.ignore_mode)).toEqual(['respect', 'none']);
        expect(targets.map(item => item.search_options.excluded_resource_uris)).toEqual([[root(ownerA) + '/outside.txt'], [root(ownerA) + '/src/keep.md']]);
        expect(targets[0].search_options.excluded_subtree_uris).toEqual([root(ownerA) + '/src']);
        await f.search.textSearch(query({ folderQueries: folders, onlyOpenEditors: true }));
        const exact = calls[1].request.queries[0].file_targets;
        expect(exact).toHaveLength(1);
        expect(exact[0].target.uri).toBe(workspaceFileSource(disk).path);
        expect(exact[0].target.search_options.excluded_subtree_uris).toEqual([]);
        expect(exact[0].target.search_options.include_globs).toEqual([]);
      });
      await withTransport(async () => {
        await expect(f.search.fileSearch({ type: 1, folderQueries: folders, filePattern: 'keep' })).rejects.toThrow('目录或过滤范围');
      }, async target => files(target, target.uri === root(ownerA) ? ['src/keep.md'] : []));
    } finally { f.instantiation.dispose(); }
  });

  test('消费注入的目标 URI identity；模型负命中及磁盘区间按同一键去重，不全局折叠 B', async () => {
    // 这是目标能力注入合同；真实 Windows owner 的 canonical 公布另行验收。
    const identity = new ExtUri(resource => resource.authority === ownerA.gatewayId);
    const a = model(uri(ownerA, 'SAME.txt'), 'removed'), lowerB = model(uri(ownerB, 'same.txt'), 'removed');
    const f = fixture([a, lowerB], [uri(ownerA, 'same.txt'), lowerB.uri, uri(ownerA, 'same.txt')], identity);
    try {
      await withTransport(async calls => {
        const progress = [], result = await f.search.textSearch(query({ folderQueries: [{ folder: uri(ownerA) }, { folder: uri(ownerB) }] }), undefined, item => progress.push(item));
        expect(a.calls).toBe(1); expect(lowerB.calls).toBe(1);
        expect(calls[0].request.queries[0].file_targets.map(item => item.target.search_options.excluded_resource_uris)).toEqual([[root(ownerA) + '/SAME.txt'], [root(ownerB) + '/same.txt']]);
        expect(result.results).toHaveLength(3);
        expect(result.results.filter(item => workspaceFileSource(item.resource).gatewayId === ownerA.gatewayId)[0].results).toHaveLength(1);
        expect(progress).toHaveLength(3);
        expect(progress.some(item => item.resource.toString() === uri(ownerA, 'same.txt').toString())).toBe(false);
        expect(progress.some(item => item.resource.toString() === uri(ownerB, 'SAME.txt').toString())).toBe(true);
      }, async target => page(target.uri, [hit(target.uri + '/same.txt'), hit(target.uri + '/SAME.txt'), hit(target.uri + '/other.txt'), hit(target.uri + '/OTHER.txt')]
        .filter(row => target.uri === root(ownerA) || row.uri.endsWith('/SAME.txt') || row.uri.endsWith('/other.txt'))));
      await withTransport(async calls => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], onlyOpenEditors: true });
        expect(calls[0].request.queries[0].file_targets).toHaveLength(1); expect(result.results).toHaveLength(1);
      }, async target => page(target.uri, [], { entries: [{ uri: target.uri.replace(/same\.txt$/, 'SAME.txt'), resource_shape: 'file', operations: ['read'] }] }));
    } finally { f.instantiation.dispose(); }
  });

  test('上游 maxResults=0 有界召回，业务正文零配额不输出，exists 只探测且不返回结果', async () => {
    const f = fixture([]);
    try {
      await withTransport(async calls => {
        const names = { type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'file', maxResults: 0 };
        expect((await f.search.fileSearch(names)).results).toHaveLength(1); expect(calls[0].request.queries[0].limit).toBe(500);
        const exists = await f.search.fileSearch({ ...names, exists: true });
        expect(exists.results).toEqual([]); expect(exists.limitHit).toBe(true); expect(calls[1].request.queries[0].limit).toBe(1);
        const absent = await f.search.fileSearch({ ...names, filePattern: 'absent', exists: true });
        expect(absent.limitHit).toBe(false);
        const zero = await f.search.textSearch(query({ maxResults: 0 })); expect(zero.results).toEqual([]); expect(zero.limitHit).toBe(true);
      }, async (target, _cursor, q) => q.operation === 'content' ? page(target.uri, [hit(target.uri + '/file.txt')]) : files(target, q.query === 'absent' ? [] : ['file.txt']));
    } finally { f.instantiation.dispose(); }
  });

  test('受限预热不缓存为完整结果，同cacheKey的上限外匹配及另一owner仍独立查询', async () => {
    const f = fixture([]), cacheKey = 'same-picker-cache';
    const common = { type: 1, folderQueries: [{ folder: uri(ownerA) }], cacheKey };
    const prefix = Array.from({ length: 500 }, (_, index) => `entry-${String(index).padStart(3, '0')}.txt`);
    const tail = 'only-after-prewarm-cap.txt';
    try {
      await withTransport(async calls => {
        const warm = await f.search.fileSearch({ ...common, filePattern: '', maxResults: 0 });
        expect(warm.results).toHaveLength(500); expect(warm.limitHit).toBe(true);
        expect(warm.results.some(item => item.resource.toString() === uri(ownerA, tail).toString())).toBe(false);
        expect(calls).toHaveLength(1);
        expect(calls[0].request.queries[0].limit).toBe(500);
        expect(calls.every(call => call.request.queries[0].file_targets.every(item => item.owner.gateway_id === ownerA.gatewayId))).toBe(true);

        const later = await f.search.fileSearch({ ...common, filePattern: 'only-after-prewarm-cap', maxResults: 10 });
        expect(later.results.map(item => item.resource.toString())).toEqual([uri(ownerA, tail).toString()]);
        expect(later.limitHit).toBe(false); expect(calls).toHaveLength(2);
        const other = await f.search.fileSearch({ ...common, folderQueries: [{ folder: uri(ownerB) }], filePattern: 'only-after-prewarm-cap', maxResults: 10 });
        expect(other.results.map(item => item.resource.toString())).toEqual([uri(ownerB, tail).toString()]);
        expect(other.limitHit).toBe(false); expect(calls).toHaveLength(3);

        await f.search.clearCache(cacheKey);
        expect(calls).toHaveLength(3);
        const reopened = await f.search.fileSearch({ ...common, filePattern: 'only-after-prewarm-cap', maxResults: 10 });
        expect(reopened.results.map(item => item.resource.toString())).toEqual([uri(ownerA, tail).toString()]);
        expect(reopened.limitHit).toBe(false); expect(calls).toHaveLength(4);
      }, async (target, cursor, q) => q.query === '**/*'
        ? files(target, cursor ? [tail] : prefix, cursor ? {} : { truncated: true, next_cursor: 'after-500' })
        : files(target, [tail]));
    } finally { f.instantiation.dispose(); }
  });

  test('已有下一文件证据时到达总配额不再取哨兵，续页完成不会继承旧受限状态', async () => {
    for (const hasB of [true, false]) {
      const f = fixture([]);
      try {
        await withTransport(async calls => {
          const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }, { folder: uri(ownerB) }],
            filePattern: 'match', maxResults: 2 });
          expect(result.results.map(item => item.resource.toString())).toEqual([uri(ownerA, 'match-a.txt').toString(),
            hasB ? uri(ownerB, 'match-b.txt').toString() : uri(ownerA, 'match-next.txt').toString()]);
          expect(result.limitHit).toBe(hasB); expect(calls).toHaveLength(hasB ? 1 : 2);
          if (!hasB) expect(calls[1].request.continuations[0].cursor).toBe('next-A');
        }, async (target, cursor) => target.uri === root(ownerB) ? files(target, hasB ? ['match-b.txt'] : [])
          : files(target, cursor ? ['match-next.txt'] : ['match-a.txt'], cursor ? {} : { truncated: true, next_cursor: 'next-A' }));
      } finally { f.instantiation.dispose(); }
    }
  });

  test('字节受限未证明还有匹配，恰好配额仍探测原续点并保留完整状态', async () => {
    const f = fixture([]);
    try {
      await withTransport(async calls => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'match', maxResults: 2 });
        expect(result.results).toHaveLength(2); expect(result.limitHit).toBe(false);
        expect(calls).toHaveLength(2); expect(calls[1].request.continuations[0].cursor).toBe('byte-boundary');
      }, async (target, cursor) => files(target, cursor ? [] : ['match-a.txt', 'match-b.txt'],
        cursor ? {} : { truncated: true, truncation_reason: 'response_byte_limit', next_cursor: 'byte-boundary' }));
    } finally { f.instantiation.dispose(); }
  });

  test('正则、整词、多行、ignore、context 和文件字节上限完整送至原 owner', async () => {
    const f = fixture([]);
    try {
      await withTransport(async calls => {
        await f.search.textSearch(query({ folderQueries: [{ folder: uri(ownerA), disregardIgnoreFiles: false }], contentPattern: { pattern: 'Need.*le', isRegExp: true,
          isWordMatch: true, wordSeparators: ' /', isCaseSensitive: false, isMultiline: true }, includePattern: { '*.txt': true }, excludePattern: { '**/hidden/**': true }, surroundingContext: 2, maxFileSize: 2048 }));
        const q = calls[0].request.queries[0]; expect(q.match_mode).toBe('regex');
        expect(q.file_targets[0].target).toEqual({ uri: root(ownerA), max_bytes: 2048, search_options: { case_mode: 'insensitive', is_regex: true,
          whole_word: true, word_separators: ' /', is_multiline: true, all_matches: true, context_lines: 2, include_globs: ['*.txt'], exclude_globs: ['**/hidden/**'], ignore_mode: 'respect', excluded_resource_uris: [], excluded_subtree_uris: [] } });
      });
    } finally { f.instantiation.dispose(); }
  });

  test('非法和未支持选项在任何 progress 与 HTTP 前失败；原生容器沿原服务', async () => {
    const local = model(uri(ownerA, 'same.txt'), 'needle'), native = model(URI.parse('workbench:/same.txt'), 'needle native'), f = fixture([local, native]);
    try {
      await withTransport(async calls => {
        for (const extra of [{ surroundingContext: 101 }, { maxFileSize: 0 }, { maxResults: -1 },
          { previewOptions: { matchLines: 2, charsPerLine: 80 } }, { previewOptions: { matchLines: 0, charsPerLine: 80 } },
          { previewOptions: { matchLines: 1, charsPerLine: 0 } }, { previewOptions: { matchLines: 1, charsPerLine: 1.5 } },
          { folderQueries: [{ folder: uri(ownerA), fileEncoding: 'utf16le' }] },
          { folderQueries: [{ folder: uri(ownerA), ignoreSymlinks: true }] },
          { folderQueries: [{ folder: uri(ownerA), disregardParentIgnoreFiles: false }] },
          { folderQueries: [{ folder: uri(ownerA), disregardGlobalIgnoreFiles: false }] },
          { extraFileResources: [URI.parse('workbench:/same.txt')] }]) expect(() => f.search.textSearchSplitSyncAsync(query(extra))).toThrow();
        expect(local.calls).toBe(0); expect(calls).toEqual([]);
        await expect(f.search.aiTextSearch({ ...query(), type: 3, contentPattern: 'needle' })).rejects.toThrow('未启用');
        const original = fixture([native]);
        try {
          original.search.registerSearchResultProvider('workbench', 1, { textSearch: async () => ({ results: [], messages: [] }) });
          const result = await original.search.textSearch(query({ folderQueries: [{ folder: URI.parse('workbench:/') }] }));
          expect(result.results.map(item => item.resource.toString())).toEqual([native.uri.toString()]); expect(calls).toEqual([]);
        } finally { original.instantiation.dispose(); }
      });
    } finally { f.instantiation.dispose(); }
  });

  test('模型和磁盘共用展示上限；跨页上下文去重且剔除哨兵附近无关行', async () => {
    const dirty = model(uri(ownerA, 'dirty.txt'), 'dirty before\nneedle\ndirty after'), f = fixture([dirty]);
    const path = root(ownerA) + '/disk.txt', context = (line, text = `context ${line}`) => ({ uri: path, line, text, truncated: false });
    try {
      await withTransport(async calls => {
        const progress = [], result = await f.search.textSearch(query({ maxResults: 3, surroundingContext: 1 }), undefined, item => progress.push(item));
        expect(result.limitHit).toBe(true);
        const disk = result.results.find(m => m.resource.toString() === uri(ownerA, 'disk.txt').toString());
        expect(disk.results.filter(m => 'lineNumber' in m)).toEqual([{ lineNumber: 1, text: 'context 1' }, { lineNumber: 4, text: 'clipped…' }]);
        expect(JSON.stringify(progress)).not.toContain('irrelevant'); expect(calls).toHaveLength(2);
      }, async (target, cursor) => cursor ? page(target.uri, [3, 10].map(line => ({ ...hit(path), line, end_line: line })), {
        contexts: [context(2), { ...context(4, 'clipped'), truncated: true }, context(9, 'irrelevant'), context(11, 'irrelevant')] })
        : page(target.uri, [{ ...hit(path), line: 2, end_line: 2 }], { truncated: true, next_cursor: 'next', contexts: [context(1), context(3)] }));
    } finally { f.instantiation.dispose(); }
  });

  test('目录和上下文越界明确拒绝，原错误 code / request_id 与受限原因可见', async () => {
    const f = fixture([]);
    try {
      for (const asset of [async target => page(root(ownerB)), async target => page(target.uri, [], { contexts: [{ uri: root(ownerB) + '/foreign.txt', line: 1, text: 'foreign', truncated: false }] })])
        await withTransport(async () => { await expect(f.search.textSearch(query())).rejects.toThrow(); }, asset);
      await withTransport(async () => { await expect(f.search.textSearch(query())).rejects.toThrow('owner_denied · exact cause · request_id: file-request-0'); },
        async () => ({ failure: { code: 'owner_denied', message: 'exact cause' } }));
      await withTransport(async () => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'file' });
        expect(result.limitHit).toBe(true); expect(result.messages[0].text).toContain('code: byte_limit');
      }, async target => page(target.uri, [], { truncated: true, truncation_reason: 'response_byte_limit' }));
    } finally { f.instantiation.dispose(); }
  });

  test('原文件分支失败和取消保留完整诊断，结构化原因不依赖错误文字', async () => {
    const f = fixture([]);
    try {
      for (const diagnostic of [
        { state: 'failed', code: 'execution_failed', error_owner: 'workspace_files', reason: 'read_failed', http_status: 403,
          message: '无法读取 /target/project/secret.txt：权限不足', retryable: false },
        { state: 'cancelled', code: 'cancelled', error_owner: 'workspace_files', reason: 'request_cancelled',
          message: '原目标文件查询已停止', retryable: true },
      ]) await withTransport(async calls => {
        const error = await f.search.textSearch(query()).then(() => undefined, failure => failure);
        expect(error).toBeInstanceOf(Error);
        expect(error.cause).toMatchObject({ type: 'branch', request_id: 'file-request-0', branch: diagnostic });
        expect(error.message).toContain(diagnostic.state === 'cancelled' ? '文件搜索已取消：' : '文件搜索失败：');
        expect(error.message).toContain(diagnostic.message);
        for (const field of ['code', 'error_owner', 'reason', 'http_status']) {
          if (diagnostic[field] === undefined) expect(error.message).not.toContain(`${field}:`);
          else expect(error.message).toContain(`${field}: ${diagnostic[field]}`);
        }
        expect(error.message).toContain('request_id: file-request-0');
        expect(calls).toHaveLength(1);
      }, async () => ({ failure: diagnostic }));
    } finally { f.instantiation.dispose(); }
  });

  test('不可继续的文件受限警告保留原说明、归属和请求身份且不信任命令链接', async () => {
    const f = fixture([]), diagnostic = { message: '原响应预算不足 [执行](command:workbench.action.closeWindow) <b>诊断</b>',
      error_owner: 'workspace_files', reason: 'response_bytes' };
    try {
      await withTransport(async calls => {
        const result = await f.search.fileSearch({ type: 1, folderQueries: [{ folder: uri(ownerA) }], filePattern: 'file' });
        expect(result.limitHit).toBe(true); expect(result.results).toEqual([]);
        expect(result.messages).toHaveLength(1);
        const warning = result.messages[0];
        expect(warning.type).toBe(2); expect(warning.trusted).not.toBe(true);
        for (const value of ['code: byte_limit', diagnostic.message, 'error_owner: workspace_files', 'reason: response_bytes', 'request_id: file-request-0']) {
          expect(warning.text).toContain(value);
        }
        expect(warning.text).not.toContain('http_status:'); expect(calls).toHaveLength(1);
      }, async target => page(target.uri, [], { truncated: true, truncation_reason: 'response_byte_limit' }), diagnostic);
    } finally { f.instantiation.dispose(); }
  });

  test('取消与 dispose 中止实际 Gateway 信号；首条 progress 后取消不投递同页后续命中', async () => {
    for (const mode of ['cancel', 'dispose', 'progress']) {
      const f = fixture([]), token = new CancellationTokenSource(), entered = Promise.withResolvers(), release = Promise.withResolvers();
      try {
        await withTransport(async calls => {
          const progress = [], pending = f.search.textSearch(query(), token.token, item => { progress.push(item); if (mode === 'progress') token.cancel(); });
          await entered.promise;
          if (mode === 'cancel') token.cancel(); else if (mode === 'dispose') f.instantiation.dispose();
          else release.resolve();
          await expect(pending).rejects.toThrow(); expect(calls[0].signal.aborted).toBe(true);
          release.resolve(); await new Promise(resolve => setTimeout(resolve, 5));
          expect(progress).toHaveLength(mode === 'progress' ? 1 : 0);
        }, async target => { entered.resolve(); await release.promise; return page(target.uri, [hit(target.uri + '/first.txt'), hit(target.uri + '/late.txt')]); });
      } finally { release.resolve(); token.dispose(); if (mode !== 'dispose') f.instantiation.dispose(); }
    }
  });

  test('已搜索模型变化、释放或服务释放均取消其网络和迟到 progress，随后释放模型监听', async () => {
    for (const mode of ['change', 'model-dispose', 'service-dispose']) {
      const a = model(uri(ownerA, 'same.txt'), 'removed'), f = fixture([a]);
      const entered = Promise.withResolvers(), release = Promise.withResolvers();
      try {
        await withTransport(async calls => {
          const progress = [], pending = f.search.textSearch(query(), undefined, item => progress.push(item));
          await entered.promise;
          expect(a.watching).toBe(true);
          if (mode === 'change') a.setValue('needle new generation');
          else if (mode === 'model-dispose') a.dispose();
          else f.instantiation.dispose();
          await expect(pending).rejects.toThrow();
          expect(calls[0].signal.aborted).toBe(true); expect(a.watching).toBe(false);
          release.resolve(); await new Promise(resolve => setTimeout(resolve, 5));
          expect(progress).toEqual([]); expect(calls).toHaveLength(1);
        }, async target => { entered.resolve(); await release.promise; return page(target.uri, [hit(target.uri + '/late.txt')]); });
      } finally { release.resolve(); if (mode !== 'service-dispose') f.instantiation.dispose(); a.dispose(); }
    }
  });

  test('A 模型变化不取消独立的 B 查询；完成和同步匹配异常都释放监听', async () => {
    const a = model(uri(ownerA, 'same.txt'), 'removed A'), b = model(uri(ownerB, 'same.txt'), 'removed B'), f = fixture([a, b]);
    const entered = [Promise.withResolvers(), Promise.withResolvers()], release = [Promise.withResolvers(), Promise.withResolvers()];
    try {
      await withTransport(async calls => {
        const progressA = [], progressB = [];
        const pendingA = f.search.textSearch(query(), undefined, item => progressA.push(item));
        const pendingB = f.search.textSearch(query({ folderQueries: [{ folder: uri(ownerB) }] }), undefined, item => progressB.push(item));
        await Promise.all(entered.map(item => item.promise));
        a.setValue('needle in next generation');
        await expect(pendingA).rejects.toThrow();
        expect(calls[0].signal.aborted).toBe(true); expect(calls[1].signal.aborted).toBe(false);
        expect(a.watching).toBe(false); expect(b.watching).toBe(true);
        release[1].resolve();
        expect((await pendingB).results.map(item => item.resource.toString())).toEqual([uri(ownerB, 'disk.txt').toString()]);
        expect(b.watching).toBe(false); expect(progressA).toEqual([]); expect(progressB).toHaveLength(1);
        release[0].resolve(); await new Promise(resolve => setTimeout(resolve, 5));
        expect(progressA).toEqual([]);
      }, async target => { const index = target.uri === root(ownerA) ? 0 : 1; entered[index].resolve(); await release[index].promise;
        return page(target.uri, [hit(target.uri + '/disk.txt')]); });
      a.findMatches = () => { throw new Error('模型匹配原始失败'); };
      expect(() => f.search.textSearchSplitSyncAsync(query())).toThrow('模型匹配原始失败');
      expect(a.watching).toBe(false);
    } finally { release.forEach(item => item.resolve()); f.instantiation.dispose(); a.dispose(); b.dispose(); }
  });

  test('等待 notebook 排除集合时修改模型也取消；同步 progress 中修改不会开始旧代际 HTTP', async () => {
    const a = model(uri(ownerA, 'same.txt'), 'needle'), f = fixture([a]);
    const waiting = Promise.withResolvers();
    try {
      await withTransport(async calls => {
        const split = f.search.textSearchSplitSyncAsync(query(), undefined, undefined, undefined, waiting.promise);
        expect(split.syncResults.results).toHaveLength(1);
        a.setValue('next draft');
        await expect(split.asyncResults).rejects.toThrow();
        expect(a.watching).toBe(false); expect(calls).toEqual([]);
        a.setValue('needle');
        expect(() => f.search.textSearchSplitSyncAsync(query(), undefined, () => a.setValue('changed during progress'))).toThrow();
        expect(a.watching).toBe(false); expect(calls).toEqual([]);
      });
    } finally { waiting.resolve([]); f.instantiation.dispose(); a.dispose(); }
  });

  test('UTF-16 片段及多行位置不从裁剪文本猜测', async () => {
    const f = fixture([]);
    try {
      const projected = textMatch({ ...hit(root(ownerA) + '/emoji.txt'), text: '🧪 needle', utf16_column: 103, utf16_end_column: 5, snippet_start_utf16_column: 100, end_line: 3 });
      expect(projected.rangeLocations[0]).toEqual({ source: { startLineNumber: 0, startColumn: 102, endLineNumber: 2, endColumn: 4 },
        preview: { startLineNumber: 0, startColumn: 3, endLineNumber: 0, endColumn: 9 } });
      expect(resourceInQuery(uri(ownerA, 'nested/a.txt'), query({ includePattern: { '*.txt': true } }), extUri)).toBe(true);
      expect(resourceInQuery(uri(ownerA, 'nested/hidden/a.txt'), query({ excludePattern: { hidden: true } }), extUri)).toBe(false);
      expect(workspaceFileSource(uri(ownerB, 'same.txt')).gatewayId).toBe('gateway-b');
    } finally { f.instantiation.dispose(); }
  });

  test('原版 preview 截取同时应用模型及远端 progress，保留独立 UTF-16 源位置且不额外读正文', async () => {
    const prefix = '🧪'.repeat(100), text = prefix + 'needle' + 'tail'.repeat(100);
    const a = model(uri(ownerA, 'draft.txt'), text), f = fixture([a]);
    const previewOptions = { matchLines: 1, charsPerLine: 30 };
    try {
      await withTransport(async calls => {
        const progress = [], result = await f.search.textSearch(query({ previewOptions }), undefined, item => progress.push(item));
        const local = result.results.find(item => item.resource.toString() === a.uri.toString()).results[0];
        const remote = result.results.find(item => item.resource.toString() === uri(ownerA, 'disk.txt').toString()).results[0];
        expect(remote.previewText).toBe(local.previewText);
        expect(remote.previewText).toContain('needle'); expect(remote.previewText).not.toContain('tail'.repeat(20));
        const range = remote.rangeLocations[0];
        expect(range.source).toEqual({ startLineNumber: 6, startColumn: 1200, endLineNumber: 6, endColumn: 1206 });
        expect(remote.previewText.slice(range.preview.startColumn, range.preview.endColumn)).toBe('needle');
        expect(range.preview).toEqual(local.rangeLocations[0].preview);
        expect(progress[1].results[0]).toBe(remote); expect(calls).toHaveLength(1);
        expect(calls[0].request.queries[0].file_targets[0].target.search_options.excluded_resource_uris).toEqual([root(ownerA) + '/draft.txt']);
      }, async target => page(target.uri, [{ ...hit(target.uri + '/disk.txt'), line: 7, end_line: 7, text,
        utf16_column: 1201, utf16_end_column: 1207, snippet_start_utf16_column: 1001, truncated: true }]));
      const multiline = textMatch({ ...hit(root(ownerA) + '/multi.txt'), line: 7, end_line: 9, text,
        utf16_column: 1201, utf16_end_column: 5, snippet_start_utf16_column: 1001 }, previewOptions);
      expect(multiline.rangeLocations[0].source).toEqual({ startLineNumber: 6, startColumn: 1200, endLineNumber: 8, endColumn: 4 });
      expect(multiline.previewText.length).toBeLessThan(text.length);
      expect(multiline.rangeLocations[0].preview.endColumn).toBeLessThanOrEqual(multiline.previewText.length);
    } finally { f.instantiation.dispose(); a.dispose(); }
  });

  test('原结果模型的长单词／emoji 前缀不挤掉命中，裁剪不切断 Unicode 字符', () => {
    for (const prefix of ['p'.repeat(256), '🧪'.repeat(128)]) {
      const result = textMatch({ ...hit(root(ownerA) + '/long.txt'), text: prefix + 'needle tail',
        utf16_column: 1201, utf16_end_column: 1207, snippet_start_utf16_column: 1201 - prefix.length },
      { matchLines: 1, charsPerLine: 1000 });
      const match = textSearchResultToMatches(result, { resource: uri(ownerA, 'long.txt') }, false)[0];
      const preview = match.preview();
      expect(preview.inside).toBe('needle'); expect(preview.after).toBe(' tail');
      expect(preview.before.length).toBeLessThan(60); expect(preview.before.isWellFormed()).toBe(true);
      expect(match.range().startColumn).toBe(1201); expect(match.range().endColumn).toBe(1207);
    }
  });
});

await writeFile(join(run.artifactsDir, 'boundaries.json'), JSON.stringify({ compiler: 'production esbuild + workbench tsconfig',
  gateway: 'explicit Gateway SSE response assets, real stream client and validation', model: 'in-memory facade with real upstream TextModelSearch',
  notProven: ['Quick Access UI', 'Electron', 'real backend request'] }, null, 2));
