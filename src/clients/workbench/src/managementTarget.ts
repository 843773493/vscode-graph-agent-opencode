import { raceCancellationError } from '../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { DisposableStore, MutableDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { Action2, MenuId, MenuRegistry, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { VIEWLET_ID } from '../upstream/src/vs/workbench/contrib/extensions/common/extensions.js';
import { onGatewayUserSessionInvalidated } from '../../web/src/api/http.js';
import { listMcpInstallationTargets } from '../../web/src/api/mcpConnections.js';
import type { ExtensionTargetListDTO, ExtensionConfigurationTargetListDTO, GatewayManagementDirectoryDTO, GatewayManagementTargetDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from './agentApi.js';
import { workspaceContext } from './workspaceContext.js';

type ManagementScope = 'extensions' | 'settings' | 'mcp';
export type ManagementTarget = GatewayManagementTargetDTO & { backend_id?: string };
const selections = new Map<ManagementScope, { entryGatewayId: string; target: ManagementTarget }>();
const selecting = new Map<ManagementScope, AbortController>();
interface ExtensionDomainRead {
  entryGatewayId: string | null;
  controller: AbortController;
  result: Promise<ExtensionTargetListDTO['targets']>;
  consumers: number;
}
const extensionDomainReads = new Map<string, ExtensionDomainRead>();
export const selectExtensionManagementTargetCommand = 'boxteam.extensions.selectTarget';
export const selectSettingsManagementTargetCommand = 'boxteam.settings.selectTarget';
export const selectMcpManagementTargetCommand = 'boxteam.mcp.selectTarget';
const extensionTitle = new MutableDisposable();
const mcpTitle = new MutableDisposable();
const extensionManagementChanged = new Emitter<void>();
export const onDidChangeExtensionManagementTarget = extensionManagementChanged.event;

/** 详情身份只取稳定安装域；显式连接仍由原对象持有用于后续传输。 */
export function sameExtensionManagementTarget(first: ManagementTarget | undefined, second: ManagementTarget | undefined): boolean {
  return first?.gateway_id === second?.gateway_id && first?.backend_id === second?.backend_id;
}
const mcpManagementChanged = new Emitter<void>();
export const onDidChangeMcpManagementTarget = mcpManagementChanged.event;

/** 仅刷新当前管理视图；在途操作的目标由发起方冻结，不随此展示事件改投。 */
export function refreshExtensionManagementTarget(target?: { gatewayId: string; backendId: string }): void {
  if (target) {
    const current = managementGateway('extensions');
    if (current.gateway_id !== target.gatewayId || current.backend_id !== target.backendId) return;
  }
  extensionManagementChanged.fire();
}

function updateExtensionTitle(): void {
  const target = selections.get('extensions')?.target;
  extensionTitle.value = MenuRegistry.appendMenuItem(MenuId.ViewContainerTitle, {
    command: { id: selectExtensionManagementTargetCommand, title: `目标：${target?.name ?? '本地'}`,
      tooltip: target ? `${target.gateway_id}${target.backend_id ? ` · ${target.backend_id}` : ''}` : '选择扩展管理目标' },
    when: ContextKeyExpr.equals('viewContainer', VIEWLET_ID), group: 'navigation', order: 0,
  });
}
function updateMcpTitle(): void {
  const target = selections.get('mcp')?.target;
  mcpTitle.value = MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
    command: { id: selectMcpManagementTargetCommand, title: `目标：${target?.name ?? '本地'}`,
      tooltip: target ? `${target.gateway_id}${target.backend_id ? ` · ${target.backend_id}` : ''}` : '选择 MCP 管理目标' },
    when: ContextKeyExpr.or(...['boxteam.mcp.view', 'workbench.views.mcp.installed', 'workbench.views.mcp.marketplace']
      .map(view => ContextKeyExpr.equals('view', view))), group: 'navigation', order: 0,
  });
}
updateExtensionTitle();
updateMcpTitle();
onGatewayUserSessionInvalidated(0, () => {
  invalidateExtensionDomainReads();
  selections.clear(); updateExtensionTitle(); updateMcpTitle(); extensionManagementChanged.fire(); mcpManagementChanged.fire();
});
workspaceContext.subscribe(() => {
  invalidateExtensionDomainReads(workspaceContext.getSnapshot().gatewayId);
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

function invalidateExtensionDomainReads(entryGatewayId?: string | null): void {
  for (const [key, read] of extensionDomainReads) {
    if (entryGatewayId !== undefined && read.entryGatewayId === entryGatewayId) continue;
    extensionDomainReads.delete(key);
    read.controller.abort(new Error('页面 Gateway 或用户身份已改变，本次扩展安装域读取已撤销'));
  }
}

async function extensionDomains(target: ManagementTarget, signal: AbortSignal) {
  signal.throwIfAborted();
  const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
  const key = JSON.stringify([entryGatewayId, target.gateway_id, target.gateway_connection_id ?? null]);
  let read = extensionDomainReads.get(key);
  if (!read) {
    const controller = new AbortController();
    read = { entryGatewayId, controller, consumers: 0,
      result: gateway.request<ExtensionTargetListDTO>('/api/gateway/extension-targets', {
        method: 'POST', body: JSON.stringify({ protocol_version: 1, gateway_id: target.gateway_id,
          gateway_connection_id: target.gateway_connection_id }), signal: controller.signal,
      }).then(result => {
        controller.signal.throwIfAborted();
        if (result.protocol_version !== 1 || result.gateway_id !== target.gateway_id || result.source !== 'live'
          || result.targets.some(domain => domain.gateway_id !== target.gateway_id || !domain.backend_id)) throw new Error('扩展安装域发现返回了不同目标');
        if (!result.targets.length) throw new Error('所选 Gateway 没有可用的扩展安装域，请显式启动目标安装服务');
        return result.targets;
      }),
    };
    extensionDomainReads.set(key, read);
    const pending = read;
    const release = () => { if (extensionDomainReads.get(key) === pending) extensionDomainReads.delete(key); };
    void pending.result.then(release, release);
  }
  read.consumers++;
  const lifetime = new DisposableStore();
  try {
    // 各消费者独立取消；身份失效也须立即拒绝不理会 abort 的底层传输。
    const combined = AbortSignal.any([signal, read.controller.signal]);
    const domains = await raceCancellationError(read.result, CancellationToken.fromAbortSignal(combined, lifetime));
    combined.throwIfAborted();
    return domains;
  } finally {
    lifetime.dispose();
    if (--read.consumers === 0) {
      if (extensionDomainReads.get(key) === read) extensionDomainReads.delete(key);
      read.controller.abort();
    }
  }
}

/** 默认单域可直接使用；多域只在标题目标动作中补选，安装命令不逐项弹窗。 */
export async function extensionManagementTarget(target: ManagementTarget, signal: AbortSignal): Promise<ManagementTarget & { backend_id: string }> {
  if (target.backend_id) return { ...target, backend_id: target.backend_id };
  const previous = selections.get('extensions'), entryGatewayId = workspaceContext.getSnapshot().gatewayId!;
  const domains = await extensionDomains(target, signal);
  if (domains.length !== 1) throw new Error('当前 Gateway 有多个安装域，请从扩展视图标题工具栏选择安装域');
  if (!domains[0].capabilities?.installation_manage) throw new Error('所选目标未授权扩展安装管理');
  const resolved = { ...target, backend_id: domains[0].backend_id };
  if (selections.get('extensions') === previous && workspaceContext.getSnapshot().gatewayId === entryGatewayId) {
    selections.set('extensions', { entryGatewayId, target: resolved });
    updateExtensionTitle();
  }
  return resolved;
}

async function mcpDomains(target: ManagementTarget, signal: AbortSignal) {
  const domains = await listMcpInstallationTargets(0, target, signal);
  signal.throwIfAborted();
  if (!domains.length) throw new Error('所选 Gateway 没有可用的 MCP 安装域，请显式启动目标安装服务');
  return domains;
}

/** 安装目标独立于工作区焦点；默认单域直接使用，多域只由标题动作明确选择。 */
export async function mcpManagementTarget(target: ManagementTarget, signal: AbortSignal): Promise<ManagementTarget & { backend_id: string }> {
  if (target.backend_id) return { ...target, backend_id: target.backend_id };
  const previous = selections.get('mcp'), entryGatewayId = workspaceContext.getSnapshot().gatewayId!;
  const domains = await mcpDomains(target, signal);
  if (domains.length !== 1) throw new Error('当前 Gateway 有多个安装域，请从 MCP 视图标题工具栏选择安装域');
  const resolved = { ...target, backend_id: domains[0].backend_id };
  if (selections.get('mcp') === previous && workspaceContext.getSnapshot().gatewayId === entryGatewayId) {
    selections.set('mcp', { entryGatewayId, target: resolved });
    updateMcpTitle();
  }
  return resolved;
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
        placeHolder: '本地指当前页面的原生 Gateway；选择不会启动工作区或扩展宿主' }, cancellation.token);
      controller.signal.throwIfAborted();
      if (!picked) return;
      let target: ManagementTarget = { ...picked.target, gateway_connection_id: picked.target.gateway_connection_id ?? undefined };
      if (scope === 'extensions') {
        const domains = await extensionDomains(target, controller.signal);
        const domain = domains.length === 1 ? domains[0] : (await quickInput.pick(domains.map(domain => ({
          label: domain.backend_id, description: target.name, target: domain,
        })), { title: `选择扩展安装域 · ${target.name}`, placeHolder: '该选择用于后续安装、更新和卸载' }, cancellation.token))?.target;
        controller.signal.throwIfAborted();
        if (!domain) return;
        target = { ...target, backend_id: domain.backend_id };
      } else if (scope === 'mcp') {
        const domains = await mcpDomains(target, controller.signal);
        const domain = domains.length === 1 ? domains[0] : (await quickInput.pick(domains.map(domain => ({
          label: domain.backend_id, description: target.name, target: domain,
        })), { title: `选择 MCP 安装域 · ${target.name}`, placeHolder: '该选择用于后续制品安装、查询和卸载' }, cancellation.token))?.target;
        controller.signal.throwIfAborted();
        if (!domain) return;
        target = { ...target, backend_id: domain.backend_id };
      } else {
        const directory = await gateway.request<ExtensionConfigurationTargetListDTO>('/api/gateway/extension-configurations/targets', {
          method: 'POST', body: JSON.stringify({ protocol_version: 1, gateway_id: target.gateway_id,
            gateway_connection_id: target.gateway_connection_id }), signal: controller.signal,
        });
        controller.signal.throwIfAborted();
        if (directory.protocol_version !== 1 || directory.gateway_id !== target.gateway_id) throw new Error('设置配置目录不属于所选 Gateway');
        if (!directory.backend_ids.length) throw new Error('所选 Gateway 没有可用的配置目标，请显式启动 Backend 后重试');
        const backendId = directory.backend_ids.length === 1 ? directory.backend_ids[0] : (await quickInput.pick(directory.backend_ids.map(id => ({
          label: id, description: target.name,
        })), { title: `选择设置配置域 · ${target.name}`, placeHolder: '后续设置输入固定到该 Backend 的 profile' }, cancellation.token))?.label;
        controller.signal.throwIfAborted();
        if (!backendId) return;
        target = { ...target, backend_id: backendId };
      }
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
