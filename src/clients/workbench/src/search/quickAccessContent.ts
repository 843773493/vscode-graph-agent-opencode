import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { toDisposable, type DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { TriggerAction } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import { streamFederatedSearch, searchBranchKey } from '../../../web/src/api/stream/federatedSearch.js';
import type { FederatedSearchBranchDTO, FederatedSearchBranchKeyDTO } from '../../../web/src/types/protocol_generated/boxteam/search/v1/public.js';
import type { GatewaySearchScopeDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v1/public.js';
import type { WorkspaceSearchResultDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { workspaceContext } from '../workspaceContext.js';
import { ThreadInput } from '../threadInput.js';
import { searchOwnerKey } from './quickAccessScope.js';
import type { SearchBranch } from './quickAccessBranches.js';
import type { ResourcePick } from './quickAccessPicks.js';
import type { ContentSearchCategory, ResourceSearchItem } from './quickAccessQuery.js';

const categories = { '会话消息': 'message', '工具结果': 'tool_result', 'LLM 请求记录': 'llm_request' } as const;
export function isContentSearchCategory(category: string): category is ContentSearchCategory { return Object.hasOwn(categories, category); }

/** 原领域页只投影为候选；网络、范围和分页统一交 Gateway。 */
function contentItems(page: WorkspaceSearchResultDTO, owner: WorkspaceOwner, label: string): ResourceSearchItem[] {
  if (page.category === 'llm_request' && page.llm_requests) return page.llm_requests.items.map(item => {
    if (!item.match?.reference || !item.session_uri) throw new Error('请求日志命中缺少 Session 资源或记录位置');
    return { kind: 'session', sessionId: item.match.reference.session_id, requestLog: item.match,
      title: item.session_title, description: item.match.preview.replace(/\s+/g, ' '),
      detail: `${label} · 会话级 LLM 请求记录 · ${item.match.reference.session_id} · ${item.match.reference.file_name}` };
  });
  if ((page.category === 'message' || page.category === 'tool_result') && page.messages) return page.messages.items.map(({ thread, match }) => {
    if (!thread || !match?.reference || thread.workspace_id !== owner.workspaceId) throw new Error('消息命中缺少精确归属或位置');
    const input = new ThreadInput({ ...owner, routeId: '', sessionId: thread.session_id, threadId: thread.thread_id, title: thread.title });
    return { kind: 'resource', resource: input.resource, message: match, title: thread.title, description: match.preview.replace(/\s+/g, ' '),
      detail: `${label} · ${thread.session_title} · ${match.role === 'tool' ? `工具结果 ${match.tool_name} · ${match.tool_call_id}` : match.role === 'user' ? '用户消息' : '助手消息'} · ${thread.session_id} / ${thread.thread_id} · ${match.turn_id}` };
  });
  throw new Error('正文搜索缺少原 owner 的领域页');
}

export function queryContentSearchBranches(owners: readonly WorkspaceOwner[], selected: ContentSearchCategory[], filter: string,
  disposables: DisposableStore, token: CancellationToken, map: (item: ResourceSearchItem, owner: WorkspaceOwner) => ResourcePick,
  progress: (branches: SearchBranch[]) => void, busy: (value: boolean) => void, scopeLabel: (label: string) => void): SearchBranch[] {
  const abort = new AbortController();
  disposables.add(toDisposable(() => abort.abort()));
  disposables.add(token.onCancellationRequested(() => abort.abort()));
  const alive = () => !abort.signal.aborted && !token.isCancellationRequested;
  const snapshot = workspaceContext.getSnapshot();
  const ownerLabel = (owner: WorkspaceOwner) => snapshot.workspaces.find(target => target.connection_kind === 'local'
    ? snapshot.gatewayId === owner.gatewayId && target.backend_workspace_id === owner.workspaceId
    : target.remote?.gateway_id === owner.gatewayId && target.remote.remote_workspace_id === owner.workspaceId)?.name ?? owner.workspaceId;
  scopeLabel(owners.map(ownerLabel).join('、'));
  const branches: SearchBranch<ContentSearchCategory>[] = selected.flatMap(category => owners.map(owner => ({ owner, category,
    key: `${searchOwnerKey(owner)}/${category}`, label: owners.length === 1 ? category : `${ownerLabel(owner)} · ${category}`,
    picks: [], pending: false, status: { id: `status-${searchOwnerKey(owner)}/${category}`, label: `${ownerLabel(owner)} · ${category} · 正在查询` } })));
  const key = (branch: SearchBranch<ContentSearchCategory>): FederatedSearchBranchKeyDTO => ({
    owner: { gateway_id: branch.owner.gatewayId, workspace_id: branch.owner.workspaceId }, category: categories[branch.category], operation: 'content',
  });
  const byKey = new Map(branches.map(branch => [searchBranchKey(key(branch)), branch]));
  let scope: GatewaySearchScopeDTO | undefined;
  const inFlight = new Map<SearchBranch<ContentSearchCategory>, symbol>();
  const refresh = () => { if (alive()) { busy(branches.some(branch => branch.pending)); progress(branches); } };
  const moreButton = { iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: '继续查询此类别' };
  const retryButton = { iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: '重新查询此类别' };
  const status = (branch: SearchBranch<ContentSearchCategory>, state: FederatedSearchBranchDTO, requestId: string) => {
    const failed = state.state === 'failed' || state.state === 'cancelled';
    if (failed) branch.picks = [];
    const limited = state.state === 'limited';
    const continuation = limited && (state.cursor !== undefined && state.cursor !== null || state.code === 'output_limit');
    branch.status = { id: `status-${branch.key}`,
      label: `${branch.label} · ${failed ? `查询失败：${state.message ?? state.code ?? state.state}`
        : limited ? `本次正文覆盖受限（${state.code ?? '还有更多结果'}）` : branch.picks.length ? '本次查询完成' : '没有匹配的资源'}`,
      detail: `本次读取 ${branch.picks.length} 项，不代表全局快照 · request_id: ${requestId}`,
      ...((continuation || (failed && state.retryable)) ? { buttons: [continuation ? moreButton : retryButton],
        trigger: async () => { await run([branch], continuation ? state.cursor : undefined); return TriggerAction.NO_ACTION; } } : {}) };
  };
  const run = async (active: SearchBranch<ContentSearchCategory>[], cursor?: string): Promise<void> => {
    if (!alive() || active.some(branch => branch.pending)) return;
    const operation = Symbol();
    for (const branch of active) {
      inFlight.set(branch, operation);
      branch.pending = true;
      if (!cursor) branch.picks = [];
      branch.status = { id: `status-${branch.key}`, label: `${branch.label} · 正在查询` };
    }
    refresh();
    const settled = new Set<SearchBranch<ContentSearchCategory>>();
    let requestId: string | null | undefined;
    try {
      await streamFederatedSearch(0, {
        scope: scope ? { mode: 'resume', scope, owners: [] } : { mode: 'selected', owners: owners.map(owner => ({ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId })) },
        queries: [...new Set(active.map(branch => branch.category))].map(category => ({ category: categories[category], operation: 'content',
          contract_version: 1, query: filter, match_mode: 'literal', limit: 50 })),
        continuations: scope ? active.map(branch => ({ branch: key(branch), cursor })) : [], max_results: 200,
      }, { signal: abort.signal, onEvent: event => {
        if (!alive()) return;
        requestId = event.request_id;
        if (event.type === 'scope') { scope = event.scope?.scope; return; }
        if (!event.branch?.branch) return;
        const branch = byKey.get(searchBranchKey(event.branch.branch));
        if (!branch) throw new Error('正文搜索返回未选择的分支');
        if (inFlight.get(branch) !== operation) return;
        if (event.type === 'page' && event.page) {
          const merged = new Map([...branch.picks, ...contentItems(event.page, branch.owner, ownerLabel(branch.owner)).map(item => map(item, branch.owner))].map(pick => [pick.id, pick]));
          branch.picks = [...merged.values()];
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
        const failureId = error instanceof HttpRequestError ? error.requestId : requestId;
        branch.status = { id: `status-${branch.key}`, label: `${branch.label} · 查询失败：${error instanceof Error ? error.message : String(error)}`,
          detail: failureId ? `request_id: ${failureId}` : undefined,
          buttons: [retryButton], trigger: async () => { await run(scope ? [branch] : branches); return TriggerAction.NO_ACTION; } };
      }
    } finally {
      for (const branch of active) if (inFlight.get(branch) === operation) { branch.pending = false; inFlight.delete(branch); }
      refresh();
    }
  };
  void run(branches);
  return branches;
}
