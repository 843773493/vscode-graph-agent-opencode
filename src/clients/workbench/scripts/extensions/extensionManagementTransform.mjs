import { join } from 'node:path';
import ts from 'typescript';
import { legacyExtensionDetailsSourcePaths, transformLegacyExtensionDetails } from './extensionLegacyDetailsTransform.mjs';

export const extensionManagementSourcePaths = [
  ...legacyExtensionDetailsSourcePaths,
  'src/vs/workbench/services/extensionManagement/common/webExtensionManagementService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagementServerService.ts',
  'src/vs/workbench/services/extensionManagement/common/extensionManagement.ts',
  'src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts',
  'src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts',
  'src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts',
  'src/vs/workbench/contrib/welcomeWalkthrough/browser/editor/vs_code_editor_walkthrough.ts',
  'src/vs/workbench/browser/parts/editor/editorStatus.ts',
  'src/vs/workbench/contrib/format/browser/formatActionsNone.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsQuickAccess.ts',
  'src/vs/workbench/contrib/notebook/browser/viewParts/notebookKernelQuickPickStrategy.ts',
  'src/vs/workbench/contrib/notebook/browser/contrib/cellStatusBar/statusBarProviders.ts',
  'src/vs/workbench/contrib/notebook/browser/view/renderers/backLayerWebView.ts',
  'src/vs/workbench/contrib/notebook/browser/outputEditor/notebookOutputEditor.ts',
  'src/vs/workbench/contrib/notebook/browser/view/cellParts/cellOutput.ts',
  'src/vs/workbench/contrib/notebook/browser/diff/diffElementOutputs.ts',
  'src/vs/workbench/contrib/notebook/browser/notebookBrowser.ts',
];

/** 用户安装只交自有包 owner；原浏览器 scanner 只保留系统／额外内置和派生缓存。 */
export function transformExtensionManagement(root, path, source, replaceOnce) {
  const legacyDetailsSource = transformLegacyExtensionDetails(root, path, source, replaceOnce);
  if (legacyDetailsSource !== undefined) return legacyDetailsSource;
  const notebookSource = transformNotebookLegacyConsumers(root, path, source, replaceOnce);
  if (notebookSource !== undefined) return notebookSource;
  if (path === join(root, 'upstream/src/vs/workbench/browser/parts/editor/editorStatus.ts')) {
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
  if (path === join(root, 'upstream/src/vs/workbench/contrib/format/browser/formatActionsNone.ts')) {
    let contents = replaceOnce(source,
      '\t\tconst extensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);\n', '');
    contents = replaceOnce(contents, '\t\tconst dialogService = accessor.get(IDialogService);\n', '');
    contents = replaceOnce(contents,
      "} else {\n\t\t\tconst langName = model.getLanguageId();\n\t\t\tconst message = nls.localize('no.provider', \"There is no formatter for '{0}' files installed.\", langName);\n\t\t\tconst { confirmed } = await dialogService.confirm({\n\t\t\t\tmessage,\n\t\t\t\tprimaryButton: nls.localize({ key: 'install.formatter', comment: ['&& denotes a mnemonic'] }, \"&&Install Formatter...\")\n\t\t\t});\n\t\t\tif (confirmed) {\n\t\t\t\textensionsWorkbenchService.openSearch(`category:formatters ${langName}`);\n\t\t\t}\n\t\t}",
      `} else {
			const langName = model.getLanguageId();
			notificationService.warn(nls.localize('no.provider.available', "No formatter is available for '{0}' files in this workbench.", langName));
		}`);
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === join(root, 'upstream/src/vs/workbench/contrib/extensions/browser/extensionsQuickAccess.ts')) {
    const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
    const install = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'InstallExtensionQuickAccessProvider');
    if (!install) throw new Error('原安装 Quick Access 类缺失');
    const contents = source.slice(0, install.getFullStart()) + source.slice(install.end);
    return pruneUnusedImportsAndConstants(path, replaceOnce(contents,
      'Press Enter to manage your extensions.', '按 Enter 管理插件包。'));
  }
  if (path === join(root, 'upstream/src/vs/workbench/contrib/welcomeGettingStarted/common/gettingStartedContent.ts')) return pluginGettingStarted(path, source);
  if (path === join(root, 'upstream/src/vs/workbench/contrib/welcomeWalkthrough/browser/editor/vs_code_editor_walkthrough.ts')) {
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
  if (path === join(root, 'upstream/src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts')) return pluginManagementContribution(root, path, source);
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
  const builtinServices = new Map([
    ['webExtensionManagementService.ts', 'WebExtensionManagementService'],
    ['extensionManagementService.ts', 'ExtensionManagementService'],
    ['extensionManagementServerService.ts', 'ExtensionManagementServerService'],
  ]);
  for (const [file, name] of builtinServices) {
    if (path === join(root, 'upstream/src/vs/workbench/services/extensionManagement/common', file)) {
      const registration = name === 'ExtensionManagementServerService'
        ? `import { IExtensionManagementServerService } from './extensionManagement.js';\nimport { InstantiationType, registerSingleton } from '../../../../platform/instantiation/common/extensions.js';\nregisterSingleton(IExtensionManagementServerService, ExtensionManagementServerService, InstantiationType.Delayed);\n` : '';
      return `import { ${name} } from ${JSON.stringify(join(root, 'src/extensions/builtinManagement.js'))};\nexport { ${name} };\n${registration}`;
    }
  }
  const userMethods = ['scanExtensionsUnderDevelopment', 'scanExistingExtension', 'scanExtensionManifest', 'addExtension', 'addExtensionFromGallery', 'removeExtension',
    'updateMetadata', 'copyExtensions', 'addWebExtension', 'addToInstalledExtensions', 'scanInstalledExtensions', 'readInstalledExtensions', 'writeInstalledExtensions'];
  if (path === join(root, 'upstream/src/vs/workbench/services/extensionManagement/common/extensionManagement.ts')) {
    const changes = new Map(['scanExtensionsUnderDevelopment', 'scanExistingExtension', 'scanExtensionManifest', 'addExtension', 'addExtensionFromGallery', 'removeExtension', 'updateMetadata', 'copyExtensions'].map(name => [name, '']));
    changes.set('scanUserExtensions', '\n  scanCustomBuiltinExtensions(options?: ScanOptions): Promise<IScannedExtension[]>;');
    return replaceOnce(replaceMembers(path, source, 'IWebExtensionsScannerService', changes), 'IExtension, ExtensionType,', 'IExtension,');
  }
  if (path === join(root, 'upstream/src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts')) {
    const changes = new Map(userMethods.map(name => [name, '']));
    changes.set('scanUserExtensions', '\n  async scanCustomBuiltinExtensions(options?: ScanOptions): Promise<IScannedExtension[]> { return this.readCustomBuiltinExtensions(options); }');
    let contents = replaceMembers(path, source, 'WebExtensionsScannerService', changes);
    for (const line of [
      "import { IUserDataProfilesService } from '../../../../platform/userDataProfile/common/userDataProfile.js';\n",
      "import { IUriIdentityService } from '../../../../platform/uriIdentity/common/uriIdentity.js';\n",
      '\t\t@IUserDataProfilesService private readonly userDataProfilesService: IUserDataProfilesService,\n',
      '\t\t@IUriIdentityService private readonly uriIdentityService: IUriIdentityService,\n',
    ]) contents = replaceOnce(contents, line, '');
    const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
    const oldAction = parsed.statements.filter(node => ts.isIfStatement(node)
      && node.getText(parsed).includes("id: 'workbench.extensions.action.openInstalledWebExtensionsResource'"));
    if (oldAction.length !== 1) throw new Error('旧浏览器安装记录入口与固定上游不匹配');
    contents = contents.slice(0, oldAction[0].getFullStart()) + contents.slice(oldAction[0].end);
    return pruneUnusedImportsAndConstants(path, contents);
  }
  return source;
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

function rewriteNotebookMethod(path, source, ownerName, methodName, createEdits) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === ownerName);
  if (!owner || !ts.isClassDeclaration(owner)) throw new Error('Notebook 转换类缺失：' + ownerName);
  const method = owner.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(parsed) === methodName);
  if (!method || !ts.isMethodDeclaration(method) || !method.body) throw new Error('Notebook 转换方法缺失：' + ownerName + '.' + methodName);
  const ifStatements = [];
  const visit = node => {
    if (ts.isIfStatement(node)) ifStatements.push(node);
    ts.forEachChild(node, visit);
  };
  visit(method.body);
  const edits = createEdits(parsed, ifStatements);
  if (!edits.length) throw new Error('Notebook 转换锚点缺失：' + ownerName + '.' + methodName);
  for (const edit of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
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
    const container = viewsService.getActiveViewPaneContainerWithId(VIEWLET_ID) as IExtensionsViewPaneContainer | undefined;
    let installError: unknown, installFailed = false;
    try { await installServicePluginPackage(accessor); }
    catch (error) { installError = error; installFailed = true; }
    try { await container?.refresh(); }
    catch (refreshError) {
      if (installFailed) throw new AggregateError([installError, refreshError], '插件包安装失败：'
        + (installError instanceof Error ? installError.message : String(installError)) + '；库存刷新失败：'
        + (refreshError instanceof Error ? refreshError.message : String(refreshError)));
      throw refreshError;
    }
    if (installFailed) throw installError;
  }
});
`;
  return pruneUnusedImportsAndConstants(path,
    `import { installServicePluginPackage } from ${JSON.stringify(join(root, 'src/extensions/servicePluginManagement.js'))};\n` + contents);
}

function transformNotebookLegacyConsumers(root, path, source, replaceOnce) {
  const notebook = join(root, 'upstream/src/vs/workbench/contrib/notebook');
  if (path === join(notebook, 'browser/contrib/cellStatusBar/statusBarProviders.ts')) {
    const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
    const matches = [];
    const visit = node => {
      if (ts.isIfStatement(node) && ts.isIdentifier(node.expression) && node.expression.text === 'registeredId'
          && node.elseStatement?.getText(parsed).includes('workbench.extensions.search')) matches.push(node);
      ts.forEachChild(node, visit);
    };
    visit(parsed);
    if (matches.length !== 1) throw new Error('Notebook 未知语言的旧扩展市场入口与固定上游不匹配');
    const statement = matches[0];
    const warning = `{
      statusBarItems.push({
        text: '$(dialog-warning)',
        tooltip: localize('notebook.cell.status.unknownLanguage', "Unknown cell language: {0}", cell.language),
        alignment: CellStatusbarAlignment.Right,
        priority: -Number.MAX_SAFE_INTEGER + 1
      });
    }`;
    return source.slice(0, statement.elseStatement.getStart(parsed)) + warning + source.slice(statement.elseStatement.end);
  }
  if (path === join(notebook, 'browser/view/renderers/backLayerWebView.ts')) {
    return replaceOnce(source, "\t\t\t\t\t\t\t\t'workbench.extensions.search',\n", '');
  }
  if (path === join(notebook, 'browser/viewParts/notebookKernelQuickPickStrategy.ts')) {
    let contents = source;
    contents = replaceOnce(contents,
      "type InstallExtensionPick = IQuickPickItem & { extensionIds: string[] };\nfunction isInstallExtensionPick(item: QuickPickInput<IQuickPickItem>): item is InstallExtensionPick {\n\treturn item.id === 'installSuggested' && 'extensionIds' in item;\n}\ntype SearchMarketplacePick = IQuickPickItem & { id: 'install' };\nfunction isSearchMarketplacePick(item: QuickPickInput<IQuickPickItem>): item is SearchMarketplacePick {\n\treturn item.id === 'install';\n}\n",
      '');
    contents = replaceOnce(contents,
      'type KernelQuickPickItem = (IQuickPickItem & { autoRun?: boolean }) | SearchMarketplacePick | InstallExtensionPick | KernelPick | GroupedKernelsPick | SourcePick | KernelSourceQuickPickItem;',
      'type KernelQuickPickItem = (IQuickPickItem & { autoRun?: boolean }) | KernelPick | GroupedKernelsPick | SourcePick | KernelSourceQuickPickItem;');
    contents = replaceOnce(contents,
      "\t\tconst extensionRecommendataionPromise = quickPickItems.length === 0\n\t\t\t? createCancelablePromise(token => this._showInstallKernelExtensionRecommendation(notebook, quickPick, this._extensionWorkbenchService, token))\n\t\t\t: undefined;\n",
      '');
    contents = replaceOnce(contents, '\t\t\textensionRecommendataionPromise?.cancel();\n', '');
    contents = replaceOnce(contents,
      "\t\tif (sourceActionCommands.length === 0 && matchResult.all.length === 0 && actions.length === 0) {\n\t\t\treturn await this._getKernelRecommendationsQuickPickItems(notebook, this._extensionWorkbenchService) ?? [];\n\t\t}\n",
      '');
    contents = replaceOnce(contents,
      '\t\tquickPick.placeholder = localize(\'selectKernel.placeholder\', "Type to choose a kernel source");',
      '\t\tconst sourcePlaceholder = localize(\'selectKernel.placeholder\', "Type to choose a kernel source");\n\t\tconst emptySourcePlaceholder = localize(\'selectKernel.emptySources\', "No kernels or kernel sources are available");\n\t\tquickPick.placeholder = sourcePlaceholder;');
    contents = replaceOnce(contents,
      '\t\t\tthis._calculdateKernelSources(editor).then(quickPickItems => {\n\t\t\t\tquickPick.items = quickPickItems;\n\t\t\t\tif (quickPick.items.length > 0) {\n\t\t\t\t\tquickPick.busy = false;\n\t\t\t\t}\n\t\t\t});',
      '\t\t\tthis._calculdateKernelSources(editor).then(quickPickItems => {\n\t\t\t\tquickPick.items = quickPickItems;\n\t\t\t\tquickPick.placeholder = quickPickItems.length ? sourcePlaceholder : emptySourcePlaceholder;\n\t\t\t\tquickPick.busy = false;\n\t\t\t});');
    contents = replaceOnce(contents,
      '\t\t\t\tconst quickPickItems = await this._calculdateKernelSources(editor);\n\t\t\t\tquickPick.items = quickPickItems;\n\t\t\t\tquickPick.busy = false;',
      '\t\t\t\tconst quickPickItems = await this._calculdateKernelSources(editor);\n\t\t\t\tquickPick.items = quickPickItems;\n\t\t\t\tquickPick.placeholder = quickPickItems.length ? sourcePlaceholder : emptySourcePlaceholder;\n\t\t\t\tquickPick.busy = false;');
    contents = rewriteNotebookMethod(path, contents, 'KernelPickerMRUStrategy', 'displaySelectAnotherQuickPick', (parsed, ifStatements) => {
      const matches = ifStatements.filter(node => node.expression.getText(parsed) === 'isSourcePick(selectedKernelPickItem)');
      if (matches.length !== 1 || !matches[0].elseStatement) throw new Error('Notebook 旧 kernel 安装分支与固定上游不匹配');
      return [{ start: matches[0].thenStatement.end, end: matches[0].elseStatement.end, text: '' }];
    });
    contents = replaceMembers(path, contents, 'KernelPickerStrategyBase', new Map([
      ['constructor', `
  constructor(
    protected readonly _notebookKernelService: INotebookKernelService,
    protected readonly _quickInputService: IQuickInputService,
    protected readonly _labelService: ILabelService,
    protected readonly _logService: ILogService,
    protected readonly _extensionService: IExtensionService,
    protected readonly _commandService: ICommandService
  ) { }`],
      ['_handleQuickPick', `
  protected async _handleQuickPick(editor: IActiveNotebookEditor, pick: KernelQuickPickItem, _quickPickItems: KernelQuickPickItem[]): Promise<boolean> {
    if (isKernelPick(pick)) {
      this._selecteKernel(editor.textModel, pick.kernel);
      return true;
    }
    if (isSourcePick(pick)) pick.action.runAction();
    return true;
  }`],
      ['_showKernelExtension', ''],
      ['_showInstallKernelExtensionRecommendation', ''],
      ['_getKernelRecommendationsQuickPickItems', ''],
      ['getSuggestedLanguage', ''],
      ['getSuggestedKernelFromLanguage', ''],
    ]));
    contents = replaceMembers(path, contents, 'KernelPickerMRUStrategy', new Map([
      ['constructor', `
  constructor(
    @INotebookKernelService _notebookKernelService: INotebookKernelService,
    @IQuickInputService _quickInputService: IQuickInputService,
    @ILabelService _labelService: ILabelService,
    @ILogService _logService: ILogService,
    @IExtensionService _extensionService: IExtensionService,
    @ICommandService _commandService: ICommandService,
    @INotebookKernelHistoryService private readonly _notebookKernelHistoryService: INotebookKernelHistoryService,
    @IOpenerService private readonly _openerService: IOpenerService
  ) {
    super(_notebookKernelService, _quickInputService, _labelService, _logService, _extensionService, _commandService);
  }`],
    ]));
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === join(notebook, 'browser/outputEditor/notebookOutputEditor.ts')) {
    let contents = replaceOnce(source,
      'return this._renderSearchForMimetype(viewModel, preferredMimeType);',
      'return this._renderMessage(viewModel, `No renderer could be found for mimetype "${preferredMimeType}".`);');
    contents = replaceMembers(path, contents, 'NotebookOutputEditor', new Map([['_renderSearchForMimetype', '']]));
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === join(notebook, 'browser/diff/diffElementOutputs.ts')) {
    let contents = replaceOnce(source,
      'return this._renderSearchForMimetype(viewModel, preferredMimeType);',
      'return this._renderMessage(viewModel, `No renderer could be found for mimetype "${preferredMimeType}".`);');
    contents = replaceMembers(path, contents, 'OutputElement', new Map([['_renderSearchForMimetype', '']]));
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === join(notebook, 'browser/view/cellParts/cellOutput.ts')) {
    let contents = replaceOnce(source,
      '\t\t@IExtensionsWorkbenchService private readonly extensionsWorkbenchService: IExtensionsWorkbenchService,\n', '');
    contents = replaceOnce(contents,
      'return this._renderSearchForMimetype(viewModel, preferredMimeType);',
      'return this._renderMessage(viewModel, `No renderer could be found for mimetype "${preferredMimeType}".`);');
    contents = rewriteNotebookMethod(path, contents, 'CellOutputElement', '_pickActiveMimeTypeRenderer', (parsed, ifStatements) => {
      const conditions = [
        node => node.getText(parsed).includes('JUPYTER_RENDERER_MIMETYPES'),
        node => node.getText(parsed).includes("pick.id === 'installRenderers'"),
      ];
      const edits = [];
      for (const matchesIf of conditions) {
        const matches = ifStatements.filter(matchesIf);
        if (matches.length !== 1) throw new Error('Notebook renderer 旧安装分支与固定上游不匹配');
        edits.push({ start: matches[0].getStart(parsed), end: matches[0].end, text: '' });
      }
      return edits;
    });
    contents = replaceMembers(path, contents, 'CellOutputElement', new Map([
      ['_renderSearchForMimetype', ''],
      ['_showJupyterExtension', ''],
    ]));
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path === join(notebook, 'browser/notebookBrowser.ts')) {
    const contents = replaceOnce(source,
      '// Hardcoding viewType/extension ID for now. TODO these should be replaced once we can\n// look them up in the marketplace dynamically.',
      '// 保留 Notebook 内置 kernel 的固定关联，供编辑器上下文状态使用。');
    const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
    const recommendations = parsed.statements.find(statement => ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some(declaration => ts.isIdentifier(declaration.name) && declaration.name.text === 'KERNEL_RECOMMENDATIONS'));
    const recommendationType = parsed.statements.find(statement => ts.isInterfaceDeclaration(statement)
      && statement.name.text === 'INotebookExtensionRecommendation');
    const recommendationWrites = parsed.statements.filter(statement => ts.isExpressionStatement(statement)
      && statement.getText(parsed).includes('KERNEL_RECOMMENDATIONS.'));
    if (!recommendations || !recommendationType || recommendationWrites.length !== 2) {
      throw new Error('Notebook 旧 kernel 推荐表与固定上游不匹配');
    }
    const start = recommendations.getFullStart();
    const end = recommendationType.end;
    return contents.slice(0, start) + contents.slice(end);
  }
  return undefined;
}

export function pruneUnusedImportsAndConstants(path, source) {
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
