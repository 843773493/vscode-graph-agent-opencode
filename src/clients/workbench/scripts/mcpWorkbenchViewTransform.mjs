export const mcpWorkbenchViewSourcePaths = [
  'src/vs/workbench/contrib/mcp/browser/mcpServersView.ts',
  'src/vs/workbench/services/progress/browser/progressService.ts',
  'src/vs/base/browser/ui/list/listPaging.ts',
];

/** 保留 MCP 原视图外壳，只读取 Backend owner 的服务列表。 */
export function transformMcpWorkbenchView(path, source, replaceOnce, replaceSection) {
  let contents = source;
  if (path.endsWith('/mcpServersView.ts')) {
    for (const line of [
      "import { MarkdownString } from '../../../../base/common/htmlContent.js';\n",
      "import { CustomizationMarketplaceConfiguration } from '../../../../platform/customizationMarketplace/common/customizationMarketplaceSources.js';\n",
      "import { AICustomizationManagementSection } from '../../chat/common/aiCustomizationWorkspaceService.js';\n",
      "import { CustomizationMarketplaceWelcome } from '../../chat/browser/aiCustomization/customizationMarketplaceWelcome.js';\n",
      '\tshowWelcomeContent?: boolean;\n',
      '\tprivate listContainer: HTMLElement | null = null;\n',
      '\tprivate welcomeContainer: HTMLElement | null = null;\n',
      '\t\tprivate readonly mpcViewOptions: McpServerListViewOptions,\n',
      "\t\t// Create welcome container\n\t\tthis.welcomeContainer = dom.append(container, dom.$('.customizations-marketplace-welcome-container.hide'));\n\t\tthis.createWelcomeContent(this.welcomeContainer);\n\n",
    ]) contents = replaceOnce(contents, line, '');
    contents = replaceSection(contents, 'export interface McpServerListViewOptions {', 'interface IQueryResult {', '', replaceOnce);
    contents = replaceOnce(contents, '\tdisposables: DisposableStore;\n', '\tdisposables: DisposableStore;\n\tmessage?: Message;\n');
    contents = replaceSection(contents, '\tprivate showWelcomeContent(', '\tprivate updateBody(', '', replaceOnce);
    contents = replaceOnce(contents, '\t\tthis.showWelcomeContent(!!this.input.showWelcomeContent);\n\t\tif (!this.input.showWelcomeContent) {\n\t\t\tthis.updateBody();\n\t\t}', '\t\tthis.updateBody(this.input.message);');
    contents = replaceOnce(contents, '\t\tthis.listContainer = dom.append(container, mcpServersList);', '\t\tconst listContainer = dom.append(container, mcpServersList);');
    contents = replaceOnce(contents, '\t\t\tthis.listContainer,', '\t\t\tlistContainer,');

    contents = `import { CancellationToken, CancellationTokenSource } from '../../../../base/common/cancellation.js';\nimport { CancellationError, getErrorMessage, isCancellationError } from '../../../../base/common/errors.js';\n` + contents;
    contents = replaceOnce(contents, 'isDisposable, MutableDisposable }', 'isDisposable, MutableDisposable, toDisposable }');
    contents = replaceOnce(contents, 'DelayedPagedModel, IPagedModel, PagedModel, IterativePagedModel', 'DelayedPagedModel, IPagedModel, PagedModel');
    contents = replaceOnce(contents, "private currentQuery = '@mcp';", "private currentQuery = '@mcp';\nprivate queryCancellation: CancellationTokenSource | undefined;");
    contents = replaceOnce(contents, '@IConfigurationService private readonly marketplaceConfigurationService:', '@IConfigurationService configurationService:');
    contents = replaceOnce(contents, '\t\t@ILogService private readonly logService: ILogService,', '\t\t@INotificationService private readonly notificationService: INotificationService,\n\t\t@ILogService private readonly logService: ILogService,');
    contents = replaceOnce(contents, 'contextMenuService, marketplaceConfigurationService, contextKeyService', 'contextMenuService, configurationService, contextKeyService');
    contents = replaceOnce(contents, '\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {\n\t\t\tif (event.affectsConfiguration(CustomizationMarketplaceConfiguration.MarketplaceEnabled) && this.isBodyVisible()) {\n\t\t\t\tvoid this.show(this.currentQuery);\n\t\t\t}\n\t\t}));\n', '');
    contents = replaceOnce(contents, '\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));', `\t\tthis._register(toDisposable(() => this.queryCancellation?.dispose(true)));
\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));`);
    contents = replaceOnce(contents, '\t\t\t\tmultipleSelectionSupport: false,', `\t\t\t\tonResolveError: error => {
\t\t\t\t\tthis.updateBody({ text: getErrorMessage(error), severity: Severity.Error });
\t\t\t\t\tthis.notificationService.error(error instanceof Error ? error : getErrorMessage(error));
\t\t\t\t},
\t\t\t\tmultipleSelectionSupport: false,`);

    contents = replaceSection(contents, '\tasync show(query: string):', '\n\tprivate renderInput()', `\tasync show(query: string): Promise<IPagedModel<IWorkbenchMcpServer>> {
\t\tthis.queryCancellation?.dispose(true);
\t\tconst cancellation = this.queryCancellation = new CancellationTokenSource();
\t\tthis.currentQuery = query;
\t\tthis.workspaceMcpQueryFailed = false;
\t\tthis.input?.disposables.dispose();
\t\tthis.input = undefined;
\t\ttry {
\t\t\tconst input = await this.query(query, cancellation.token);
\t\t\tif (this.queryCancellation !== cancellation || cancellation.token.isCancellationRequested) {
\t\t\t\tinput.disposables.dispose();
\t\t\t\tthrow new CancellationError();
\t\t\t}
\t\t\tthis.input = input;
\t\t\tthis.renderInput();
\t\t\tif (input.onDidChangeModel) input.disposables.add(input.onDidChangeModel(model => {
\t\t\t\tif (this.input !== input) return;
\t\t\t\tinput.model = model;
\t\t\t\tthis.renderInput();
\t\t\t}));
\t\t\treturn input.model;
\t\t} catch (error) {
\t\t\tif (this.queryCancellation !== cancellation || cancellation.token.isCancellationRequested) throw new CancellationError();
\t\t\tthis.workspaceMcpQueryFailed = true;
\t\t\tif (!isCancellationError(error)) this.updateBody({ text: getErrorMessage(error), severity: Severity.Error });
\t\t\tthrow error;
\t\t}
\t}
`, replaceOnce);
    contents = replaceSection(contents, '\tprivate async query(query: string):', '\n\tprivate mergeChangedMcpServers(', `\tprivate async query(query: string, token: CancellationToken): Promise<IQueryResult> {
\t\tconst disposables = new DisposableStore();
\t\tlet servers = await this.mcpWorkbenchService.queryLocal();
\t\tif (token.isCancellationRequested) throw new CancellationError();
\t\tconst text = query.replace('@mcp', '').trim().toLocaleLowerCase();
\t\tconst filter = (items: IWorkbenchMcpServer[]) => text
\t\t\t? items.filter(server => (server.label + ' ' + server.description).toLocaleLowerCase().includes(text))
\t\t\t: items;
\t\tservers = filter(servers);
\t\tconst onDidChangeModel = disposables.add(new Emitter<IPagedModel<IWorkbenchMcpServer>>());
\t\tdisposables.add(Event.debounce(this.mcpWorkbenchService.onChange, () => undefined)(() => {
\t\t\tconst updated = this.mergeChangedMcpServers(servers, filter([...this.mcpWorkbenchService.local]));
\t\t\tif (updated) {
\t\t\t\tservers = updated;
\t\t\t\tonDidChangeModel.fire(new PagedModel(servers));
\t\t\t}
\t\t}));
\t\tdisposables.add(this.mcpWorkbenchService.onReset(() => {
\t\t\tservers = filter([...this.mcpWorkbenchService.local]);
\t\t\tonDidChangeModel.fire(new PagedModel(servers));
\t\t}));
\t\treturn { model: new PagedModel(servers), onDidChangeModel: onDidChangeModel.event, disposables };
\t}
`, replaceOnce);
    contents = replaceOnce(contents, 'new SyncDescriptor(McpServersListView, [{ showMarketplaceWelcome: true }])', 'new SyncDescriptor(McpServersListView)');
    contents = replaceOnce(contents, 'new SyncDescriptor(McpServersListView, [{}])', 'new SyncDescriptor(McpServersListView)');
  }
  if (path.endsWith('/progressService.ts')) {
    contents = replaceOnce(contents, `\t\tpromise.finally(() => {
\t\t\tclearTimeout(delayHandle);
\t\t\tdispose(activityProgress);
\t\t});`, `\t\tconst clearProgress = () => {
\t\t\tclearTimeout(delayHandle);
\t\t\tdispose(activityProgress);
\t\t};
\t\tvoid promise.then(clearProgress, clearProgress);`);
  }
  if (path.endsWith('/listPaging.ts')) {
    contents = replaceOnce(contents, "import { Event } from '../../../common/event.js';", "import { Event } from '../../../common/event.js';\nimport { isCancellationError, onUnexpectedError } from '../../../common/errors.js';");
    contents = replaceOnce(contents, '\t\tprivate modelProvider: () => IPagedModel<TElement>\n', '\t\tprivate modelProvider: () => IPagedModel<TElement>,\n\t\tprivate readonly onResolveError: (error: unknown) => void,\n');
    contents = replaceOnce(contents, '\t\tpromise.then(entry => this.renderer.renderElement(entry, index, data.data!, details));', `\t\tvoid promise.then(entry => {
\t\t\tif (!cts.token.isCancellationRequested && model === this.modelProvider()) this.renderer.renderElement(entry, index, data.data!, details);
\t\t}, error => {
\t\t\tif (!cts.token.isCancellationRequested && model === this.modelProvider() && !isCancellationError(error)) this.onResolveError(error);
\t\t});`);
    contents = replaceOnce(contents, 'export interface IPagedListOptions<T> {', 'export interface IPagedListOptions<T> {\n\treadonly onResolveError?: (error: unknown) => void;');
    contents = replaceOnce(contents, 'new PagedRenderer<T, ITemplateData<T>>(r, modelProvider)', 'new PagedRenderer<T, ITemplateData<T>>(r, modelProvider, options.onResolveError ?? onUnexpectedError)');
  }
  return contents;
}
