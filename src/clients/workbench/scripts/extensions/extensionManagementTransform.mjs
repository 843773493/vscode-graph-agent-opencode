import { join } from 'node:path';
import ts from 'typescript';

export const extensionManagementSourcePaths = [
  'src/vs/workbench/services/extensionManagement/common/webExtensionManagementService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagement.ts',
  'src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementServerService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsWorkbenchService.ts',
  'src/vs/workbench/contrib/extensions/common/extensions.ts',
  'src/vs/workbench/contrib/extensions/common/extensionsInput.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionEditor.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsActions.ts',
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
      ['_targetPlatformPromise', ''],
      ['getTargetPlatform', `\n  getTargetPlatform(options?: InstallOptions): Promise<TargetPlatform> {
    return this.extensionManagementServerService.webExtensionManagementServer!.extensionManagementService.getTargetPlatform(options);
  }`],
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
    contents = replaceOnce(contents, 'areSameExtensions, computeTargetPlatform', 'areSameExtensions');
    contents = replaceOnce(contents,
      '\t\t@IExtensionManifestPropertiesService private readonly extensionManifestPropertiesService: IExtensionManifestPropertiesService,\n\t\t@IFileService private readonly fileService: IFileService,',
      '\t\t@IExtensionManifestPropertiesService private readonly extensionManifestPropertiesService: IExtensionManifestPropertiesService,');
    contents = replaceOnce(contents, ', getWorkspaceSupportTypeMessage', '');
    return contents;
  }
  if (path === paths[5]) {
    let contents = transformManagementView(root, path, source, replaceOnce);
    contents = replaceOnce(contents, 'private async fetchInstalledExtensions(productVersion?: IProductVersion): Promise<void> {',
      'private _installedRequest = 0;\n  private async fetchInstalledExtensions(productVersion?: IProductVersion): Promise<void> {\n  const request = ++this._installedRequest;');
    contents = replaceOnce(contents, '\n\t\t// dedup workspace, user and system extensions', '\n  if (request !== this._installedRequest) return;\n\t\t// dedup workspace, user and system extensions');
    return contents;
  }
  if (path === paths[6]) {
    let contents = `import { sameExtensionManagementTarget, type ManagementTarget } from ${JSON.stringify(join(root, 'src/managementTarget.js'))};\n`
      + replaceOnce(source, 'export interface IExtension {', 'export interface IExtension {\n  readonly managementTarget?: ManagementTarget;');
    return replaceOnce(contents, 'if (areSameExtensions(container.extension.identifier, extension.identifier)) {',
      'if (areSameExtensions(container.extension.identifier, extension.identifier) && sameExtensionManagementTarget(container.extension.managementTarget, extension.managementTarget)) {');
  }
  if (path === paths[8]) {
    return `import { sameExtensionManagementTarget } from ${JSON.stringify(join(root, 'src/managementTarget.js'))};\n`
      + replaceOnce(source, 'if (e && areSameExtensions(e.identifier, extension.identifier) && e.server === extension.server) {',
        'if (e && areSameExtensions(e.identifier, extension.identifier) && e.server === extension.server\n      && sameExtensionManagementTarget(e.managementTarget, extension.managementTarget)) {');
  }
  if (path === paths[7]) {
    let contents = `import { sameExtensionManagementTarget } from ${JSON.stringify(join(root, 'src/managementTarget.js'))};\n` + source;
    contents = replaceOnce(contents, 'path: join(this._extension.identifier.id, \'extension\')',
      `path: join(this._extension.identifier.id, 'extension'),
      query: this._extension.managementTarget ? JSON.stringify([this._extension.managementTarget.gateway_id, this._extension.managementTarget.backend_id]) : ''`);
    contents = replaceOnce(contents, 'return other instanceof ExtensionsInput && areSameExtensions(this._extension.identifier, other._extension.identifier);',
      'return other instanceof ExtensionsInput && areSameExtensions(this._extension.identifier, other._extension.identifier)\n      && sameExtensionManagementTarget(this._extension.managementTarget, other._extension.managementTarget);');
    contents = replaceOnce(contents, 'return localize(\'extensionsInputName\', "Extension: {0}", this._extension.displayName);',
      'return localize(\'extensionsInputName\', "Extension: {0}", this._extension.displayName)\n      + (this._extension.managementTarget ? \' · \' + this._extension.managementTarget.name : \'\');');
    return replaceOnce(contents, 'override getIcon(): ThemeIcon | undefined {', `override getDescription(): string | undefined {
    const target = this._extension.managementTarget;
    return target ? target.name + ' · ' + target.backend_id : undefined;
  }

  override getIcon(): ThemeIcon | undefined {`);
  }
  if (path === paths[9]) {
    const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
    const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'InstallAnotherVersionAction');
    const run = owner?.members.find(node => node.name?.getText(parsed) === 'run');
    if (!run) throw new Error('原版本选择动作缺少 run');
    let replacement = run.getText(parsed).replace(/\bthis\.extension\b/g, 'extension');
    // 确认和版本列表异步等待期间，原详情事件不能改投这次动作。
    replacement = replaceOnce(replacement, 'if (!this.enabled)', 'const extension = this.extension;\n    if (!this.enabled)');
    replacement = replaceOnce(replacement, 'const targetPlatform =',
      'const installOptions = captureTargetInstallOptions({}, extension.local, extension.managementTarget);\n    const targetPlatform =');
    replacement = replaceOnce(replacement,
      'const targetPlatform = extension.server ? await extension.server.extensionManagementService.getTargetPlatform() : await this.extensionManagementService.getTargetPlatform();',
      'const targetPlatform = await this.extensionManagementService.getTargetPlatform(installOptions);');
    replacement = replaceOnce(replacement, 'const options = { installPreReleaseVersion:', 'const options = { ...installOptions, installPreReleaseVersion:');
    return `import { captureTargetInstallOptions } from ${JSON.stringify(join(root, 'src/extensions/nativeManagement.js'))};\n`
      + source.slice(0, run.getStart(parsed)) + replacement + source.slice(run.end);
  }
  if (path === join(root, 'upstream/src/vs/platform/extensionManagement/common/extensionManagement.ts')) {
    let contents = replaceOnce(source, 'canInstall(extension: IGalleryExtension): Promise<true | IMarkdownString>;',
      'canInstall(extension: IGalleryExtension, targetPlatform?: TargetPlatform): Promise<true | IMarkdownString>;');
    return replaceOnce(contents, 'getTargetPlatform(): Promise<TargetPlatform>;', 'getTargetPlatform(options?: InstallOptions): Promise<TargetPlatform>;');
  }
  if (path === join(root, 'upstream/src/vs/platform/extensionManagement/common/abstractExtensionManagementService.ts')) {
    let contents = replaceOnce(source, 'private readonly _onInstallExtension =', 'protected readonly _onInstallExtension =');
    contents = replaceOnce(contents, 'async canInstall(extension: IGalleryExtension): Promise<true | IMarkdownString> {',
      'async canInstall(extension: IGalleryExtension, targetPlatform?: TargetPlatform): Promise<true | IMarkdownString> {');
    contents = replaceOnce(contents, 'if (!(await this.isExtensionPlatformCompatible(extension))) {',
      'const platform = targetPlatform ?? await this.getTargetPlatform();\n    if (!(await this.isExtensionPlatformCompatible(extension, platform))) {');
    contents = replaceOnce(contents, 'TargetPlatformToString(await this.getTargetPlatform())', 'TargetPlatformToString(platform)');
    contents = replaceOnce(contents, 'protected async isExtensionPlatformCompatible(extension: IGalleryExtension): Promise<boolean> {\n\t\tconst currentTargetPlatform = await this.getTargetPlatform();',
      'protected async isExtensionPlatformCompatible(extension: IGalleryExtension, targetPlatform?: TargetPlatform): Promise<boolean> {\n    const currentTargetPlatform = targetPlatform ?? await this.getTargetPlatform();');
    return contents;
  }
  return source;
}

/** 原列表、详情和写入共用同一对象归属；展示目标变化不能改投已有对象。 */
function transformManagementView(root, path, source, replaceOnce) {
  const target = JSON.stringify(join(root, 'src/managementTarget.js'));
  const native = JSON.stringify(join(root, 'src/extensions/nativeManagement.js'));
  let contents = `import { extensionManagementTarget, managementGateway, onDidChangeExtensionManagementTarget, sameExtensionManagementTarget, type ManagementTarget } from ${target};
import { captureTargetInstallOptions } from ${native};\n` + source;
  contents = replaceOnce(contents, 'Disposable, MutableDisposable, toDisposable', 'Disposable, DisposableStore, MutableDisposable, toDisposable');
  contents = replaceOnce(contents, 'private readonly resourceExtensionInfo: { resourceExtension: IResourceExtension; isWorkspaceScoped: boolean } | undefined,',
    'private readonly resourceExtensionInfo: { resourceExtension: IResourceExtension; isWorkspaceScoped: boolean } | undefined,\n    galleryTarget: ManagementTarget | undefined,');
  contents = replaceOnce(contents, '\tpublic enablementState:', '\treadonly managementTarget: ManagementTarget | undefined;\n\n\tpublic enablementState:');
  contents = replaceOnce(contents, '\t) {\n\t}\n\n\tget resourceExtension()', `\t) {
    this.managementTarget = local?.installationTarget?.kind === 'backend'
      ? captureTargetInstallOptions({}, local).nativeTarget : galleryTarget;
\t}

\tget resourceExtension()`);
  const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true), additions = [];
  const visit = node => {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'createInstance'
      && node.arguments[0]?.getText(parsed) === 'Extension') additions.push(node.end - 1);
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  for (const position of additions.reverse()) contents = contents.slice(0, position) + ', undefined' + contents.slice(position);
  contents = replaceOnce(contents, 'this.server, undefined, source, undefined, undefined);',
    "this.server, undefined, source, undefined, managementGateway('extensions'));");

  contents = replaceOnce(contents, 'export class ExtensionsWorkbenchService extends Disposable implements IExtensionsWorkbenchService, IURLHandler {', `export class ExtensionsWorkbenchService extends Disposable implements IExtensionsWorkbenchService, IURLHandler {
  private managementTargetVersion = 0;
  private async resolveManagementTarget(token: CancellationToken): Promise<ManagementTarget> {
    const selected = managementGateway('extensions'), lifetime = new DisposableStore();
    try { return await extensionManagementTarget(selected, CancellationToken.toAbortSignal(token, lifetime)); }
    finally { lifetime.dispose(); }
  }
  private checkManagementTarget(version: number): void {
    if (version !== this.managementTargetVersion || this._store.isDisposed) throw new CancellationError();
  }`);
  // 监听归原服务生命周期；只失效列表查询，不重绑运行宿主或已打开详情。
  const constructor = contents.indexOf('\n\tconstructor(', contents.indexOf('export class ExtensionsWorkbenchService'));
  const superCall = contents.indexOf('\n\t\tsuper();', constructor);
  contents = contents.slice(0, superCall) + contents.slice(superCall).replace('\n\t\tsuper();',
    '\n\t\tsuper();\n    this._register(onDidChangeExtensionManagementTarget(() => this.managementTargetVersion++));');

  contents = replaceOnce(contents, 'async queryGallery(arg1: any, arg2?: any): Promise<IPager<IExtension>> {',
    'async queryGallery(arg1: any, arg2?: any): Promise<IPager<IExtension>> {\n    const version = this.managementTargetVersion;');
  contents = replaceOnce(contents, 'const token: CancellationToken = CancellationToken.isCancellationToken(arg1) ? arg1 : arg2;',
    'const token: CancellationToken = CancellationToken.isCancellationToken(arg1) ? arg1 : arg2;\n    const target = await this.resolveManagementTarget(token);\n    this.checkManagementTarget(version);');
  contents = replaceOnce(contents, 'const pager = await this.galleryService.query(options, token);',
    'const pager = await this.galleryService.query(options, token);\n    this.checkManagementTarget(version);');
  contents = replaceOnce(contents, 'const page = await pager.getPage(pageIndex, token);',
    'this.checkManagementTarget(version);\n        const page = await pager.getPage(pageIndex, token);\n        this.checkManagementTarget(version);');
  contents = contents.replaceAll('this.fromGallery(gallery, extensionsControlManifest)', 'this.fromGallery(gallery, extensionsControlManifest, target)');
  contents = replaceOnce(contents, 'async getExtensions(extensionInfos: IExtensionInfo[], arg1: any, arg2?: any): Promise<IExtension[]> {',
    'async getExtensions(extensionInfos: IExtensionInfo[], arg1: any, arg2?: any): Promise<IExtension[]> {\n    const version = this.managementTargetVersion;');
  contents = replaceOnce(contents, '\n\t\textensionInfos.forEach(e => e.preRelease', `
    const target = await this.resolveManagementTarget(CancellationToken.isCancellationToken(arg1) ? arg1 : arg2);
    this.checkManagementTarget(version);
\t\textensionInfos.forEach(e => e.preRelease`);
  contents = replaceOnce(contents, 'const galleryExtensions = await this.galleryService.getExtensions(extensionInfos, arg1, arg2);',
    'const galleryExtensions = await this.galleryService.getExtensions(extensionInfos, arg1, arg2);\n    this.checkManagementTarget(version);');
  contents = replaceOnce(contents, 'private fromGallery(gallery: IGalleryExtension, extensionsControlManifest: IExtensionsControlManifest): IExtension {',
    'private fromGallery(gallery: IGalleryExtension, extensionsControlManifest: IExtensionsControlManifest, target: ManagementTarget): IExtension {');
  contents = replaceOnce(contents, 'let extension = this.getInstalledExtensionMatchingGallery(gallery);',
    'let extension = this.getInstalledExtensionMatchingGallery(gallery, target);');
  contents = replaceOnce(contents, 'private getInstalledExtensionMatchingGallery(gallery: IGalleryExtension): IExtension | null {',
    'private getInstalledExtensionMatchingGallery(gallery: IGalleryExtension, target?: ManagementTarget): IExtension | null {');
  const matching = contents.indexOf('private getInstalledExtensionMatchingGallery(');
  contents = contents.slice(0, matching) + contents.slice(matching).replace('for (const installed of this.local) {',
    'for (const installed of this.local) {\n      if (target && !sameExtensionManagementTarget(installed.managementTarget, target)) continue;');
  const fromGallery = contents.indexOf('private fromGallery('), matchingEnd = contents.indexOf('private getInstalledExtensionMatchingGallery(', fromGallery);
  const creator = 'undefined, undefined, gallery, undefined, undefined);';
  contents = contents.slice(0, fromGallery) + contents.slice(fromGallery, matchingEnd).replace(creator,
    'this.extensionManagementServerService.webExtensionManagementServer ?? undefined, undefined, gallery, undefined, target);') + contents.slice(matchingEnd);
  contents = replaceOnce(contents, 'private async _install(arg: string | URI | IExtension, installOptions: InstallExtensionOptions = {}, progressLocation?: ProgressLocation | string): Promise<IExtension | undefined> {', `private async _install(arg: string | URI | IExtension, installOptions: InstallExtensionOptions = {}, progressLocation?: ProgressLocation | string): Promise<IExtension | undefined> {
    const object = typeof arg === 'string' || URI.isUri(arg) ? undefined : arg;
    const captured = captureTargetInstallOptions(installOptions, object?.local, object?.managementTarget);
    installOptions = { ...installOptions, ...captured };`);
  contents = replaceOnce(contents, 'canInstall(galleryExtension: IGalleryExtension): Promise<true | IMarkdownString> {\n\t\treturn this.server.extensionManagementService.canInstall(galleryExtension);',
    `async canInstall(galleryExtension: IGalleryExtension, target?: ManagementTarget): Promise<true | IMarkdownString> {
    const platform = await this.server.extensionManagementService.getTargetPlatform(captureTargetInstallOptions({}, undefined, target));
    return this.server.extensionManagementService.canInstall(galleryExtension, platform);`);
  for (const service of ['localExtensions', 'remoteExtensions', 'webExtensions']) {
    contents = replaceOnce(contents, `this.${service}.canInstall(extension.gallery)`, `this.${service}.canInstall(extension.gallery, extension.managementTarget)`);
  }
  contents = replaceOnce(contents, 'const targetPlatform = extension?.server ? await extension.server.extensionManagementService.getTargetPlatform() : undefined;',
    'const targetPlatform = await this.extensionManagementService.getTargetPlatform(installOptions);');
  const install = contents.indexOf('private async _install(');
  contents = contents.slice(0, install) + contents.slice(install).replace(creator,
    'this.extensionManagementServerService.webExtensionManagementServer ?? undefined, undefined, gallery, undefined, captured.nativeTarget);');
  // 结果来自同一实际写入回执；当前列表可能已经切换，不能再按公开 ID 等待另一目标。
  contents = replaceOnce(contents, 'return await this.waitAndGetInstalledExtension(local.identifier);', `const result = extension instanceof Extension && extension.managementTarget ? extension : this.instantiationService.createInstance(Extension,
          ext => this.getExtensionState(ext), ext => this.getRuntimeState(ext),
          this.extensionManagementServerService.getExtensionManagementServer(local) ?? undefined, local, undefined, undefined, undefined);
        result.local = local;
        return result;`);
  contents = replaceMembers(path, contents, 'ExtensionsWorkbenchService', new Map([['waitAndGetInstalledExtension', '']]));
  contents = replaceOnce(contents, 'private getExtensionState(extension: Extension): ExtensionState {', `private getExtensionState(extension: Extension): ExtensionState {
    if (!this.installing.includes(extension) && extension.managementTarget
      && !sameExtensionManagementTarget(extension.managementTarget, managementGateway('extensions'))) {
      return extension.local ? ExtensionState.Installed : ExtensionState.Uninstalled;
    }`);
  contents = replaceOnce(contents, 'this.installing.some(i => areSameExtensions(i.identifier, extension.identifier) && (!extension.server || i.server === extension.server))',
    'this.installing.some(i => areSameExtensions(i.identifier, extension.identifier) && sameExtensionManagementTarget(i.managementTarget, extension.managementTarget) && (!extension.server || i.server === extension.server))');
  contents = replaceOnce(contents, 'this.installing.some(e => !!e.gallery && areSameExtensions(e.gallery.identifier, extension.gallery!.identifier))',
    'this.installing.some(e => !!e.gallery && areSameExtensions(e.gallery.identifier, extension.gallery!.identifier) && sameExtensionManagementTarget(e.managementTarget, extension.managementTarget))');
  contents = replaceOnce(contents, 'this.uninstalling.some(e => areSameExtensions(e.identifier, extension.identifier))',
    'this.uninstalling.some(e => areSameExtensions(e.identifier, extension.identifier) && sameExtensionManagementTarget(e.managementTarget, extension.managementTarget))');
  contents = replaceOnce(contents, '(e.gallery && extension.gallery && areSameExtensions(e.gallery.identifier, extension.gallery.identifier))',
    '(e.gallery && extension.gallery && areSameExtensions(e.gallery.identifier, extension.gallery.identifier) && sameExtensionManagementTarget(e.managementTarget, extension.managementTarget))');
  contents = replaceOnce(contents, 'const runningExtension = this.extensionService.extensions.find(e => areSameExtensions({ id: e.identifier.value }, extension.identifier));', `const runningExtension = this.extensionService.extensions.find(e => areSameExtensions({ id: e.identifier.value }, extension.identifier)
      && (extension.managementTarget ? e.installationTarget?.kind === 'backend'
        && e.installationTarget.gatewayId === extension.managementTarget.gateway_id && e.installationTarget.backendId === extension.managementTarget.backend_id
        : e.installationTarget?.kind === 'web'));`);
  contents = replaceOnce(contents, 'const extension = e.local ? e : this.local.find(local => areSameExtensions(local.identifier, e.identifier));',
    'const extension = e.local ? e : this.local.find(local => areSameExtensions(local.identifier, e.identifier) && sameExtensionManagementTarget(local.managementTarget, e.managementTarget));');
  contents = replaceOnce(contents, 'for (const packExtension of this.getAllPackedExtensions(extension, this.local)) {',
    'for (const packExtension of this.getAllPackedExtensions(extension, this.local.filter(local => sameExtensionManagementTarget(local.managementTarget, extension.managementTarget)))) {');
  contents = replaceOnce(contents, 'for (const { local } of this.local) {',
    'for (const { local } of this.local.filter(item => sameExtensionManagementTarget(item.managementTarget, e.managementTarget))) {');
  contents = replaceOnce(contents, '() => this.extensionManagementService.uninstallExtensions(extensionsToUninstall).then(() => undefined)',
    `async () => {
      await this.extensionManagementService.uninstallExtensions(extensionsToUninstall);
      if (extension instanceof Extension) { extension.local = undefined; this._onChange.fire(extension); }
    }`);
  return contents;
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
