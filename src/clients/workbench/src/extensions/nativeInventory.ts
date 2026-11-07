import { revive } from '../../upstream/src/vs/base/common/marshalling.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { ExtensionIdentifier, bindExtensionDescription, type ExtensionInstallationTarget, type IExtensionDescription } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import type { ExtensionInstallationInventoryDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';

export type NativeExtensionTarget = Extract<ExtensionInstallationTarget, { kind: 'backend' }> & { readonly gatewayConnectionId?: string };

/** 运行扫描与管理列表消费同一原生库存；正文不能改写 envelope 中的安装身份。 */
export function nativeInventoryDescriptions(inventory: ExtensionInstallationInventoryDTO, target: NativeExtensionTarget, enablementView?: string) {
  if (inventory.protocol_version !== 1 || inventory.gateway_id !== target.gatewayId || inventory.backend_id !== target.backendId
    || inventory.profile_scope !== 'default' || !inventory.inventory?.profile_id || !inventory.inventory.target_platform
    || !Array.isArray(inventory.inventory.extensions)) throw new Error('扩展库存响应的目标、默认 profile 或平台不匹配');
  const descriptions = inventory.inventory.extensions.map(entry => {
    if (!entry.extension_key) throw new Error('扩展库存缺少精确发布 key');
    const value = revive(entry.description) as IExtensionDescription | undefined;
    const identifier = value?.identifier as { value?: unknown } | undefined;
    if (entry.source !== 'profile' || !value || typeof value.publisher !== 'string' || typeof value.name !== 'string'
      || typeof value.version !== 'string' || typeof identifier?.value !== 'string'
      || !ExtensionIdentifier.equals(identifier.value, value.publisher + '.' + value.name)
      || !URI.isUri(value.extensionLocation) || value.extensionLocation.scheme !== 'file'
      || typeof entry.is_valid !== 'boolean' || !Array.isArray(entry.validations)) throw new Error('扩展库存包含非法原生描述或扫描诊断');
    return bindExtensionDescription({ ...value, identifier: new ExtensionIdentifier(identifier.value),
      extensionKey: entry.extension_key, installationProfileId: inventory.inventory!.profile_id,
      gatewayConnectionId: target.gatewayConnectionId, isValid: entry.is_valid, enablementView,
      validations: entry.validations.map(validation => [validation.severity, validation.message]),
    }, { kind: 'backend', gatewayId: target.gatewayId, backendId: target.backendId });
  });
  if (new Set(descriptions.map(extension => extension.extensionKey)).size !== descriptions.length) throw new Error('扩展库存包含重复发布 key');
  return descriptions;
}

/** 当前管理资源不取得运行资格；旧代际资源必须使用 Web admission 的 frozen content URL。 */
export function currentExtensionPackageUri(target: NativeExtensionTarget, profileId: string, extensionKey: string, extensionId: string): URI {
  const prefix = '/api/gateway/extension-packages/' + (target.gatewayConnectionId ? `via/${encodeURIComponent(target.gatewayConnectionId)}/` : '');
  const path = 'current/' + [target.gatewayId, target.backendId, profileId, extensionKey, extensionId].map(encodeURIComponent).join('/') + '/';
  return URI.parse(new URL(prefix + path, globalThis.location.href).href);
}
