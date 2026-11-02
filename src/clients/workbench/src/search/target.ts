import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { gatewayWorkspaceOwner, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from '../agentApi.js';
import { changeFileSource } from '../changeFiles.js';
import { ThreadInput } from '../threadInput.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import type { SearchScope } from './scope.js';
import type { AnythingQuickAccessProviderRunOptions, IQuickAccessOptions } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';

/** 同一次模式转接保留发起归属，不重新采样后来获得焦点的编辑器。 */
export interface SearchInvocationOptions extends AnythingQuickAccessProviderRunOptions { searchOwner?: WorkspaceOwner; searchOwnerError?: string }

/** 原 Controller 的同次模式转接传递调用参数；新入口在浮层聚焦前捕获归属。 */
export function prepareSearchInvocation(options: IQuickAccessOptions | undefined, editors: IEditorService): IQuickAccessOptions {
  const supplied = options?.providerOptions as SearchInvocationOptions | undefined;
  const providerOptions: SearchInvocationOptions = { ...supplied };
  if (!('fileSearchDelegate' in providerOptions) && !providerOptions.searchOwner && !providerOptions.searchOwnerError) {
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
  const snapshot = workspaceContext.getSnapshot();
  const target = snapshot.workspaces.find(workspace => workspace.workspace_id === snapshot.activeId);
  if (snapshot.error || snapshot.loading || !target || !snapshot.gatewayId) throw new Error(snapshot.error ?? '没有明确搜索工作区，请先选择工作区');
  return gatewayWorkspaceOwner(target, snapshot.gatewayId);
}

export async function loadSearchScope(owner: WorkspaceOwner, signal: AbortSignal): Promise<SearchScope> {
  const target = await gateway.resolveWorkspaceTarget(owner, signal);
  if (target.status !== 'ready') throw new Error(`工作区 ${target.name} 当前不可查询（${target.status}），请明确启动或连接后再搜索`);
  const roots = await gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots', { signal }, target.workspace_id);
  return { version: 1, workspaces: [{ ...owner, rootUri: roots.workspace_uri, label: target.name }] };
}
