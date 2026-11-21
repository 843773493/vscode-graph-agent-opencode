import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { Action2, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { t } from '../../shared/core/i18n/index.js';
import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { onGatewayTokenInvalidated } from '../../web/src/api/http.js';
import type { GatewayManagementTargetDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { listGatewayManagementTargets } from '../../web/src/gatewayApi.js';
import { workspaceContext } from './workspaceContext.js';
import type { GatewayWorkspace } from '../../web/src/types/backend.js';

type ManagementScope = 'extensions' | 'settings' | 'panel';
export type ManagementTarget = GatewayManagementTargetDTO;
const selections = new Map<ManagementScope, { entryGatewayId: string; target: ManagementTarget }>();
const selecting = new Map<ManagementScope, AbortController>();
const workspaceSelections = new Map<string, string>();
export const selectExtensionManagementTargetCommand = 'boxteam.extensions.selectTarget';
export const selectSettingsManagementTargetCommand = 'boxteam.settings.selectTarget';
export const selectPanelManagementTargetCommand = 'boxteam.panel.selectTarget';
const extensionManagementChanged = new Emitter<void>();
export const onDidChangeExtensionManagementTarget = extensionManagementChanged.event;

const panelManagementChanged = new Emitter<void>();
export const onDidChangePanelManagementTarget = panelManagementChanged.event;

/** 管理操作绑定页面 Gateway 和凭据；管理选择改变不会改变操作 owner。 */
export function createManagementTargetLifecycle(message: string, expectedEntryGatewayId?: string) {
  const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!entryGatewayId) throw new Error(t('workbench.managementTarget.gatewayNotReady'));
  if (expectedEntryGatewayId && entryGatewayId !== expectedEntryGatewayId) throw new Error(message);
  const cancellation = new CancellationTokenSource();
  const controller = new AbortController();
  const invalidate = () => { cancellation.cancel(); controller.abort(new Error(message)); };
  const releaseAuth = onGatewayTokenInvalidated(0, invalidate);
  const releaseContext = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== entryGatewayId) invalidate();
  });
  const check = () => {
    if (cancellation.token.isCancellationRequested || workspaceContext.getSnapshot().gatewayId !== entryGatewayId) {
      invalidate();
      throw new Error(message);
    }
  };
  return {
    entryGatewayId,
    token: cancellation.token,
    signal: controller.signal,
    check,
    dispose() { releaseAuth(); releaseContext(); cancellation.dispose(); controller.abort(); },
  };
}

workspaceContext.subscribe(() => {
  let extensionChanged = false, panelChanged = false;
  for (const [scope, selection] of selections) {
    if (selection.entryGatewayId !== workspaceContext.getSnapshot().gatewayId) {
      selections.delete(scope);
      if (scope === 'extensions') extensionChanged = true;
      if (scope === 'panel') panelChanged = true;
    }
  }
  if (extensionChanged) { extensionManagementChanged.fire(); }
  if (panelChanged) panelManagementChanged.fire();
});

/** 各管理视图只保存自己的展示选择；原 Gateway 仍持有路由和安装事实。 */
export function managementGateway(scope: ManagementScope, explicitGatewayId?: string): ManagementTarget {
  const entry = workspaceContext.getSnapshot().gatewayId;
  if (!entry) throw new Error(t('workbench.managementTarget.gatewayNotReady'));
  const selection = selections.get(scope);
  if (selection && selection.entryGatewayId === entry && (!explicitGatewayId || selection.target.gateway_id === explicitGatewayId)) return { ...selection.target };
  const id = explicitGatewayId ?? entry;
  return { gateway_id: id, name: id === entry ? t('workbench.managementTarget.local') : id, connection_kind: id === entry ? 'local' : 'remote_gateway' };
}

/** 只记住管理入口的展示选择，不读取导航 activeId，也不激活工作区。 */
export function managementWorkspace(scope: ManagementScope, target: Pick<ManagementTarget, 'gateway_id' | 'gateway_connection_id'>, workspaceId?: string): GatewayWorkspace {
  const entry = workspaceContext.getSnapshot().gatewayId;
  if (!entry) throw new Error(t('workbench.managementTarget.gatewayNotReady'));
  const key = JSON.stringify([entry, scope, target.gateway_id, target.gateway_connection_id]);
  const items = workspaceContext.getSnapshot().workspaces.filter(item => item.gateway_id === target.gateway_id);
  const id = workspaceId ?? workspaceSelections.get(key);
  const selected = id === undefined ? (scope === 'panel' ? items.find(item => item.system_default) : undefined) ?? items[0] : items.find(item => item.workspace_id === id);
  if (!selected) throw new Error(t('plugins.noWorkspaces'));
  if (workspaceId !== undefined) {
    workspaceSelections.set(key, workspaceId);
    if (scope === 'panel') panelManagementChanged.fire();
  }
  return selected;
}

/** 下拉与命令共用同一展示选择，不激活工作区。 */
export function selectManagementTarget(scope: ManagementScope, target: ManagementTarget, workspaceId?: string): void {
  const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
  if (!entryGatewayId) throw new Error(t('workbench.managementTarget.gatewayNotReady'));
  if (workspaceId !== undefined) {
    workspaceSelections.set(JSON.stringify([entryGatewayId, scope, target.gateway_id, target.gateway_connection_id]), workspaceId);
  }
  selections.set(scope, { entryGatewayId, target: { ...target } });
  if (scope === 'extensions') { extensionManagementChanged.fire(); }
  if (scope === 'panel') panelManagementChanged.fire();
}
export async function listManagementTargets(signal?: AbortSignal): Promise<ManagementTarget[]> {
  const entry = workspaceContext.getSnapshot().gatewayId;
  const directory = await listGatewayManagementTargets(0, signal);
  if (directory.local_gateway_id !== entry || workspaceContext.getSnapshot().gatewayId !== entry) {
    throw new Error(t('workbench.managementTarget.directoryOwnerMismatch'));
  }
  // HTTP 的空路由只在目录入口归一，避免本地目标与冻结输入出现两项身份。
  return directory.items.map(target => ({ ...target, gateway_connection_id: target.gateway_connection_id ?? undefined }));
}

export function managementTargetDisplayName(target: ManagementTarget | undefined): string {
  if (!target || target.connection_kind === 'local') return t('workbench.managementTarget.local');
  return target.name;
}

for (const scope of ['extensions', 'settings', 'panel'] as const) registerAction2(class extends Action2 {
  constructor() {
    super({ id: scope === 'extensions' ? selectExtensionManagementTargetCommand : scope === 'panel' ? selectPanelManagementTargetCommand : selectSettingsManagementTargetCommand,
      title: managementTargetActionTitle(scope), f1: true });
  }
  async run(accessor: ServicesAccessor): Promise<ManagementTarget | undefined> {
    const quickInput = accessor.get(IQuickInputService);
    const current = managementGateway(scope), entry = workspaceContext.getSnapshot().gatewayId!;
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    selecting.get(scope)?.abort(new Error(t('workbench.managementTarget.selectionSuperseded')));
    selecting.set(scope, controller);
    controller.signal.addEventListener('abort', () => cancellation.cancel(), { once: true });
    const invalidate = () => { controller.abort(new Error(t('workbench.managementTarget.accessChanged'))); cancellation.cancel(); };
    const releaseUser = onGatewayTokenInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); });
    try {
      const targets = await listManagementTargets(controller.signal);
      controller.signal.throwIfAborted();
      const picked = await quickInput.pick(targets.map(target => ({
        label: target.connection_kind === 'local' ? t('workbench.managementTarget.local') : target.name,
        description: target.gateway_id, detail: target.connection_error ?? target.gateway_connection_id,
        picked: target.gateway_id === current.gateway_id && target.gateway_connection_id === current.gateway_connection_id, target,
      })), { title: t(managementTargetActionKey(scope)),
        placeHolder: t('workbench.managementTarget.pickHint') }, cancellation.token);
      controller.signal.throwIfAborted();
      if (!picked) return;
      const target = picked.target;
      selectManagementTarget(scope, target);
      return target;
    } finally {
      if (selecting.get(scope) === controller) selecting.delete(scope);
      releaseUser(); releaseContext(); cancellation.dispose(); controller.abort();
    }
  }
});

function managementTargetActionKey(scope: ManagementScope): 'workbench.managementTarget.selectExtensions' | 'workbench.panel.selectTarget' | 'workbench.managementTarget.selectSettings' {
  return scope === 'extensions' ? 'workbench.managementTarget.selectExtensions'
    : scope === 'panel' ? 'workbench.panel.selectTarget' : 'workbench.managementTarget.selectSettings';
}

function managementTargetActionTitle(scope: ManagementScope): { value: string; original: string } {
  const key = managementTargetActionKey(scope);
  return { value: t(key), original: actionTitles[key] };
}
