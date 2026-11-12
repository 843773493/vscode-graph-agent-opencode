import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { toDisposable, type DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { TriggerAction } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS, HttpRequestError } from '../../../web/src/api/http.js';
import { IncompleteSearchStreamError, SearchStreamReadError, streamFederatedSearch, searchBranchKey } from '../../../web/src/api/stream/federatedSearch.js';
import type { FederatedDiagnosticLogTargetDTO, FederatedSearchBranchDTO, FederatedSearchBranchKeyDTO, FederatedSearchQueryDTO } from '../../../web/src/types/protocol_generated/boxteam/search/v1/public.js';
import type { GatewaySearchScopeDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { WorkspaceSearchResultDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { workspaceContext } from '../workspaceContext.js';
import { ThreadInput } from '../threadInput.js';
import { searchOwnerKey } from './quickAccessScope.js';
import { type ResourcePick, rankResourcePicks } from './quickAccessPicks.js';
import type { FuzzyScorerCache } from '../../upstream/src/vs/base/common/fuzzyScorer.js';
import { quickAccessFileQuery, searchCategoryLabel, type ContentSearchCategory, type ResourceSearchItem, type SearchCategory } from './quickAccessQuery.js';
import { loadSearchRoots } from './target.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { formatNumber, t } from '../../../shared/core/i18n/index.js';

function httpFailureDetail(error: HttpRequestError): string {
  const fields: string[] = [];
  const detail = error.detail;
  if (typeof detail === 'string') {
    fields.push(`detail: ${detail}`);
  } else if (detail && typeof detail === 'object') {
    const values = !Array.isArray(detail) ? detail as Record<string, unknown> : undefined;
    for (const key of ['code', 'owner', 'reason']) {
      const value = values?.[key];
      if (typeof value === 'string') fields.push(`${key}: ${value}`);
    }
    const serialized = JSON.stringify(detail);
    if (serialized) fields.push(`detail: ${serialized}`);
  }
  fields.push(`http_status: ${error.status}`);
  if (error.requestId) fields.push(`request_id: ${error.requestId}`);
  return fields.join(' · ');
}

interface SearchBranch<Category extends SearchCategory = SearchCategory> {
  contextOwner: WorkspaceOwner;
  diagnostics?: FederatedDiagnosticLogTargetDTO;
  diagnosticSummary?: string;
  category: Category;
  key: string;
  label: string;
  fileRoot?: string;
  picks: ResourcePick[];
  pending: boolean;
  status: ResourcePick;
}


const categories = { '文件': 'file', '会话': 'catalog', 'Thread': 'thread', '会话消息': 'message', '工具结果': 'tool_result', 'LLM 请求记录': 'llm_request', 'Trace': 'trace', '运行日志': 'runtime_log', 'Gateway 日志': 'runtime_log' } as const;
export function isContentSearchCategory(category: string): category is ContentSearchCategory { return category === '会话消息' || category === '工具结果' || category === 'LLM 请求记录' || category === 'Trace' || category === '运行日志' || category === 'Gateway 日志'; }

/** 原领域页只投影为候选；网络、范围和分页统一交 Gateway。 */
function searchItems(page: WorkspaceSearchResultDTO, owner: WorkspaceOwner, label: string): ResourceSearchItem[] {
  if (page.category === 'file' && page.files?.node) return page.files.entries.map(item => {
    if (item.resource_shape !== 'file') throw new Error(t('quickAccess.error.fileProviderDirectory'));
    const resource = workspaceFileUri(owner, item.canonical_uri ?? item.uri, page.files!.node!.canonical_uri ?? page.files!.node!.uri);
    const relative = resource.path.split('/').slice(3);
    return { kind: 'resource', resource, uri: item.canonical_uri ?? item.uri, title: relative.at(-1)!, description: `${label} · ${relative.slice(0, -1).join('/')}`, detail: relative.join('/') };
  });
  if (page.category === 'catalog' && page.catalog) return page.catalog.items.map(({ node, relative_path }) => {
    if (!node) throw new Error(t('quickAccess.error.catalogMissingNode'));
    if (node.kind === 'folder') return { kind: 'folder', nodeId: node.node_id, title: node.name,
      description: `${label} · ${relative_path} · ${node.node_id}`, detail: t('quickAccess.detail.folder', node.node_id) };
    if (!node.session_id || !node.session?.uri) throw new Error(t('quickAccess.error.catalogMissingUri'));
    return { kind: 'session', sessionId: node.session_id, uri: node.session.uri, title: node.name,
      description: `${label} · ${relative_path} · ${node.session_id}`, detail: t('quickAccess.detail.session', node.session_id) };
  });
  if (page.category === 'thread' && page.threads) return page.threads.items.map(item => {
    if (item.workspace_id !== owner.workspaceId || !item.thread_uri) throw new Error(t('quickAccess.error.threadMissingUri'));
    const input = new ThreadInput({ ...owner, workspaceKey: '', sessionId: item.session_id, threadId: item.thread_id, title: item.title });
    return { kind: 'resource', resource: input.resource, uri: item.thread_uri, title: item.title,
      description: `${label} · ${item.session_title} · ${item.thread_id}`, detail: `${item.kind} Thread · ${item.session_id} / ${item.thread_id}` };
  });
  if (page.category === 'trace' && page.traces) return page.traces.items.map(item => {
    const reference = item.match?.reference;
    if (!reference || !item.session_uri) throw new Error(t('quickAccess.error.traceMissingLocation'));
    const input = new ThreadInput({ ...owner, workspaceKey: '', sessionId: reference.session_id, threadId: reference.thread_id, title: item.session_title });
    return { kind: 'resource', resource: input.resource, uri: item.session_uri, trace: item.match, title: item.session_title,
      description: item.match!.preview.replace(/\s+/g, ' '),
      detail: `${label} · Trace ${item.match!.event_type} · ${reference.session_id} / ${reference.thread_id} · ${reference.event_id}` };
  });
  if (page.category === 'llm_request' && page.llm_requests) return page.llm_requests.items.map(item => {
    if (!item.match?.reference || !item.session_uri) throw new Error(t('quickAccess.error.requestMissingLocation'));
    return { kind: 'session', sessionId: item.match.reference.session_id, uri: item.session_uri, requestLog: item.match,
      title: item.session_title, description: item.match.preview.replace(/\s+/g, ' '),
      detail: `${label} · ${t('quickAccess.detail.requestLog', item.match.reference.session_id, item.match.reference.file_name)}` };
  });
  if ((page.category === 'message' || page.category === 'tool_result') && page.messages) return page.messages.items.map(({ thread, match }) => {
    if (!thread?.thread_uri || !match?.reference || thread.workspace_id !== owner.workspaceId) throw new Error(t('quickAccess.error.messageMissingLocation'));
    const input = new ThreadInput({ ...owner, workspaceKey: '', sessionId: thread.session_id, threadId: thread.thread_id, title: thread.title });
    return { kind: 'resource', resource: input.resource, uri: thread.thread_uri, message: match, title: thread.title, description: match.preview.replace(/\s+/g, ' '),
      detail: `${label} · ${thread.session_title} · ${match.role === 'tool' ? t('quickAccess.detail.toolResult', match.tool_name, match.tool_call_id) : t(match.role === 'user' ? 'quickAccess.detail.userMessage' : 'quickAccess.detail.assistantMessage')} · ${thread.session_id} / ${thread.thread_id} · ${match.turn_id}` };
  });
  throw new Error(t('quickAccess.error.missingDomainPage'));
}

export function queryFederatedSearchBranches(owners: readonly WorkspaceOwner[], selected: readonly SearchCategory[], filter: string,
  disposables: DisposableStore, token: CancellationToken, cache: FuzzyScorerCache, map: (item: ResourceSearchItem, owner: WorkspaceOwner) => ResourcePick,
  progress: (branches: SearchBranch[]) => void, busy: (value: boolean) => void, scopeLabel: (label: string) => void, instantiation: IInstantiationService,
  loadRoots: typeof loadSearchRoots = loadSearchRoots): SearchBranch[] {
  const abort = new AbortController();
  disposables.add(toDisposable(() => abort.abort()));
  disposables.add(token.onCancellationRequested(() => abort.abort()));
  const alive = () => !abort.signal.aborted && !token.isCancellationRequested;
  const snapshot = workspaceContext.getSnapshot();
  const ownerLabel = (owner: WorkspaceOwner) => snapshot.workspaces.find(target => target.gateway_id === owner.gatewayId && target.workspace_id === owner.workspaceId)?.name ?? owner.workspaceId;
  const gatewayLabel = (gatewayId: string) => gatewayId === snapshot.gatewayId ? t('quickAccess.localGateway')
    : snapshot.workspaces.flatMap(workspace => workspace.remote_gateways).find(gateway => gateway.gateway_id === gatewayId)?.name ?? gatewayId;
  scopeLabel(owners.map(ownerLabel).join(t('quickAccess.listSeparator')));
  const branches: SearchBranch<SearchCategory>[] = selected.flatMap(category => {
    const diagnostic = category === '运行日志' || category === 'Gateway 日志';
    const selectedOwners = diagnostic ? [...new Map(owners.map(owner => [owner.gatewayId, owner])).values()] : owners;
    return selectedOwners.map(owner => {
      const diagnostics = diagnostic ? { gateway_id: owner.gatewayId, scope: { source: category === '运行日志' ? 'workspace' : 'gateway',
        workspace_ids: category === '运行日志' ? owners.filter(item => item.gatewayId === owner.gatewayId).map(item => item.workspaceId) : [] } } : undefined;
      const categoryLabel = searchCategoryLabel(category);
      const label = diagnostic ? `${gatewayLabel(owner.gatewayId)} · ${categoryLabel}` : owners.length === 1 ? categoryLabel : `${ownerLabel(owner)} · ${categoryLabel}`;
      return { contextOwner: owner, diagnostics, category, key: `${diagnostic ? owner.gatewayId : searchOwnerKey(owner)}/${category}`, label,
        picks: [], pending: false, status: { id: `status-${searchOwnerKey(owner)}/${category}`, label: `${label} · ${t('quickAccess.querying')}` } };
    });
  });
  const key = (branch: SearchBranch<SearchCategory>): FederatedSearchBranchKeyDTO => ({
    owner: branch.diagnostics ? undefined : { gateway_id: branch.contextOwner.gatewayId, workspace_id: branch.contextOwner.workspaceId }, diagnostics: branch.diagnostics,
    category: categories[branch.category], operation: isContentSearchCategory(branch.category) ? 'content' : 'name',
    ...(branch.fileRoot ? { resource_uri: branch.fileRoot } : {}),
  });
  const fileQueries = new Map<SearchBranch, FederatedSearchQueryDTO>();
  const fileLimit = Math.max(1, Math.min(50, Math.floor(1000 / owners.length)));
  const byKey = new Map(branches.map(branch => [searchBranchKey(key(branch)), branch]));
  let scope: GatewaySearchScopeDTO | undefined;
  const inFlight = new Map<SearchBranch<SearchCategory>, symbol>();
  const refresh = () => { if (alive()) { busy(branches.some(branch => branch.pending)); progress(branches); } };
  const moreButton = { iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: t('quickAccess.moreCategory') };
  const retryButton = { iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: t('quickAccess.retryCategory') };
  const status = (branch: SearchBranch<SearchCategory>, state: FederatedSearchBranchDTO, requestId: string) => {
    const failed = state.state === 'failed', cancelled = state.state === 'cancelled';
    if (failed) branch.picks = [];
    const limited = state.state === 'limited';
    const continuation = limited && (state.cursor !== undefined && state.cursor !== null || state.code === 'output_limit');
    branch.status = { id: `status-${branch.key}`,
      label: `${branch.label} · ${cancelled ? t('quickAccess.queryCancelled', state.message ?? state.code ?? state.state)
        : failed ? t('quickAccess.queryFailed', (state.code === 'deadline' ? t('quickAccess.timeoutPrefix') : '') + (state.message ?? state.code ?? state.state))
        : limited ? t('quickAccess.queryLimited', state.code ?? t('quickAccess.moreResults')) : branch.picks.length ? t('quickAccess.queryComplete') : t('quickAccess.noResults')}`,
      detail: [branch.diagnosticSummary ?? t('quickAccess.readCount', formatNumber(branch.picks.length)),
        !failed && !cancelled && state.message, state.code && `code: ${state.code}`,
        state.error_owner && `error_owner: ${state.error_owner}`, state.reason && `reason: ${state.reason}`,
        ...(state.diagnostics ?? []).map(notice => `${notice.severity}: ${notice.message} · code: ${notice.code} · owner: ${notice.owner}`),
        typeof state.http_status === 'number' && `http_status: ${state.http_status}`, `request_id: ${requestId}`].filter(Boolean).join(' · '),
      ...((continuation || ((failed || cancelled) && state.retryable)) ? { buttons: [continuation ? moreButton : retryButton],
        trigger: async () => { await run([branch], continuation ? state.cursor : undefined); return TriggerAction.NO_ACTION; } } : {}) };
  };
  const run = async (active: SearchBranch<SearchCategory>[], cursor?: string): Promise<void> => {
    if (!alive() || active.some(branch => branch.pending)) return;
    const operation = Symbol();
    const requestAbort = new AbortController(), deadline = (Date.now() + DEFAULT_API_REQUEST_TIMEOUT_MS) / 1000;
    const cancel = () => requestAbort.abort(abort.signal.reason);
    abort.signal.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(() => requestAbort.abort(new DOMException(t('quickAccess.timeoutRoots'), 'TimeoutError')), DEFAULT_API_REQUEST_TIMEOUT_MS);
    for (const branch of active) {
      inFlight.set(branch, operation);
      branch.pending = true;
      if (!cursor) branch.picks = [];
      branch.status = { id: `status-${branch.key}`, label: `${branch.label} · ${t('quickAccess.querying')}` };
    }
    refresh();
    const settled = new Set<SearchBranch<SearchCategory>>();
    let requestId: string | null | undefined;
    try {
      const fileBranches = active.filter(branch => branch.category === '文件' && !fileQueries.has(branch));
      if (fileBranches.length) {
        const roots = await loadRoots(fileBranches.map(branch => branch.contextOwner), requestAbort.signal);
        if (!alive()) return;
        roots.forEach((root, index) => {
          const branch = fileBranches[index];
          try {
            if (root.status === 'rejected') throw root.reason;
            const query = quickAccessFileQuery(filter, { version: 1, workspaces: [root.value] }, instantiation);
            if (query.file_targets.length !== 1) throw new Error(t('quickAccess.error.fileRootCount'));
            fileQueries.set(branch, query);
            branch.fileRoot = query.file_targets[0].target!.uri;
            byKey.set(searchBranchKey(key(branch)), branch);
          } catch (error) {
            settled.add(branch); branch.pending = false;
            branch.status = { id: `status-${branch.key}`, label: `${branch.label} · ${t('quickAccess.queryFailed', error instanceof Error ? error.message : String(error))}`,
              detail: error instanceof HttpRequestError ? httpFailureDetail(error) : undefined,
              buttons: [retryButton], trigger: async () => { await run([branch]); return TriggerAction.NO_ACTION; } };
          }
        });
        refresh();
      }
      const ready = active.filter(branch => !settled.has(branch));
      if (!ready.length) return;
      const queryGroups = new Map<string, SearchBranch<SearchCategory>[]>();
      for (const branch of ready) {
        const groupKey = `${categories[branch.category]}/${isContentSearchCategory(branch.category) ? 'content' : 'name'}`;
        const group = queryGroups.get(groupKey) ?? [];
        group.push(branch); queryGroups.set(groupKey, group);
      }
      const queries = [...queryGroups.values()].map(group => {
        const category = group[0].category;
        if (category === '文件') {
          const selectedFiles = group;
          const first = fileQueries.get(selectedFiles[0])!;
          if (selectedFiles.some(branch => { const query = fileQueries.get(branch)!; return query.query !== first.query || query.match_mode !== first.match_mode; })) {
            throw new Error(t('quickAccess.error.fileRootConflict'));
          }
          return { ...first, limit: fileLimit, file_targets: selectedFiles.flatMap(branch => fileQueries.get(branch)!.file_targets) };
        }
        return { category: categories[category], operation: isContentSearchCategory(category) ? 'content' : 'name',
          contract_version: 1, query: filter, match_mode: isContentSearchCategory(category) ? 'literal' : 'fuzzy', limit: 50, file_targets: [],
          diagnostic_targets: group.flatMap(branch => branch.diagnostics ? [branch.diagnostics] : []) };
      });
      await streamFederatedSearch(0, {
        scope: scope ? { mode: 'resume', scope, owners: [] } : { mode: 'selected', owners: owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId })) },
        queries,
        continuations: scope ? ready.map(branch => ({ branch: key(branch), cursor })) : [],
        deadline_at: deadline,
        max_results: ready.every(branch => branch.category === '文件') ? fileLimit * ready.length : 200,
      }, { signal: requestAbort.signal, onEvent: event => {
        if (!alive()) return;
        requestId = event.request_id;
        if (event.type === 'scope') { scope = event.scope?.scope; return; }
        if (!event.branch?.branch) return;
        const branch = byKey.get(searchBranchKey(event.branch.branch));
        if (!branch) throw new Error(t('quickAccess.error.unselectedBranch'));
        if (inFlight.get(branch) !== operation) return;
        if (event.type === 'page' && (event.page || event.diagnostic_logs)) {
          const logs = event.diagnostic_logs;
          const items: ResourceSearchItem[] = logs ? logs.items.map(hit => {
            if (!hit.source || !hit.reference || !logs.scope) throw new Error(t('quickAccess.error.logMissingLocation'));
            const workspaces = hit.source.workspace_ids.map(id => ownerLabel({ gatewayId: logs.gateway_id, workspaceId: id }));
            return { kind: 'log', location: { scope: logs.scope, reference: hit.reference }, title: hit.source.label,
              description: hit.preview.replace(/\s+/g, ' '),
              detail: `${gatewayLabel(logs.gateway_id)} · ${hit.source.source === 'gateway' ? t('quickAccess.gatewayControl') : t('quickAccess.managedProcess', workspaces.join(t('quickAccess.listSeparator')), workspaces.length > 1 ? t('quickAccess.sharedSourceSuffix') : '')} · ${formatNumber(hit.reference.line_start)}` };
          }) : searchItems(event.page!, branch.contextOwner, ownerLabel(branch.contextOwner));
          if (logs) branch.diagnosticSummary = t('quickAccess.logScanSummary', formatNumber(logs.scanned_sources), formatNumber(logs.scanned_lines), formatNumber(logs.scanned_bytes), logs.sources.some(source => source.status === 'unavailable') ? t('quickAccess.unavailableSourcesSuffix') : '');
          const merged = new Map([...branch.picks, ...items.map(item => map(item, branch.contextOwner))].map(pick => [pick.id, pick]));
          branch.picks = isContentSearchCategory(branch.category) ? [...merged.values()] : rankResourcePicks([...merged.values()], filter, cache);
          status(branch, event.branch, event.request_id);
        } else if (event.type === 'branch') {
          settled.add(branch);
          branch.pending = false;
          status(branch, event.branch, event.request_id);
        }
        refresh();
      } });
    } catch (error) {
      if (!alive()) return;
      const unfinished = active.filter(branch => !settled.has(branch));
      for (const branch of unfinished.length ? unfinished : active) {
        if (inFlight.get(branch) !== operation) continue;
        if (cursor) branch.picks = [];
        const message = error instanceof Error ? error.message : String(error);
        const failureId = error instanceof HttpRequestError || error instanceof IncompleteSearchStreamError || error instanceof SearchStreamReadError ? error.requestId : requestId;
        branch.status = { id: `status-${branch.key}`, label: `${branch.label} · ${t('quickAccess.queryFailed', error instanceof DOMException && error.name === 'TimeoutError' ? t('quickAccess.timeoutTarget') : message)}`,
          detail: error instanceof HttpRequestError ? httpFailureDetail(error) : failureId ? `request_id: ${failureId}` : undefined,
          buttons: [retryButton], trigger: async () => { await run(scope ? [branch] : branches); return TriggerAction.NO_ACTION; } };
      }
    } finally {
      clearTimeout(timer); abort.signal.removeEventListener('abort', cancel);
      for (const branch of active) if (inFlight.get(branch) === operation) { branch.pending = false; inFlight.delete(branch); }
      refresh();
    }
  };
  // 名称可立即返回；文件只在原根元数据就绪后一次提交，不逐 owner 排查询队列。
  const names = branches.filter(branch => branch.category !== '文件');
  const files = branches.filter(branch => branch.category === '文件');
  if (names.length) void run(names);
  if (files.length) void run(files);
  return branches;
}
