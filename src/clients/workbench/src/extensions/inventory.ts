import { revive } from '../../upstream/src/vs/base/common/marshalling.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { ExtensionIdentifier, IExtensionDescription, IBoundExtensionDescription, IExtensionWorkspaceView, bindExtensionDescription, installationTargetKey } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { ExtensionHostTargetDTO, ExtensionInstallationInventoryDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from '../agentApi.js';
import { workspaceFileSource } from '../workspaceFiles.js';

export interface TargetExtensionInventory {
  readonly views: readonly IExtensionWorkspaceView[];
  readonly extensions: IBoundExtensionDescription[];
}

/** 只读取原窗口明确挂接的根；默认虚拟 Workbench 根继续只运行 Web 扩展。 */
export async function scanTargetExtensions(context: IWorkspaceContextService): Promise<TargetExtensionInventory> {
  const workspace = await context.getCompleteWorkspace();
  const owners = workspace.folders.filter(folder => folder.uri.scheme === 'boxteam-file').map(folder => {
    const source = workspaceFileSource(folder.uri);
    if (source.path !== source.root) throw new Error('Node 宿主只能挂接原工作区文件根');
    return source;
  });
  const groups = new Map<string, { target: ExtensionHostTargetDTO; workspaceIds: string[] }>();
  for (const owner of owners) {
    const payload = { protocol_version: 1, gateway_id: owner.gatewayId, workspace_ids: [owner.workspaceId] };
    const target = await gateway.request<ExtensionHostTargetDTO>('/api/gateway/extension-hosts/resolve', {
      method: 'POST', body: JSON.stringify(payload),
    });
    assertTarget(target, payload);
    const key = installationTargetKey({ kind: 'backend', gatewayId: target.gateway_id, backendId: target.backend_id });
    const group = groups.get(key) ?? { target, workspaceIds: [] };
    if (group.workspaceIds.includes(owner.workspaceId)) throw new Error('Node 宿主工作区集合重复');
    group.workspaceIds.push(owner.workspaceId);
    groups.set(key, group);
  }
  const views: IExtensionWorkspaceView[] = [], extensions: IBoundExtensionDescription[] = [];
  for (const { target, workspaceIds } of groups.values()) {
    const request = { protocol_version: 1, gateway_id: target.gateway_id, backend_id: target.backend_id, workspace_ids: workspaceIds };
    // 完整集合再次经原 owner 核验；单根发现不能代替 multi-root 的最终准入。
    const resolved = await gateway.request<ExtensionHostTargetDTO>('/api/gateway/extension-hosts/resolve', {
      method: 'POST', body: JSON.stringify(request),
    });
    assertTarget(resolved, request);
    const inventory = await gateway.request<ExtensionInstallationInventoryDTO>('/api/gateway/extension-installations/inventory', {
      method: 'POST', body: JSON.stringify(request),
    });
    if (inventory.protocol_version !== 1 || inventory.gateway_id !== request.gateway_id || inventory.backend_id !== request.backend_id
      || JSON.stringify(inventory.workspace_ids) !== JSON.stringify(workspaceIds) || inventory.profile_scope !== 'default'
      || !inventory.inventory?.profile_id || !Array.isArray(inventory.inventory.extensions)) {
      throw new Error('扩展库存响应的目标、默认 profile 或有序工作区视图不匹配');
    }
    const installationTarget = { kind: 'backend' as const, gatewayId: inventory.gateway_id, backendId: inventory.backend_id };
    views.push({ installationTarget, viewId: 'default', workspaceIds });
    for (const entry of inventory.inventory.extensions) {
      const value = revive(entry.description) as IExtensionDescription | undefined;
      const identifier = value?.identifier as { value?: unknown } | undefined;
      if (entry.source !== 'profile' || !value || typeof value.publisher !== 'string' || typeof value.name !== 'string'
        || typeof value.version !== 'string' || typeof identifier?.value !== 'string'
        || !ExtensionIdentifier.equals(identifier.value, value.publisher + '.' + value.name)
        || !URI.isUri(value.extensionLocation) || value.extensionLocation.scheme !== 'file'
        || typeof entry.is_valid !== 'boolean' || !Array.isArray(entry.validations)) {
        throw new Error('扩展库存包含非法原生描述或扫描诊断');
      }
      extensions.push(bindExtensionDescription({ ...value,
        identifier: new ExtensionIdentifier(identifier.value),
        isValid: entry.is_valid,
        enablementView: JSON.stringify(['default', workspaceIds]),
        validations: entry.validations.map(validation => [validation.severity, validation.message]),
      }, installationTarget));
    }
  }
  return { views, extensions };
}

function assertTarget(target: ExtensionHostTargetDTO, expected: { gateway_id: string; workspace_ids: string[]; backend_id?: string }): void {
  if (target.protocol_version !== 1 || target.source !== 'live' || !target.backend_id || target.gateway_id !== expected.gateway_id
    || expected.backend_id && target.backend_id !== expected.backend_id || JSON.stringify(target.workspace_ids) !== JSON.stringify(expected.workspace_ids)) {
    throw new Error('Node 宿主发现响应没有匹配明确稳定目标');
  }
  if (target.channel_state !== 'available' || !target.runtime) {
    throw new Error('Node 宿主通道不可用：' + (target.reason ?? target.channel_state));
  }
}
