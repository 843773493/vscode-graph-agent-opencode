import { revive } from '../../upstream/src/vs/base/common/marshalling.js';
import type { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { ExtensionKind } from '../../upstream/src/vs/platform/environment/common/environment.js';
import { ExtensionIdentifier, IExtensionDescription, IBoundExtensionDescription, IExtensionWorkspaceView, bindExtensionDescription, installationTargetKey } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { ILogService } from '../../upstream/src/vs/platform/log/common/log.js';
import type { ExtensionHostTargetDTO, ExtensionInstallationInventoryDTO, ExtensionTargetDTO, ExtensionTargetListDTO, ExtensionTargetViewDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from '../agentApi.js';
import { workspaceFileSource } from '../workspaceFiles.js';
import { targetExecutionKind } from './targetContext.js';
import { TargetWebHostConnection } from './webConnection.js';
import { nativeInventoryDescriptions } from './nativeInventory.js';

export interface TargetExtensionInventory {
  readonly views: readonly IExtensionWorkspaceView[];
  readonly extensions: IBoundExtensionDescription[];
}

/** 挂接根决定运行视图；零根窗口只从页面入口 Gateway 读取目标用户 Web 扩展。 */
export async function scanTargetExtensions(context: IWorkspaceContextService,
  readKinds: (extension: IExtensionDescription) => readonly ExtensionKind[], lifetime: DisposableStore,
  entryGatewayId: string, logService: Pick<ILogService, 'warn'>): Promise<TargetExtensionInventory> {
  const workspace = await context.getCompleteWorkspace();
  const owners = workspace.folders.filter(folder => folder.uri.scheme === 'boxteam-file').map(folder => {
    const source = workspaceFileSource(folder.uri);
    if (source.path !== source.root) throw new Error('目标宿主只能挂接原工作区文件根');
    return source;
  });
  const groups = new Map<string, { target: ExtensionTargetDTO & { capabilities: NonNullable<ExtensionTargetDTO['capabilities']> }; workspaceIds: string[] }>();
  for (const owner of owners) {
    const payload = { protocol_version: 1, gateway_id: owner.gatewayId, workspace_ids: [owner.workspaceId] };
    const target = await gateway.request<ExtensionTargetViewDTO>('/api/gateway/extension-targets/resolve', {
      method: 'POST', body: JSON.stringify(payload),
    });
    assertTargetView(target, payload);
    const key = installationTargetKey({ kind: 'backend', gatewayId: target.gateway_id, backendId: target.backend_id });
    const group = groups.get(key) ?? { target, workspaceIds: [] };
    if (group.workspaceIds.includes(owner.workspaceId)) throw new Error('目标宿主工作区集合重复');
    group.workspaceIds.push(owner.workspaceId);
    groups.set(key, group);
  }
  if (!owners.length) {
    if (!entryGatewayId) throw new Error('零根窗口缺少页面入口 Gateway 身份');
    const directory = await gateway.request<ExtensionTargetListDTO>('/api/gateway/extension-targets', {
      method: 'POST', body: JSON.stringify({ protocol_version: 1, gateway_id: entryGatewayId }),
    });
    if (directory.protocol_version !== 1 || directory.gateway_id !== entryGatewayId || directory.source !== 'live'
      || directory.targets.length > 1) throw new Error('零根窗口需要页面入口 Gateway 的唯一实时扩展安装域');
    if (!directory.targets.length) {
      logService.warn('页面入口 Gateway 未配置扩展管理组件，目标用户扩展不可用');
      return { views: [], extensions: [] };
    }
    const target = directory.targets[0];
    assertInstallationTarget(target, { gateway_id: entryGatewayId });
    groups.set(installationTargetKey({ kind: 'backend', gatewayId: target.gateway_id, backendId: target.backend_id }), { target, workspaceIds: [] });
  }
  const views: IExtensionWorkspaceView[] = [], extensions: IBoundExtensionDescription[] = [];
  for (const { target, workspaceIds } of groups.values()) {
    const request = { protocol_version: 1, gateway_id: target.gateway_id, backend_id: target.backend_id, workspace_ids: workspaceIds };
    // 单根回执已覆盖完整集合；多根在原 owner 核验完整有序视图，不合并分根权限。
    let resolvedTarget = target;
    if (workspaceIds.length > 1) {
      const resolved = await gateway.request<ExtensionTargetViewDTO>('/api/gateway/extension-targets/resolve', {
        method: 'POST', body: JSON.stringify(request),
      });
      assertTargetView(resolved, request);
      resolvedTarget = resolved;
    }
    const installationTarget = { kind: 'backend' as const, gatewayId: target.gateway_id, backendId: target.backend_id };
    const view: IExtensionWorkspaceView = { installationTarget, viewId: 'default', workspaceIds, capabilities: resolvedTarget.capabilities };
    // 安装目标不等同运行宿主；仅安装权限的窗口不得请求运行态或读取启动库存。
    if (!view.capabilities.host_manage) { views.push(view); continue; }
    const inventory = await gateway.request<ExtensionInstallationInventoryDTO>('/api/gateway/extension-installations/inventory', {
      method: 'POST', body: JSON.stringify({ protocol_version: 1, gateway_id: request.gateway_id, backend_id: request.backend_id }),
    });
    const descriptions = nativeInventoryDescriptions(inventory, installationTarget, JSON.stringify(['default', workspaceIds]));
    let nodeRuntime: ExtensionHostTargetDTO | undefined;
    if (workspaceIds.length && descriptions.some(extension => extension.isValid && extension.main && readKinds(extension).includes('workspace'))) {
      nodeRuntime = await gateway.request<ExtensionHostTargetDTO>('/api/gateway/extension-hosts/resolve', {
        method: 'POST', body: JSON.stringify(request),
      });
      assertTarget(nodeRuntime, request);
    }
    const browser = descriptions.filter(extension => {
      if (!extension.isValid) return false;
      const kind = targetExecutionKind(extension, readKinds(extension), nodeRuntime?.channel_state === 'available');
      if (!workspaceIds.length && extension.main && !kind) {
        logService.warn('扩展在零根窗口没有合法运行宿主：' + extension.identifier.value);
      }
      return kind === 'web';
    });
    let webHost: TargetWebHostConnection | undefined;
    if (browser.length) {
      webHost = lifetime.add(new TargetWebHostConnection({ ...request, view_id: view.viewId,
        profile_id: inventory.inventory!.profile_id, extension_keys: browser.map(extension => extension.extensionKey!) }));
      await webHost.connect();
      const frozen = webHost.connection.inventory!;
      for (const extension of browser) {
        const entry = frozen.extensions.find(entry => entry.extension_key === extension.extensionKey);
        if (!entry?.is_valid || entry.source !== 'profile') throw new Error('Web 宿主冻结发布缺少有效的 profile 描述');
        const value = revive(entry.description) as IExtensionDescription | undefined;
        if (!value || !ExtensionIdentifier.equals(value.identifier?.value, extension.identifier) || value.version !== extension.version
          || value.publisher !== extension.publisher || value.name !== extension.name
          || !URI.isUri(value.extensionLocation) || value.extensionLocation.scheme !== 'file') {
          throw new Error('Web 宿主冻结发布与库存的精确扩展实例不匹配');
        }
        const index = descriptions.indexOf(extension);
        descriptions[index] = bindExtensionDescription({ ...value, identifier: extension.identifier, extensionKey: entry.extension_key,
          installationProfileId: extension.installationProfileId, gatewayConnectionId: extension.gatewayConnectionId,
          isValid: true, enablementView: extension.enablementView,
          validations: entry.validations.map(validation => [validation.severity, validation.message]),
          extensionLocation: webHost.packageUri(entry.extension_key, extension.identifier.value),
        }, installationTarget);
      }
    }
    views.push({ ...view, nodeRuntime, webHost });
    extensions.push(...descriptions);
  }
  return { views, extensions };
}

function assertInstallationTarget(target: ExtensionTargetDTO,
  expected: { gateway_id: string; backend_id?: string }): asserts target is ExtensionTargetDTO & {
    capabilities: NonNullable<ExtensionTargetDTO['capabilities']>;
  } {
  if (target.protocol_version !== 1 || target.source !== 'live' || !target.backend_id || target.gateway_id !== expected.gateway_id
    || expected.backend_id && target.backend_id !== expected.backend_id
    || typeof target.capabilities?.installation_manage !== 'boolean' || typeof target.capabilities.host_manage !== 'boolean'
    || !(target.capabilities.installation_manage || target.capabilities.host_manage)) {
    throw new Error('扩展目标发现响应没有匹配明确身份或有效管理权限');
  }
}

function assertTargetView(target: ExtensionTargetViewDTO,
  expected: { gateway_id: string; workspace_ids: string[]; backend_id?: string }): asserts target is ExtensionTargetViewDTO & {
    capabilities: NonNullable<ExtensionTargetViewDTO['capabilities']>;
  } {
  assertInstallationTarget(target, expected);
  if (JSON.stringify(target.workspace_ids) !== JSON.stringify(expected.workspace_ids)) {
    throw new Error('扩展目标发现响应没有匹配明确工作区集合');
  }
}

function assertTarget(target: ExtensionHostTargetDTO, expected: { gateway_id: string; workspace_ids: string[]; backend_id?: string }): void {
  if (target.protocol_version !== 1 || target.source !== 'live' || !target.backend_id || target.gateway_id !== expected.gateway_id
    || expected.backend_id && target.backend_id !== expected.backend_id || JSON.stringify(target.workspace_ids) !== JSON.stringify(expected.workspace_ids)) {
    throw new Error('Node 宿主发现响应没有匹配明确稳定目标');
  }
  if (!['available', 'unavailable', 'draining'].includes(target.channel_state)
    || target.channel_state === 'available' && !target.runtime) throw new Error('Node 宿主发现返回不一致的运行时状态');
}
