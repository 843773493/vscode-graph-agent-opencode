import { join } from 'node:path';

export const extensionEnablementSourcePaths = [
  'src/vs/platform/extensionManagement/common/extensionManagement.ts',
  'src/vs/platform/extensionManagement/common/extensionEnablementService.ts',
  'src/vs/workbench/services/extensionManagement/browser/extensionEnablementService.ts',
];

export function transformExtensionEnablement(root, path, source, replaceOnce) {
  const paths = extensionEnablementSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = `import type { ExtensionInstallationTarget } from '../../extensions/common/extensions.js';\n` + source;
    return replaceOnce(contents, 'export interface IExtensionIdentifier {', `export interface IExtensionIdentifier {
  /** Web 启用状态沿原 profile scope；Backend 增加稳定目标，workspace 记录再带明确视图。 */
  enablementTarget?: Extract<ExtensionInstallationTarget, { kind: 'backend' }>;
  workspaceView?: string;`);
  }
  if (path === paths[1]) {
    let contents = `import { installationTargetKey } from '../../extensions/common/extensions.js';\n` + source;
    contents = contents.replaceAll('areSameExtensions(', 'areSameEnablementIdentifiers(');
    contents = replaceOnce(contents, 'value.map(({ id, uuid }): IExtensionIdentifier => ({ id, uuid }))', 'value.map(({ id, uuid, enablementTarget, workspaceView }): IExtensionIdentifier => ({ id, uuid, enablementTarget, workspaceView }))');
    return contents + `
export function areSameEnablementIdentifiers(a: IExtensionIdentifier, b: IExtensionIdentifier): boolean {
  return areSameExtensions(a, b)
    && (a.enablementTarget ? installationTargetKey(a.enablementTarget) : null) === (b.enablementTarget ? installationTargetKey(b.enablementTarget) : null)
    && (a.workspaceView ?? null) === (b.workspaceView ?? null);
}\n`;
  }
  if (path !== paths[2]) return source;
  let contents = replaceOnce(source, 'import { StorageManager }', 'import { StorageManager, areSameEnablementIdentifiers }');
  contents = replaceOnce(contents, 'ExtensionType, EXTENSIONS_', 'installationTargetKey, ExtensionType, EXTENSIONS_');
  contents = replaceOnce(contents, '\tprivate get hasWorkspace():', `
  private _enablementIdentifier(extension: IExtension): IExtensionIdentifier {
    const target = extension.installationTarget;
    if (target?.kind !== 'backend') return extension.identifier;
    return { ...extension.identifier, enablementTarget: target, workspaceView: extension.enablementView };
  }
  private _sameInstallation(a: IExtension, b: IExtension): boolean {
    return (a.installationTarget ? installationTargetKey(a.installationTarget) : null)
      === (b.installationTarget ? installationTargetKey(b.installationTarget) : null);
  }
\tprivate get hasWorkspace():`);
  contents = contents.replaceAll('this._getUserEnablementState(extension.identifier)', 'this._getUserEnablementState(this._enablementIdentifier(extension))');
  contents = contents.replaceAll('this._isDisabledGlobally(extension.identifier)', 'this._isDisabledGlobally(this._enablementIdentifier(extension))');
  for (const method of ['_enableExtension', '_disableExtension', '_enableExtensionInWorkspace', '_disableExtensionInWorkspace']) {
    contents = contents.replaceAll(`this.${method}(extension.identifier)`, `this.${method}(this._enablementIdentifier(extension))`);
  }
  contents = replaceOnce(contents, 'this.globalExtensionEnablementService.getDisabledExtensions().some(e => areSameExtensions(e, identifier))', 'this.globalExtensionEnablementService.getDisabledExtensions().some(e => areSameEnablementIdentifiers(e, { ...identifier, workspaceView: undefined }))');
  contents = contents.replaceAll('areSameExtensions(e, identifier)', 'areSameEnablementIdentifiers(e, identifier)');
  contents = contents.replaceAll('areSameExtensions(disabledExtension, identifier)', 'areSameEnablementIdentifiers(disabledExtension, identifier)');
  contents = contents.replaceAll('enableExtension(identifier, SOURCE)', 'enableExtension({ ...identifier, workspaceView: undefined }, SOURCE)');
  contents = contents.replaceAll('disableExtension(identifier, SOURCE)', 'disableExtension({ ...identifier, workspaceView: undefined }, SOURCE)');
  contents = replaceOnce(contents, 'private _isDisabledByVirtualWorkspace(extension: IExtension, workspaceType: WorkspaceType): boolean {', `private _isDisabledByVirtualWorkspace(extension: IExtension, workspaceType: WorkspaceType): boolean {
    // 已认证 Backend view 在 Node 侧由真实 file 根初始化，窗口投影 scheme 不使它成为虚拟文件系统。
    if (extension.installationTarget?.kind === 'backend') return false;`);
  contents = replaceOnce(contents, 'private _isDisabledByExtensionKind(extension: IExtension): boolean {', `private _isDisabledByExtensionKind(extension: IExtension): boolean {
    if (extension.installationTarget?.kind === 'backend') {
      const kinds = this.extensionManifestPropertiesService.getExtensionKind(extension.manifest);
      return !(extension.manifest.main && kinds.includes('workspace') || extension.manifest.browser && kinds.includes('web'));
    }`);
  contents = replaceOnce(contents, '(this.extensionManagementServerService.getExtensionManagementServer(e) === this.extensionManagementServerService.getExtensionManagementServer(extension) || ((e.manifest.main || e.manifest.browser) && e.manifest.api === \'none\'))', `(this._sameInstallation(e, extension) || (e.installationTarget?.kind !== 'backend' && ((e.manifest.main || e.manifest.browser) && e.manifest.api === 'none')))`);
  contents = replaceOnce(contents, '!result.includes(dependent) && dependent.manifest.extensionDependencies?', '!result.includes(dependent) && this._sameInstallation(dependent, extension) && dependent.manifest.extensionDependencies?');
  contents = contents.replaceAll('getExtensionDependencies(this.extensionsManager.extensions, extension)', 'getExtensionDependencies(this.extensionsManager.extensions.filter(candidate => this._sameInstallation(candidate, extension)), extension)');
  const managerStart = contents.indexOf('class ExtensionsManager');
  const owner = contents.slice(0, managerStart).replaceAll('areSameExtensions(e.identifier, extension.identifier)', '(this._sameInstallation(e, extension) && areSameExtensions(e.identifier, extension.identifier))');
  contents = owner + contents.slice(managerStart);
  contents = replaceOnce(contents, 'if (extensions.some(e =>\n\t\t\t\t(options.dependencies', 'if (extensions.some(e => this._sameInstallation(e, extension) && (\n\t\t\t\t(options.dependencies');
  contents = replaceOnce(contents, "|| (options.pack && e.manifest.extensionPack?.some(id => areSameExtensions({ id }, extension.identifier))))) {", "|| (options.pack && e.manifest.extensionPack?.some(id => areSameExtensions({ id }, extension.identifier)))))) {");
  contents = replaceOnce(contents, 'extensionIdentifiers.some(identifier => areSameExtensions(identifier, installedExtension.identifier))', 'extensionIdentifiers.some(identifier => areSameEnablementIdentifiers(identifier, { ...this._enablementIdentifier(installedExtension), workspaceView: undefined }))');
  contents = replaceOnce(contents, 'removed.forEach(({ identifier }) => this._reset(identifier));', 'removed.forEach(extension => this._reset(this._enablementIdentifier(extension)));');
  contents = replaceOnce(contents, 'this.globalExtensionEnablementService.enableExtension(extension);', 'this.globalExtensionEnablementService.enableExtension({ ...extension, workspaceView: undefined });');
  return contents;
}
