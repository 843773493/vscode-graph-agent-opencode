import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { MutableDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { Action2, MenuId, MenuRegistry, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { VIEWLET_ID } from '../upstream/src/vs/workbench/contrib/extensions/common/extensions.js';
import { onGatewayUserSessionInvalidated } from '../../web/src/api/http.js';
import type { GatewayManagementDirectoryDTO, GatewayManagementTargetDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { workspaceContext } from './workspaceContext.js';

type ManagementScope = 'extensions' | 'settings' | 'mcp';
export type ManagementTarget = GatewayManagementTargetDTO;
const selections = new Map<ManagementScope, { entryGatewayId: string; target: ManagementTarget }>();
const selecting = new Map<ManagementScope, AbortController>();
export const selectExtensionManagementTargetCommand = 'boxteam.extensions.selectTarget';
export const selectSettingsManagementTargetCommand = 'boxteam.settings.selectTarget';
export const selectMcpManagementTargetCommand = 'boxteam.mcp.selectTarget';
const extensionTitle = new MutableDisposable();
const mcpTitle = new MutableDisposable();
const extensionManagementChanged = new Emitter<void>();
export const onDidChangeExtensionManagementTarget = extensionManagementChanged.event;

const mcpManagementChanged = new Emitter<void>();
export const onDidChangeMcpManagementTarget = mcpManagementChanged.event;

function updateExtensionTitle(): void {
  const target = selections.get('extensions')?.target;
  extensionTitle.value = MenuRegistry.appendMenuItem(MenuId.ViewContainerTitle, {
    command: { id: selectExtensionManagementTargetCommand, title: `目标：${target?.name ?? '本地'}`,
      tooltip: target ? `${target.gateway_id}` : '选择扩展管理目标' },
    when: ContextKeyExpr.equals('viewContainer', VIEWLET_ID), group: 'navigation', order: 0,
  });
}
function updateMcpTitle(): void {
  const target = selections.get('mcp')?.target;
  mcpTitle.value = MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
    command: { id: selectMcpManagementTargetCommand, title: `目标：${target?.name ?? '本地'}`,
      tooltip: target ? `${target.gateway_id}` : '选择 MCP 管理目标' },
    when: ContextKeyExpr.equals('view', 'boxteam.mcp.view'), group: 'navigation', order: 0,
  });
}
updateExtensionTitle();
updateMcpTitle();
onGatewayUserSessionInvalidated(0, () => {
  selections.clear(); updateExtensionTitle(); updateMcpTitle(); extensionManagementChanged.fire(); mcpManagementChanged.fire();
});
workspaceContext.subscribe(() => {
  let extensionChanged = false, mcpChanged = false;
  for (const [scope, selection] of selections) {
    if (selection.entryGatewayId !== workspaceContext.getSnapshot().gatewayId) {
      selections.delete(scope);
      if (scope === 'extensions') extensionChanged = true;
      if (scope === 'mcp') mcpChanged = true;
    }
  }
  if (extensionChanged) { updateExtensionTitle(); extensionManagementChanged.fire(); }
  if (mcpChanged) { updateMcpTitle(); mcpManagementChanged.fire(); }
});

/** 各管理视图只保存自己的展示选择；原 Gateway 仍持有路由和安装事实。 */
export function managementGateway(scope: ManagementScope, explicitGatewayId?: string): ManagementTarget {
  const entry = workspaceContext.getSnapshot().gatewayId;
  if (!entry) throw new Error('Gateway 身份尚未完成初始化，无法确定管理目标');
  const selection = selections.get(scope);
  if (selection && selection.entryGatewayId === entry && (!explicitGatewayId || selection.target.gateway_id === explicitGatewayId)) return { ...selection.target };
  const id = explicitGatewayId ?? entry;
  return { gateway_id: id, name: id === entry ? '本地' : id, connection_kind: id === entry ? 'local' : 'remote_gateway' };
}

for (const scope of ['extensions', 'settings', 'mcp'] as const) registerAction2(class extends Action2 {
  constructor() {
    super({ id: scope === 'extensions' ? selectExtensionManagementTargetCommand : scope === 'mcp' ? selectMcpManagementTargetCommand : selectSettingsManagementTargetCommand,
      title: scope === 'extensions' ? { value: '选择扩展管理目标', original: 'Select Extension Management Target' }
        : scope === 'mcp' ? { value: '选择 MCP 管理目标', original: 'Select MCP Management Target' }
          : { value: '选择设置管理目标', original: 'Select Settings Management Target' }, f1: true });
  }
  async run(accessor: ServicesAccessor): Promise<ManagementTarget | undefined> {
    const quickInput = accessor.get(IQuickInputService);
    const current = managementGateway(scope), entry = workspaceContext.getSnapshot().gatewayId!;
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    selecting.get(scope)?.abort(new Error('同一管理视图已重新选择目标'));
    selecting.set(scope, controller);
    controller.signal.addEventListener('abort', () => cancellation.cancel(), { once: true });
    const invalidate = () => { controller.abort(new Error('页面 Gateway 或用户身份已改变，请重新选择管理目标')); cancellation.cancel(); };
    const releaseUser = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); });
    try {
      const directory = await gateway.request<GatewayManagementDirectoryDTO>('/api/gateway/management/gateways', { signal: controller.signal });
      controller.signal.throwIfAborted();
      if (directory.local_gateway_id !== entry) throw new Error('管理目录不属于当前页面 Gateway');
      const picked = await quickInput.pick(directory.items.map(target => ({
        label: target.connection_kind === 'local' ? '本地' : target.name,
        description: target.gateway_id, detail: target.connection_error ?? target.gateway_connection_id,
        picked: target.gateway_id === current.gateway_id && target.gateway_connection_id === current.gateway_connection_id, target,
      })), { title: scope === 'extensions' ? '扩展管理目标' : scope === 'mcp' ? 'MCP 管理目标' : '设置管理目标',
        placeHolder: '本地指当前页面的原生 Gateway；选择不会启动工作区或插件进程' }, cancellation.token);
      controller.signal.throwIfAborted();
      if (!picked) return;
      const target: ManagementTarget = { ...picked.target, gateway_connection_id: picked.target.gateway_connection_id ?? undefined };
      selections.set(scope, { entryGatewayId: entry, target });
      if (scope === 'extensions') { updateExtensionTitle(); extensionManagementChanged.fire(); }
      if (scope === 'mcp') { updateMcpTitle(); mcpManagementChanged.fire(); }
      return target;
    } finally {
      if (selecting.get(scope) === controller) selecting.delete(scope);
      releaseUser(); releaseContext(); cancellation.dispose(); controller.abort();
    }
  }
});
