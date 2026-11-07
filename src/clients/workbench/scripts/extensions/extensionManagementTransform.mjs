import { join } from 'node:path';
import ts from 'typescript';

export const extensionManagementSourcePaths = [
  'src/vs/workbench/services/extensionManagement/common/webExtensionManagementService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagement.ts',
  'src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementServerService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsWorkbenchService.ts',
];

/** 用户安装只交原生 owner；原浏览器 scanner 只保留系统／额外内置和派生缓存。 */
export function transformExtensionManagement(root, path, source, replaceOnce) {
  const paths = extensionManagementSourcePaths.map(path => join(root, 'upstream', path));
  if (path === paths[0]) return `export { WebExtensionManagementService } from ${JSON.stringify(join(root, 'src/extensions/nativeManagement.js'))};\n`;
  const userMethods = ['scanExistingExtension', 'scanExtensionManifest', 'addExtension', 'addExtensionFromGallery', 'removeExtension',
    'updateMetadata', 'copyExtensions', 'addWebExtension', 'addToInstalledExtensions', 'scanInstalledExtensions', 'readInstalledExtensions', 'writeInstalledExtensions'];
  if (path === paths[1]) {
    const changes = new Map(['scanExistingExtension', 'scanExtensionManifest', 'addExtension', 'addExtensionFromGallery', 'removeExtension', 'updateMetadata', 'copyExtensions'].map(name => [name, '']));
    changes.set('scanUserExtensions', '\n  scanCustomBuiltinExtensions(options?: ScanOptions): Promise<IScannedExtension[]>;');
    return replaceOnce(replaceMembers(path, source, 'IWebExtensionsScannerService', changes), 'IExtension, ExtensionType,', 'IExtension,');
  }
  if (path === paths[2]) {
    const changes = new Map(userMethods.map(name => [name, '']));
    changes.set('scanUserExtensions', '\n  async scanCustomBuiltinExtensions(options?: ScanOptions): Promise<IScannedExtension[]> { return this.readCustomBuiltinExtensions(options); }');
    let contents = replaceMembers(path, source, 'WebExtensionsScannerService', changes);
    for (const line of [
      "import { IUserDataProfilesService } from '../../../../platform/userDataProfile/common/userDataProfile.js';\n",
      "import { IUriIdentityService } from '../../../../platform/uriIdentity/common/uriIdentity.js';\n",
      '\t\t@IUserDataProfilesService private readonly userDataProfilesService: IUserDataProfilesService,\n',
      '\t\t@IUriIdentityService private readonly uriIdentityService: IUriIdentityService,\n',
    ]) contents = replaceOnce(contents, line, '');
    return contents;
  }
  if (path === paths[3]) {
    let contents = replaceMembers(path, source, 'ExtensionManagementServerService', new Map([
      ['constructor', `\n  constructor(@IInstantiationService instantiationService: IInstantiationService) {
    this.webExtensionManagementServer = { id: 'web', label: '扩展目标', extensionManagementService: instantiationService.createInstance(WebExtensionManagementService) };
  }`],
      ['getExtensionManagementServer', `\n  getExtensionManagementServer(extension: IExtension): IExtensionManagementServer {
    if (!extension.installationTarget) throw new Error('扩展管理对象缺少明确安装身份：' + extension.identifier.id);
    return this.webExtensionManagementServer!;
  }`],
      ['getExtensionInstallLocation', `\n  getExtensionInstallLocation(extension: IExtension): ExtensionInstallLocation {
    this.getExtensionManagementServer(extension);
    return extension.installationTarget?.kind === 'backend' ? ExtensionInstallLocation.Remote : ExtensionInstallLocation.Web;
  }`],
    ]));
    for (const line of contents.split('\n').filter(line => /^import /.test(line) && /\b(localize|IRemoteAgentService|Schemas|IChannel|ILabelService|isWeb|RemoteExtensionManagementService)\b/.test(line))) contents = replaceOnce(contents, line + '\n', '');
    return contents;
  }
  if (path === paths[4]) {
    let contents = `import { captureTargetInstallOptions } from ${JSON.stringify(join(root, 'src/extensions/nativeManagement.js'))};\n` + source;
    contents = replaceMembers(path, contents, 'ExtensionManagementService', new Map([
      ['canInstallGalleryExtension', `\n  private async canInstallGalleryExtension(gallery: IGalleryExtension): Promise<true | IMarkdownString> {
    return this.extensionManagementServerService.webExtensionManagementServer!.extensionManagementService.canInstall(gallery);
  }`],
      ['getInstallableExtensionManagementServers', '\n  private getInstallableExtensionManagementServers(_manifest: IExtensionManifest): IExtensionManagementServer[] { return [...this.servers]; }'],
      ['getExtensionManagementServersToInstall', '\n  private async getExtensionManagementServersToInstall(_gallery: IGalleryExtension, _manifest: IExtensionManifest): Promise<IExtensionManagementServer[]> { return [...this.servers]; }'],
      ['updateFromGallery', `\n  async updateFromGallery(gallery: IGalleryExtension, extension: ILocalExtension, installOptions?: InstallOptions): Promise<ILocalExtension> {
    const options = captureTargetInstallOptions({ ...installOptions, isApplicationScoped: extension.isApplicationScoped }, extension);
    const server = this.getServer(extension);
    if (!server) throw new Error('扩展更新对象缺少原管理服务');
    return server.extensionManagementService.installFromGallery(gallery, options);
  }`],
      ['checkInstallingExtensionOnWeb', ''],
    ]));
    contents = replaceOnce(contents, 'async installGalleryExtensions(extensions: InstallExtensionInfo[]): Promise<InstallExtensionResult[]> {',
      'async installGalleryExtensions(extensions: InstallExtensionInfo[]): Promise<InstallExtensionResult[]> {\n  extensions = extensions.map(({ extension, options }) => ({ extension, options: captureTargetInstallOptions(options) }));');
    contents = replaceOnce(contents, 'async installFromGallery(gallery: IGalleryExtension, installOptions?: InstallOptions, servers?: IExtensionManagementServer[]): Promise<ILocalExtension> {',
      'async installFromGallery(gallery: IGalleryExtension, installOptions?: InstallOptions, servers?: IExtensionManagementServer[]): Promise<ILocalExtension> {\n  installOptions = captureTargetInstallOptions(installOptions);');
    for (const call of ['await this.checkInstallingExtensionOnWeb(extension, manifest);', 'await this.checkInstallingExtensionOnWeb(gallery, manifest);']) {
      const expression = new RegExp('\\n\\t+if \\(!(?:options|installOptions)\\?\\.donotIncludePackAndDependencies\\) \\{\\n\\t+' + call.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\n\\t+\\}');
      const match = contents.match(expression);
      if (!match) throw new Error('原浏览器安装提示消费者缺失：' + call);
      contents = replaceOnce(contents, match[0], '');
    }
    contents = replaceOnce(contents, 'ExtensionManagementError, ', '');
    contents = replaceOnce(contents, ', getWorkspaceSupportTypeMessage', '');
    return contents;
  }
  if (path === paths[5]) {
    let contents = replaceOnce(source, 'private async fetchInstalledExtensions(productVersion?: IProductVersion): Promise<void> {',
      'private _installedRequest = 0;\n  private async fetchInstalledExtensions(productVersion?: IProductVersion): Promise<void> {\n  const request = ++this._installedRequest;');
    contents = replaceOnce(contents, '\n\t\t// dedup workspace, user and system extensions', '\n  if (request !== this._installedRequest) return;\n\t\t// dedup workspace, user and system extensions');
    return contents;
  }
  if (path === join(root, 'upstream/src/vs/platform/extensionManagement/common/abstractExtensionManagementService.ts')) {
    return replaceOnce(source, 'private readonly _onInstallExtension =', 'protected readonly _onInstallExtension =');
  }
  return source;
}

function replaceMembers(path, source, ownerName, replacements) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const owners = parsed.statements.filter(node => (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) && node.name?.text === ownerName);
  if (owners.length !== 1) throw new Error('原扩展管理 owner 不唯一：' + ownerName);
  const edits = [];
  for (const [name, replacement] of replacements) {
    const members = owners[0].members.filter(node => (ts.isConstructorDeclaration(node) ? 'constructor' : node.name?.getText(parsed)) === name);
    if (members.length !== 1) throw new Error('原扩展管理成员不唯一：' + ownerName + '.' + name);
    edits.push({ start: members[0].getFullStart(), end: members[0].end, replacement });
  }
  for (const edit of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, edit.start) + edit.replacement + source.slice(edit.end);
  return source;
}
