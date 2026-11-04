import { Limiter } from '../../upstream/src/vs/base/common/async.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRoute, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gatewayWorkspaceRequestHeaders } from '../../../web/src/api/http.js';
import type { WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from '../agentApi.js';
import { changeFileSource } from '../readonlyDocuments.js';
import { ThreadInput } from '../threadInput.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { sourceDocumentSource } from '../sourceDocuments.js';
import type { SearchScope } from './scope.js';
import type { AnythingQuickAccessProviderRunOptions, IQuickAccessOptions } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';

/** 同一次模式转接保留发起归属，不重新采样后来获得焦点的编辑器。 */
export interface SearchInvocationOptions extends AnythingQuickAccessProviderRunOptions {
  searchOwner?: WorkspaceOwner;
  searchOwnerError?: string;
  /** 用户明确选择的查询集合；发起 owner 仍独立保留。 */
  searchOwners?: readonly WorkspaceOwner[];
}

/** 原 Controller 的同次模式转接传递调用参数；新入口在浮层聚焦前捕获归属。 */
export function prepareSearchInvocation(options: IQuickAccessOptions | undefined, editors: IEditorService): IQuickAccessOptions {
  const supplied = options?.providerOptions as SearchInvocationOptions | undefined;
  const providerOptions: SearchInvocationOptions = { ...supplied };
  if (!('fileSearchDelegate' in providerOptions) && !providerOptions.searchOwners && !providerOptions.searchOwner && !providerOptions.searchOwnerError) {
    try { providerOptions.searchOwner = captureSearchOwner(editors); }
    catch (error) { providerOptions.searchOwnerError = String(error); }
  }
  return { ...options, providerOptions };
}

/** 必须在 Quick Input 获得焦点前同步读取；异步查根期间不重新采样活动目标。 */
export function captureSearchOwner(editors: IEditorService): WorkspaceOwner {
  const input = editors.activeEditor;
  if (input instanceof ThreadInput) return { gatewayId: input.owner.gatewayId, workspaceId: input.owner.workspaceId };
  const resource = EditorResourceAccessor.getCanonicalUri(input, { supportSideBySide: SideBySideEditor.PRIMARY });
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
  const target = snapshot.workspaces.find(workspace => workspace.workspace_id === snapshot.activeId);
  if (snapshot.error || snapshot.loading || !target || !snapshot.gatewayId) throw new Error(snapshot.error ?? '没有明确搜索工作区，请先选择工作区');
  return gatewayWorkspaceOwner(target, snapshot.gatewayId);
}

export async function loadSearchRoots(owners: readonly WorkspaceOwner[], signal: AbortSignal): Promise<PromiseSettledResult<SearchScope['workspaces'][number]>[]> {
  if (!owners.length) throw new Error('请先选择明确搜索工作区');
  const routes = await gateway.workspaceRoutes(signal), limiter = new Limiter<SearchScope['workspaces'][number]>(3);
  try {
    return await Promise.allSettled(owners.map(owner => limiter.queue(async () => {
      signal.throwIfAborted();
      const target = requireGatewayWorkspaceRoute(routes.items, routes.gatewayId, owner);
      if (target.status !== 'ready') throw new Error(`工作区 ${target.name} 当前不可查询（${target.status}），请明确启动或连接后再搜索`);
      const roots = await gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots',
        { signal, headers: gatewayWorkspaceRequestHeaders(target) }, target.workspace_id);
      return { ...owner, rootUri: roots.workspace_uri, label: target.name };
    })));
  } finally { limiter.dispose(); }
}

/** 原搜索视图需要完整范围；Quick Access 则逐根表达元数据失败。 */
export async function loadSearchScope(owners: readonly WorkspaceOwner[], signal: AbortSignal): Promise<SearchScope> {
  const roots = await loadSearchRoots(owners, signal);
  return { version: 1, workspaces: roots.map(root => {
    if (root.status === 'rejected') throw root.reason;
    return root.value;
  }) };
}
