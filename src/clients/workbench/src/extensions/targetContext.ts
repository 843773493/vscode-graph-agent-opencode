import type { ExtensionKind } from '../../upstream/src/vs/platform/environment/common/environment.js';
import { installationTargetKey, type IExtension, type IExtensionDescription, type IExtensionWorkspaceView } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { Event } from '../../upstream/src/vs/base/common/event.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { ExtensionConfigurationViewRequest, ExtensionHostConnectionDTO, ExtensionWebHostConnectionDTO, ExtensionWebHostOpenRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';

export interface ITargetWebHostConnection extends IDisposable {
  readonly connection: ExtensionWebHostConnectionDTO;
  readonly request: ExtensionWebHostOpenRequest;
  readonly onDidClose: Event<string>;
  packageUri(extensionKey: string, extensionId: string): URI;
}

/** 业务归属引用已认证通道；Node 的进程初始化和原生 URI 转换继续由 Node host 持有。 */
export type TargetExtensionContext = (
  | { readonly kind: 'node'; readonly connection: ExtensionHostConnectionDTO }
  | { readonly kind: 'web'; readonly connection: ExtensionWebHostConnectionDTO }
) & { readonly gatewayConnectionId?: string };

export function targetWorkspace(context: TargetExtensionContext) {
  const workspace = context.kind === 'node' ? context.connection.initialization?.workspace : context.connection.context?.workspace;
  if (!workspace) throw new Error('目标宿主缺少已认证的工作区视图');
  return workspace;
}

export function targetSettings(context: TargetExtensionContext) {
  const settings = context.kind === 'node' ? context.connection.initialization?.environment?.remote_settings : context.connection.context?.remote_settings;
  if (!settings) throw new Error('目标宿主缺少已认证的 profile 设置资源');
  return settings;
}

export function targetProfileId(context: TargetExtensionContext): string {
  const id = context.kind === 'node' ? context.connection.initialization?.environment?.profile_id : context.connection.profile_id;
  if (!id) throw new Error('目标宿主缺少已认证的 profile 身份');
  return id;
}

export function targetOperatingSystem(context: TargetExtensionContext): string {
  const os = context.kind === 'node' ? context.connection.initialization?.os : context.connection.context?.os;
  if (!os || !['linux', 'windows', 'macos'].includes(os)) throw new Error('目标宿主缺少已认证的平台');
  return os;
}

export function targetConfigurationRequest(context: TargetExtensionContext): ExtensionConfigurationViewRequest {
  const { gateway_id, backend_id, view_id, workspace_ids } = context.connection;
  const target = { protocol_version: 1, gateway_id, backend_id, view_id, workspace_ids: [...workspace_ids],
    gateway_connection_id: context.gatewayConnectionId };
  return { ...target, generation: context.connection.generation };
}

/** 使用原 manifest 的有序运行偏好；无代码贡献按原 Web 资格加载，不要求入口脚本。 */
export function targetExecutionKind(extension: Pick<IExtensionDescription, 'main' | 'browser'>, kinds: readonly ExtensionKind[], nodeAvailable: boolean): 'node' | 'web' | undefined {
  for (const kind of kinds) {
    if (kind === 'workspace' && extension.main && nodeAvailable) return 'node';
    if (kind === 'web' && (extension.browser || !extension.main)) return 'web';
  }
  return undefined;
}

/** 只从原 Tracker 的目标视图说明 Node 不可运行原因，不改变用户启用状态。 */
export function targetRuntimeUnavailableReason(extension: IExtension, kinds: readonly ExtensionKind[], views: readonly IExtensionWorkspaceView[]): string | undefined {
  const target = extension.installationTarget;
  if (target?.kind !== 'backend' || targetExecutionKind(extension.manifest, kinds, true) !== 'node') return undefined;
  const view = views.find(view => installationTargetKey(view.installationTarget) === installationTargetKey(target));
  if (!view) return '此扩展当前不可运行：当前窗口没有该安装目标的运行视图。';
  if (!view.capabilities.host_manage) return '此扩展当前不可运行：没有目标扩展宿主管理权限。';
  if (targetExecutionKind(extension.manifest, kinds, view.nodeRuntime?.channel_state === 'available')) return undefined;
  if (!view.workspaceIds.length) return '此扩展当前不可运行：Node 扩展需要挂接该目标的工作区。';
  return '此扩展当前不可运行：目标 Node 扩展宿主不可用（' + (view.nodeRuntime?.reason ?? 'runtime_missing') + '）。';
}
