import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { DEFAULT_MAX_SEARCH_RESULTS, IFileMatch, IFileQuery, IFolderQuery, isFilePatternMatch, ISearchComplete, ISearchProgressItem, ISearchResultProvider, ITextQuery, ITextSearchMatch, QueryType, TextSearchCompleteMessageType } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { ResourceNavigationContextDTO, ResourceNavigationMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { streamFederatedSearch, searchBranchKey } from '../../../web/src/api/stream/federatedSearch.js';
import type { FederatedSearchContinuationDTO } from '../../../web/src/types/protocol_generated/boxteam/search/v1/public.js';
import type { GatewaySearchScopeDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v1/public.js';
import { workspaceFileSource, workspaceFileUri } from '../workspaceFiles.js';
import { fileOptions, openResources, resourceInQuery, validateQuery, validateTextQuery } from './query.js';

/** 只把原文件 owner 的精确区间投影为搜索结果，不为片段再读一次正文。 */
export function textMatch(hit: ResourceNavigationMatchDTO): ITextSearchMatch {
  if (hit.end_line === undefined || hit.utf16_column === undefined || hit.utf16_end_column === undefined || hit.snippet_start_utf16_column === undefined) {
    throw new Error('文件搜索响应缺少完整 UTF-16 区间');
  }
  const start = hit.utf16_column - hit.snippet_start_utf16_column;
  const end = hit.end_line === hit.line ? hit.utf16_end_column - hit.snippet_start_utf16_column : hit.text.length;
  return { previewText: hit.text + '\n', rangeLocations: [{
    source: { startLineNumber: hit.line - 1, startColumn: hit.utf16_column - 1,
      endLineNumber: hit.end_line - 1, endColumn: hit.utf16_end_column - 1 },
    // 后端预览仅含起始行；多行或长命中只高亮实际可见部分。
    preview: { startLineNumber: 0, startColumn: Math.min(start, hit.text.length), endLineNumber: 0,
      endColumn: Math.min(end, hit.text.length) },
  }] };
}

export class WorkspaceSearchProvider extends Disposable implements ISearchResultProvider {
  private readonly active = new Set<CancellationTokenSource>();
  private readonly resourceKey = (resource: URI) => this.uriIdentity.extUri.getComparisonKey(resource);

  constructor(@IEditorService private readonly editors: IEditorService,
    @IUriIdentityService private readonly uriIdentity: IUriIdentityService) { super(); }

  override dispose(): void {
    for (const source of this.active) source.cancel();
    super.dispose();
  }

  async getAIName(): Promise<undefined> { return undefined; }
  async clearCache(): Promise<void> { /* 原文件 owner 不持有查询缓存。 */ }

  fileSearch(query: IFileQuery, token?: CancellationToken): Promise<ISearchComplete> {
    return this.search(query, undefined, token);
  }

  textSearch(query: ITextQuery, onProgress?: (item: ISearchProgressItem) => void, token?: CancellationToken,
    models = new ResourceSet()): Promise<ISearchComplete> {
    return this.search(query, onProgress, token, models);
  }

  private async search(query: IFileQuery | ITextQuery, onProgress?: (item: ISearchProgressItem) => void,
    token = CancellationToken.None, models = new ResourceSet()): Promise<ISearchComplete> {
    const identity = this.uriIdentity.extUri;
    query = query.type === QueryType.Text ? validateTextQuery(query, identity) : validateQuery(query, identity);
    models = new ResourceSet([...models], this.resourceKey);
    if (token.isCancellationRequested) throw new CancellationError();
    if (this._store.isDisposed) throw new Error('文件搜索服务已释放');
    const source = new CancellationTokenSource(token), controller = new AbortController();
    const cancellation = source.token.onCancellationRequested(() => controller.abort());
    this.active.add(source);
    let timedOut = false;
    // 沿原导航的十秒预算限制整个查询；续页不重新获得完整等待期限。
    const deadlineAt = Date.now() + 10000;
    const timer = setTimeout(() => { timedOut = true; source.cancel(); }, Math.max(0, deadlineAt - Date.now()));
    if (deadlineAt <= Date.now()) { timedOut = true; source.cancel(); }
    const results = new Map<string, IFileMatch>();
    const contexts = new Map<string, Map<number, ResourceNavigationContextDTO>>();
    const messages: ISearchComplete['messages'] = [];
    let count = 0, limitHit = false;
    let continuation: { scope: GatewaySearchScopeDTO; branches: FederatedSearchContinuationDTO[] } | undefined;
    // 原 FileSearchManager 把 0 当预热哨兵；这里无缓存，只执行服务硬上限内的有界召回。
    const maximum = query.type === QueryType.File && query.maxResults === 0 ? 500 : query.maxResults ?? DEFAULT_MAX_SEARCH_RESULTS;
    // 冻结本代际覆盖集合；每个目标只发送其自身根内的规范身份。
    const coveredResources = [...models];
    const seen = new Set<string>();
    const targets: { resource: URI; folder: IFolderQuery }[] = query.onlyOpenEditors
      ? openResources(this.editors, query, identity).map(resource => ({ resource, folder: query.folderQueries.find(folder => resourceInQuery(resource, { ...query, folderQueries: [folder] }, identity))! }))
      : query.folderQueries.map(folder => ({ resource: folder.folder, folder }));
    try {
      const planned = targets.flatMap(({ resource: target, folder }) => {
        if (query.type === QueryType.Text && models.has(target)) return [];
        if (query.onlyOpenEditors && query.type === QueryType.File && query.filePattern && !isFilePatternMatch({
          relativePath: identity.relativePath(folder.folder, target)!, searchPath: undefined,
        }, query.filePattern, !query.shouldGlobMatchFilePattern)) return [];
        const owner = workspaceFileSource(target);
        const options = fileOptions(query, folder, identity);
        // 已打开文件已按原目录语义过滤；精确文件请求不重新把 include 当 basename 模式。
        if (query.onlyOpenEditors) { options.include_globs = []; options.exclude_globs = []; }
        if (query.type === QueryType.Text) Object.assign(options, {
          case_mode: query.contentPattern.isCaseSensitive ? 'sensitive' : 'insensitive',
          is_regex: !!query.contentPattern.isRegExp, whole_word: !!query.contentPattern.isWordMatch,
          word_separators: query.contentPattern.wordSeparators, is_multiline: !!query.contentPattern.isMultiline, all_matches: true,
          context_lines: query.surroundingContext ?? 0,
          excluded_resource_uris: coveredResources.filter(resource => !query.onlyOpenEditors && resourceInQuery(resource, { ...query, folderQueries: [folder] }, identity))
            .map(resource => workspaceFileSource(resource).path).sort(),
        });
        return [{ target, folder, owner, wire: { owner: { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId },
          target: { uri: owner.path, search_options: options, max_bytes: query.type === QueryType.Text ? query.maxFileSize ?? 65536 : 65536 } } }];
      });
      if (!planned.length) return { results: [], messages: [], limitHit: false };
      const operation = query.type === QueryType.Text ? 'content' : 'name';
      const byKey = new Map(planned.map(item => [searchBranchKey({ owner: item.wire.owner, category: 'file', operation, resource_uri: item.owner.path }), item]));
      // 页大小保持不变；后续只读未完成根，不因分支减少改变原 cursor 的绑定。
      const pageLimit = Math.max(1, Math.min(500, Math.floor(1000 / planned.length), query.type === QueryType.File && query.exists ? 1 : Math.floor((maximum + 1) / planned.length)));
      const owners = [...new Map(planned.map(item => [JSON.stringify(item.wire.owner), item.wire.owner])).values()];
      const requestQuery = { category: 'file', operation, contract_version: 1,
        query: query.type === QueryType.Text ? query.contentPattern.pattern : query.onlyOpenEditors ? '**/*' : query.filePattern || '**/*',
        match_mode: query.type === QueryType.Text ? query.contentPattern.isRegExp ? 'regex' : 'literal'
          : query.onlyOpenEditors || query.shouldGlobMatchFilePattern || !query.filePattern ? 'glob' : 'fuzzy',
        limit: pageLimit, file_targets: planned.map(item => item.wire) };
      do {
        if (source.token.isCancellationRequested) throw new CancellationError();
        let scope = continuation?.scope;
        const branches: FederatedSearchContinuationDTO[] = [];
        await raceCancellationError(streamFederatedSearch(0, {
          scope: scope ? { mode: 'resume', scope, owners: [] } : { mode: 'selected', owners },
          queries: [requestQuery], continuations: continuation?.branches ?? [], max_results: pageLimit * planned.length,
          deadline_at: deadlineAt / 1000,
        }, { signal: controller.signal, onEvent: event => {
          if (source.token.isCancellationRequested) throw new CancellationError();
          if (event.type === 'scope') { scope = event.scope!.scope; return; }
          if (!event.branch?.branch) return;
          const plan = byKey.get(searchBranchKey(event.branch.branch));
          if (!plan) throw new Error('文件搜索返回了其它目录根');
          const { owner, folder, target } = plan;
          if (event.type === 'branch') {
            if (event.branch.state === 'failed' || event.branch.state === 'cancelled') throw new Error(
              `文件搜索失败：${event.branch.code} · ${event.branch.message ?? ''} · request_id: ${event.request_id}`);
            if (event.branch.state === 'limited') {
              if (event.branch.cursor || event.branch.code === 'output_limit') branches.push({ branch: event.branch.branch, cursor: event.branch.cursor });
              else {
                limitHit = true;
                messages.push({ type: TextSearchCompleteMessageType.Warning, text: `文件搜索受限：${event.branch.code}（${owner.path}）` });
              }
            }
            return;
          }
          if (event.type !== 'page') return;
          const page = event.page?.files;
          if (!page || page.node?.uri !== owner.path) throw new Error('文件搜索响应目标与请求不一致');
          const rows = query.type === QueryType.Text ? page.matches.map(hit => ({ uri: hit.uri, hit })) : page.entries
            .filter(entry => entry.resource_shape === 'file').map(entry => ({ uri: entry.uri, hit: undefined }));
          for (const row of rows) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const resource = workspaceFileUri(owner, row.uri, owner.root);
            if (query.onlyOpenEditors && !identity.isEqual(resource, target)) throw new Error('精确文件搜索返回了其它文件');
            if (!resourceInQuery(resource, { ...query, folderQueries: [folder] }, identity)) throw new Error('文件搜索响应越过本次目录或过滤范围');
            if (models.has(resource)) continue;
            const key = this.resourceKey(resource);
            const matchKey = key + (row.hit ? `:${row.hit.line}:${row.hit.utf16_column}:${row.hit.end_line}:${row.hit.utf16_end_column}` : '');
            if (seen.has(matchKey)) continue;
            seen.add(matchKey);
            if (count >= maximum || (query.type === QueryType.File && query.exists)) { limitHit = true; break; }
            count++;
            const match = results.get(key) ?? { resource, results: [] };
            if (row.hit) match.results!.push(textMatch(row.hit));
            results.set(key, match);
            onProgress?.({ resource, results: row.hit ? [textMatch(row.hit)] : undefined });
          }
          for (const line of page.contexts) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const resource = workspaceFileUri(owner, line.uri, owner.root);
            if (query.onlyOpenEditors && !identity.isEqual(resource, target)) throw new Error('精确文件搜索返回了其它文件的上下文');
            if (!resourceInQuery(resource, { ...query, folderQueries: [folder] }, identity)) throw new Error('文件搜索上下文越过本次目录或过滤范围');
            if (models.has(resource)) continue;
            const key = this.resourceKey(resource);
            const lines = contexts.get(key) ?? new Map<number, ResourceNavigationContextDTO>();
            lines.set(line.line, line);
            contexts.set(key, lines);
          }
        } }), source.token);
        if (!scope) throw new Error('文件搜索缺少冻结范围');
        continuation = branches.length ? { scope, branches } : undefined;
      } while (continuation && !limitHit);
      if (source.token.isCancellationRequested) throw new CancellationError();
      if (query.type === QueryType.Text && query.surroundingContext) {
        for (const [key, file] of results) {
          const ranges = file.results!.flatMap(match => 'rangeLocations' in match ? match.rangeLocations.map(location => location.source) : [])
            .sort((a, b) => a.startLineNumber - b.startLineNumber);
          let next = 0, previousEnd = -1;
          // 完成后按最终接受的命中去重；跨页命中和上限哨兵不能留下重复或无关上下文。
          for (const line of [...(contexts.get(key)?.values() ?? [])].sort((a, b) => a.line - b.line)) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const row = line.line - 1;
            while (next < ranges.length && ranges[next].endLineNumber < row) {
              previousEnd = Math.max(previousEnd, ranges[next++].endLineNumber);
            }
            if (next < ranges.length && ranges[next].startLineNumber <= row) continue;
            if (!(previousEnd >= 0 && row - previousEnd <= query.surroundingContext)
              && !(next < ranges.length && ranges[next].startLineNumber - row <= query.surroundingContext)) continue;
            const context = { lineNumber: line.line, text: line.text + (line.truncated ? '…' : '') };
            file.results!.push(context);
            onProgress?.({ resource: file.resource, results: [context] });
            if (line.truncated) messages.push({ type: TextSearchCompleteMessageType.Warning, text: `搜索上下文行已截断：${line.uri}:${line.line}` });
          }
        }
      }
      if (source.token.isCancellationRequested) throw new CancellationError();
      return { results: [...results.values()], messages, limitHit };
    } catch (error) {
      if (timedOut) throw new Error('文件搜索超过本次总期限', { cause: error });
      throw error;
    } finally {
      clearTimeout(timer); cancellation.dispose(); source.dispose(); this.active.delete(source);
    }
  }
}
