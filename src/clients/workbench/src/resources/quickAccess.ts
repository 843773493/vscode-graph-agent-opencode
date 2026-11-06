import { Limiter } from '../../upstream/src/vs/base/common/async.js';
import { CancellationToken, CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Codicon } from '../../upstream/src/vs/base/common/codicons.js';
import { matchesFuzzy } from '../../upstream/src/vs/base/common/filters.js';
import { DisposableStore, toDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThemeIcon } from '../../upstream/src/vs/base/common/themables.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { PickerQuickAccessProvider, type IPickerQuickAccessItem } from '../../upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.js';
import { Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService, type IQuickPick } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { HttpRequestError, onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import type { ResourceNavigationResultDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { gateway } from '../agentApi.js';
import type { ResourceOpenOptions } from '../resourceOpener.js';
import { chooseSearchOwners } from '../search/quickAccessScope.js';
import type { SearchInvocationOptions } from '../search/target.js';
import { workspaceContext } from '../workspaceContext.js';

interface SourcePick extends IPickerQuickAccessItem { id: string }
interface ResourceAccessOptions extends SearchInvocationOptions { resourceGroup?: number }
interface SourceRun {
  picker: IQuickPick<SourcePick, { useSeparators: true }>;
  owners: readonly WorkspaceOwner[];
  gatewayId: string | null;
  group: number;
  ownerError?: string;
  invalidated: boolean;
}

/** 首条来源纵向链只发现根 AGENTS；不借 runtime 编译或读取全文填充候选。 */
export class ResourceQuickAccess extends PickerQuickAccessProvider<SourcePick> {
  static readonly PREFIX = 'resource ';
  private run?: SourceRun;
  constructor(@IQuickInputService private readonly quick: IQuickInputService,
    @IOpenerService private readonly opener: IOpenerService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @IConfigurationService private readonly configuration: IConfigurationService,
    @INotificationService private readonly notifications: INotificationService,
  ) { super(ResourceQuickAccess.PREFIX, { canAcceptInBackground: true,
    noResultsPick: { id: 'no-sources', label: '所选工作区没有匹配的 AGENTS 来源' } }); }

  override provide(picker: SourceRun['picker'], token: CancellationToken, options: ResourceAccessOptions = {}) {
    const lifetime = new DisposableStore(), access = new CancellationTokenSource(token);
    const run: SourceRun = { picker, owners: (options.searchOwners ?? (options.searchOwner ? [options.searchOwner] : [])).map(owner => ({ ...owner })),
      gatewayId: workspaceContext.getSnapshot().gatewayId, group: options.resourceGroup ?? this.groups.activeGroup.id,
      ownerError: options.searchOwnerError, invalidated: false };
    this.run = run;
    const invalidate = () => {
      run.invalidated = true;
      access.cancel();
      picker.activeItems = picker.selectedItems = [];
      picker.items = [{ id: 'source-access-invalidated', label: 'Gateway 或用户身份已改变，请重新查找来源' }];
    };
    lifetime.add(toDisposable(onGatewayUserSessionInvalidated(0, invalidate)));
    lifetime.add(toDisposable(workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== run.gatewayId) invalidate(); })));
    const scope = { iconClass: ThemeIcon.asClassName(Codicon.filter), tooltip: '选择来源所属工作区' };
    picker.buttons = [...picker.buttons, scope];
    picker.placeholder = '查找工作区根 AGENTS · 浏览不读取正文或激活来源';
    lifetime.add(picker.onDidTriggerButton(button => {
      if (button !== scope) return;
      const value = picker.value;
      this.report((async () => {
        let invalidated = false;
        const release = onGatewayUserSessionInvalidated(0, () => { invalidated = true; });
        try {
          picker.hide();
          const owners = await chooseSearchOwners(this.quick, run.owners);
          if (invalidated) throw new Error('用户身份已改变，请重新选择来源所属工作区');
          this.assertAccess(run);
          const providerOptions: ResourceAccessOptions = { ...options, searchOwners: owners ?? run.owners, resourceGroup: run.group };
          this.quick.quickAccess.show(value, { providerOptions });
        } finally { release(); }
      })());
    }));
    // 先取消实际读取，再释放原 Provider，避免子 token 提前解绑后发布迟到响应。
    lifetime.add(toDisposable(() => access.dispose(true)));
    lifetime.add(super.provide(picker, access.token, options));
    lifetime.add(toDisposable(() => { picker.buttons = picker.buttons.filter(button => button !== scope); if (this.run === run) this.run = undefined; }));
    return lifetime;
  }

  protected override async _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken): Promise<SourcePick[]> {
    const run = this.run!, controller = new AbortController(), limiter = new Limiter<SourcePick[]>(3);
    disposables.add(token.onCancellationRequested(() => controller.abort()));
    disposables.add(toDisposable(() => controller.abort()));
    if (!run.owners.length) return [{ id: 'source-owner-required', label: run.ownerError ?? '请先明确选择来源所属工作区',
      detail: '使用“选择来源所属工作区”按钮；不会自动启动或激活目标' }];
    try {
      this.assertAccess(run);
      const routes = await gateway.workspaces(false, controller.signal);
      const results = await Promise.allSettled(run.owners.map(owner => limiter.queue(async () => {
        controller.signal.throwIfAborted();
        const target = requireGatewayWorkspaceRequestTarget(routes.items, owner);
        if (target.status !== 'ready') throw new Error(`${target.name} 当前不可用（${target.status}），请明确启动或连接`);
        const result = await gateway.request<ResourceNavigationResultDTO>('/api/v1/resources/agent-spec', { signal: controller.signal }, gatewayWorkspaceKey(target));
        const node = result.node;
        if (!node || node.resource_shape !== 'file' || !node.operations.includes('read')) throw new Error('AGENTS 来源未公布文件读取能力');
        const highlights = matchesFuzzy(filter, 'AGENTS.md');
        if (filter && !highlights) return [];
        return [{ id: `${gatewayWorkspaceKey(owner)}/${node.uri}`, label: 'AGENTS.md', description: `工作区根来源 · ${target.name}`,
          detail: `${owner.gatewayId} · ${owner.workspaceId} · ${node.uri}`, highlights: { label: highlights ?? undefined },
          accept: (mods, event) => {
            this.assertAccess(run);
            const pinned = this.configuration.getValue<boolean>('workbench.editor.enablePreviewFromQuickOpen') === false
              || this.configuration.getValue<boolean>('workbench.editor.enablePreview') === false;
            const openOptions: ResourceOpenOptions = { fromUserGesture: true, allowContributedOpeners: true,
              boxteamResourceOwner: { ...owner }, boxteamTargetGroup: run.group, openToSide: mods.alt || pinned && mods.ctrlCmd,
              editorOptions: { pinned: mods.ctrlCmd || event.inBackground || pinned, preserveFocus: event.inBackground } };
            this.report(this.opener.open(node.uri, openOptions));
          },
        }];
      })));
      if (token.isCancellationRequested || run.invalidated) return [];
      this.assertAccess(run);
      return results.flatMap((result, index): SourcePick[] => result.status === 'fulfilled' ? result.value : [{
        id: `source-read-failed-${index}`, label: `AGENTS 来源不可用：${result.reason instanceof Error ? result.reason.message : String(result.reason)}`,
        detail: result.reason instanceof HttpRequestError && result.reason.requestId ? `request_id: ${result.reason.requestId}` : gatewayWorkspaceKey(run.owners[index]),
      }]);
    } catch (cause) {
      if (token.isCancellationRequested || run.invalidated) return [];
      return [{ id: 'source-read-failed', label: `AGENTS 来源读取失败：${cause instanceof Error ? cause.message : String(cause)}` }];
    } finally { limiter.dispose(); }
  }
  private assertAccess(run: SourceRun): void {
    if (run.invalidated || workspaceContext.getSnapshot().gatewayId !== run.gatewayId) throw new Error('Gateway 或用户身份已改变，请重新查找来源');
  }
  private report(operation: Promise<unknown>): void { void operation.catch(cause => this.notifications.error(cause)); }
}

Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
  ctor: ResourceQuickAccess, prefix: ResourceQuickAccess.PREFIX, placeholder: '查找已接入的来源资源',
  helpEntries: [{ description: '查找来源资源（工作区根 AGENTS）', commandId: 'boxteam.quickAccess.resources', commandCenterOrder: 44 }],
});
registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.quickAccess.resources', title: { value: '查找来源资源', original: 'Find Source Resources' }, f1: true }); }
  run(accessor: ServicesAccessor, owner?: WorkspaceOwner): void {
    const providerOptions: SearchInvocationOptions | undefined = owner ? { searchOwner: owner } : undefined;
    accessor.get(IQuickInputService).quickAccess.show(ResourceQuickAccess.PREFIX, { providerOptions });
  }
});
