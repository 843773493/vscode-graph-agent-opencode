import { join } from 'node:path';

export const extensionStorageSourcePaths = [
  'src/vs/platform/extensionManagement/common/extensionStorage.ts',
  'src/vs/workbench/api/browser/mainThreadStorage.ts',
  'src/vs/workbench/api/common/extHostStorage.ts',
  'src/vs/workbench/api/browser/mainThreadSecretState.ts',
  'src/vs/workbench/api/common/extHostSecretState.ts',
];

/** Memento 继续由原 profile owner 保存；目标与稳定视图贯穿原写入和通知。 */
export function transformExtensionStorage(root, path, source, replaceOnce, runtime) {
  const paths = extensionStorageSourcePaths.map(value => join(root, 'upstream', value));
  const secretError = '目标 Node 扩展暂不支持 SecretStorage；不能访问工作台的秘密存储';
  if (path === paths[4] && runtime === 'node') {
    // TODO(6.6)：接入目标消费侧的秘密 owner；拒绝必须发生在值进入浏览器 RPC 之前。
    let contents = replaceOnce(source, 'ExtHostSecretStateShape, MainContext, MainThreadSecretStateShape', 'ExtHostSecretStateShape');
    contents = replaceOnce(contents, '\tprivate _proxy: MainThreadSecretStateShape;\n', '');
    contents = replaceOnce(contents, 'this._proxy = mainContext.getProxy(MainContext.MainThreadSecretState);', 'void mainContext;');
    for (const [method, args] of [['getPassword', 'extensionId, key'], ['setPassword', 'extensionId, key, value'], ['deletePassword', 'extensionId, key'], ['getKeys', 'extensionId']]) {
      contents = replaceOnce(contents, `return this._proxy.$${method}(${args});`, `${args.split(', ').map(arg => `void ${arg};`).join(' ')}\n    return Promise.reject(new Error(${JSON.stringify(secretError)}));`);
    }
    return contents;
  }
  if (path === paths[3]) {
    let contents = `import { ExtensionHostKind } from '../../services/extensions/common/extensionHostKind.js';\n` + source;
    contents = replaceOnce(contents, 'private readonly _proxy: ExtHostSecretStateShape;', 'private readonly _proxy: ExtHostSecretStateShape;\n  private readonly _targetSecretsUnavailable: boolean;');
    contents = replaceOnce(contents, 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostSecretState);', `this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostSecretState);
    // TODO(6.6)：目标秘密尚未接线，不向 Node 广播 Web profile 的秘密键或借用其正文。
    this._targetSecretsUnavailable = extHostContext.extensionHost.runningLocation.kind === ExtensionHostKind.Remote;
    if (this._targetSecretsUnavailable) return;`);
    for (const declaration of ['$getPassword(extensionId: string, key: string): Promise<string | undefined>', '$setPassword(extensionId: string, key: string, value: string): Promise<void>', '$deletePassword(extensionId: string, key: string): Promise<void>', '$getKeys(extensionId: string): Promise<string[]>']) {
      contents = replaceOnce(contents, `${declaration} {`, `${declaration} {\n    if (this._targetSecretsUnavailable) return Promise.reject(new Error(${JSON.stringify(secretError)}));`);
    }
    return contents;
  }
  if (path === paths[0]) {
    let contents = replaceOnce(source, "import { IExtension }", "import { ExtensionInstallationTarget, installationTargetKey, IExtension }");
    contents = replaceOnce(contents, 'const EXTENSION_KEYS_ID_VERSION_REGEX', "const TARGET_EXTENSION_STATE_PREFIX = 'boxteam.extensionState/';\n\nconst EXTENSION_KEYS_ID_VERSION_REGEX");
    contents = replaceOnce(contents, 'export interface IExtensionStorageService {', `export interface IExtensionStorageScope {
  readonly installationTarget: ExtensionInstallationTarget;
  readonly workspaceId: string;
}

export interface IExtensionStorageService {
  getExtensionStateKey(extension: IExtension | IGalleryExtension | string, global: boolean, scope?: IExtensionStorageScope): string;`);
    contents = contents.replaceAll('global: boolean):', 'global: boolean, scope?: IExtensionStorageScope):');
    contents = replaceOnce(contents, 'this.getExtensionStateRaw(extension, global);', 'this.getExtensionStateRaw(extension, global, scope);');
    contents = replaceOnce(contents, '\tprivate getExtensionId(', `
  getExtensionStateKey(extension: IExtension | IGalleryExtension | string, global: boolean, scope?: IExtensionStorageScope): string {
    const id = this.getExtensionId(extension);
    if (!scope) return id;
    return TARGET_EXTENSION_STATE_PREFIX + JSON.stringify([installationTargetKey(scope.installationTarget), global ? null : scope.workspaceId, id.toLowerCase()]);
  }

\tprivate getExtensionId(`);
    const rawStart = contents.indexOf('\tgetExtensionStateRaw(extension:', contents.indexOf('export class ExtensionStorageService'));
    const keysStart = contents.indexOf('\tsetKeysForSync(', rawStart);
    const stateMethods = contents.slice(rawStart, keysStart);
    contents = replaceOnce(contents, stateMethods, stateMethods
      .replaceAll('this.getExtensionId(extension)', 'this.getExtensionStateKey(extension, global, scope)')
      .replaceAll('global ? StorageScope.PROFILE', 'scope || global ? StorageScope.PROFILE'));
    return contents;
  }
  if (path === paths[2] && runtime === 'node') {
    return replaceOnce(source, 'this._proxy.$registerExtensionStorageKeysToSync(extension, keys);', `// TODO(6.6)：目标状态的跨设备同步须迁齐原同步消费者；不能只保存无人读取的声明。
    void extension; void keys;
    throw new Error('目标 Node 扩展暂不支持 setKeysForSync 跨设备状态同步');`);
  }
  if (path !== paths[1]) return source;
  let contents = replaceOnce(source, 'IExtensionIdWithVersion, IExtensionStorageService', 'IExtensionIdWithVersion, IExtensionStorageScope, IExtensionStorageService');
  contents = `import { ExtensionIdentifier } from '../../../platform/extensions/common/extensions.js';
import { ExtensionHostKind } from '../../services/extensions/common/extensionHostKind.js';\n` + contents;
  contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
  contents = contents.replaceAll('_sharedStorageKeysToWatch', '_storageKeysToWatch');
  contents = replaceOnce(contents, 'Map<string, boolean> = new Map<string, boolean>();', 'Map<string, { shared: boolean; extensionId: string }> = new Map();\n  private _disposed = false;');
  contents = replaceOnce(contents, 'if (this._storageKeysToWatch.has(e.key)) {', 'const watch = this._storageKeysToWatch.get(e.key);\n      if (watch) {');
  contents = replaceOnce(contents, 'this._proxy.$acceptValue(true, e.key, rawState);', 'this._proxy.$acceptValue(watch.shared, watch.extensionId, rawState);');
  contents = replaceOnce(contents, '\t\tthis._storageListener.dispose();', '\t\tthis._disposed = true;\n\t\tthis._storageListener.dispose();');
  const start = contents.indexOf('\tasync $initializeExtensionStorage(');
  const end = contents.indexOf('\n\tprivate async checkAndMigrateExtensionStorage(', start);
  return replaceOnce(contents, contents.slice(start, end), `
  private storageScope(extensionId: string): IExtensionStorageScope | undefined {
    if (this._disposed) throw new Error('扩展状态的宿主代际已经关闭');
    const host = this.extHostContext.extensionHost, binding = host.binding;
    if (!binding) {
      if (host.runningLocation.kind === ExtensionHostKind.Remote) throw new Error('目标扩展状态缺少认证绑定');
      return undefined;
    }
    const description = host.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId));
    if (!description || (!host.extensions!.containsExtension(description.identifier) &&
        (!(description.main || description.browser) || description.api !== 'none'))) {
      throw new Error('扩展状态不属于发起宿主：' + extensionId);
    }
    const workspaceId = binding.initialization?.workspace?.id;
    if (!workspaceId) throw new Error('目标扩展状态缺少稳定工作区视图');
    return { installationTarget: { kind: 'backend', gatewayId: binding.gateway_id, backendId: binding.backend_id }, workspaceId };
  }

  async $initializeExtensionStorage(shared: boolean, extensionId: string): Promise<string | undefined> {
    const scope = this.storageScope(extensionId);
    // 旧 Web profile 沿原迁移；新目标命名空间不借用 Web 或其它目标的历史正文／文件。
    // TODO(6.6)：目标扩展重命名迁移须同时迁齐目标文件 owner，不能调用窗口的旧文件迁移。
    if (!scope) {
      await this.checkAndMigrateExtensionStorage(extensionId, shared);
      this.storageScope(extensionId);
    }
    if (shared || scope) {
      const key = this._extensionStorageService.getExtensionStateKey(extensionId, shared, scope);
      this._storageKeysToWatch.set(key, { shared, extensionId });
    }
    return this._extensionStorageService.getExtensionStateRaw(extensionId, shared, scope);
  }

  async $setValue(shared: boolean, key: string, value: object): Promise<void> {
    this._extensionStorageService.setExtensionState(key, value, shared, this.storageScope(key));
  }

  $registerExtensionStorageKeysToSync(extension: IExtensionIdWithVersion, keys: string[]): void {
    if (this.storageScope(extension.id)) throw new Error('目标 Node 扩展暂不支持 setKeysForSync 跨设备状态同步');
    this._extensionStorageService.setKeysForSync(extension, keys);
  }
`);
}
