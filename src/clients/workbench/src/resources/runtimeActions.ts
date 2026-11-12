import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { getWorkbenchContribution } from '../../upstream/src/vs/workbench/common/contributions.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { controlWorkspaceResource, listWorkspaceResources, type ResourceAction, type ResourceThreadRef, type WorkspaceResourceDTO } from '../../../web/src/api/workspaceResources.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { resourceActionLabels, settleWorkspaceResourceOperation, workspaceResourceName, type WorkspaceResourceActionSource } from '../../../web/src/hooks/workspace/resourceActions.js';
import { gateway } from '../agentApi.js';
import { captureSearchOwner } from '../search/target.js';
import { ThreadInput } from '../threadInput.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceActionCommands } from '../search/workspaceActions.js';
import type { WorkspacePanel } from '../workspacePanel.js';
import { WorkspacePanelState } from '../workspacePanelState.js';

export const resourceActionCommand = (action: ResourceAction) => `boxteam.resource.${action}`;
export const resourceFocusCommand = 'boxteam.resource.focus';
export interface RuntimeResourceCommandTarget {
  owner: WorkspaceOwner;
  resource: WorkspaceResourceDTO;
  thread?: ResourceThreadRef;
  source?: WorkspaceResourceActionSource;
}

/** 保留原 owner 的能力和 revision；Thread 关联与资源寿命分别执行，不相互替代。 */
async function runResourceAction(accessor: ServicesAccessor, action: ResourceAction,
  supplied?: RuntimeResourceCommandTarget): Promise<WorkspaceResourceDTO | undefined> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
  const dialogs = accessor.get(IDialogService), notifications = accessor.get(INotificationService);
  const initial = workspaceContext.getSnapshot(), origin = editors.activeEditor;
  const explicit = supplied ? { ...supplied, owner: { ...supplied.owner }, resource: structuredClone(supplied.resource),
    thread: supplied.thread ? { ...supplied.thread } : undefined } : undefined;
  const originThread = origin instanceof ThreadInput ? { owner: { ...origin.owner },
    thread: { session_id: origin.owner.sessionId, thread_id: origin.owner.threadId } } : undefined;
  if (!initial.gatewayId) throw new Error('Gateway 登记身份尚未加载');
  let invalidated = false;
  const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const assertAccess = () => {
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) throw new Error('Gateway 或访问凭据已改变，请重新选择资源');
  };
  let pending: ReturnType<INotificationService['status']> | undefined;
  try {
    let owner = explicit?.owner;
    if (!owner) {
      if (origin || initial.activeId) owner = { ...captureSearchOwner(editors) };
      else owner = (await quick.pick(initial.workspaces.map(item => ({ label: item.name,
        description: `${item.gateway_id} · ${item.workspace_id}`, owner: { gatewayId: item.gateway_id, workspaceId: item.workspace_id } })),
        { title: '选择资源所属工作区', placeHolder: '浏览不会启动或激活工作区' }))?.owner;
    }
    if (!owner) return;
    const workspaceKey = gatewayWorkspaceKey(owner);
    // 已挂载组件持有精确 owner 与 scope 句柄，不为原按钮重复查询控制面。
    if (!explicit?.source) {
      const routes = await gateway.workspaces(false);
      assertAccess();
      const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
      if (target.status !== 'ready') throw new Error(`资源工作区当前不可用（${target.status}），请明确启动或连接`);
    }
    const resource = explicit?.resource ?? (await quick.pick(
      (await Promise.all(['terminal', 'browser'].map(kind => listWorkspaceResources(0, workspaceKey, kind as 'terminal' | 'browser'))))
        .flatMap(list => list.items).filter(item => item.available_actions.includes(action)).map(item => ({
          label: workspaceResourceName(item), description: `${item.kind} · ${item.status}`,
          detail: item.resource_id, resource: item,
        })), { title: resourceActionLabels[action], placeHolder: `${owner.gatewayId} · ${owner.workspaceId}` }))?.resource;
    if (!resource) return;
    assertAccess();
    if (resource.workspace_id !== owner.workspaceId) throw new Error('资源回执不属于明确选择的工作区');
    if (resource.kind !== 'terminal' && resource.kind !== 'browser') throw new Error('此管理入口只处理终端或浏览器资源');
    if (!resource.available_actions.includes(action)) throw new Error(`资源当前不支持${resourceActionLabels[action]}`);
    let thread = action === 'attach' || action === 'detach' ? explicit?.thread : undefined;
    if (action === 'attach' && !thread && originThread?.owner.gatewayId === owner.gatewayId && originThread.owner.workspaceId === owner.workspaceId) thread = originThread.thread;
    if ((action === 'attach' || action === 'detach') && !thread) {
      const candidates = action === 'detach' ? resource.thread_associations : editors.editors.flatMap(input =>
        input instanceof ThreadInput && input.owner.gatewayId === owner.gatewayId && input.owner.workspaceId === owner.workspaceId
          ? [{ session_id: input.owner.sessionId, thread_id: input.owner.threadId }] : []);
      const unique = [...new Map(candidates.map(ref => [`${ref.session_id}/${ref.thread_id}`, ref])).values()];
      if (!unique.length) throw new Error('没有可选择的明确 Thread，请先打开目标 Thread');
      thread = (await quick.pick(unique.map(ref => ({ label: ref.thread_id, description: ref.session_id, thread: ref })),
        { title: action === 'detach' ? '选择要解除的 Thread 关联' : '选择要关联的 Thread' }))?.thread;
      if (!thread) return;
    }
    if (action === 'detach' && !resource.thread_associations.some(ref => ref.session_id === thread!.session_id && ref.thread_id === thread!.thread_id)) {
      throw new Error('所选 Thread 不属于该资源的现有关联');
    }
    if (action === 'attach' && resource.retention_scope === 'thread'
      && (resource.source_thread?.session_id !== thread!.session_id || resource.source_thread.thread_id !== thread!.thread_id)) {
      throw new Error('临时资源只能关联来源 Thread；跨 Thread 使用前请明确保留到工作区');
    }
    if (action === 'delete' && !explicit?.source) {
      const confirmed = await dialogs.confirm({ type: 'warning', message: `删除${resource.kind === 'terminal' ? '终端' : '浏览器'}？`,
        detail: workspaceResourceName(resource), primaryButton: '删除资源' });
      if (!confirmed.confirmed) return;
    }
    assertAccess();
    pending = notifications.status(`正在${resourceActionLabels[action]}：${resource.resource_id}`);
    let result: WorkspaceResourceDTO;
    if (explicit?.source) {
      const source = explicit.source;
      if (source.workspaceKey !== workspaceKey || source.resource.resource_id !== resource.resource_id
        || source.resource.owner_revision !== resource.owner_revision) throw new Error('资源动作句柄与冻结目标不一致');
      result = await source.run(action, thread);
    } else {
      const settled = await settleWorkspaceResourceOperation(0, workspaceKey, resource.kind, () => {
        assertAccess(); return controlWorkspaceResource(0, workspaceKey, resource, action, thread);
      }, () => !invalidated && workspaceContext.getSnapshot().gatewayId === initial.gatewayId);
      if (!settled.succeeded) throw new Error(settled.error, { cause: settled.cause });
      result = settled.resource;
    }
    assertAccess();
    notifications.info(`${resourceActionLabels[action]}已确认：${result.resource_id}`);
    return result;
  } finally { pending?.close(); release(); }
}

registerAction2(class extends Action2 {
  constructor() { super({ id: resourceFocusCommand, title: { value: '聚焦工作区资源', original: 'Focus Workspace Resource' }, f1: false }); }
  async run(accessor: ServicesAccessor, supplied: RuntimeResourceCommandTarget): Promise<void> {
    const commands = accessor.get(ICommandService), views = accessor.get(IViewsService);
    const owner = { ...supplied.owner }, resource = structuredClone(supplied.resource);
    const initialGateway = workspaceContext.getSnapshot().gatewayId;
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    const assertAccess = () => {
      if (invalidated || workspaceContext.getSnapshot().gatewayId !== initialGateway) throw new Error('Gateway 或访问凭据已改变，请重新选择资源');
    };
    try {
      if (!initialGateway) throw new Error('Gateway 登记身份尚未加载');
      if (resource.workspace_id !== owner.workspaceId) throw new Error('资源回执不属于明确选择的工作区');
      if (resource.kind !== 'terminal' && resource.kind !== 'browser') throw new Error('此入口只聚焦终端或浏览器资源');
      if (resource.status === 'deleted') throw new Error('所选资源已删除');
      const key = gatewayWorkspaceKey(owner);
      if (workspaceContext.getSnapshot().activeId !== key) {
        await commands.executeCommand(workspaceActionCommands.activate, owner);
        assertAccess();
        // 原导航的 React 发布可能尚未提交，沿已有镜像回读，不自行写入活动工作区。
        await workspaceContext.refresh();
      }
      await getWorkbenchContribution<WorkspacePanelState>(WorkspacePanelState.ID).whenRestored(key);
      assertAccess();
      if (workspaceContext.getSnapshot().activeId !== key) throw new Error('工作区选择已改变，请重新聚焦资源');
      const view = await views.openView<WorkspacePanel>(resource.kind === 'terminal' ? 'boxteam.workspace.terminal' : 'boxteam.browser.view', true);
      assertAccess();
      if (workspaceContext.getSnapshot().activeId !== key) throw new Error('工作区选择已改变，请重新聚焦资源');
      if (!view) throw new Error('资源所属工作区面板不可用');
      view.revealResource(owner, resource.resource_id);
    } finally { release(); }
  }
});

for (const action of Object.keys(resourceActionLabels) as ResourceAction[]) registerAction2(class extends Action2 {
  constructor() { super({ id: resourceActionCommand(action), title: { value: resourceActionLabels[action], original: resourceActionLabels[action] }, f1: true }); }
  run(accessor: ServicesAccessor, target?: RuntimeResourceCommandTarget) { return runResourceAction(accessor, action, target); }
});
