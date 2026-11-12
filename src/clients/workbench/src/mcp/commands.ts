import { IQuickInputService, type IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IMcpService } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpService } from './service.js';
import { WorkspaceMcpServer } from './server.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { managementGateway } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { URI } from '../../upstream/src/vs/base/common/uri.js';
import { addMcpConfiguration } from './addConfiguration.js';
import { mcpConfigurationErrorMessage } from './configuration.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { McpWorkspaceRequestTarget } from './catalog.js';

export const openWorkspaceMcpServerCommand = 'boxteam.mcp.openServer';

registerAction2(class extends Action2 {
  constructor() { super({ id: openWorkspaceMcpServerCommand, title: '打开指定 MCP 服务', f1: false }); }
  async run(accessor: ServicesAccessor, supplied: { target: McpWorkspaceRequestTarget; serverId: string; showOptions?: boolean }): Promise<void> {
    const service = accessor.get(IMcpService), commands = accessor.get(ICommandService);
    if (!(service instanceof WorkspaceMcpService)) throw new Error('MCP 命令未接入项目 owner');
    const target = { ...supplied.target }, serverId = supplied.serverId, showOptions = supplied.showOptions;
    const servers = await service.queryWorkspace(target);
    const server = servers.find(item => item.name === serverId);
    if (!server) throw new Error(`MCP 服务已不在原工作区目录中：${serverId}`);
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
    if (!initial.gatewayId) throw new Error('Gateway 身份尚未完成初始化');
    const selected = managementGateway('mcp');
    const controller = new AbortController();
    const invalidate = () => controller.abort(new Error('Gateway 或访问凭据已改变，请重新添加 MCP 配置'));
    const releaseUser = onGatewayTokenInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
    });
    const assertAccess = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
    };
    try {
      await addMcpConfiguration(selected, selected.name, quickInput, notifications, assertAccess, controller.signal, configUri);
    } finally { releaseUser(); releaseContext(); controller.abort(); }
  } catch (cause) { notifications.error(mcpConfigurationErrorMessage(cause)); }
}

/** 原命令只操作明确选中对象；通配启动也冻结本次所选工作区的对象集合。 */
export async function runWorkspaceMcpCommand(accessor: ServicesAccessor, id: unknown, action: 'start' | 'stop' | 'restart' | 'output', options?: { waitForLiveTools?: boolean }): Promise<void> {
  const service = accessor.get(IMcpService);
  if (!(service instanceof WorkspaceMcpService)) throw new Error('MCP 命令未接入项目 owner');
  if (typeof id !== 'string') throw new Error('MCP 命令必须指定完整 Gateway／工作区的服务 ID');
  if (options?.waitForLiveTools) throw new Error('Workbench 不注册项目 MCP 的可执行工具；请使用目标工作区 Agent 的工具目录');
  const servers = id === '*' && action === 'start' ? [...service.local] : service.servers.get().filter(server => server.id === id);
  if (!servers.length) throw new Error('MCP 服务已不在原目标目录中');
  for (const server of servers) {
    if (action === 'start') await server.start();
    else if (action === 'stop') await server.stop();
    else if (action === 'output') await server.showOutput();
    else { await server.stop(); await server.start(); }
  }
}

/** 原 Server Options 命令只对选中对象携带的目标操作，不再进入浏览器的 OAuth／enablement owner。 */
export async function showWorkspaceMcpOptions(accessor: ServicesAccessor, id: string): Promise<void> {
  const service = accessor.get(IMcpService);
  if (!(service instanceof WorkspaceMcpService)) throw new Error('MCP 命令未接入项目 owner');
  const server = service.servers.get().find(server => server.id === id);
  if (!(server instanceof WorkspaceMcpServer)) throw new Error('MCP 服务已不在原目标目录中');
  const items: (IQuickPickItem & { run: () => Promise<void> })[] = [];
  if (server.canConnect) items.push({ label: 'Start Server', run: async () => { await server.start(); } });
  if (server.canAuthorize) items.push({ label: 'Authorize', run: () => server.authorize() });
  if (server.canStop) items.push({ label: server.isPending ? 'Cancel Connection Operation' : 'Stop Server', run: () => server.stop() });
  items.push({ label: 'Show Details', run: () => service.open(server) }, { label: 'Refresh MCP Status', run: () => server.catalog.refresh({ refresh: true }) });
  const pick = await accessor.get(IQuickInputService).pick(items, { placeHolder: `${server.label} · ${server.catalog.label} · ${server.statusLabel}` });
  await pick?.run();
}
