import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { matchesFuzzy } from '../../upstream/src/vs/base/common/filters.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { PickerQuickAccessProvider, TriggerAction, type IPickerQuickAccessItem, type Picks } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { DefaultQuickAccessFilterValue, Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService, type IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { discoverGatewayWorkspaces } from '../../../web/src/api/gateway/catalogSearch.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { errorDiagnostic, errorMessage } from '../../../web/src/utils/errorMessage.js';
import { failureSummary } from '../../../shared/core/failurePresentation.js';
import { copyTextToClipboard } from '../../../web/src/utils/clipboard.js';
import { gatewayWorkspaceKey, gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { chooseSearchOwners } from './quickAccessScope.js';
import type { SearchInvocationOptions } from './target.js';
import { workspaceActionCommands, type WorkspaceAction } from './workspaceActions.js';

interface WorkspacePick extends IPickerQuickAccessItem { id: string }
interface WorkspaceOptions extends SearchInvocationOptions { workspaceRestore?: { value: string; id?: string } }
interface WorkspaceRun {
  picker: IQuickPick<WorkspacePick, { useSeparators: true }>;
  gatewayId: string | null;
  owners: readonly WorkspaceOwner[];
  options: WorkspaceOptions;
  invalidated: boolean;
}

/** 工作区模式只发现控制面元数据；选择候选后才明确选择范围或业务动作。 */
export class WorkspaceControlQuickAccess extends PickerQuickAccessProvider<WorkspacePick> {
  static readonly PREFIX = 'workspace ';
  private run?: WorkspaceRun;
  constructor(
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @ICommandService private readonly commands: ICommandService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @INotificationService private readonly notifications: INotificationService,
  ) { super(WorkspaceControlQuickAccess.PREFIX, { noResultsPick: { id: 'empty-workspaces', label: t('quickAccess.workspace.noResults') } }); }

  get defaultFilterValue() {
    return this.configuration.getValue<boolean>('workbench.quickOpen.preserveInput') ? DefaultQuickAccessFilterValue.LAST : undefined;
  }

  override provide(picker: WorkspaceRun['picker'], token: CancellationToken, options: WorkspaceOptions = {}) {
    const lifetime = new DisposableStore(), access = new CancellationTokenSource(token);
    const run: WorkspaceRun = { picker, options, gatewayId: workspaceContext.getSnapshot().gatewayId,
      owners: options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : []), invalidated: false };
    this.run = run;
    const invalidate = () => {
      run.invalidated = true; access.cancel();
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'workspace-identity-invalidated', label: t('workbench.workspaceAction.identityChanged') }];
      picker.busy = false;
    };
    lifetime.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidate();
    })));
    const scope = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: t('quickAccess.workspace.chooseScope') };
    picker.buttons = [...picker.buttons, scope];
    picker.placeholder = t('quickAccess.workspace.placeholder');
    lifetime.add(picker.onDidTriggerButton(button => { if (button === scope) this.report(this.chooseScope(run)); }));
    lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(button => button !== scope); access.dispose(true); picker.busy = false; }));
    lifetime.add(super.provide(picker, access.token, options));
    lifetime.add(toDisposable(() => { if (this.run === run) this.run = undefined; }));
    return lifetime;
  }

  protected override _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken): Promise<Picks<WorkspacePick>> {
    const run = this.run!, controller = new AbortController(), rows = new Map<string, WorkspacePick>();
    disposables.add(token.onCancellationRequested(() => controller.abort()));
    disposables.add(toDisposable(() => controller.abort()));
    let loading = false;
    const alive = () => !token.isCancellationRequested && !run.invalidated;
    const publish = async (cursor?: string): Promise<TriggerAction> => {
      if (loading || !alive()) return TriggerAction.NO_ACTION;
      const active = run.picker.activeItems[0];
      run.picker.busy = true;
      const items = await load(cursor);
      if (alive()) {
        run.picker.items = items;
        if (active && items.includes(active)) run.picker.activeItems = [active];
        run.picker.busy = false;
      }
      return TriggerAction.NO_ACTION;
    };
    const load = async (cursor?: string): Promise<WorkspacePick[]> => {
      loading = true;
      try {
        this.assertAccess(run);
        const page = await discoverGatewayWorkspaces(0, filter, { cursor, signal: controller.signal });
        if (!alive()) return [];
        this.assertAccess(run);
        if (page.gateway_id !== run.gatewayId) throw new Error(t('quickAccess.workspace.gatewayMismatch'));
        for (const item of page.items) {
          if (!item.owner) throw new Error(t('quickAccess.workspace.ownerMissing'));
          const owner = gatewayWorkspaceOwner(item.owner), id = gatewayWorkspaceKey(owner);
          if (rows.has(id)) continue;
          const target = workspaceContext.getSnapshot().workspaces.find(workspace => gatewayWorkspaceKey(workspace) === id);
          const description = `${owner.gatewayId} · ${target?.status ?? t('quickAccess.workspace.registrationStatusPending')}`;
          rows.set(id, { id, label: item.name, description, detail: `${item.breadcrumb_names.join('/')} · ${owner.workspaceId}`,
            highlights: { label: matchesFuzzy(filter, item.name) ?? undefined, description: matchesFuzzy(filter, description) ?? undefined },
            accept: () => this.report(this.chooseAction(run, owner, item.name)) });
        }
        const items = [...rows.values()];
        if (!page.complete) {
          if (!page.cursor) throw new Error(t('quickAccess.workspace.cursorMissing'));
          items.push({ id: 'workspace-discovery-more', label: t('quickAccess.workspace.more'),
            buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: t('quickAccess.workspace.continue') }], trigger: () => publish(page.cursor) });
        }
        return items;
      } catch (error) {
        if (!alive()) return [];
        return [...rows.values(), { id: 'workspace-discovery-failed', label: failureSummary(t('quickAccess.workspace.failed', errorMessage(error))),
          buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: t('quickAccess.workspace.retry') },
            { iconClass: ThemeIcon.asClassName(Codicon.copy), tooltip: t('shell.copyDiagnostic') }],
          trigger: index => { if (index === 0) return publish(cursor); this.report(copyTextToClipboard(errorDiagnostic(error))); return TriggerAction.NO_ACTION; } }];
      } finally { loading = false; }
    };
    return load().then(items => ({ items, active: run.options.workspaceRestore?.value === run.picker.value
      ? items.find(item => item.id === run.options.workspaceRestore?.id) : undefined }));
  }

  private assertAccess(run: WorkspaceRun): void {
    if (run.invalidated || !run.gatewayId || workspaceContext.getSnapshot().gatewayId !== run.gatewayId) throw new Error(t('quickAccess.workspace.accessUnavailable'));
  }

  private restore(run: WorkspaceRun, value: string, activeId?: string): void {
    this.assertAccess(run);
    const providerOptions: WorkspaceOptions = { ...run.options, workspaceRestore: { value, id: activeId } };
    this.quickInput.quickAccess.show(value, { preserveValue: true, providerOptions });
  }

  private search(run: WorkspaceRun, owners: readonly WorkspaceOwner[]): void {
    this.assertAccess(run);
    const providerOptions: WorkspaceOptions = { ...run.options, searchOwners: owners, searchOwnerError: undefined };
    this.quickInput.quickAccess.show('', { providerOptions });
  }

  private async chooseScope(run: WorkspaceRun): Promise<void> {
    this.assertAccess(run);
    const value = run.picker.value, activeId = run.picker.activeItems[0]?.id;
    run.picker.hide();
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    try {
      const selected = await chooseSearchOwners(this.quickInput, run.owners);
      if (invalidated) throw new Error(t('quickAccess.workspace.scopeCredentialsChanged'));
      if (selected) this.search(run, selected); else this.restore(run, value, activeId);
    } finally { release(); }
  }

  private async chooseAction(run: WorkspaceRun, owner: WorkspaceOwner, name: string): Promise<void> {
    this.assertAccess(run);
    const value = run.picker.value, target = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, owner);
    const choices: { label: string; description?: string; action: WorkspaceAction | 'search' }[] = [
      { label: t('workbench.workspaceAction.details'), action: 'details' },
      { label: t('quickAccess.workspace.searchHere'), description: t('quickAccess.workspace.searchHereHint'), action: 'search' },
    ];
    if (target.status === 'ready') choices.push({ label: t('quickAccess.workspace.switchHere'), action: 'activate' });
    if (target.connection_kind === 'remote_gateway') choices.push({ label: t('workbench.workspaceAction.reconnect'), action: 'reconnect' });
    if (target.managed && target.status !== 'ready') choices.push({ label: t('workbench.workspaceAction.start'), description: t('quickAccess.workspace.startHint'), action: 'start' });
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    try {
      const selected = await this.quickInput.pick(choices, { title: t('quickAccess.workspace.actionsTitle', name), placeHolder: `${owner.gatewayId} · ${owner.workspaceId}` });
      if (invalidated) throw new Error(t('quickAccess.workspace.actionCredentialsChanged'));
      this.assertAccess(run);
      if (!selected) { this.restore(run, value, gatewayWorkspaceKey(owner)); return; }
      if (selected.action === 'search') this.search(run, [owner]);
      else await this.commands.executeCommand(workspaceActionCommands[selected.action], owner);
    } finally { release(); }
  }

  private report(operation: Promise<unknown>): void { void operation.catch(error => this.notifications.error(error)); }
}

Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
  ctor: WorkspaceControlQuickAccess, prefix: WorkspaceControlQuickAccess.PREFIX,
  placeholder: t('quickAccess.workspace.registeredPlaceholder'),
  helpEntries: [{ description: t('quickAccess.workspace.actionTitle'), commandId: 'boxteam.quickAccess.workspaces', commandCenterOrder: 44 }],
});
registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.quickAccess.workspaces', title: { value: t('quickAccess.workspace.actionTitle'), original: actionTitles['quickAccess.workspace.actionTitle'] }, f1: true }); }
  run(accessor: ServicesAccessor): void { accessor.get(IQuickInputService).quickAccess.show(WorkspaceControlQuickAccess.PREFIX); }
});
