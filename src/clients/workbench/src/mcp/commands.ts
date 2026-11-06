import { IQuickInputService, type IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IMcpService } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpService } from './service.js';
import { WorkspaceMcpServer } from './server.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { IProgressService } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { manageMcpArtifacts } from './artifacts.js';

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

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.mcp.manageArtifacts', title: { value: '管理目标制品', original: 'Manage Target Artifacts' },
    category: { value: 'MCP', original: 'MCP' }, f1: true }); }
  async run(accessor: ServicesAccessor, explicit?: WorkspaceOwner): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), progress = accessor.get(IProgressService), notifications = accessor.get(INotificationService);
    const initial = workspaceContext.getSnapshot();
    if (initial.loading || initial.error || !initial.gatewayId) throw new Error(initial.error ?? '工作区目标尚未完成初始化');
    const controller = new AbortController();
    const invalidate = () => controller.abort(new Error('Gateway 或用户身份已改变，请重新选择 MCP 制品目标'));
    const release = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
    });
    const assertAccess = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
    };
    try {
      const owner = explicit ? { ...explicit } : (await quickInput.pick(initial.workspaces.map(workspace => ({
        label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
        owner: gatewayWorkspaceOwner(workspace),
      })), { title: '选择 MCP 制品目标', placeHolder: '选择用于访问安装域的已就绪工作区；不会连接、启动或激活目标' }))?.owner;
      if (!owner) return;
      assertAccess();
      const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, owner);
      if (workspace.status !== 'ready') throw new Error('目标工作区尚未就绪，请先显式连接或启动 Backend');
      await manageMcpArtifacts(workspace, workspace.name, quickInput, progress, notifications, assertAccess, controller.signal);
    } finally { release(); releaseContext(); controller.abort(); }
  }
});
