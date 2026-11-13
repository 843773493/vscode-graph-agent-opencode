import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { getWorkbenchContribution } from '../../upstream/src/vs/workbench/common/contributions.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { controlWorkspaceResource, getWorkspaceResource, listWorkspaceResources, type ResourceAction, type ResourceThreadRef, type WorkspaceResourceDTO } from '../../../web/src/api/workspaceResources.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { resourceActionLabels, resourceActionMessageKeys, settleWorkspaceResourceOperation, workspaceResourceName, type WorkspaceResourceActionSource } from '../../../web/src/hooks/workspace/resourceActions.js';
import { gateway } from '../agentApi.js';
import { captureSearchOwner, type SearchInvocationOptions } from '../search/target.js';
import type { ResourcePick } from '../search/quickAccessPicks.js';
import { ThreadInput } from '../threadInput.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceActionCommands } from '../search/workspaceActions.js';
import type { WorkspacePanel } from '../workspacePanel.js';
import { WorkspacePanelState } from '../workspacePanelState.js';

export const resourceActionCommand = (action: ResourceAction) => `boxteam.resource.${action}`;
export const resourceFocusCommand = 'boxteam.resource.focus';
export interface RuntimeResourceCommandTarget {
  owner: WorkspaceOwner;
  resource: WorkspaceResourceDTO | Pick<WorkspaceResourceDTO, 'resource_id' | 'owner_revision'>;
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
  if (!initial.gatewayId) throw new Error(t('workbench.resource.gatewayNotLoaded'));
  let invalidated = false;
  const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const releaseGateway = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidated = true;
  });
  const assertAccess = () => {
    if (invalidated) throw new Error(t('workbench.resource.accessChanged'));
  };
  let pending: ReturnType<INotificationService['status']> | undefined;
  try {
    let owner = explicit?.owner;
    if (!owner) {
      if (origin || initial.activeId) owner = { ...captureSearchOwner(editors) };
      else owner = (await quick.pick(initial.workspaces.map(item => ({ label: item.name,
        description: `${item.gateway_id} · ${item.workspace_id}`, owner: { gatewayId: item.gateway_id, workspaceId: item.workspace_id } })),
        { title: t('workbench.resource.chooseWorkspace'), placeHolder: t('workbench.resource.chooseWorkspaceHint') }))?.owner;
    }
    if (!owner) return;
    const workspaceKey = gatewayWorkspaceKey(owner);
    // 已挂载组件持有精确 owner 与 scope 句柄，不为原按钮重复查询控制面。
    if (!explicit?.source) {
      const routes = await gateway.workspaces(false);
      assertAccess();
      const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
      if (target.status !== 'ready') throw new Error(t('workbench.resource.workspaceUnavailable', target.status));
    }
    const resource = explicit?.resource ? 'workspace_id' in explicit.resource ? explicit.resource
      : await getWorkspaceResource(0, workspaceKey, explicit.resource.resource_id) : (await quick.pick(
      (await Promise.all(['terminal', 'browser'].map(kind => listWorkspaceResources(0, workspaceKey, kind as 'terminal' | 'browser'))))
        .flatMap(list => list.items).filter(item => item.available_actions.includes(action)).map(item => ({
          label: workspaceResourceName(item), description: `${item.kind} · ${item.status}`,
          detail: item.resource_id, resource: item,
        })), { title: resourceActionLabels[action], placeHolder: `${owner.gatewayId} · ${owner.workspaceId}` }))?.resource;
    if (!resource) return;
    assertAccess();
    if (explicit && resource.owner_revision !== explicit.resource.owner_revision) throw new Error(t('workbench.resource.revisionChanged'));
    if (resource.workspace_id !== owner.workspaceId) throw new Error(t('workbench.resource.ownerMismatch'));
    if (resource.kind !== 'terminal' && resource.kind !== 'browser') throw new Error(t('workbench.resource.unsupportedKind'));
    if (!resource.available_actions.includes(action)) throw new Error(t('workbench.resource.actionUnavailable', resourceActionLabels[action]));
    let thread = action === 'attach' || action === 'detach' ? explicit?.thread : undefined;
    if (action === 'attach' && !thread && originThread?.owner.gatewayId === owner.gatewayId && originThread.owner.workspaceId === owner.workspaceId) thread = originThread.thread;
    if (action === 'attach' && !thread) {
      const providerOptions: SearchInvocationOptions = { searchOwners: [owner], resourceSelection: 'thread' };
      const selected = await quick.quickAccess.pick('thread ', { preserveValue: true, enabledProviderPrefixes: ['thread '],
        placeholder: t('workbench.resource.chooseAttachThread'), providerOptions });
      assertAccess();
      if (!selected?.length) return;
      const selectedResource = (selected[0] as ResourcePick).resource;
      if (!selectedResource || selectedResource.scheme !== 'boxteam-thread') throw new Error(t('workbench.resource.ownerMismatch'));
      const selectedOwner = ThreadInput.fromResource(selectedResource).owner;
      if (selectedOwner.gatewayId !== owner.gatewayId || selectedOwner.workspaceId !== owner.workspaceId) throw new Error(t('workbench.resource.ownerMismatch'));
      thread = { session_id: selectedOwner.sessionId, thread_id: selectedOwner.threadId };
    }
    if (action === 'detach' && !thread) {
      const associations = [...new Map(resource.thread_associations.map(ref => [`${ref.session_id}/${ref.thread_id}`, ref])).values()];
      if (!associations.length) throw new Error(t('workbench.resource.threadNotAssociated'));
      thread = (await quick.pick(associations.map(ref => ({ label: ref.thread_id, description: ref.session_id, thread: ref })),
        { title: t('workbench.resource.chooseDetachThread') }))?.thread;
      if (!thread) return;
    }
    if (action === 'detach' && !resource.thread_associations.some(ref => ref.session_id === thread!.session_id && ref.thread_id === thread!.thread_id)) {
      throw new Error(t('workbench.resource.threadNotAssociated'));
    }
    if (action === 'attach' && resource.retention_scope === 'thread'
      && (resource.source_thread?.session_id !== thread!.session_id || resource.source_thread.thread_id !== thread!.thread_id)) {
      throw new Error(t('workbench.resource.temporaryThreadOnly'));
    }
    if (action === 'delete' && !explicit?.source) {
      const confirmed = await dialogs.confirm({ type: 'warning', message: t(resource.kind === 'terminal' ? 'workbench.resource.deleteTerminal' : 'workbench.resource.deleteBrowser'),
        detail: workspaceResourceName(resource), primaryButton: t('resources.deleteResource') });
      if (!confirmed.confirmed) return;
    }
    assertAccess();
    pending = notifications.status(t('workbench.resource.actionPending', resourceActionLabels[action], resource.resource_id));
    let result: WorkspaceResourceDTO;
    if (explicit?.source) {
      const source = explicit.source;
      if (source.workspaceKey !== workspaceKey || source.resource.resource_id !== resource.resource_id
        || source.resource.owner_revision !== resource.owner_revision) throw new Error(t('workbench.resource.sourceMismatch'));
      result = await source.run(action, thread);
    } else {
      const settled = await settleWorkspaceResourceOperation(0, workspaceKey, resource.kind, () => {
        assertAccess(); return controlWorkspaceResource(0, workspaceKey, resource, action, thread);
      }, () => !invalidated);
      if (!settled.succeeded) throw new Error(settled.error, { cause: settled.cause });
      result = settled.resource;
    }
    assertAccess();
    notifications.info(t('workbench.resource.actionConfirmed', resourceActionLabels[action], result.resource_id));
    return result;
  } finally { pending?.close(); release(); releaseGateway(); }
}

registerAction2(class extends Action2 {
  constructor() { super({ id: resourceFocusCommand, title: { value: t('workbench.resource.focus'), original: actionTitles['workbench.resource.focus'] }, f1: false }); }
  async run(accessor: ServicesAccessor, supplied: RuntimeResourceCommandTarget & { resource: WorkspaceResourceDTO }): Promise<void> {
    const commands = accessor.get(ICommandService), views = accessor.get(IViewsService);
    const owner = { ...supplied.owner }, resource = structuredClone(supplied.resource);
    const initialGateway = workspaceContext.getSnapshot().gatewayId;
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    const releaseGateway = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== initialGateway) invalidated = true;
    });
    const assertAccess = () => {
      if (invalidated) throw new Error(t('workbench.resource.accessChanged'));
    };
    try {
      if (!initialGateway) throw new Error(t('workbench.resource.gatewayNotLoaded'));
      if (resource.workspace_id !== owner.workspaceId) throw new Error(t('workbench.resource.ownerMismatch'));
      if (resource.kind !== 'terminal' && resource.kind !== 'browser') throw new Error(t('workbench.resource.unsupportedFocusKind'));
      if (resource.status === 'deleted') throw new Error(t('workbench.resource.deleted'));
      const key = gatewayWorkspaceKey(owner);
      if (workspaceContext.getSnapshot().activeId !== key) {
        await commands.executeCommand(workspaceActionCommands.activate, owner);
        assertAccess();
        // 原导航的 React 发布可能尚未提交，沿已有镜像回读，不自行写入活动工作区。
        await workspaceContext.refresh();
      }
      await getWorkbenchContribution<WorkspacePanelState>(WorkspacePanelState.ID).whenRestored(key);
      assertAccess();
      if (workspaceContext.getSnapshot().activeId !== key) throw new Error(t('workbench.resource.workspaceChanged'));
      const view = await views.openView<WorkspacePanel>(resource.kind === 'terminal' ? 'boxteam.workspace.terminal' : 'boxteam.browser.view', true);
      assertAccess();
      if (workspaceContext.getSnapshot().activeId !== key) throw new Error(t('workbench.resource.workspaceChanged'));
      if (!view) throw new Error(t('workbench.resource.panelUnavailable'));
      view.revealResource(owner, resource.resource_id);
    } finally { release(); releaseGateway(); }
  }
});

for (const action of Object.keys(resourceActionLabels) as ResourceAction[]) registerAction2(class extends Action2 {
  constructor() { super({ id: resourceActionCommand(action), title: { value: resourceActionLabels[action], original: actionTitles[resourceActionMessageKeys[action]] }, f1: true }); }
  run(accessor: ServicesAccessor, target?: RuntimeResourceCommandTarget) { return runResourceAction(accessor, action, target); }
});
