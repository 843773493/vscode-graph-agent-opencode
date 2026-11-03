import { Limiter, raceCancellationError, timeout } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { TriggerAction } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS, HttpRequestError } from '../../../web/src/api/http.js';
import { StaleTurnCursorHttpError } from '../../../web/src/api/session/sessionTurnHistory.js';
import { awaitWithAbort } from '../../../web/src/utils/abortable.js';
import { requireGatewayWorkspaceRoute, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { searchOwnerKey } from './quickAccessScope.js';
import { rankResourcePicks, type ResourcePick } from './quickAccessPicks.js';
import { searchQuickAccessFiles, type SearchCategory, type ResourceSearchItem } from './quickAccessQuery.js';
import { isContentSearchCategory, queryFederatedSearchBranches, type FederatedSearchCategory } from './quickAccessFederation.js';
import type { WorkspaceSearchService } from './searchService.js';
import type { FuzzyScorerCache } from '../../upstream/src/vs/base/common/fuzzyScorer.js';

export interface SearchBranch<Category extends SearchCategory = SearchCategory> {
  owner: WorkspaceOwner;
  category: Category;
  key: string;
  label: string;
  picks: ResourcePick[];
  pending: boolean;
  status: ResourcePick;
}

/** 各领域保持同一输入代际；名称与正文只消费 Gateway 联邦调度。 */
export function querySearchBranches(owners: readonly WorkspaceOwner[], categories: SearchCategory[], filter: string,
  disposables: DisposableStore, token: CancellationToken, instantiation: IInstantiationService, search: WorkspaceSearchService,
  cache: FuzzyScorerCache, map: (item: ResourceSearchItem, owner: WorkspaceOwner) => ResourcePick,
  progress: (branches: SearchBranch[]) => void, busy: (value: boolean) => void, scopeLabel: (label: string) => void): SearchBranch[] {
  if (categories.some(isContentSearchCategory) && !categories.every(isContentSearchCategory)) throw new Error('名称与正文查询须分别选择模式');
  let federated: SearchBranch[] = [], files: SearchBranch[] = [];
  let federatedBusy = false, filesBusy = false;
  const refresh = () => progress([...federated, ...files]);
  const selected = categories.filter((category): category is FederatedSearchCategory => category !== '文件');
  if (selected.length) federated = queryFederatedSearchBranches(owners, selected, filter, disposables, token, cache, map,
    branches => { federated = branches; refresh(); }, value => { federatedBusy = value; busy(federatedBusy || filesBusy); }, scopeLabel);
  if (categories.includes('文件')) files = queryFileSearchBranches(owners, filter, disposables, token, instantiation, search, cache, map,
    branches => { files = branches; refresh(); }, value => { filesBusy = value; busy(federatedBusy || filesBusy); }, scopeLabel);
  return [...federated, ...files];
}

/** 文件多根配额和原 QueryBuilder 尚须一并迁移；此队列不再查询名称或正文。 */
function queryFileSearchBranches(owners: readonly WorkspaceOwner[], filter: string,
  disposables: DisposableStore, token: CancellationToken, instantiation: IInstantiationService, search: WorkspaceSearchService,
  cache: FuzzyScorerCache, map: (item: ResourceSearchItem, owner: WorkspaceOwner) => ResourcePick,
  progress: (branches: SearchBranch[]) => void, busy: (value: boolean) => void, scopeLabel: (label: string) => void): SearchBranch[] {
  const controller = new AbortController();
  disposables.add(toDisposable(() => controller.abort()));
  disposables.add(token.onCancellationRequested(() => controller.abort()));
  const alive = () => !controller.signal.aborted && !token.isCancellationRequested;
  const category = '文件';
  const branches: SearchBranch<'文件'>[] = owners.map(owner => ({ owner, category,
    key: `${searchOwnerKey(owner)}/${category}`, label: owners.length === 1 ? category : `${owner.workspaceId} · ${category}`,
    picks: [], pending: false, status: { id: `status-${searchOwnerKey(owner)}/${category}`, label: `${owner.workspaceId} · ${category} · 正在查询` } }));
  const limiter = disposables.add(new Limiter<void>(3));
  let routes: ReturnType<typeof gateway.workspaceRoutes> | undefined;
  const labels = new Map(owners.map(owner => [searchOwnerKey(owner), owner.workspaceId]));
  const resolveTarget = async (owner: WorkspaceOwner) => {
    const snapshot = await (routes ??= timeout(200, token).then(() => gateway.workspaceRoutes(controller.signal)));
    const target = requireGatewayWorkspaceRoute(snapshot.items, snapshot.gatewayId, owner);
    if (target.status !== 'ready') throw new Error(`工作区 ${target.name} 当前不可查询（${target.status}），请明确启动或连接`);
    labels.set(searchOwnerKey(owner), target.name);
    if (alive()) scopeLabel([...labels.values()].join('、'));
    return target;
  };
  const queue = async (branch: SearchBranch<'文件'>, cursor?: string): Promise<void> => {
    if (!alive() || branch.pending) return;
    branch.pending = true;
    const source = new CancellationTokenSource(token), abort = new AbortController();
    const cancellation = source.token.onCancellationRequested(() => abort.abort());
    const cancel = () => source.cancel();
    controller.signal.addEventListener('abort', cancel, { once: true });
    let timedOut = false;
    // 排队、路由和该页读取共用原 JSON 期限；重试是用户明确发起的新读取。
    const deadlineAt = Date.now() + DEFAULT_API_REQUEST_TIMEOUT_MS;
    const timer = setTimeout(() => { timedOut = true; source.cancel(); }, DEFAULT_API_REQUEST_TIMEOUT_MS);
    busy(true);
    branch.status = { id: `status-${branch.key}`, label: `${branch.label} · 正在查询` };
    progress(branches);
    try {
      await raceCancellationError(limiter.queue(async () => {
        if (source.token.isCancellationRequested || !alive()) return;
        const target = await awaitWithAbort(resolveTarget(branch.owner), abort.signal);
        if (source.token.isCancellationRequested || !alive()) return;
        branch.label = owners.length === 1 ? branch.category : `${target.name} · ${branch.category}`;
        const page = await searchQuickAccessFiles(filter, branch.owner, target, target.name,
          abort.signal, source.token, instantiation, search, cursor, deadlineAt);
        if (source.token.isCancellationRequested || !alive()) return;
        const merged = new Map([...branch.picks, ...page.items.map(item => map(item, branch.owner))].map(pick => [pick.id, pick]));
        branch.picks = rankResourcePicks([...merged.values()], filter, cache);
        branch.status = { id: `status-${branch.key}`,
          label: `${branch.label} · ${page.limited ?? (page.cursor ? '还有更多结果' : branch.picks.length ? '本次查询完成' : '没有匹配的资源')}`,
          detail: `本次读取 ${branch.picks.length} 项，不代表全局快照${page.requestIds.length ? ` · request_id: ${page.requestIds.join('、')}` : ''}`,
          ...(page.cursor ? { buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: '继续查询此类别' }],
            trigger: async () => { await queue(branch, page.cursor); return TriggerAction.NO_ACTION; } } : {}) };
      }), source.token);
    } catch (error) {
      if (!alive()) return;
      // 续页失败撤下该分支旧页，独立重查；其它 owner 的有效结果不受影响。
      if (cursor) branch.picks = [];
      const message = timedOut ? '查询超时（含排队与目标读取），请重试' : error instanceof Error ? error.message : String(error);
      const requestId = error instanceof HttpRequestError || error instanceof StaleTurnCursorHttpError ? error.requestId : undefined;
      branch.status = { id: `status-${branch.key}`, label: `${branch.label}${cursor ? '续页，请重新' : ''}查询失败：${message}`,
        detail: requestId ? `request_id: ${requestId}` : undefined,
        buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: cursor ? '重新查询此类别' : '重试此查询' }],
        trigger: async () => { routes = undefined; await queue(branch); return TriggerAction.NO_ACTION; } };
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel); cancellation.dispose(); source.dispose();
      branch.pending = false;
      if (alive()) { busy(branches.some(item => item.pending)); progress(branches); }
    }
  };
  for (const branch of branches) void queue(branch);
  return branches;
}
