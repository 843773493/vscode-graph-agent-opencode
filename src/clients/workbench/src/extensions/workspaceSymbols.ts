import { WorkspaceSymbolProviderRegistry, type IWorkspaceSymbolProvider } from '../../upstream/src/vs/workbench/contrib/search/common/search.js';
import type { IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gatewayWorkspaceKey } from '../../../web/src/api/workspaceIdentity.js';

/** Provider 没有目录参数：必须在调用前选定完整宿主视图，不能查询后按结果 URI 过滤。 */
export function workspaceSymbolProviders(owners: readonly WorkspaceOwner[]): IWorkspaceSymbolProvider[] {
  if (!owners.length) throw new Error('请先选择明确的符号查询工作区');
  const requested = new Set(owners.map(gatewayWorkspaceKey)), covered = new Set<string>();
  const providers = WorkspaceSymbolProviderRegistry.all().filter(provider => {
    const binding = provider.extensionHost?.binding;
    if (!binding) return false;
    const workspace = binding.initialization?.workspace;
    if (!workspace) throw new Error('目标符号 Provider 缺少已认证的工作区视图');
    const view = workspace.folders.map(folder => gatewayWorkspaceKey({ gatewayId: binding.gateway_id, workspaceId: folder.workspace_id }));
    if (!view.some(owner => requested.has(owner))) return false;
    if (view.some(owner => !requested.has(owner))) throw new Error('符号 Provider 只能查询完整宿主视图，请同时选择：' + workspace.folders.map(folder => folder.name).join('、'));
    for (const owner of view) covered.add(owner);
    return true;
  });
  if ([...requested].some(owner => !covered.has(owner))) throw new Error('所选工作区没有已注册的目标符号 Provider');
  return providers;
}

/** 扩展内的公开查询继承发起代际；Web 保持原窗口查询。 */
export function workspaceSymbolProvidersForHost(host: IExtensionHost | undefined): IWorkspaceSymbolProvider[] | undefined {
  return host?.binding ? WorkspaceSymbolProviderRegistry.all().filter(provider => provider.extensionHost === host) : undefined;
}
