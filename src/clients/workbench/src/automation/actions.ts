import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { listGatewayWorkspaces } from '../../../web/src/gatewayApi.js';
import { listPersistentTasks, type PersistentTaskDTO, type PersistentTaskConfigurationDTO } from '../../../web/src/api/persistentTasks.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { availableTaskActions, taskActionLabels, requestTaskMutation, settleTaskOperation, type PersistentTaskAction, type PersistentTaskActionSource } from '../../../web/src/hooks/persistentTasks/taskActions.js';
import { taskName } from '../../../web/src/components/workspace/persistentTasks/taskPresentation.js';
import { configurationProblem } from '../../../web/src/components/workspace/persistentTasks/TaskEditor.js';
import { captureSearchOwner } from '../search/target.js';
import { workspaceContext } from '../workspaceContext.js';
import { AutomationInput } from './input.js';
import type { AutomationEditorOptions } from './pane.js';

export const taskActionCommand = (action: PersistentTaskAction) => `boxteam.automation.${action}`;
export function automationActions(task: PersistentTaskDTO): PersistentTaskAction[] {
  return availableTaskActions(task).filter(action => action !== 'edit' || Boolean(task.configuration && !configurationProblem(task.configuration)));
}
export interface AutomationCommandTarget {
  owner: WorkspaceOwner;
  task: PersistentTaskDTO;
  source?: PersistentTaskActionSource;
  configuration?: PersistentTaskConfigurationDTO;
}

/** Action 固定目标，业务写入及回读仅交给原任务结算函数或发起 Hook 的临时句柄。 */
async function runTaskAction(accessor: ServicesAccessor, action: PersistentTaskAction, supplied?: AutomationCommandTarget): Promise<boolean> {
  const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService);
  const dialogs = accessor.get(IDialogService), notifications = accessor.get(INotificationService);
  const initial = workspaceContext.getSnapshot(), controller = new AbortController();
  if (!initial.gatewayId) throw new Error('Gateway 登记身份尚未加载');
  let invalidated = false;
  let pending: ReturnType<INotificationService['status']> | undefined;
  const release = onGatewayUserSessionInvalidated(0, () => { invalidated = true; });
  const assertAccess = () => {
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) throw new Error('Gateway 或用户身份已改变，请重新选择任务');
  };
  // 显式参数在任何 await 前复制，保留所选 revision，不用新列表静默替换。
  const explicit = supplied ? { ...supplied, owner: { ...supplied.owner }, task: structuredClone(supplied.task),
    configuration: supplied.configuration ? structuredClone(supplied.configuration) : undefined } : undefined;
  try {
    let owner = explicit?.owner;
    if (!owner) {
      if (initial.loading || initial.error) throw new Error(initial.error ?? '工作区登记信息尚未加载');
      const resource = EditorResourceAccessor.getCanonicalUri(editors.activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
      if (resource?.scheme.startsWith('boxteam-') || initial.activeId !== null) owner = { ...captureSearchOwner(editors) };
      else {
        const selected = await quick.pick(initial.workspaces.map(item => ({ label: item.name,
          description: `${item.gateway_id} · ${item.workspace_id}`, owner: gatewayWorkspaceOwner(item) })),
          { title: '选择任务所属工作区', placeHolder: '浏览不会启动、连接或激活工作区' });
        if (!selected) return false;
        owner = selected.owner;
      }
    }
    const routes = await listGatewayWorkspaces(0, { checkHealth: false });
    assertAccess();
    const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
    if (target.status !== 'ready') throw new Error(`任务所属工作区当前不可用（${target.status}），请明确启动或连接`);
    const task = explicit?.task ?? (await quick.pick(
      (await listPersistentTasks(0, target, controller.signal)).items.filter(item => automationActions(item).includes(action)).map(item => ({
        label: taskName(item), description: `${item.application_type} · ${item.lifecycle} · revision ${item.revision}`,
        detail: item.task_id, task: item,
      })), { title: taskActionLabels[action], placeHolder: `${owner.gatewayId} · ${owner.workspaceId}` }))?.task;
    if (!task) return false;
    assertAccess();
    if (task.workspace_id !== owner.workspaceId) throw new Error('任务回执不属于明确选择的工作区');
    if (!automationActions(task).includes(action === 'update' ? 'edit' : action)) throw new Error(
      ((action === 'edit' || action === 'update') && task.configuration && configurationProblem(task.configuration))
      || `任务当前状态不支持${taskActionLabels[action]}`);
    if (action === 'terminate') {
      const result = await dialogs.confirm({ type: 'warning', message: '终止持久任务？',
        detail: '将停止后续检查与投递。已经接受的工作执行继续由执行入口管理。', primaryButton: '终止任务' });
      if (!result.confirmed) return false;
      assertAccess();
    }
    if (explicit?.source) {
      const source = explicit.source, sourceOwner = gatewayWorkspaceOwner(source.target);
      if (sourceOwner.gatewayId !== owner.gatewayId || sourceOwner.workspaceId !== owner.workspaceId
        || source.task.task_id !== task.task_id || source.task.revision !== task.revision) throw new Error('任务动作句柄与冻结目标不一致');
      if (action !== 'details' && action !== 'history' && action !== 'edit') pending = notifications.status(`正在${taskActionLabels[action]}：${taskName(task)}`);
      return await source.run(action, explicit.configuration);
    }
    if (action === 'details' || action === 'history' || action === 'edit') {
      const options: AutomationEditorOptions = { pinned: true, automationAction: action, automationTask: task };
      await editors.openEditor(new AutomationInput({ ...owner }, task), options);
      return true;
    }
    pending = notifications.status(`正在${taskActionLabels[action]}：${taskName(task)}`);
    const result = await settleTaskOperation(0, target, controller.signal,
      () => { assertAccess(); return requestTaskMutation(0, target, task, action, controller.signal, explicit?.configuration); }, () => {});
    assertAccess();
    if (result.error) throw new Error(result.error);
    if (result.succeeded) notifications.info(`${taskActionLabels[action]}已确认：${taskName(task)}`);
    return result.succeeded;
  } finally { pending?.close(); release(); }
}

for (const action of Object.keys(taskActionLabels) as PersistentTaskAction[]) registerAction2(class extends Action2 {
  constructor() { super({ id: taskActionCommand(action), title: { value: taskActionLabels[action], original: taskActionLabels[action] }, f1: action !== 'update' }); }
  run(accessor: ServicesAccessor, target?: AutomationCommandTarget): Promise<boolean> { return runTaskAction(accessor, action, target); }
});
