import type { ExtensionKind } from '../../upstream/src/vs/platform/environment/common/environment.js';
import type { IExtensionDescription } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { Event } from '../../upstream/src/vs/base/common/event.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { ConfigurationDefaultSource } from '../../upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import type { ExtensionConfigurationProfileViewRequest, ExtensionConfigurationViewDTO, ExtensionConfigurationViewRequest, ExtensionHostConnectionDTO, ExtensionWebHostConnectionDTO, ExtensionWebHostOpenRequest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';

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

/** 独立设置持有明确 profile/view，不伪造扩展运行代际。 */
export type TargetConfigurationContext = TargetExtensionContext | {
  readonly kind: 'settings';
  readonly connection: ExtensionConfigurationViewDTO;
  readonly gatewayConnectionId?: string;
};

/** 目标设置仅消费所选安装集合；原内置贡献和非扩展核心默认值继续保留。 */
export function targetConfigurationSourceFilter(context?: TargetConfigurationContext): (source: ConfigurationDefaultSource | undefined) => boolean {
  if (context?.kind !== 'settings') return source => typeof source !== 'object' || !source?.configurationOnly;
  const ids = new Set(context.connection.extensions.map(extension => extension.extension_id.toLowerCase()));
  return source => typeof source !== 'object' || !source?.id || !!source.isBuiltin || ids.has(source.id.toLowerCase());
}

export function targetWorkspace(context: TargetConfigurationContext) {
  const workspace = context.kind === 'node' ? context.connection.initialization?.workspace : context.connection.context?.workspace;
  if (!workspace) throw new Error('目标宿主缺少已认证的工作区视图');
  return workspace;
}

export function targetSettings(context: TargetConfigurationContext) {
  const settings = context.kind === 'node' ? context.connection.initialization?.environment?.remote_settings : context.connection.context?.remote_settings;
  if (!settings) throw new Error('目标宿主缺少已认证的 profile 设置资源');
  return settings;
}

export function targetProfileId(context: TargetConfigurationContext): string {
  const id = context.kind === 'node' ? context.connection.initialization?.environment?.profile_id : context.connection.profile_id;
  if (!id) throw new Error('目标宿主缺少已认证的 profile 身份');
  return id;
}

export function targetOperatingSystem(context: TargetConfigurationContext): string {
  const os = context.kind === 'node' ? context.connection.initialization?.os : context.connection.context?.os;
  if (!os || !['linux', 'windows', 'macos'].includes(os)) throw new Error('目标宿主缺少已认证的平台');
  return os;
}

export function targetConfigurationRequest(context: TargetConfigurationContext): ExtensionConfigurationViewRequest | ExtensionConfigurationProfileViewRequest {
  const { gateway_id, backend_id, view_id, workspace_ids } = context.connection;
  const target = { protocol_version: 1, gateway_id, backend_id, view_id, workspace_ids: [...workspace_ids],
    gateway_connection_id: context.gatewayConnectionId };
  return context.kind === 'settings' ? { ...target, profile_id: context.connection.profile_id }
    : { ...target, generation: context.connection.generation };
}

/** 使用原 manifest 的有序运行偏好；无代码贡献按原 Web 资格加载，不要求入口脚本。 */
export function targetExecutionKind(extension: Pick<IExtensionDescription, 'main' | 'browser'>, kinds: readonly ExtensionKind[], nodeAvailable: boolean): 'node' | 'web' | undefined {
  for (const kind of kinds) {
    if (kind === 'workspace' && extension.main && nodeAvailable) return 'node';
    if (kind === 'web' && (extension.browser || !extension.main)) return 'web';
  }
  return undefined;
}
