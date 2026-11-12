import { t } from '../../../shared/core/i18n/index.js';
import { raceCancellationError } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ResourceSet } from '../../upstream/src/vs/base/common/map.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IUriIdentityService } from '../../upstream/src/vs/platform/uriIdentity/common/uriIdentity.js';
import { DEFAULT_MAX_SEARCH_RESULTS, IFileMatch, IFileQuery, IFolderQuery, isFilePatternMatch, ISearchComplete, ISearchProgressItem, ISearchResultProvider, ITextQuery, ITextSearchMatch, ITextSearchPreviewOptions, QueryType, TextSearchCompleteMessageType, TextSearchMatch } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { ResourceNavigationContextDTO, ResourceNavigationMatchDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { streamFederatedSearch, searchBranchKey } from '../../../web/src/api/stream/federatedSearch.js';
import type { FederatedSearchContinuationDTO, FederatedSearchEventDTO } from '../../../web/src/types/protocol_generated/boxteam/search/v1/public.js';
import type { GatewaySearchScopeDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { workspaceFileSource, workspaceFileUri } from '../workspaceFiles.js';
import { canonicalFileQuery, fileOptions, openResources, resourceInFolder, resourceInQuery, validateQuery, validateTextQuery } from './query.js';

type SearchRetry = (token: CancellationToken, onProgress: (item: ISearchProgressItem) => void) => Promise<ISearchComplete>;
interface PreparedSearch {
  deadlineAt: number;
  watchRetryModels?: (source: CancellationTokenSource) => IDisposable & { validate(): void };
}
interface SearchResume {
  scope: GatewaySearchScopeDTO;
  branch: FederatedSearchContinuationDTO;
  pageLimit: number;
  exactResources: URI[];
  budget: { remaining: number; seen: Set<string> };
}

function branchDiagnostic(event: FederatedSearchEventDTO): string {
  const status = event.branch!;
  return [status.code && `code: ${status.code}`, status.message,
    status.error_owner && `error_owner: ${status.error_owner}`, status.reason && `reason: ${status.reason}`,
    typeof status.http_status === 'number' && `http_status: ${status.http_status}`, `request_id: ${event.request_id}`,
    `retryable: ${status.retryable}`, `branch: ${JSON.stringify(status.branch)}`].filter(Boolean).join(' · ');
}

/** 保留原分支事件；重试仍走同一个文件 Provider，不携带另一份匹配正文。 */
export class WorkspaceSearchBranchError extends Error {
  declare readonly cause: FederatedSearchEventDTO;

  constructor(event: FederatedSearchEventDTO, readonly messages: ISearchComplete['messages'], readonly limitHit: boolean,
    readonly retry?: SearchRetry) {
    super(t(event.branch!.state === 'cancelled' ? 'quickAccess.files.cancelled' : 'quickAccess.files.failed',
      (event.branch!.code === 'deadline' ? t('quickAccess.timeoutPrefix') : '') + branchDiagnostic(event)), { cause: event });
    this.name = 'WorkspaceSearchBranchError';
  }
}

/** 只识别本文件的具名分支错误；传输、协议及未知错误保持原失败。 */
export function workspaceSearchBranchErrors(error: unknown): WorkspaceSearchBranchError[] | undefined {
  if (error instanceof WorkspaceSearchBranchError) return [error];
  if (error instanceof AggregateError && error.errors.length && error.errors.every(item => item instanceof WorkspaceSearchBranchError)) return error.errors;
  return undefined;
}

/** 只把原文件 owner 的精确区间投影为搜索结果，不为片段再读一次正文。 */
export function textMatch(hit: ResourceNavigationMatchDTO, previewOptions?: ITextSearchPreviewOptions): ITextSearchMatch {
  if (hit.end_line === undefined || hit.utf16_column === undefined || hit.utf16_end_column === undefined || hit.snippet_start_utf16_column === undefined) {
    throw new Error(t('quickAccess.files.missingUtf16Range'));
  }
  const start = hit.utf16_column - hit.snippet_start_utf16_column;
  const end = hit.end_line === hit.line ? hit.utf16_end_column - hit.snippet_start_utf16_column : hit.text.length;
  // 原 owner 只交付有界首行片段；复用原版预览截取，真实跳转区间保持独立。
  const match = new TextSearchMatch(hit.text + '\n', { startLineNumber: 0, startColumn: Math.min(start, hit.text.length),
    endLineNumber: 0, endColumn: Math.min(end, hit.text.length) }, previewOptions);
  match.rangeLocations[0].source = { startLineNumber: hit.line - 1, startColumn: hit.utf16_column - 1,
    endLineNumber: hit.end_line - 1, endColumn: hit.utf16_end_column - 1 };
  const preview = match.rangeLocations[0].preview;
  match.rangeLocations[0].preview = { ...preview, endColumn: Math.min(preview.endColumn, match.previewText.length) };
  return match;
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
    models = new ResourceSet(), prepared?: PreparedSearch): Promise<ISearchComplete> {
    return this.search(query, onProgress, token, models, prepared);
  }

  private async search(query: IFileQuery | ITextQuery, onProgress?: (item: ISearchProgressItem) => void,
    token = CancellationToken.None, models = new ResourceSet(), prepared?: PreparedSearch, resume?: SearchResume): Promise<ISearchComplete> {
    const deadlineAt = prepared?.deadlineAt ?? Date.now() + 10000;
    if (!prepared) query = await canonicalFileQuery(query, token, deadlineAt);
    const identity = this.uriIdentity.extUri;
    query = query.type === QueryType.Text ? validateTextQuery(query, identity) : validateQuery(query, identity);
    models = new ResourceSet([...models], this.resourceKey);
    if (token.isCancellationRequested) throw new CancellationError();
    if (this._store.isDisposed) throw new Error(t('quickAccess.files.disposed'));
    const source = new CancellationTokenSource(token), controller = new AbortController();
    const cancellation = source.token.onCancellationRequested(() => controller.abort());
    this.active.add(source);
    let timedOut = false;
    // 沿原导航的十秒预算限制整个查询；续页不重新获得完整等待期限。
    const timer = setTimeout(() => { timedOut = true; source.cancel(); }, Math.max(0, deadlineAt - Date.now()));
    if (deadlineAt <= Date.now()) { timedOut = true; source.cancel(); }
    const results = new Map<string, IFileMatch>();
    const contexts = new Map<string, Map<number, ResourceNavigationContextDTO>>();
    const messages: ISearchComplete['messages'] = [];
    let limitHit = false;
    let continuation = resume && { scope: resume.scope, branches: [resume.branch] };
    let frozenScope = resume?.scope;
    const failures: FederatedSearchEventDTO[] = [];
    // 原 FileSearchManager 把 0 当预热哨兵；这里无缓存，只执行服务硬上限内的有界召回。
    const maximum = query.type === QueryType.File && query.maxResults === 0 ? 500 : query.maxResults ?? DEFAULT_MAX_SEARCH_RESULTS;
    const budget = resume?.budget ?? { remaining: maximum, seen: new Set<string>() };
    // 冻结本代际覆盖集合；每个目标只发送其自身根内的规范身份。
    const coveredResources = [...models];
    const seen = budget.seen;
    const exactResources = resume?.exactResources ?? (query.onlyOpenEditors ? openResources(this.editors, query, identity)
      : (query.extraFileResources ?? []).filter(resource => resourceInQuery(resource, query, identity)
        && !query.folderQueries.some(folder => identity.isEqual(resource, folder.folder))));
    const targets: { resource: URI; folder: IFolderQuery; exact: boolean }[] = [
      ...(query.onlyOpenEditors || query.type === QueryType.Text ? exactResources : [])
        .map(resource => ({ resource, folder: query.folderQueries.find(folder => resourceInFolder(resource, query, folder, identity))!, exact: true })),
      ...(query.onlyOpenEditors ? [] : query.folderQueries.map(folder => ({ resource: folder.folder, folder, exact: false }))),
    ];
    let modelWatch: ReturnType<NonNullable<PreparedSearch['watchRetryModels']>> | undefined;
    try {
      if (resume) modelWatch = prepared?.watchRetryModels?.(source);
      if (source.token.isCancellationRequested) throw new CancellationError();
      // 原 FileSearchManager 直接匹配额外文件；名称发现不为这些已给定身份重读正文。
      if (query.type === QueryType.File && !query.onlyOpenEditors) {
        for (const resource of exactResources) {
          const folder = query.folderQueries.find(folder => resourceInFolder(resource, query, folder, identity))!;
          if (query.filePattern && !isFilePatternMatch({ relativePath: identity.relativePath(folder.folder, resource)!, searchPath: undefined },
            query.filePattern, !query.shouldGlobMatchFilePattern)) continue;
          if (query.exists || budget.remaining <= 0) return { results: [...results.values()], messages, limitHit: true };
          results.set(this.resourceKey(resource), { resource });
          budget.remaining--;
        }
      }
      const operation = query.type === QueryType.Text ? 'content' : 'name';
      const planned = targets.flatMap(({ resource: target, folder, exact }) => {
        if (query.type === QueryType.Text && models.has(target)) return [];
        if (exact && query.type === QueryType.File && query.filePattern && !isFilePatternMatch({
          relativePath: identity.relativePath(folder.folder, target)!, searchPath: undefined,
        }, query.filePattern, !query.shouldGlobMatchFilePattern)) return [];
        const owner = workspaceFileSource(target);
        if (resume && searchBranchKey(resume.branch.branch!) !== searchBranchKey({ owner: { gateway_id: owner.gatewayId,
          workspace_id: owner.workspaceId }, category: 'file', operation, resource_uri: owner.path })) return [];
        const options = fileOptions(query, folder, identity);
        // 显式文件已按原目录语义过滤；不把相对目录模式重新应用到 basename。
        if (exact) { options.include_globs = []; options.exclude_globs = []; options.excluded_subtree_uris = []; }
        else options.excluded_resource_uris = exactResources.filter(resource => resourceInFolder(resource, query, folder, identity))
          .map(resource => workspaceFileSource(resource).path).sort();
        if (query.type === QueryType.Text) Object.assign(options, {
          case_mode: query.contentPattern.isCaseSensitive ? 'sensitive' : 'insensitive',
          is_regex: !!query.contentPattern.isRegExp, whole_word: !!query.contentPattern.isWordMatch,
          word_separators: query.contentPattern.wordSeparators, is_multiline: !!query.contentPattern.isMultiline, all_matches: true,
          context_lines: query.surroundingContext ?? 0,
          excluded_resource_uris: [...new Set([...options.excluded_resource_uris,
            ...coveredResources.filter(resource => !exact && resourceInFolder(resource, query, folder, identity))
              .map(resource => workspaceFileSource(resource).path)])].sort(),
        });
        return [{ target, folder, exact, owner, wire: { owner: { gateway_id: owner.gatewayId, workspace_id: owner.workspaceId },
          target: { uri: owner.path, search_options: options, max_bytes: query.type === QueryType.Text ? query.maxFileSize ?? 65536 : 65536 } } }];
      });
      if (!planned.length) return { results: [...results.values()], messages, limitHit: false };
      const byKey = new Map(planned.map(item => [searchBranchKey({ owner: item.wire.owner, category: 'file', operation, resource_uri: item.owner.path }), item]));
      // 页大小保持不变；后续只读未完成根，不因分支减少改变原 cursor 的绑定。
      const pageLimit = resume?.pageLimit ?? Math.max(1, Math.min(500, Math.floor(1000 / planned.length), query.type === QueryType.File && query.exists ? 1 : Math.floor((budget.remaining + 1) / planned.length)));
      const owners = [...new Map(planned.map(item => [JSON.stringify(item.wire.owner), item.wire.owner])).values()];
      const requestQuery = { category: 'file', operation, contract_version: 1,
        query: query.type === QueryType.Text ? query.contentPattern.pattern : query.onlyOpenEditors ? '**/*' : query.filePattern || '**/*',
        match_mode: query.type === QueryType.Text ? query.contentPattern.isRegExp ? 'regex' : 'literal'
          : query.onlyOpenEditors || query.shouldGlobMatchFilePattern || !query.filePattern ? 'glob' : 'fuzzy',
        limit: pageLimit, file_targets: planned.map(item => item.wire), diagnostic_targets: [] };
      do {
        if (source.token.isCancellationRequested) throw new CancellationError();
        let scope = continuation?.scope;
        const branches: FederatedSearchContinuationDTO[] = [];
        let moreFiles = false;
        await raceCancellationError(streamFederatedSearch(0, {
          scope: scope ? { mode: 'resume', scope, owners: [] } : { mode: 'selected', owners },
          queries: [requestQuery], continuations: continuation?.branches ?? [], max_results: pageLimit * planned.length,
          deadline_at: deadlineAt / 1000,
        }, { signal: controller.signal, onEvent: event => {
          if (source.token.isCancellationRequested) throw new CancellationError();
          if (event.type === 'scope') { frozenScope = scope = event.scope!.scope; return; }
          if (!event.branch?.branch) return;
          const plan = byKey.get(searchBranchKey(event.branch.branch));
          if (!plan) throw new Error(t('quickAccess.files.unexpectedRoot'));
          const { owner, folder, target, exact } = plan;
          if (event.type === 'branch') {
            for (const notice of event.branch.diagnostics ?? []) messages.push({
              type: notice.severity === 'info' ? TextSearchCompleteMessageType.Information : TextSearchCompleteMessageType.Warning,
              text: `${notice.message} · code: ${notice.code} · owner: ${notice.owner} · request_id: ${event.request_id}`,
              trusted: false,
            });
            const diagnostic = branchDiagnostic(event);
            // 原错误留待流结算；其它成功分支及其续页仍按原顺序交付。
            if (event.branch.state === 'failed' || event.branch.state === 'cancelled') { failures.push(event); return; }
            if (event.branch.state === 'limited') {
              if (query.type === QueryType.File && event.branch.code === 'result_limit') moreFiles = true;
              if (event.branch.cursor || event.branch.code === 'output_limit') branches.push({ branch: event.branch.branch, cursor: event.branch.cursor });
              else {
                limitHit = true;
                messages.push({ type: TextSearchCompleteMessageType.Warning, text: t('quickAccess.files.limited', diagnostic, owner.path) });
              }
            }
            return;
          }
          if (event.type !== 'page') return;
          const page = event.page?.files;
          if (!page || page.node?.uri !== owner.path) throw new Error(t('quickAccess.files.targetMismatch'));
          if (exact && page.node.resource_shape !== 'file') throw new Error(t('quickAccess.files.expectedFile'));
          const canonical = new Map(page.entries.map(entry => [entry.uri, entry.canonical_uri ?? entry.uri]));
          const rows = query.type === QueryType.Text ? page.matches.map(hit => ({ uri: hit.uri, hit })) : page.entries
            .filter(entry => entry.resource_shape === 'file').map(entry => ({ uri: entry.uri, hit: undefined }));
          for (const row of rows) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const resource = workspaceFileUri(owner, canonical.get(row.uri) ?? row.uri, owner.root);
            if (exact && !identity.isEqual(resource, target)) throw new Error(t('quickAccess.files.exactFileMismatch'));
            if (!resourceInFolder(resource, query, folder, identity)) throw new Error(t('quickAccess.files.outsideScope'));
            if (models.has(resource)) continue;
            const key = this.resourceKey(resource);
            const matchKey = key + (row.hit ? `:${row.hit.line}:${row.hit.utf16_column}:${row.hit.end_line}:${row.hit.utf16_end_column}` : '');
            if (seen.has(matchKey)) continue;
            if (budget.remaining <= 0 || (query.type === QueryType.File && query.exists)) { limitHit = true; break; }
            seen.add(matchKey);
            budget.remaining--;
            const match = results.get(key) ?? { resource, results: [] };
            const text = row.hit ? textMatch(row.hit, query.type === QueryType.Text ? query.previewOptions : undefined) : undefined;
            if (text) match.results!.push(text);
            results.set(key, match);
            onProgress?.({ resource, results: text ? [text] : undefined });
          }
          for (const line of page.contexts) {
            if (source.token.isCancellationRequested) throw new CancellationError();
            const resource = workspaceFileUri(owner, canonical.get(line.uri) ?? line.uri, owner.root);
            if (exact && !identity.isEqual(resource, target)) throw new Error(t('quickAccess.files.contextFileMismatch'));
            if (!resourceInFolder(resource, query, folder, identity)) throw new Error(t('quickAccess.files.contextOutsideScope'));
            if (models.has(resource)) continue;
            const key = this.resourceKey(resource);
            const lines = contexts.get(key) ?? new Map<number, ResourceNavigationContextDTO>();
            lines.set(line.line, line);
            contexts.set(key, lines);
          }
        } }), source.token);
        if (!scope) throw new Error(t('quickAccess.files.missingFrozenScope'));
        // 原 owner 的 result_limit 已确认下一文件；字节受限仍须沿续点探测。
        if (budget.remaining <= 0 && moreFiles) limitHit = true;
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
          }
        }
      }
      if (source.token.isCancellationRequested) throw new CancellationError();
      modelWatch?.validate();
      if (failures.length) {
        const scope = frozenScope!;
        const errors = failures.map(event => {
          const branch = { branch: event.branch!.branch, cursor: event.branch!.cursor };
          const retry: SearchRetry | undefined = query.type === QueryType.Text && event.branch!.retryable && budget.remaining > 0 ? (retryToken, progress) => {
            // 每次显式重试只读原失败输入；已交付匹配共用同一剩余额度和去重集合。
            if (budget.remaining <= 0) return Promise.reject(new Error(t('quickAccess.queryLimited', t('quickAccess.moreResults'))));
            return this.search(query, progress, retryToken, models, { ...prepared, deadlineAt: Date.now() + 10000 }, { scope, branch, pageLimit, exactResources, budget });
          } : undefined;
          return new WorkspaceSearchBranchError(event, messages, limitHit || budget.remaining <= 0, retry);
        });
        throw errors.length === 1 ? errors[0] : new AggregateError(errors, errors.map(error => error.message).join('\n'));
      }
      return { results: [...results.values()], messages, limitHit };
    } catch (error) {
      modelWatch?.validate();
      if (timedOut) throw new Error(t('quickAccess.files.deadlineExceeded'), { cause: error });
      throw error;
    } finally {
      modelWatch?.dispose(); clearTimeout(timer); cancellation.dispose(); source.dispose(); this.active.delete(source);
    }
  }
}
