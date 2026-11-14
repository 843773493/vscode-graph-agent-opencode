import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { matchesFuzzy } from '../../upstream/src/vs/base/common/filters.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { PickerQuickAccessProvider, type IPickerQuickAccessItem } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService, type IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { HttpRequestError, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { listPersistentTasks, type PersistentTaskDTO } from '../../../web/src/api/persistentTasks.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { taskActionLabels } from '../../../web/src/hooks/persistentTasks/taskActions.js';
import { taskLabel, taskName, taskType } from '../../../web/src/components/workspace/persistentTasks/taskPresentation.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { chooseSearchOwners } from '../search/quickAccessScope.js';
import type { SearchInvocationOptions } from '../search/target.js';
import { automationActions, taskActionCommand } from './actions.js';

interface TaskPick extends IPickerQuickAccessItem { id: string }
interface TaskRun {
  picker: IQuickPick<TaskPick, { useSeparators: true }>;
  owners: readonly WorkspaceOwner[];
  options: SearchInvocationOptions;
  gatewayId: string | null;
  invalidated: boolean;
}

/** 项目 automation 与原版 task 前缀分离；候选只读取原任务 DTO，没有预览写入。 */
export class AutomationQuickAccess extends PickerQuickAccessProvider<TaskPick> {
  static readonly PREFIX = 'automation ';
  private run?: TaskRun;
  constructor(@IQuickInputService private readonly quick: IQuickInputService,
    @ICommandService private readonly commands: ICommandService,
    @INotificationService private readonly notifications: INotificationService,
  ) { super(AutomationQuickAccess.PREFIX, { noResultsPick: { id: 'no-tasks', label: t('quickAccess.automation.noResults') } }); }

  override provide(picker: TaskRun['picker'], token: CancellationToken, options: SearchInvocationOptions = {}) {
    const lifetime = new DisposableStore(), access = new CancellationTokenSource(token);
    const run: TaskRun = { picker, options, owners: (options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : [])).map(owner => ({ ...owner })),
      gatewayId: workspaceContext.getSnapshot().gatewayId, invalidated: false };
    this.run = run;
    const invalidate = () => {
      run.invalidated = true;
      access.cancel();
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'task-access-invalidated', label: t('quickAccess.automation.identityChanged') }];
    };
    lifetime.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidate(); })));
    const scope = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: t('quickAccess.automation.chooseWorkspace') };
    picker.buttons = [...picker.buttons, scope];
    picker.placeholder = t('quickAccess.automation.placeholder');
    lifetime.add(picker.onDidTriggerButton(button => {
      if (button !== scope) return;
      const value = picker.value;
      this.report((async () => {
        let invalidated = false;
        const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
        const releaseGateway = workspaceContext.subscribe(() => {
          if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) run.invalidated = true;
        });
        try {
          picker.hide();
          const owners = await chooseSearchOwners(this.quick, run.owners);
          if (invalidated) throw new Error(t('quickAccess.automation.workspaceCredentialsChanged'));
          this.assertAccess(run);
          const providerOptions: SearchInvocationOptions = { ...options, searchOwners: owners ?? run.owners };
          this.quick.quickAccess.show(value, { providerOptions });
        } finally { release(); releaseGateway(); }
      })());
    }));
    // 先取消父 token，再释放原 Provider；倒序会让子查询先解绑，迟到读取仍可发布。
    lifetime.add(toDisposable(() => access.dispose(true)));
    lifetime.add(super.provide(picker, access.token, options));
    lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(button => button !== scope); if (this.run === run) this.run = undefined; }));
    return lifetime;
  }

  protected override async _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken): Promise<TaskPick[]> {
    const run = this.run!, controller = new AbortController();
    disposables.add(token.onCancellationRequested(() => controller.abort()));
    disposables.add(toDisposable(() => controller.abort()));
    const alive = () => !token.isCancellationRequested && !run.invalidated;
    if (!run.owners.length) return [{ id: 'task-owner-required', label: run.options.searchOwnerError ?? t('quickAccess.automation.workspaceRequired'),
      detail: t('quickAccess.automation.workspaceHint') }];
    try {
      this.assertAccess(run);
      const routes = await gateway.workspaces(false, controller.signal);
      const results = await Promise.allSettled(run.owners.map(async owner => {
        const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
        if (target.status !== 'ready') throw new Error(t('quickAccess.sources.workspaceUnavailable', target.name, target.status));
        const snapshot = await listPersistentTasks(0, target, controller.signal);
        return snapshot.items.flatMap(task => {
          if (task.workspace_id !== owner.workspaceId) throw new Error(t('quickAccess.automation.ownerMismatch'));
          const label = taskName(task), highlights = matchesFuzzy(filter, label);
          if (filter && !highlights && !task.task_id.includes(filter)) return [];
          const frozen = structuredClone(task);
          return [{ id: `${gatewayWorkspaceKey(owner)}/${task.task_id}`, label,
            description: `${taskType(task)} · ${taskLabel(task.lifecycle)} · ${target.name}`,
            detail: `${owner.gatewayId} · ${owner.workspaceId} · ${task.task_id} · revision ${task.revision}`,
            highlights: { label: highlights ?? undefined },
            accept: () => this.report(this.chooseAction(run, { ...owner }, frozen, label)),
          }];
        });
      }));
      if (!alive()) return [];
      this.assertAccess(run);
      return results.flatMap((result, index): TaskPick[] => result.status === 'fulfilled' ? result.value : [{
        id: `task-read-failed-${index}`, label: t('quickAccess.automation.failed', String(result.reason instanceof Error ? result.reason.message : result.reason)),
        detail: result.reason instanceof HttpRequestError && result.reason.requestId ? `request_id: ${result.reason.requestId}` : gatewayWorkspaceKey(run.owners[index]),
      }]);
    } catch (cause) {
      if (!alive()) return [];
      return [{ id: 'task-read-failed', label: t('quickAccess.automation.failed', cause instanceof Error ? cause.message : String(cause)) }];
    }
  }
  private assertAccess(run: TaskRun): void {
    if (run.invalidated || workspaceContext.getSnapshot().gatewayId !== run.gatewayId) throw new Error(t('quickAccess.automation.identityChanged'));
  }
  private async chooseAction(run: TaskRun, owner: WorkspaceOwner, task: PersistentTaskDTO, name: string): Promise<void> {
    const value = run.picker.value;
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    const releaseGateway = workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) run.invalidated = true;
    });
    try {
      this.assertAccess(run);
      const selected = await this.quick.pick(automationActions(task).map(action => ({ action, label: taskActionLabels[action],
        description: action === 'checkOnly' ? t('quickAccess.automation.checkOnlyHint') : undefined })),
        { title: t('quickAccess.automation.actionsTitle', name), placeHolder: `${owner.gatewayId} · ${owner.workspaceId} · revision ${task.revision}` });
      if (invalidated) throw new Error(t('quickAccess.automation.credentialsChanged'));
      this.assertAccess(run);
      if (!selected) {
        const providerOptions: SearchInvocationOptions = { ...run.options, searchOwners: run.owners };
        this.quick.quickAccess.show(value, { providerOptions });
        return;
      }
      await this.commands.executeCommand(taskActionCommand(selected.action), { owner, task });
    } finally { release(); releaseGateway(); }
  }
  private report(operation: Promise<unknown>): void { void operation.catch(cause => this.notifications.error(cause)); }
}

Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
  ctor: AutomationQuickAccess, prefix: AutomationQuickAccess.PREFIX, placeholder: t('quickAccess.automation.registeredPlaceholder'),
  helpEntries: [{ description: t('quickAccess.automation.actionTitle'), commandId: 'boxteam.quickAccess.automation', commandCenterOrder: 45 }],
});
registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.quickAccess.automation', title: { value: t('quickAccess.automation.actionTitle'), original: actionTitles['quickAccess.automation.actionTitle'] }, f1: true }); }
  run(accessor: ServicesAccessor): void { accessor.get(IQuickInputService).quickAccess.show(AutomationQuickAccess.PREFIX); }
});
