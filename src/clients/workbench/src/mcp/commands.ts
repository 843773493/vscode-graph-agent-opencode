import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { t } from '../../../shared/core/i18n/index.js';
import { IQuickInputService, type IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IInstantiationService, type ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IMcpService } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpService } from './service.js';
import { WorkspaceMcpServer } from './server.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { managementGateway, managementWorkspace, managementTargetDisplayName, selectManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { addMcpConfiguration } from './addConfiguration.js';
import { errorMessage, ReportedError } from '../../../web/src/utils/errorMessage.js';
import { Action2, MenuId, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { PluginEditorInput } from '../extensions/pluginEditor.js';
import { IExtensionsWorkbenchService } from '../../upstream/src/vs/workbench/contrib/extensions/common/extensions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { McpWorkspaceRequestTarget } from './catalog.js';

export const openWorkspaceMcpServerCommand = 'boxteam.mcp.openServer';

/** 原详情和菜单共用同一精确对象；配置及插件参数继续交各自原 owner。 */
export async function configureWorkspaceMcpServer(accessor: ServicesAccessor, server: WorkspaceMcpServer, pluginParameters = false): Promise<void> {
  const service = accessor.get(IMcpService), commands = accessor.get(ICommandService);
  if (!(service instanceof WorkspaceMcpService) || !service.servers.get().includes(server) || !server.data) {
    throw new Error(t('workbench.mcp.serverRemoved'));
  }
  const target = { ...server.catalog.target };
  if (pluginParameters) {
    const plugin = server.data.service;
    if (!plugin) throw new Error(t('workbench.mcp.pluginParametersUnavailable'));
    await commands.executeCommand('boxteam.settings.pluginParameters', { gateway_id: plugin.gateway_id,
      gateway_connection_id: target.gateway_connection_id, workspace_id: plugin.workspace_id ?? undefined,
      package_id: plugin.package_id, package_version: plugin.package_version, plugin_id: plugin.plugin_id });
  } else {
    await commands.executeCommand('boxteam.settings.openTarget', { gateway_id: target.gateway_id,
      gateway_connection_id: target.gateway_connection_id, name: target.gateway_id,
      connection_kind: target.gateway_id === workspaceContext.getSnapshot().gatewayId ? 'local' : 'remote_gateway' },
    { workspace_id: target.workspace_id, source_layer: 'workspace' }, { settingsCategory: 'tools' });
  }
}

/** 跳转原插件详情，插件生命周期继续由原配置及 Gateway owner 管理。 */
export async function manageWorkspaceMcpPlugin(accessor: ServicesAccessor, server: WorkspaceMcpServer): Promise<void> {
  const plugin = server.data?.service;
  if (!plugin) throw new Error(t('workbench.mcp.pluginParametersUnavailable'));
  const target = { gateway_id: plugin.gateway_id, gateway_connection_id: server.catalog.target.gateway_connection_id };
  const workspace = plugin.workspace_id ? { ...target, workspace_id: plugin.workspace_id, backend_id: server.catalog.target.backend_id } : undefined;
  await accessor.get(IEditorService).openEditor(new PluginEditorInput(target, workspace, workspace ? 'workspace' : 'gateway',
    plugin.package_id, plugin.package_version, plugin.plugin_id, plugin.plugin_id), { pinned: true });
}

const mcpPanel = ContextKeyExpr.equals('view', 'boxteam.mcp.view');
for (const kind of ['add', 'refresh'] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: `boxteam.mcp.${kind}`, title: { value: t(kind === 'add' ? 'workbench.mcp.addConnection' : 'workbench.mcp.refreshStatus'),
    original: kind === 'add' ? 'Add MCP Connection' : 'Refresh MCP Connections' }, icon: kind === 'add' ? Codicon.add : Codicon.refresh,
    menu: { id: MenuId.ViewTitle, when: mcpPanel, group: 'navigation', order: kind === 'add' ? 1 : 2 } }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    if (kind === 'add') { await addWorkspaceMcpConfiguration(accessor); return; }
    const service = accessor.get(IMcpService);
    if (!(service instanceof WorkspaceMcpService)) throw new Error(t('workbench.mcp.commandOwnerMissing'));
    await service.queryLocal({ refresh: true });
  }
});
for (const kind of ['configure', 'plugins'] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: `boxteam.mcp.${kind}`, title: { value: t(kind === 'configure' ? 'workbench.mcp.configureConnections' : 'workbench.mcp.managePlugins'),
    original: kind === 'configure' ? 'Configure Workspace MCP Connections' : 'Manage Service Plugins' },
    icon: kind === 'configure' ? Codicon.gear : Codicon.extensions,
    menu: { id: MenuId.ViewTitle, when: mcpPanel, group: '2_management', order: kind === 'configure' ? 1 : 2 } }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    if (kind === 'plugins') {
      selectManagementTarget('extensions', managementGateway('panel'));
      await accessor.get(IExtensionsWorkbenchService).openSearch(''); return;
    }
    const target = managementGateway('panel'), workspace = managementWorkspace('panel', target);
    await accessor.get(ICommandService).executeCommand('boxteam.settings.openTarget', target,
      { workspace_id: workspace.workspace_id, source_layer: 'workspace' }, { settingsCategory: 'tools' });
  }
});

registerAction2(class extends Action2 {
  constructor() { super({ id: openWorkspaceMcpServerCommand, title: { value: t('workbench.mcp.openServer'), original: actionTitles['workbench.mcp.openServer'] }, f1: false }); }
  async run(accessor: ServicesAccessor, supplied: { target: McpWorkspaceRequestTarget; serverId: string; showOptions?: boolean }): Promise<void> {
    const service = accessor.get(IMcpService), commands = accessor.get(ICommandService);
    if (!(service instanceof WorkspaceMcpService)) throw new Error(t('workbench.mcp.commandOwnerMissing'));
    const target = { ...supplied.target }, serverId = supplied.serverId, showOptions = supplied.showOptions;
    const servers = await service.queryWorkspace(target);
    const server = servers.find(item => item.name === serverId);
    if (!server) throw new Error(t('workbench.mcp.workspaceServerRemoved', serverId));
    if (showOptions) await commands.executeCommand('workbench.mcp.serverOptions', server.id);
    else await service.open(server);
  }
});

/** 原 Add 的全部调用方共用此 seam，错误进入原通知通道，目标和身份贯穿所有输入及提交。 */
export async function addWorkspaceMcpConfiguration(accessor: ServicesAccessor, configUri?: URI | string): Promise<void> {
  const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
  try {
    if (workspaceContext.getSnapshot().loading) await workspaceContext.refresh();
    const initial = workspaceContext.getSnapshot();
    if (initial.error) throw new Error(initial.error);
    if (!initial.gatewayId) throw new Error(t('workbench.mcp.gatewayNotInitialized'));
    const selected = managementGateway('panel');
    const workspace = managementWorkspace('panel', selected);
    const controller = new AbortController();
    const invalidate = () => controller.abort(new Error(t('workbench.mcp.configurationAccessChanged')));
    const releaseUser = onGatewayTokenInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
    });
    const assertAccess = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
    };
    try {
      await addMcpConfiguration(selected, managementTargetDisplayName(selected), quickInput, notifications, assertAccess, controller.signal, configUri, workspace.workspace_id);
    } finally { releaseUser(); releaseContext(); controller.abort(); }
  } catch (cause) {
    notifications.error(cause instanceof Error ? cause : errorMessage(cause));
    throw new ReportedError(errorMessage(cause), cause);
  }
}

/** 原命令只操作明确选中对象；通配启动也冻结本次所选工作区的对象集合。 */
export async function runWorkspaceMcpCommand(accessor: ServicesAccessor, id: unknown, action: 'start' | 'stop' | 'restart' | 'output', options?: { waitForLiveTools?: boolean }): Promise<void> {
  const service = accessor.get(IMcpService);
  if (!(service instanceof WorkspaceMcpService)) throw new Error(t('workbench.mcp.commandOwnerMissing'));
  if (typeof id !== 'string') throw new Error(t('workbench.mcp.exactServerRequired'));
  if (options?.waitForLiveTools) throw new Error(t('workbench.mcp.useAgentTools'));
  const servers = id === '*' && action === 'start' ? [...service.local] : service.servers.get().filter(server => server.id === id);
  if (!servers.length) throw new Error(t('workbench.mcp.serverRemoved'));
  for (const server of servers) {
    if (action === 'start') await server.start();
    else if (action === 'stop') await server.stop();
    else if (action === 'output') await server.showOutput();
    else { await server.stop(); await server.start(); }
  }
}

/** 原 Server Options 命令只对选中对象携带的目标操作，不再进入浏览器的 OAuth／enablement owner。 */
export async function showWorkspaceMcpOptions(accessor: ServicesAccessor, id: string): Promise<void> {
  const service = accessor.get(IMcpService), instantiation = accessor.get(IInstantiationService);
  if (!(service instanceof WorkspaceMcpService)) throw new Error(t('workbench.mcp.commandOwnerMissing'));
  const server = service.servers.get().find(server => server.id === id);
  if (!(server instanceof WorkspaceMcpServer)) throw new Error(t('workbench.mcp.serverRemoved'));
  const items: (IQuickPickItem & { run: () => Promise<void> })[] = [];
  if (server.canConnect) items.push({ label: t('workbench.mcp.startServer'), run: async () => { await server.start(); } });
  if (server.canAuthorize) items.push({ label: t('workbench.mcp.authorize'), run: () => server.authorize() });
  if (server.canStop) items.push({ label: server.isPending ? t('workbench.mcp.cancelConnection') : t('workbench.mcp.stopServer'), run: () => server.stop() });
  items.push({ label: t('workbench.mcp.showDetails'), run: () => service.open(server) }, { label: t('workbench.mcp.refreshStatus'), run: () => server.catalog.refresh({ refresh: true }) });
  const configure = (pluginParameters = false) => instantiation.invokeFunction(configureWorkspaceMcpServer, server, pluginParameters);
  items.push({ label: t('workbench.mcp.configurationSource'), run: () => configure() });
  if (server.data?.service) items.push({ label: t('workbench.mcp.managePlugin'), run: () => instantiation.invokeFunction(manageWorkspaceMcpPlugin, server) },
    { label: t('workbench.mcp.pluginParameters'), run: () => configure(true) });
  const pick = await accessor.get(IQuickInputService).pick(items, { placeHolder: `${server.label} · ${server.catalog.label} · ${server.statusLabel}` });
  await pick?.run();
}
