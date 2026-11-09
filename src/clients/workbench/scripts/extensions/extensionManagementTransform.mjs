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
  'src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts',
  'src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts',
  'src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts',
  'src/vs/workbench/contrib/welcomeWalkthrough/browser/editor/vs_code_editor_walkthrough.ts',
  'src/vs/workbench/browser/parts/editor/editorStatus.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsQuickAccess.ts',
];

/** 用户安装只交原生 owner；原浏览器 scanner 只保留系统／额外内置和派生缓存。 */
export function transformExtensionManagement(root, path, source, replaceOnce) {
  const paths = extensionManagementSourcePaths.map(path => join(root, 'upstream', path));
  if (path === paths[14]) {
    let contents = replaceOnce(source, '\t\tconst commandService = accessor.get(ICommandService);\n', '');
    contents = replaceOnce(contents, '\t\tconst galleryService = accessor.get(IExtensionGalleryService);\n', '');
    contents = replaceOnce(contents, '\t\tlet galleryAction: IAction | undefined;\n', '');
    const start = contents.indexOf('\t\t\tif (galleryService.isEnabled()) {');
    const end = contents.indexOf('\n\n\t\t\tconfigureLanguageSettings', start);
    if (start < 0 || end < 0) throw new Error('原语言选择器市场入口缺失');
    contents = contents.slice(0, start) + contents.slice(end);
    contents = replaceOnce(contents, '\t\tif (pick === galleryAction) {\n\t\t\tgalleryAction.run();\n\t\t\treturn;\n\t\t}\n', '');
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === paths[15]) {
    const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
    const install = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'InstallExtensionQuickAccessProvider');
    if (!install) throw new Error('原安装 Quick Access 类缺失');
    const contents = source.slice(0, install.getFullStart()) + source.slice(install.end);
    return pruneUnusedImportsAndConstants(path, replaceOnce(contents,
      'Press Enter to manage your extensions.', '按 Enter 管理插件包。'));
  }
  if (path === paths[12]) return pluginGettingStarted(path, source);
  if (path === paths[13]) {
    let contents = replaceOnce(source,
      '>**Tip:** While we ship JavaScript and TypeScript support out of the box other languages can be upgraded with better IntelliSense through one of the many [extensions](command:workbench.extensions.action.showPopularExtensions).',
      '>**Tip:** JavaScript and TypeScript IntelliSense is built in. Available language features depend on the language support included in this workbench.');
    contents = replaceOnce(contents,
      '>**Tip:** Additional formatters are available in the [extension gallery](command:workbench.extensions.action.showPopularExtensions).  Formatting support can also be configured via [settings](command:workbench.action.openGlobalSettings) e.g. enabling |editor.formatOnSave|.',
      '>**Tip:** Configure the available formatting support in [settings](command:workbench.action.openGlobalSettings), for example by enabling |editor.formatOnSave|.');
    return replaceOnce(contents,
      '>**Tip:** The [extension gallery](command:workbench.extensions.action.showPopularExtensions) includes snippets for almost every framework and language imaginable.  You can also create your own [user-defined snippets](command:workbench.action.openSnippets).',
      '>**Tip:** Create your own [user-defined snippets](command:workbench.action.openSnippets) for the languages you use.');
  }
  if (path === paths[11]) return pluginManagementContribution(root, path, source);
  const extensionsViewlet = join(root, 'upstream/src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts');
  if (path === extensionsViewlet) {
    let contents = `import { ServicePluginPackagesView } from ${JSON.stringify(join(root, 'src/extensions/servicePluginManagement.js'))};\n` + source;
    contents = replaceMembers(path, contents, 'ExtensionsViewletViewsContribution', new Map([['registerViews', `
  private registerViews(): void {
    const viewDescriptors: IViewDescriptor[] = [{ id: 'boxteam.views.extensionPackages', name: localize2('servicePluginPackages', "插件包"),
      ctorDescriptor: new SyncDescriptor(ServicePluginPackagesView), order: 1 }];
    Registry.as<IViewsRegistry>(Extensions.ViewsRegistry).registerViews(viewDescriptors, this.container);
  }`], ['constructor', `
  constructor() {
    super();
    this.container = Registry.as<IViewContainersRegistry>(Extensions.ViewContainersRegistry).get(VIEWLET_ID)!;
    this.registerViews();
  }`], ['createDefaultExtensionsViewDescriptors', ''], ['createSearchExtensionsViewDescriptors', ''],
      ['createRecommendedExtensionsViewDescriptors', ''], ['createBuiltinExtensionsViewDescriptors', ''],
      ['createUnsupportedWorkspaceExtensionsViewDescriptors', ''], ['createOtherLocalFilteredExtensionsViewDescriptors', '']]));
    contents = contents.replace('interface IExtensionsViewletState {\n\t\'query.value\'?\: string;\n}', '');
    contents = contents.replace('ExtensionsViewPaneContainer extends ViewPaneContainer<IExtensionsViewletState>', 'ExtensionsViewPaneContainer extends ViewPaneContainer');
    const containerChanges = new Map([
      ['extensionsSearchValueContextKey', '\n  private readonly extensionsSearchValueContextKey: IContextKey<string>;'],
      ['defaultViewsContextKey', '\n  private readonly defaultViewsContextKey: IContextKey<boolean>;'], ['sortByContextKey', ''],
      ['searchMarketplaceExtensionsContextKey', ''], ['searchMcpServersContextKey', '\n  private readonly searchMcpServersContextKey: IContextKey<boolean>;'],
      ['searchAgentPluginsContextKey', '\n  private readonly searchAgentPluginsContextKey: IContextKey<boolean>;'],
      ['searchHasTextContextKey', '\n  private readonly searchHasTextContextKey: IContextKey<boolean>;'], ['sortByUpdateDateContextKey', ''], ['installedExtensionsContextKey', ''],
      ['searchInstalledExtensionsContextKey', ''], ['searchRecentlyUpdatedExtensionsContextKey', ''],
      ['searchExtensionUpdatesContextKey', ''], ['searchOutdatedExtensionsContextKey', ''], ['searchEnabledExtensionsContextKey', ''],
      ['searchDisabledExtensionsContextKey', ''], ['hasInstalledExtensionsContextKey', ''], ['builtInExtensionsContextKey', ''],
      ['searchBuiltInExtensionsContextKey', ''], ['searchWorkspaceUnsupportedExtensionsContextKey', ''],
      ['searchDeprecatedExtensionsContextKey', ''], ['searchRestartRequiredExtensionsContextKey', ''],
      ['recommendedExtensionsContextKey', ''], ['searchDelayer', ''], ['root', ''], ['header', ''], ['searchBox', ''],
      ['notificationContainer', ''], ['searchViewletState', "\n  private queryValue = '';"], ['extensionGalleryManifest', ''], ['notificationDisposables', ''],
      ['_dimension', ''],
      ['constructor', `\n  constructor(
        @IInstantiationService instantiationService: IInstantiationService,
        @IConfigurationService configurationService: IConfigurationService,
        @IWorkbenchLayoutService layoutService: IWorkbenchLayoutService,
        @IContextMenuService contextMenuService: IContextMenuService,
        @ITelemetryService telemetryService: ITelemetryService,
        @IExtensionService extensionService: IExtensionService,
        @IThemeService themeService: IThemeService,
        @IStorageService storageService: IStorageService,
        @IWorkspaceContextService contextService: IWorkspaceContextService,
        @IViewDescriptorService viewDescriptorService: IViewDescriptorService,
        @ILogService logService: ILogService,
        @IContextKeyService private readonly contextKeyService: IContextKeyService,
        @INotificationService private readonly notificationService: INotificationService,
      ) {
        super(VIEWLET_ID, { mergeViewWithContainerWhenSingleView: true }, instantiationService, configurationService, layoutService,
          contextMenuService, telemetryService, extensionService, themeService, storageService, contextService, viewDescriptorService, logService);
        this.searchMcpServersContextKey = SearchMcpServersContext.bindTo(contextKeyService);
        this.searchAgentPluginsContextKey = SearchAgentPluginsContext.bindTo(contextKeyService);
        this.defaultViewsContextKey = DefaultViewsContext.bindTo(contextKeyService);
        this.searchHasTextContextKey = SearchHasTextContext.bindTo(contextKeyService);
        this.extensionsSearchValueContextKey = ExtensionsSearchValueContext.bindTo(contextKeyService);
      }`],
      ['create', ''], ['focus', ''], ['layout', ''],
      ['searchValue', '\n  get searchValue(): string { return this.queryValue; }'],
      ['normalizedQuery', ''],
      ['search', `\n  search(value: string): void {
        this.queryValue = value;
        this.contextKeyService.bufferChangeEvents(() => {
          this.searchMcpServersContextKey.set(/(?:^|\\s)@mcp(?:\\s|$)/i.test(value));
          this.searchAgentPluginsContextKey.set(/(?:^|\\s)@agentPlugins(?:\\s|$)/i.test(value));
          this.defaultViewsContextKey.set(!value);
          this.searchHasTextContextKey.set(!!value.trim());
          this.extensionsSearchValueContextKey.set(value);
        });
        (this.getView('boxteam.views.extensionPackages') as ServicePluginPackagesView | undefined)?.search(value);
        void this.showExtensionsViews(this.panes).catch(error => this.onError(error));
      }`],
      ['refresh', `\n  async refresh(): Promise<void> {
        await (this.getView('boxteam.views.extensionPackages') as ServicePluginPackagesView | undefined)?.refresh();
        await this.showExtensionsViews(this.panes);
      }`],
      ['onDidAddViewDescriptors', `\n  protected override onDidAddViewDescriptors(added: IAddedViewDescriptorRef[]): ViewPane[] {
        const views = super.onDidAddViewDescriptors(added);
        void this.showExtensionsViews(views).catch(error => this.onError(error));
        return views;
      }`],
      ['showExtensionsViews', `\n  private async showExtensionsViews(views: ViewPane[]): Promise<void> {
        await Promise.all(views.map(async view => {
          if (view instanceof AbstractExtensionsListView) await view.show(this.queryValue);
        }));
      }`],
      ['onError', '\n  private onError(error: Error): void { if (!isCancellationError(error)) this.notificationService.error(error); }'],
    ]);
    for (const name of ['renderNotificaiton', 'updateInstalledExtensionsContexts', 'triggerSearch', 'saveState', 'doSearch',
      'alertSearchResult', 'getFirstExpandedPane', 'focusListView', 'onViewletOpen', 'progress', 'isSupportedDragElement']) {
      containerChanges.set(name, '');
    }
    const viewlet = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true).statements
      .find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtensionsViewPaneContainer');
    for (const member of viewlet.members) {
      const name = ts.isConstructorDeclaration(member) ? 'constructor' : member.name?.getText(ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true));
      if (name && !containerChanges.has(name)) containerChanges.set(name, '');
    }
    contents = replaceMembers(path, contents, 'ExtensionsViewPaneContainer', containerChanges);
    contents = contents.replace(', show, DragAndDropObserver', ', DragAndDropObserver');
    contents = contents.replace(/\nconst REMOTE_CATEGORY:[^\n]+\n/, '\n');
    contents = contents.replace("import { IInstantiationService, ServicesAccessor } from '../../../../platform/instantiation/common/instantiation.js';", "import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';");
    contents = contents.replace(', WORKSPACE_RECOMMENDATIONS_VIEW_ID', '').replace(', OUTDATED_EXTENSIONS_VIEW_ID', '').replace(', CONTEXT_HAS_GALLERY', '').replace(', CONTEXT_EXTENSIONS_GALLERY_STATUS', '');
    contents = contents.replace("import { InstallLocalExtensionsInRemoteAction, InstallRemoteExtensionsInLocalAction } from './extensionsActions.js';\n", '');
    contents = contents.replace('IWorkbenchExtensionEnablementService, IExtensionManagementServerService, IExtensionManagementServer', 'IWorkbenchExtensionEnablementService, IExtensionManagementServerService');
    contents = contents.replace(/import \{ ExtensionsListView, EnabledExtensionsView, DisabledExtensionsView, RecommendedExtensionsView, WorkspaceRecommendedExtensionsView, ServerInstalledExtensionsView, DefaultRecommendedExtensionsView, UntrustedWorkspaceUnsupportedExtensionsView, UntrustedWorkspacePartiallySupportedExtensionsView, VirtualWorkspaceUnsupportedExtensionsView, VirtualWorkspacePartiallySupportedExtensionsView, DefaultPopularExtensionsView, DeprecatedExtensionsView, SearchMarketplaceExtensionsView, RecentlyUpdatedExtensionsView, OutdatedExtensionsView, StaticQueryExtensionsView, NONE_CATEGORY, AbstractExtensionsListView \} from '\.\/extensionsViews\.js';\n/, "import { ExtensionsListView, AbstractExtensionsListView } from './extensionsViews.js';\n");
    contents = contents.replace("import { ILabelService } from '../../../../platform/label/common/label.js';\n", '');
    contents = contents.replace("import { registerAction2, Action2, MenuId } from '../../../../platform/actions/common/actions.js';\n", '');
    contents = contents.replace("import { VirtualWorkspaceContext, WorkbenchStateContext } from '../../../common/contextkeys.js';\n", '');
    contents = contents.replace("import { EXTENSION_CATEGORIES } from '../../../../platform/extensions/common/extensions.js';\n", '');
    contents = contents.replace("import { ILocalizedString } from '../../../../platform/action/common/action.js';\n", '');
    contents = contents.replace('IContextKeyService, ContextKeyExpr, RawContextKey, IContextKey', 'IContextKeyService, RawContextKey, IContextKey');
    contents = contents.replace("import { installLocalInRemoteIcon } from './extensionsIcons.js';\n", '');
    contents = pruneUnusedImportsAndConstants(path, contents);
    return contents;
  }
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

/** 欢迎步骤沿原数据 owner 改写，不展示已退役市场或尚未开放的语言扩展能力。 */
function pluginGettingStarted(path, source) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const visit = node => {
    if (ts.isObjectLiteralExpression(node)) {
      const id = node.properties.find(property => ts.isPropertyAssignment(property) && property.name.getText(parsed) === 'id');
      const value = id && ts.isStringLiteral(id.initializer) ? id.initializer.text : undefined;
      if (value === 'extensionsWebWeb' || value === 'extensions') {
        edits.push({ start: node.getStart(parsed), end: node.end, text: `{
          id: '${value}',
          title: localize('gettingStarted.pluginPackages.title', '管理插件包'),
          description: localize('gettingStarted.pluginPackages.description', '安装本地插件包，并分别管理当前 Gateway、当前工作区及以后新建工作区的插件。\\n{0}',
            Button(localize('managePluginPackages', '管理插件包'), 'command:workbench.extensions.action.installedExtensions')),
          ${node.properties.find(property => ts.isPropertyAssignment(property) && property.name.getText(parsed) === 'when').getText(parsed)},
          media: { type: 'markdown', path: 'empty' },
        }` });
      } else if (value === 'findLanguageExtensionsWeb') {
        edits.push({ start: node.getStart(parsed), end: node.end + 1, text: '' });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  if (edits.length !== 3) throw new Error('欢迎页插件步骤与固定上游不匹配');
  for (const edit of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
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

/** 保留原容器与被内置服务消费的单例，删除旧用户扩展 UI 的整套注册。 */
function pluginManagementContribution(root, path, source) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const kept = parsed.statements.filter(statement => {
    if (ts.isImportDeclaration(statement)) return true;
    if (ts.isVariableStatement(statement)) return statement.declarationList.declarations.some(item => item.name.getText(parsed) === 'VIEW_CONTAINER');
    if (!ts.isExpressionStatement(statement) || !ts.isCallExpression(statement.expression)) return false;
    const call = statement.expression;
    return call.expression.getText(parsed) === 'registerSingleton'
      || call.expression.getText(parsed) === 'Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider';
  });
  const contents = kept.map(statement => statement.getText(parsed)).join('\n') + `
Registry.as<IWorkbenchContributionsRegistry>(WorkbenchExtensions.Workbench)
  .registerWorkbenchContribution(ExtensionsViewletViewsContribution, LifecyclePhase.Restored);

CommandsRegistry.registerCommand('workbench.extensions.search', (accessor: ServicesAccessor, query: string = '') =>
  accessor.get(IExtensionsWorkbenchService).openSearch(query));

for (const [id, title] of [
  ['workbench.extensions.action.focusExtensionsView', '聚焦插件包视图'],
  ['workbench.extensions.action.installedExtensions', '显示已安装插件包'],
  ['workbench.extensions.action.clearExtensionsSearchResults', '清空插件包搜索'],
] as const) registerAction2(class extends Action2 {
  constructor() { super({ id, title: { value: title, original: title }, f1: true }); }
  run(accessor: ServicesAccessor) { return accessor.get(IExtensionsWorkbenchService).openSearch(''); }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'workbench.extensions.action.refreshExtension', title: localize2('refreshPluginPackages', '刷新插件包'), f1: true,
    icon: refreshIcon, menu: { id: MenuId.ViewContainerTitle,
      when: ContextKeyExpr.equals('viewContainer', VIEWLET_ID), group: 'navigation', order: 2 } }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const container = accessor.get(IViewsService).getActiveViewPaneContainerWithId(VIEWLET_ID) as IExtensionsViewPaneContainer | undefined;
    await container?.refresh();
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.plugins.installPackage', title: localize2('installPluginPackage', '安装插件包'), f1: true }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const viewsService = accessor.get(IViewsService);
    await installServicePluginPackage(accessor);
    const container = viewsService.getActiveViewPaneContainerWithId(VIEWLET_ID) as IExtensionsViewPaneContainer | undefined;
    await container?.refresh();
  }
});
`;
  return pruneUnusedImportsAndConstants(path,
    `import { installServicePluginPackage } from ${JSON.stringify(join(root, 'src/extensions/servicePluginManagement.js'))};\n` + contents);
}

function pruneUnusedImportsAndConstants(path, source) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const references = new Map();
  const visit = (node, inImport = false) => {
    if (ts.isImportDeclaration(node)) return;
    if (ts.isIdentifier(node)) references.set(node.text, (references.get(node.text) ?? 0) + 1);
    ts.forEachChild(node, child => visit(child, inImport));
  };
  visit(parsed);
  const edits = [];
  for (const statement of parsed.statements) {
    if (ts.isImportDeclaration(statement)) {
      const clause = statement.importClause;
      if (!clause) continue;
      const defaultName = clause.name && (references.get(clause.name.text) ?? 0) > 0 ? clause.name.text : undefined;
      const bindings = clause.namedBindings;
      let bindingText;
      if (bindings && ts.isNamespaceImport(bindings)) {
        bindingText = (references.get(bindings.name.text) ?? 0) > 0 ? bindings.getText(parsed) : undefined;
      } else if (bindings && ts.isNamedImports(bindings)) {
        const names = bindings.elements.filter(item => (references.get(item.name.text) ?? 0) > 0);
        if (names.length) bindingText = `{ ${names.map(item => item.getText(parsed)).join(', ')} }`;
      }
      if (!defaultName && !bindingText) {
        edits.push({ start: statement.getStart(parsed), end: statement.end, text: '' });
        continue;
      }
      const imported = [defaultName, bindingText].filter(Boolean).join(', ');
      const clauseType = clause.isTypeOnly ? 'type ' : '';
      edits.push({ start: statement.getStart(parsed), end: statement.end,
        text: `import ${clauseType}${imported} from ${statement.moduleSpecifier.getText(parsed)};` });
      continue;
    }
    if (ts.isVariableStatement(statement) && !statement.modifiers?.some(item => item.kind === ts.SyntaxKind.ExportKeyword)
        && statement.declarationList.declarations.every(item => ts.isIdentifier(item.name)
          && (references.get(item.name.text) ?? 0) === 1)) {
      edits.push({ start: statement.getStart(parsed), end: statement.end, text: '' });
    }
  }
  for (const edit of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  return source;
}
