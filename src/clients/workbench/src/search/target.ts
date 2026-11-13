import { t } from '../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey } from "../../../web/src/api/workspaceIdentity.js";
import { Limiter } from '../../upstream/src/vs/base/common/async.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gatewayWorkspaceRequestHeaders, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { awaitWithAbort } from '../../../web/src/utils/abortable.js';
import type { WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from '../agentApi.js';
import { changeFileSource } from '../readonlyDocuments.js';
import { ThreadInput } from '../threadInput.js';
import { AutomationInput } from '../automation/input.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { sourceDocumentSource } from '../sourceDocuments.js';
import type { SearchScope } from './scope.js';
import { pluginCommandContextKeys } from '../extensions/pluginCommandRegistry.js';
import type { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { AnythingQuickAccessProviderRunOptions, IQuickAccessOptions } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';

/** 同一次模式转接保留发起归属，不重新采样后来获得焦点的编辑器。 */
export interface SearchInvocationOptions extends AnythingQuickAccessProviderRunOptions {
  searchOwner?: WorkspaceOwner;
  /** 管理动作的只选择模式：接受不打开目标，也不提供写入或侧开按钮。 */
  resourceSelection?: 'session' | 'thread';
  pluginCommandOwner?: { gatewayId: string; workspaceId?: string };
  pluginCommandContext?: readonly [string, unknown][];
  searchOwnerError?: string;
  /** 用户明确选择的查询集合；发起 owner 仍独立保留。 */
  searchOwners?: readonly WorkspaceOwner[];
}

/** 原 Controller 的同次模式转接传递调用参数；新入口在浮层聚焦前捕获归属。 */
export function prepareSearchInvocation(options: IQuickAccessOptions | undefined, editors: IEditorService, context: IContextKeyService): IQuickAccessOptions {
  const supplied = options?.providerOptions as SearchInvocationOptions | undefined;
  const providerOptions: SearchInvocationOptions = { ...supplied };
  if (!('fileSearchDelegate' in providerOptions) && !providerOptions.searchOwners && !providerOptions.searchOwner && !providerOptions.searchOwnerError) {
    try { providerOptions.searchOwner = captureSearchOwner(editors); }
    catch (error) { providerOptions.searchOwnerError = String(error); }
  }
  if (!providerOptions.pluginCommandOwner) {
    const snapshot = workspaceContext.getSnapshot();
    providerOptions.pluginCommandOwner = providerOptions.searchOwner
      ?? (!editors.activeEditor && !snapshot.activeId && !snapshot.loading && !snapshot.error && snapshot.gatewayId
        ? { gatewayId: snapshot.gatewayId } : undefined);
  }
  providerOptions.pluginCommandContext ??= [...pluginCommandContextKeys].map(key => [key, context.getContextKeyValue(key)]);
  return { ...options, providerOptions };
}

/** 必须在 Quick Input 获得焦点前同步读取；异步查根期间不重新采样活动目标。 */
export function captureSearchOwner(editors: IEditorService, input: IEditorService['activeEditor'] | null = editors.activeEditor): WorkspaceOwner {
  if (input instanceof AutomationInput) return { ...input.owner };
  if (input instanceof ThreadInput) return { gatewayId: input.owner.gatewayId, workspaceId: input.owner.workspaceId };
  const resource = EditorResourceAccessor.getCanonicalUri(input ?? undefined, { supportSideBySide: SideBySideEditor.PRIMARY });
  if (resource?.scheme === 'boxteam-file') {
    const { gatewayId, workspaceId } = workspaceFileSource(resource);
    return { gatewayId, workspaceId };
  }
  if (resource?.scheme === 'boxteam-change') {
    const { gatewayId, workspaceId } = changeFileSource(resource).owner;
    return { gatewayId, workspaceId };
  }
  if (resource?.scheme === 'boxteam-source') {
    const { gatewayId, workspaceId } = sourceDocumentSource(resource);
    return { gatewayId, workspaceId };
  }
  const snapshot = workspaceContext.getSnapshot();
  const target = snapshot.workspaces.find(workspace => gatewayWorkspaceKey(workspace) === snapshot.activeId);
  if (snapshot.error || snapshot.loading || !target || !snapshot.gatewayId) throw new Error(snapshot.error ?? t('quickAccess.search.workspaceMissing'));
  return gatewayWorkspaceOwner(target);
}

export async function loadSearchRoots(owners: readonly WorkspaceOwner[], signal: AbortSignal): Promise<PromiseSettledResult<SearchScope['workspaces'][number]>[]> {
  if (!owners.length) throw new Error(t('quickAccess.search.workspaceRequired'));
  const routes = await gateway.workspaces(false, signal), limiter = new Limiter<SearchScope['workspaces'][number]>(3);
  try {
    return await Promise.allSettled(owners.map(owner => limiter.queue(async () => {
      signal.throwIfAborted();
      const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
      if (target.status !== 'ready') throw new Error(t('quickAccess.search.workspaceUnavailable', target.name, target.status));
      const roots = await gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots',
        { signal, headers: gatewayWorkspaceRequestHeaders(target) }, gatewayWorkspaceKey(target));
      return { ...owner, rootUri: roots.workspace_uri, label: target.name };
    })));
  } finally { limiter.dispose(); }
}

/** 单次浮层共享已成功和在途的根读取；输入取消只停止等待，关闭浮层才取消实际读取。 */
export function createSearchRootLoader(lifetime: AbortSignal): typeof loadSearchRoots {
  type Result = PromiseSettledResult<SearchScope['workspaces'][number]>;
  const roots = new Map<string, Promise<Result>>();
  const key = (owner: WorkspaceOwner) => JSON.stringify([owner.gatewayId, owner.workspaceId]);
  return async (owners, signal) => {
    lifetime.throwIfAborted();
    signal.throwIfAborted();
    const missing = owners.filter(owner => !roots.has(key(owner)));
    if (missing.length) {
      const loading = loadSearchRoots(missing, lifetime).catch((reason: unknown): Result[] => missing.map(() => ({ status: 'rejected', reason })));
      missing.forEach((owner, index) => {
        const pending = loading.then(results => {
          const result = results[index];
          if (result.status === 'rejected') roots.delete(key(owner));
          return result;
        });
        roots.set(key(owner), pending);
      });
    }
    return await awaitWithAbort(Promise.all(owners.map(owner => roots.get(key(owner))!)), signal);
  };
}

/** 原搜索视图需要完整范围；Quick Access 则逐根表达元数据失败。 */
export async function loadSearchScope(owners: readonly WorkspaceOwner[], signal: AbortSignal): Promise<SearchScope> {
  // Search Editor／SearchView 的初次范围加载早于文件查询，必须在这里保护元数据读取。
  const controller = new AbortController(), reading = AbortSignal.any([signal, controller.signal]);
  const invalidate = () => controller.abort(new Error(t('quickAccess.files.identityChanged')));
  const releaseToken = onGatewayTokenInvalidated(0, invalidate);
  let gatewayId = workspaceContext.getSnapshot().gatewayId;
  const releaseGateway = workspaceContext.subscribe(() => {
    const current = workspaceContext.getSnapshot().gatewayId;
    if (gatewayId && current !== gatewayId) invalidate();
    gatewayId = current;
  });
  try {
    const roots = await loadSearchRoots(owners, reading);
    reading.throwIfAborted();
    return { version: 1, workspaces: roots.map(root => {
      if (root.status === 'rejected') throw root.reason;
      return root.value;
    }) };
  } finally { releaseToken(); releaseGateway(); }
}
