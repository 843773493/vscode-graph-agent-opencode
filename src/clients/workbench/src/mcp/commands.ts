import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { t } from '../../../shared/core/i18n/index.js';
import { IQuickInputService, type IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IInstantiationService, type ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IMcpService } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpService } from './service.js';
import { WorkspaceMcpServer } from './server.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { managementGateway, managementTargetDisplayName } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { addMcpConfiguration } from './addConfiguration.js';
import { mcpConfigurationErrorMessage } from './configuration.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { McpWorkspaceRequestTarget } from './catalog.js';

export const openWorkspaceMcpServerCommand = 'boxteam.mcp.openServer';

/** 原详情和菜单共用同一精确对象；配置及插件参数继续交各自原 owner。 */
export async function configureWorkspaceMcpServer(accessor: ServicesAccessor, server: WorkspaceMcpServer, pluginParameters = false): Promise<void> {
  const service = accessor.get(IMcpService), commands = accessor.get(ICommandService);
  if (!(service instanceof WorkspaceMcpService) || !service.servers.get().includes(server) || !server.data) {
    throw new Error(t('workbench.mcp.serverRemoved'));
  }
  if (server.catalog.snapshot.error) throw new Error(server.catalog.snapshot.error);
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
    { workspace_id: target.workspace_id });
  }
}

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
    const selected = managementGateway('mcp');
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
      await addMcpConfiguration(selected, managementTargetDisplayName(selected), quickInput, notifications, assertAccess, controller.signal, configUri);
    } finally { releaseUser(); releaseContext(); controller.abort(); }
  } catch (cause) { notifications.error(mcpConfigurationErrorMessage(cause)); }
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
  if (server.data?.service) items.push({ label: t('workbench.mcp.pluginParameters'), run: () => configure(true) });
  const pick = await accessor.get(IQuickInputService).pick(items, { placeHolder: `${server.label} · ${server.catalog.label} · ${server.statusLabel}` });
  await pick?.run();
}
