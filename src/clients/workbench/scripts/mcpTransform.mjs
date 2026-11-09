import { join } from 'node:path';
import { mcpWorkbenchViewSourcePaths, transformMcpWorkbenchView } from './mcpWorkbenchViewTransform.mjs';

export const mcpSourcePaths = [
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  'src/vs/workbench/contrib/mcp/browser/mcp.contribution.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpCommands.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpServerActions.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpServerEditor.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpWorkbenchService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts',
  ...mcpWorkbenchViewSourcePaths,
];

function replaceSection(source, from, to, replacement, replaceOnce) {
  const begin = source.indexOf(from), end = source.indexOf(to, begin + from.length);
  if (begin < 0 || end < 0) throw new Error(`上游 MCP 转换边界缺失：${from} / ${to}`);
  return replaceOnce(source, source.slice(begin, end), replacement);
}

/** 固定上游保留原 UI／命令；从注册点移除其项目 MCP 的第二 client／配置发现链。 */
export function transformMcp(root, path, source, replaceOnce) {
  const relative = mcpSourcePaths.find(candidate => path === join(root, 'upstream', candidate));
  if (!relative) return source;
  const serverImport = `import { WorkspaceMcpServer } from ${JSON.stringify(join(root, 'src/mcp/server.js'))};\n`;
  let contents = source;
  if (relative.endsWith('/api/common/extHost.api.impl.ts')) {
    contents = replaceOnce(contents,
      '\t\t\tregisterMcpServerDefinitionProvider(id, provider) {\n\t\t\t\treturn extHostMcp.registerMcpConfigurationProvider(extension, id, provider);\n\t\t\t},',
      '\t\t\tregisterMcpServerDefinitionProvider(id, _provider) {\n\t\t\t\tthrow new Error(`MCP Provider "${id}" 已停用，请通过目标插件包管理器配置服务`);\n\t\t\t},');
  }
  if (relative.endsWith('/mcp.contribution.ts')) {
    contents = replaceOnce(contents, "import { McpService } from '../common/mcpService.js';", `import { WorkspaceMcpService, WorkspaceMcpWorkbenchService } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};`);
    contents = replaceOnce(contents, "import { MCPContextsInitialisation, McpWorkbenchService } from './mcpWorkbenchService.js';", "import { MCPContextsInitialisation } from './mcpWorkbenchService.js';");
    contents = replaceOnce(contents, 'registerSingleton(IMcpService, McpService, InstantiationType.Delayed);', 'registerSingleton(IMcpService, WorkspaceMcpService, InstantiationType.Delayed);');
    contents = replaceOnce(contents, 'registerSingleton(IMcpWorkbenchService, McpWorkbenchService, InstantiationType.Eager);', 'registerSingleton(IMcpWorkbenchService, WorkspaceMcpWorkbenchService, InstantiationType.Eager);');
    contents = replaceSection(contents, 'mcpDiscoveryRegistry.register(new SyncDescriptor(RemoteNativeMpcDiscovery));', "registerWorkbenchContribution2('mcpContextKeys'", '', replaceOnce);
    for (const line of [
      "registerWorkbenchContribution2('mcpResourceFilesystem', McpResourceFilesystem, WorkbenchPhase.BlockRestore);",
      'registerWorkbenchContribution2(McpLanguageModelToolContribution.ID, McpLanguageModelToolContribution, WorkbenchPhase.AfterRestored);',
      "registerWorkbenchContribution2('mcpGatewayToolBrokerRemote', McpGatewayToolBrokerContribution, WorkbenchPhase.AfterRestored);",
      "registerWorkbenchContribution2('mcpActionRendering', MCPServerActionRendering, WorkbenchPhase.BlockRestore);",
      "registerWorkbenchContribution2('mcpAddContext', McpAddContextContribution, WorkbenchPhase.Eventually);",
      'registerWorkbenchContribution2(McpConfigMigrationContribution.ID, McpConfigMigrationContribution, WorkbenchPhase.Eventually);',
    ]) contents = replaceOnce(contents, line, '');
    for (const module of [
      '../common/discovery/extensionMcpDiscovery.js', '../common/discovery/installedMcpServersDiscovery.js',
      '../common/discovery/mcpDiscovery.js', '../common/discovery/nativeMcpRemoteDiscovery.js',
      '../common/discovery/pluginMcpDiscovery.js', '../common/discovery/workspaceMcpDiscoveryAdapter.js',
      '../common/discovery/workspaceDotMcpDiscovery.js', '../common/mcpLanguageModelToolContribution.js',
      '../common/mcpResourceFilesystem.js', './mcpGatewayToolBrokerContribution.js', './mcpAddContextContribution.js',
      './mcpDiscovery.js', './mcpMigration.js',
    ]) {
      const line = contents.split('\n').find(line => line.startsWith('import ') && line.endsWith(`from '${module}';`));
      if (!line) throw new Error(`上游 MCP import 锚点缺失：${module}`);
      contents = replaceOnce(contents, line + '\n', '');
    }
    contents = replaceOnce(contents, 'McpConfirmationServerOptionsCommand, MCPServerActionRendering, McpServerOptionsCommand', 'McpConfirmationServerOptionsCommand, McpServerOptionsCommand');
  }
  if (relative.endsWith('/mcpCommands.ts')) {
    // 原命令 ID 保留；尚未接入的写行为在命令 seam 拒绝，不能绕到原配置／凭据 owner。
    for (const [name, next] of [
      ['McpAgentHostServerOptionsCommand', 'McpConfirmationServerOptionsCommand'],
      ['McpConfirmationServerOptionsCommand', 'McpServerOptionsCommand'],
      ['InstallFromManifestAction', 'RemoveStoredInput'],
      ['RemoveStoredInput', 'EditStoredInput'], ['EditStoredInput', 'SetOAuthClientSecret'],
      ['SetOAuthClientSecret', 'ShowConfiguration'], ['ShowConfiguration', 'ShowOutput'],
      ['OpenUserMcpResourceCommand', 'OpenRemoteUserMcpResourceCommand'],
      ['OpenRemoteUserMcpResourceCommand', 'OpenWorkspaceFolderMcpResourceCommand'],
      ['OpenWorkspaceFolderMcpResourceCommand', 'OpenWorkspaceMcpResourceCommand'],
      ['OpenWorkspaceMcpResourceCommand', 'McpBrowseResourcesCommand'],
      ['McpConfigureSamplingModels', 'McpStartPromptingServerCommand'],
      ['McpStartPromptingServerCommand', 'McpSkipCurrentAutostartCommand'],
    ]) {
      const begin = contents.indexOf(`export class ${name} extends Action2 {`), end = contents.indexOf(`export class ${next} extends Action2 {`, begin);
      const original = contents.slice(begin, end);
      const run = original.search(/\n\t(?:override )?(?:async )?run\(/);
      if (begin < 0 || end < 0 || run < 0) throw new Error(`上游 MCP 命令 seam 缺失：${name}`);
      contents = replaceOnce(contents, original, original.slice(0, run) + '\n\toverride async run(): Promise<never> { throw unavailableWorkspaceMcpWrite(); }\n}\n\n');
    }
    const addBegin = contents.indexOf('export class AddConfigurationAction extends Action2 {');
    const addEnd = contents.indexOf('export class InstallFromManifestAction extends Action2 {', addBegin);
    const originalAdd = contents.slice(addBegin, addEnd);
    if (addBegin < 0 || addEnd < 0 || !originalAdd.includes('\n\tasync run(')) throw new Error('上游 MCP Add 命令 seam 缺失');
    // 项目 MCP 不依赖原 Chat setup；旧文件菜单尚无精确 writer，不提供换来源的假动作。
    contents = replaceOnce(contents, originalAdd, `export class AddConfigurationAction extends Action2 {
  constructor() { super({ id: McpCommandIds.AddConfiguration, title: localize2('mcp.addConfiguration', 'Add Server...'), category, f1: true,
    metadata: { description: localize2('mcp.addConfiguration.description', 'Adds a plugin service or external HTTP server to the selected workspace configuration') } }); }
  override run(accessor: ServicesAccessor, configUri?: URI | string): Promise<void> { return addWorkspaceMcpConfiguration(accessor, configUri); }
}

`);
    const listBegin = contents.indexOf('export class ListMcpServerCommand extends Action2 {');
    const listRun = contents.indexOf('\n\toverride async run(', listBegin);
    if (listBegin < 0 || listRun < 0) throw new Error('上游 MCP List 注册 seam 缺失');
    const listRegistration = contents.slice(listBegin, listRun);
    contents = replaceOnce(contents, listRegistration, replaceOnce(listRegistration,
      '\t\t\tprecondition: ContextKeyExpr.and(ChatContextKeys.Setup.hidden.negate(), ChatContextKeys.Setup.disabledInWorkspace.negate()),\n', ''));
    contents = replaceSection(contents, '\tprivate async _runWithMode(', '\tprivate async _runLocal(', '', replaceOnce);
    contents = replaceSection(contents, '\tprivate async _runAgentHost(', 'interface IListMcpServerServices', '}\n\n', replaceOnce);
    contents = replaceSection(contents, "type AgentHostMcpServerLifecycleAction = 'start' | 'stop';", 'export class McpAgentHostServerOptionsCommand', '', replaceOnce);
    contents = replaceOnce(contents, '\t\t\tchatWidgetService: accessor.get(IChatWidgetService),\n\t\t\tagentHostCustomizations: accessor.get(IAgentHostCustomizationService),\n', '');
    contents = replaceOnce(contents, '\treadonly chatWidgetService: IChatWidgetService;\n\treadonly agentHostCustomizations: IAgentHostCustomizationService;\n', '');
    contents = replaceOnce(contents, 'return this._runWithMode(services, undefined);', 'return this._runLocal(services);');
    contents = replaceOnce(contents, '\t\tconst store = new DisposableStore();\n\t\tconst pick = quickInput.createQuickPick<ItemType>', '\t\tawait mcpService.activateCollections();\n\t\tconst store = new DisposableStore();\n\t\tif (mcpService instanceof WorkspaceMcpService) store.add(mcpService.observe());\n\t\tconst pick = quickInput.createQuickPick<ItemType>');
    contents = replaceOnce(contents, '\t\tmcpService.activateCollections();\n\n\t\tstore.add(pick);', '\t\tstore.add(pick);');
    contents = replaceOnce(contents, 'mcpService.servers.read(reader).slice().sort(', 'mcpService.servers.read(reader).filter(server => !(mcpService instanceof WorkspaceMcpService) || mcpService.local.includes(server as WorkspaceMcpServer)).sort(');
    contents = replaceOnce(contents, 'if (firstRun && pick.items.length > 3)', 'if (firstRun && pick.items.length > 2)');
    contents = replaceOnce(contents, 'commandService.executeCommand(McpCommandIds.AddConfiguration);', 'await commandService.executeCommand(McpCommandIds.AddConfiguration);');
    contents = replaceOnce(contents, 'commandService.executeCommand(McpCommandIds.ServerOptions, picked.id);', 'await commandService.executeCommand(McpCommandIds.ServerOptions, picked.id);');
    contents = replaceSection(contents, 'export class McpServerOptionsCommand extends Action2 {', 'export class MCPServerActionRendering', `export class McpServerOptionsCommand extends Action2 {
  constructor() { super({ id: McpCommandIds.ServerOptions, title: localize2('mcp.options', 'Server Options'), category, f1: false }); }
  override run(accessor: ServicesAccessor, id: string): Promise<void> { return showWorkspaceMcpOptions(accessor, id); }
}

`, replaceOnce);
    contents = replaceSection(contents, 'export class MCPServerActionRendering', 'export class ResetMcpTrustCommand', '', replaceOnce);
    contents = replaceSection(contents, 'interface IAgentHostMcpServerCommandArg {', 'export class RestartServer', '', replaceOnce);
    contents = replaceSection(contents, 'async function openMcpConfigurationFile(', '/** Options accepted by', '', replaceOnce);
    for (const [name, next, action] of [
      ['ShowOutput', 'RestartServer', 'output'], ['RestartServer', 'StartServer', 'restart'],
      ['StartServer', 'StopServer', 'start'], ['StopServer', 'McpBrowseCommand', 'stop'],
    ]) {
      const begin = contents.indexOf(`export class ${name} extends Action2 {`), end = contents.indexOf(`export class ${next} extends Action2 {`, begin);
      const original = contents.slice(begin, end), run = original.search(/\n\t(?:override )?(?:async )?run\(/);
      if (begin < 0 || end < 0 || run < 0) throw new Error(`上游 MCP 生命周期命令 seam 缺失：${name}`);
      contents = replaceOnce(contents, original, original.slice(0, run) + `\n\toverride run(accessor: ServicesAccessor, serverId: unknown, options?: { waitForLiveTools?: boolean }): Promise<void> { return runWorkspaceMcpCommand(accessor, serverId, '${action}', options); }\n}\n\n`);
    }
    contents = replaceOnce(contents, "\t\tconst extensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);\n\t\tconst viewsService = accessor.get(IViewsService);\n\t\tawait extensionsWorkbenchService.openSearch('');\n\t\tawait viewsService.openView(InstalledMcpServersViewId, true);", "\t\tawait accessor.get(IMcpWorkbenchService).openSearch('');");
    contents = replaceOnce(contents, '\t\tawait accessor.get(ICommandService).executeCommand(\n\t\t\tAICustomizationManagementCommands.OpenMarketplace,\n\t\t\tAICustomizationManagementSection.McpServers,\n\t\t);', "\t\tawait accessor.get(IMcpWorkbenchService).openSearch('');");
    contents = replaceOnce(contents, "McpConnectionState.toString(server.connectionState.read(reader)),", "server instanceof WorkspaceMcpServer ? server.statusLabel : McpConnectionState.toString(server.connectionState.read(reader)),");
    // 下线的写链与第二 client 依赖不进入本运行面；保留原列表和仍开放命令需要的导入。
    const commandImports = new Set([
      'Codicon', 'ThemeIcon', 'URI', 'groupBy', 'DisposableStore', 'autorun', 'ILocalizedString', 'localize', 'localize2',
      'Action2', 'MenuId', 'MenuRegistry', 'ICommandService', 'ContextKeyExpr', 'IInstantiationService',
      'ServicesAccessor', 'ILogService', 'mcpAutoStartConfig', 'McpAutoStartValue', 'INotificationService',
      'IQuickInputService', 'IQuickPickItem', 'IQuickPickSeparator', 'RemoteNameContext',
      'WorkbenchStateContext', 'WorkspaceFolderCountContext',
      'ChatContextKeys', 'ChatModeKind',
      'isContributionDisabled', 'extensionsFilterSubMenu', 'McpCommandIds',
      'McpContextKeys', 'HasInstalledMcpServersContext', 'IMcpServer', 'IMcpService',
      'IMcpWorkbenchService', 'InstalledMcpServersViewId', 'McpConnectionState', 'McpResourceQuickAccess', 'McpResourceQuickPick',
    ]);
    contents = contents.replace(/^import \{ ([^}]+) \} from ([^\n]+);$/gm, (_line, imports, module) => {
      const retained = imports.split(', ').filter(name => commandImports.has(name));
      return retained.length ? `import { ${retained.join(', ')} } from ${module};` : '';
    });
    contents = `import { WorkspaceMcpService, unavailableWorkspaceMcpWrite } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};\nimport { addWorkspaceMcpConfiguration, showWorkspaceMcpOptions, runWorkspaceMcpCommand } from ${JSON.stringify(join(root, 'src/mcp/commands.js'))};\n` + serverImport + contents;
  }
  if (relative.endsWith('/mcpServerActions.ts')) {
    const installBegin = contents.indexOf('export class InstallAction extends McpServerAction {');
    const installEnd = contents.indexOf('export class InstallInWorkspaceAction', installBegin);
    if (installBegin < 0 || installEnd < 0) throw new Error('上游 MCP InstallAction 边界缺失');
    const originalInstall = contents.slice(installBegin, installEnd);
    let install = replaceOnce(originalInstall, '\t\t@IMcpService private readonly mcpService: IMcpService,\n', '');
    install = replaceOnce(install, `\t\tconst installed = await this.mcpWorkbenchService.install(this.mcpServer);

\t\tawait startServerByFilter(this.mcpService, s => {
\t\t\treturn s.definition.id === installed.id;
\t\t});`, '\t\tawait this.mcpWorkbenchService.install(this.mcpServer);');
    contents = replaceOnce(contents, originalInstall, install);
    contents = replaceOnce(contents, "import { IAuthenticationService } from '../../../services/authentication/common/authentication.js';", '');
    contents = replaceOnce(contents, "import { IAccountQuery, IAuthenticationQueryService } from '../../../services/authentication/common/authenticationQuery.js';", '');
    contents = replaceSection(contents, 'export class AuthServerAction extends McpServerAction {', 'export class ShowServerOutputAction', `export class AuthServerAction extends McpServerAction {
  constructor() {
    super('mcp.authorize', localize('mcp.authorize', 'Authorize'), 'extension-action label prominent account', false);
    this.update();
  }
  update(): void {
    const server = this.mcpServer instanceof WorkspaceMcpServer ? this.mcpServer : undefined;
    this.enabled = server instanceof WorkspaceMcpServer && server.canAuthorize;
    this.class = 'extension-action label prominent account' + (this.enabled ? '' : ' hide');
  }
  override async run(): Promise<void> {
    const server = this.mcpServer instanceof WorkspaceMcpServer ? this.mcpServer : undefined;
    if (!(server instanceof WorkspaceMcpServer)) throw new Error('原 MCP 目标已移除');
    await server.authorize();
  }
}

`, replaceOnce);
    // 只迁移已接入 owner 的生命周期动作，其余配置／安装动作保留原 local 收窄。
    for (const [name, next, condition] of [
      ['StartServerAction', 'StopServerAction', '!McpConnectionState.canBeStarted(serverState.state)'],
      ['StopServerAction', 'RestartServerAction', 'McpConnectionState.canBeStarted(serverState.state)'],
    ]) {
      const begin = contents.indexOf(`export class ${name}`), end = contents.indexOf(`export class ${next}`, begin);
      let action = contents.slice(begin, end);
      action = replaceOnce(action, '\t\tif (!this.mcpServer.local) {\n\t\t\treturn;\n\t\t}\n', '');
      action = replaceOnce(action, 'if (!this.mcpServer) {', 'if (!(this.mcpServer instanceof WorkspaceMcpServer)) {');
      action = replaceOnce(action, `if (${condition}) {`, `if (server instanceof WorkspaceMcpServer ? !server.${name === 'StartServerAction' ? 'canConnect' : 'canStop'} : ${condition}) {`);
      if (name === 'StopServerAction') action = replaceOnce(action, "this.label = localize('stop', \"Stop Server\");", "this.label = server instanceof WorkspaceMcpServer && server.isPending ? localize('mcp.cancelOperation', 'Cancel Connection Operation') : localize('stop', 'Stop Server');");
      contents = replaceOnce(contents, contents.slice(begin, end), action);
    }
    contents = serverImport + contents;
  }
  if (relative.endsWith('/mcpServerEditor.ts')) {
    const actionImports = contents.split('\n').find(line => line.startsWith('import { ButtonWithDropDownExtensionAction,') && line.endsWith("from './mcpServerActions.js';"));
    if (!actionImports) throw new Error('上游 MCP 详情 action import 锚点缺失');
    contents = replaceOnce(contents, actionImports, "import { InstallAction, StartServerAction, StopServerAction, AuthServerAction, ButtonWithDropDownExtensionAction, ButtonWithDropdownExtensionActionViewItem, DropDownAction, McpServerStatusAction } from './mcpServerActions.js';");
    contents = replaceSection(contents, '\t\tconst actions = [', '\n\t\tconst actionsAndStatusContainer', `\t\tconst actions = [
      this.instantiationService.createInstance(InstallAction, false),
      this.instantiationService.createInstance(StartServerAction),
      this.instantiationService.createInstance(StopServerAction),
      this.instantiationService.createInstance(AuthServerAction),
    ];
`, replaceOnce);
    contents = replaceOnce(contents, 'if (extension.readmeUrl || extension.gallery?.readme) {', 'if (extension instanceof WorkspaceMcpServer || extension.readmeUrl || extension.gallery?.readme) {');
    contents = replaceOnce(contents, 'private mcpServerManifest: Cache<IGalleryMcpServerConfiguration> | null;', `private mcpServerManifest: Cache<IGalleryMcpServerConfiguration> | null;
private readonly workspaceMcpObservation = this._register(new MutableDisposable());
protected override setEditorVisible(visible: boolean): void {
  super.setEditorVisible(visible);
  const server = this.input instanceof McpServerEditorInput ? this.input.mcpServer : undefined;
  this.workspaceMcpObservation.value = visible && server instanceof WorkspaceMcpServer ? server.observe() : undefined;
}`);
    contents = replaceOnce(contents, '\t\tthis.mcpServerReadme = new Cache(() => mcpServer.getReadme(token));', `\t\tthis.mcpServerReadme = new Cache(() => mcpServer.getReadme(token));
this.workspaceMcpObservation.value = this.isVisible() && mcpServer instanceof WorkspaceMcpServer ? mcpServer.observe() : undefined;`);
    contents = replaceOnce(contents, 'this.renderNavbar(mcpServer, template, preserveFocus);', 'this.renderNavbar(mcpServer, template, preserveFocus, token);');
    contents = replaceOnce(contents, 'private renderNavbar(extension: IWorkbenchMcpServer, template: IExtensionEditorTemplate, preserveFocus: boolean): void {', 'private renderNavbar(extension: IWorkbenchMcpServer, template: IExtensionEditorTemplate, preserveFocus: boolean, token: CancellationToken): void {');
    contents = replaceOnce(contents, '\toverride clearInput(): void {', '\toverride clearInput(): void {\nthis.workspaceMcpObservation.clear();');
    contents = replaceOnce(contents, 'if (e === extension) {', `if (e === extension) {
if (extension instanceof WorkspaceMcpServer) {
  template.description.textContent = extension.description;
  this.mcpServerReadme = new Cache(() => extension.getReadme(token));
  if (template.navbar.currentId === McpServerEditorTab.Readme) this.onNavbarChange(extension, { id: McpServerEditorTab.Readme, focus: false }, template);
}`);
    contents = serverImport + contents;
  }
  if (relative.endsWith('/mcpServersView.ts')) {
    // 原列表的 hidden 规则归 Extensions 容器；项目面板在可移动视图自身接回原样式作用域。
    contents = replaceOnce(contents, '\t\tsuper.renderBody(container);', "\t\tsuper.renderBody(container);\nif (this.id === 'boxteam.mcp.view') this.element.classList.add('extensions-viewlet');");
    contents = replaceOnce(contents, "private currentQuery = '@mcp';", "private currentQuery = '@mcp';\nprivate workspaceMcpQueryFailed = false;");
    contents = replaceOnce(contents, '\t\tthis.currentQuery = query;', '\t\tthis.workspaceMcpQueryFailed = false;\n\t\ttry {\n\t\tthis.currentQuery = query;');
    contents = replaceOnce(contents, '\t\treturn this.input.model;\n\t}\n\n\tprivate renderInput()', '\t\treturn this.input.model;\n\t\t} catch (error) { this.workspaceMcpQueryFailed = true; throw error; }\n\t}\n\n\tprivate renderInput()');
    contents = replaceOnce(contents, '\t\tthis._register(this.list.onContextMenu(e => this.onContextMenu(e), this));', `\t\tthis._register(this.list.onContextMenu(e => this.onContextMenu(e), this));
if (this.id === 'boxteam.mcp.view') void this.show('').catch(error => {
  if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
});`);
    contents = replaceOnce(contents, '"MCP Servers - Installed"', '"MCP Servers - Configured"');
    contents = replaceOnce(contents, '\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {', `\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));
this._register(this.mcpWorkbenchService.onChange(() => {
  if (this.workspaceMcpQueryFailed && this.currentQuery === '' && this.isBodyVisible()
    && this.mcpWorkbenchService instanceof WorkspaceMcpWorkbenchService && this.mcpWorkbenchService.isLocalCatalogAvailable) {
    void this.show('').catch(error => {
      if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
    });
  }
}));
this._register(this.mcpWorkbenchService.onReset(() => {
  if (this.currentQuery === '' && this.isBodyVisible()) void this.show('').catch(error => {
    if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
  });
}));
\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {`);
    contents = replaceOnce(contents, '\t\t\t\tupdateEnablement();', '\t\t\t\tdata.description.textContent = mcpServer.description;\n\t\t\t\tupdateEnablement();');
    contents = replaceOnce(contents, 'if (newMcpServers.every(r => r.id !== oldMcpServer.id) && newMcpServers.some(r => r.name === oldMcpServer.name)) {', 'if (newMcpServers.every(r => r.id !== oldMcpServer.id)) {');
    contents = replaceOnce(contents, 'disposables.add(this.mcpWorkbenchService.onReset(() => onDidChangeModel.fire(new PagedModel([...this.mcpWorkbenchService.local]))));', 'disposables.add(this.mcpWorkbenchService.onReset(() => { servers = [...this.mcpWorkbenchService.local]; onDidChangeModel.fire(new PagedModel(servers)); }));');
    contents = `import { observeWorkspaceMcpView, WorkspaceMcpWorkbenchService } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};\n` + contents;
  }
  if (relative.endsWith('/mcpWorkbenchService.ts')) {
    // Context key 复用唯一 owner 已处理真实错误的初次读取，避免另起查询留下未处理拒绝。
    contents = replaceOnce(contents, 'mcpWorkbenchService.queryLocal().finally(() => {', 'mcpWorkbenchService.whenInitialLocalMcpServersLoaded.then(() => {');
  }
  return transformMcpWorkbenchView(relative, contents, replaceOnce, replaceSection);
}
