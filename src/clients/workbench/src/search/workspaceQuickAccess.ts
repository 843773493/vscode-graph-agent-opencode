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
import { HttpRequestError, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
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
  ) { super(WorkspaceControlQuickAccess.PREFIX, { noResultsPick: { id: 'empty-workspaces', label: '当前 Gateway 没有匹配的已登记工作区' } }); }

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
      picker.items = [{ id: 'workspace-identity-invalidated', label: 'Gateway 或访问凭据已改变，请重新选择工作区' }];
      picker.busy = false;
    };
    lifetime.add(toDisposable(onGatewayTokenInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => {
      if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidate();
    })));
    const scope = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: '选择资源搜索范围' };
    picker.buttons = [...picker.buttons, scope];
    picker.placeholder = '查找已登记工作区 · 浏览不会启动或切换 · Enter 选择动作';
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
        if (page.gateway_id !== run.gatewayId) throw new Error('工作区发现返回了其它 Gateway 的控制面');
        for (const item of page.items) {
          if (!item.owner) throw new Error('工作区发现缺少稳定 Gateway／UUID 归属');
          const owner = gatewayWorkspaceOwner(item.owner), id = gatewayWorkspaceKey(owner);
          if (rows.has(id)) continue;
          const target = workspaceContext.getSnapshot().workspaces.find(workspace => gatewayWorkspaceKey(workspace) === id);
          const description = `${owner.gatewayId} · ${target?.status ?? '登记状态待刷新'}`;
          rows.set(id, { id, label: item.name, description, detail: `${item.breadcrumb_names.join('/')} · ${owner.workspaceId}`,
            highlights: { label: matchesFuzzy(filter, item.name) ?? undefined, description: matchesFuzzy(filter, description) ?? undefined },
            accept: () => this.report(this.chooseAction(run, owner, item.name)) });
        }
        const items = [...rows.values()];
        if (!page.complete) {
          if (!page.cursor) throw new Error('工作区发现尚未完成但没有原 cursor');
          items.push({ id: 'workspace-discovery-more', label: '还有已登记工作区，明确继续发现',
            buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.more), tooltip: '继续发现工作区' }], trigger: () => publish(page.cursor) });
        }
        return items;
      } catch (error) {
        if (!alive()) return [];
        return [...rows.values(), { id: 'workspace-discovery-failed', label: `工作区发现失败：${error instanceof Error ? error.message : String(error)}`,
          detail: error instanceof HttpRequestError && error.requestId ? `request_id: ${error.requestId}` : undefined,
          buttons: [{ iconClass: ThemeIcon.asClassName(Codicon.refresh), tooltip: '重试原工作区发现查询' }], trigger: () => publish(cursor) }];
      } finally { loading = false; }
    };
    return load().then(items => ({ items, active: run.options.workspaceRestore?.value === run.picker.value
      ? items.find(item => item.id === run.options.workspaceRestore?.id) : undefined }));
  }

  private assertAccess(run: WorkspaceRun): void {
    if (run.invalidated || !run.gatewayId || workspaceContext.getSnapshot().gatewayId !== run.gatewayId) throw new Error('Gateway 或访问凭据不可用，请重新选择工作区');
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
      if (invalidated) throw new Error('访问凭据已改变，请重新选择搜索范围');
      if (selected) this.search(run, selected); else this.restore(run, value, activeId);
    } finally { release(); }
  }

  private async chooseAction(run: WorkspaceRun, owner: WorkspaceOwner, name: string): Promise<void> {
    this.assertAccess(run);
    const value = run.picker.value, target = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, owner);
    const choices: { label: string; description?: string; action: WorkspaceAction | 'search' }[] = [
      { label: '在此工作区查找资源', description: '只改变搜索范围', action: 'search' },
    ];
    if (target.status === 'ready') choices.push({ label: '切换到此工作区', action: 'activate' });
    if (target.connection_kind === 'remote_gateway') choices.push({ label: '连接工作区 Gateway', action: 'reconnect' });
    if (target.managed && target.status !== 'ready') choices.push({ label: '启动工作区 Backend', description: '同一 Backend 的工作区将一起启动', action: 'start' });
    let invalidated = false;
    const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    try {
      const selected = await this.quickInput.pick(choices, { title: `工作区动作 · ${name}`, placeHolder: `${owner.gatewayId} · ${owner.workspaceId}` });
      if (invalidated) throw new Error('访问凭据已改变，请重新选择工作区动作');
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
  placeholder: '查找已登记工作区 · 选择范围或明确执行动作',
  helpEntries: [{ description: '查找工作区并选择动作', commandId: 'boxteam.quickAccess.workspaces', commandCenterOrder: 44 }],
});
registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.quickAccess.workspaces', title: { value: '查找工作区并选择动作', original: 'Find Workspaces' }, f1: true }); }
  run(accessor: ServicesAccessor): void { accessor.get(IQuickInputService).quickAccess.show(WorkspaceControlQuickAccess.PREFIX); }
});
