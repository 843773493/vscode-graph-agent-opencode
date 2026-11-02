import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import { relativePath } from '../../upstream/src/vs/base/common/resources.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { DEFAULT_MAX_SEARCH_RESULTS, IFileMatch, IFileQuery, IFolderQuery, isFilePatternMatch, ISearchComplete, ISearchProgressItem, ISearchResultProvider, ITextQuery, ITextSearchMatch, QueryType, TextSearchCompleteMessageType } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { ResourceNavigationMatchDTO, ResourceNavigationResultDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from '../agentApi.js';
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

  constructor(@IEditorService private readonly editors: IEditorService) { super(); }

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
    if (query.type === QueryType.Text) validateTextQuery(query); else validateQuery(query);
    if (token.isCancellationRequested) throw new CancellationError();
    if (this._store.isDisposed) throw new Error('文件搜索服务已释放');
    const source = new CancellationTokenSource(token), controller = new AbortController();
    const cancellation = source.token.onCancellationRequested(() => controller.abort());
    this.active.add(source);
    let timedOut = false;
    // 沿原导航的十秒预算限制整个查询；续页不重新获得完整等待期限。
    const timer = setTimeout(() => { timedOut = true; source.cancel(); }, 10000);
    const results = new Map<string, IFileMatch>();
    const messages: ISearchComplete['messages'] = [];
    let count = 0, limitHit = false;
    const maximum = query.maxResults ?? DEFAULT_MAX_SEARCH_RESULTS;
    const routes = new Map<string, Promise<string>>();
    const seen = new Set<string>();
    const targets: { resource: URI; folder: IFolderQuery }[] = query.onlyOpenEditors
      ? openResources(this.editors, query).map(resource => ({ resource, folder: query.folderQueries.find(folder => resourceInQuery(resource, { ...query, folderQueries: [folder] }))! }))
      : query.folderQueries.map(folder => ({ resource: folder.folder, folder }));
    try {
      for (const { resource: target, folder } of targets) {
        if (limitHit) break;
        if (query.type === QueryType.Text && models.has(target)) continue;
        if (query.onlyOpenEditors && query.type === QueryType.File && query.filePattern && !isFilePatternMatch({
          relativePath: relativePath(folder.folder, target)!, searchPath: undefined,
        }, query.filePattern, !query.shouldGlobMatchFilePattern)) continue;
        if (source.token.isCancellationRequested) throw new CancellationError();
        const owner = workspaceFileSource(target), key = JSON.stringify([owner.gatewayId, owner.workspaceId]);
        let route = routes.get(key);
        if (!route) { route = gateway.resolveWorkspace(owner, controller.signal); routes.set(key, route); }
        const routeId = await raceCancellationError(route, source.token);
        const options = fileOptions(query, folder);
        // 已打开文件已按原目录语义过滤；精确文件请求不重新把 include 当 basename 模式。
        if (query.onlyOpenEditors) { options.include_globs = []; options.exclude_globs = []; }
        if (query.type === QueryType.Text) Object.assign(options, {
          case_mode: query.contentPattern.isCaseSensitive ? 'sensitive' : 'insensitive',
          is_regex: !!query.contentPattern.isRegExp, whole_word: !!query.contentPattern.isWordMatch,
          word_separators: query.contentPattern.wordSeparators, is_multiline: !!query.contentPattern.isMultiline, all_matches: true,
        });
        // 固定每一目标的请求合同，分页 cursor 绑定包括 limit 在内的全部选项。
        const request = { uri: owner.path, operation: 'search', query: query.type === QueryType.Text ? query.contentPattern.pattern : query.onlyOpenEditors ? '**/*' : query.filePattern || '**/*',
          search_mode: query.type === QueryType.Text ? 'content' : query.onlyOpenEditors || query.shouldGlobMatchFilePattern || !query.filePattern ? 'glob' : 'fuzzy',
          search_options: options, limit: Math.max(1, Math.min(1000, maximum + 1)),
          ...(query.type === QueryType.Text && query.maxFileSize !== undefined ? { max_bytes: query.maxFileSize } : {}) };
        let cursor: string | undefined;
        do {
          if (source.token.isCancellationRequested) throw new CancellationError();
          const page = await raceCancellationError(gateway.request<ResourceNavigationResultDTO>('/api/v1/resources/navigation', {
            method: 'POST', body: JSON.stringify({ ...request, cursor }), signal: controller.signal,
          }, routeId), source.token);
          if (source.token.isCancellationRequested) throw new CancellationError();
          if (page.node?.uri !== owner.path) throw new Error('文件搜索响应目标与请求不一致');
          const rows = query.type === QueryType.Text ? page.matches.map(hit => ({ uri: hit.uri, hit })) : page.entries
            .filter(entry => entry.resource_shape === 'file').map(entry => ({ uri: entry.uri, hit: undefined }));
          for (const row of rows) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const resource = workspaceFileUri(owner, row.uri, owner.root);
            if (query.onlyOpenEditors && resource.toString() !== target.toString()) throw new Error('精确文件搜索返回了其它文件');
            if (!resourceInQuery(resource, { ...query, folderQueries: [folder] })) throw new Error('文件搜索响应越过本次目录或过滤范围');
            if (models.has(resource)) continue;
            const identity = resource.toString() + (row.hit ? `:${row.hit.line}:${row.hit.utf16_column}:${row.hit.end_line}:${row.hit.utf16_end_column}` : '');
            if (seen.has(identity)) continue;
            seen.add(identity);
            if (count >= maximum || (query.type === QueryType.File && query.exists)) { limitHit = true; break; }
            count++;
            const match = results.get(resource.toString()) ?? { resource, results: [] };
            if (row.hit) match.results!.push(textMatch(row.hit));
            results.set(resource.toString(), match);
            onProgress?.({ resource, results: row.hit ? [textMatch(row.hit)] : undefined });
          }
          cursor = page.next_cursor;
          if (page.truncated && !cursor) {
            limitHit = true;
            messages.push({ type: TextSearchCompleteMessageType.Warning, text: `文件搜索受限：${page.truncation_reason ?? '未知预算'}（${owner.path}）` });
          }
        } while (cursor && !limitHit);
      }
      if (source.token.isCancellationRequested) throw new CancellationError();
      return { results: [...results.values()], messages, limitHit };
    } catch (error) {
      if (timedOut) throw new Error('文件搜索超过本次十秒总期限', { cause: error });
      throw error;
    } finally {
      clearTimeout(timer); cancellation.dispose(); source.dispose(); this.active.delete(source);
    }
  }
}
