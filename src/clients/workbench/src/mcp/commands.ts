import { IQuickInputService, type IQuickPickItem } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IMcpService } from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { WorkspaceMcpService } from './service.js';
import { WorkspaceMcpServer } from './server.js';

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
