import { append, $, clearNode } from '../../upstream/src/vs/base/browser/dom.js';
import { ViewPane, type IViewPaneOptions } from '../../upstream/src/vs/workbench/browser/parts/views/viewPane.js';
import { IKeybindingService } from '../../upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../upstream/src/vs/workbench/common/views.js';
import { IInstantiationService, type ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { IThemeService } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { IHoverService } from '../../upstream/src/vs/platform/hover/browser/hover.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { DeferredPromise, raceTimeout, timeout } from '../../upstream/src/vs/base/common/async.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import type { InstallPluginRequest, PluginBinding, PluginInstallOperationDTO, PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginSourceCatalogDTO, PluginTarget, PluginUninstallResultDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { gateway, GatewayRequestError } from '../agentApi.js';
import { createManagementTargetLifecycle, managementGateway, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import type { GatewayDiagnosticsDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { watchPluginRuntimeStatus } from '../../../web/src/api/pluginRuntimeStatus.js';

import { manageConfig, setPluginBindings, readWorkspaceDefaults, readWorkspaceBindings, choosePluginWorkspace, writeOnlyFields, type Scope } from '../settings/pluginConfiguration.js';

type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);
const activePluginInstallations = new Map<string, Promise<void>>();
const pluginGatewayLabel = (target: PluginTarget) => `原生 Gateway ${target.gateway_id}${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''}`;

function requireInstallationOwner(target: PluginTarget, operationId: string, operation: PluginInstallOperationDTO): PluginInstallOperationDTO {
  if (operation.gateway_id !== target.gateway_id || operation.operation_id !== operationId) {
    throw new Error(`插件安装操作回执不属于原 Gateway 或 operation_id：${operationId}`);
  }
  return operation;
}

/** 原 Extensions Viewlet 中展示 Gateway 自有插件模型，不伪造 IExtension 或 Gallery 对象。 */
export class ServicePluginPackagesView extends ViewPane {
  private scope: Scope = 'gateway';
  private content: HTMLElement | undefined;
  private selected: { packageId: string; pluginId: string; version: string } | undefined;
  private workspaceTarget: GatewayWorkspaceRequestTarget | undefined;
  private refreshRevision = 0;
  private searchInput: HTMLInputElement | undefined;
  private query = '';
  private controls: HTMLElement | undefined;
  private runtimeStatusController: AbortController | undefined;
  private runtimeStatus: PluginRuntimeStatusDTO | undefined;
  private workspaceBindings: readonly PluginBinding[] | undefined;
  private updateDetailsStatus: (() => void) | undefined;

  constructor(options: IViewPaneOptions, @IKeybindingService keybindings: IKeybindingService,
    @IContextMenuService contextMenu: IContextMenuService, @IConfigurationService configuration: IConfigurationService,
    @IContextKeyService contextKeys: IContextKeyService, @IViewDescriptorService descriptors: IViewDescriptorService,
    @IInstantiationService instantiation: IInstantiationService, @IOpenerService opener: IOpenerService,
    @IThemeService theme: IThemeService, @IHoverService hover: IHoverService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
    @IProgressService private readonly progress: IProgressService,
    @IDialogService private readonly dialogs: IDialogService) {
    super(options, keybindings, contextMenu, configuration, contextKeys, descriptors, instantiation, opener, theme, hover);
    this._register(onDidChangeExtensionManagementTarget(() => {
      this.stopRuntimeStatus();
      this.refreshRevision++;
      this.workspaceTarget = undefined;
      this.selected = undefined;
      void this.refresh();
    }));
    this._register(this.onDidChangeBodyVisibility(visible => {
      if (visible) void this.refresh();
      else { this.refreshRevision++; this.stopRuntimeStatus(); }
    }));
  }

  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    container.classList.add('service-plugin-packages-view');
    this.searchInput = append(container, $('input')) as HTMLInputElement;
    this.searchInput.type = 'search';
    this.searchInput.setAttribute('aria-label', '搜索已安装插件包');
    this.searchInput.placeholder = '搜索已安装插件包';
    this.searchInput.value = this.query;
    this.searchInput.addEventListener('input', () => void this.commands.executeCommand('workbench.extensions.search', this.searchInput!.value)
      .catch(error => this.notifications.error(errorsText(error))));
    this.controls = append(container, $('div'));
    const selector = append(this.controls, $('select')) as HTMLSelectElement;
    selector.setAttribute('aria-label', '插件管理范围');
    selector.innerHTML = '<option value="gateway">当前 Gateway</option><option value="workspace">当前工作区</option><option value="defaults">以后新建工作区</option>';
    append(this.controls, $('p')).textContent = '“以后新建工作区”只影响之后创建的新工作区，不会启用当前或已有工作区插件。';
    selector.addEventListener('change', () => {
      if (selector.value === 'gateway' || selector.value === 'workspace' || selector.value === 'defaults') this.scope = selector.value;
      this.workspaceTarget = undefined;
      this.selected = undefined;
      this.refreshRevision++;
      void this.refresh();
    });
    const install = append(this.controls, $('button'));
    install.textContent = '安装插件包';
    install.addEventListener('click', () => void this.commands.executeCommand('boxteam.plugins.installPackage')
      .catch(error => this.notifications.error(errorsText(error))));
    this.content = append(container, $('.service-plugin-packages-content'));
    void this.refresh();
  }

  get searchValue(): string { return this.query; }

  async openPlugin(target: PluginTarget, packageId: string, version: string, pluginId: string, scope: 'gateway' | 'workspace'): Promise<void> {
    const checkTarget = () => {
      const current = managementGateway('extensions');
      if (current.gateway_id !== target.gateway_id || current.gateway_connection_id !== target.gateway_connection_id) {
        throw new Error('插件管理目标已改变，请从原目标重新查找插件');
      }
    };
    checkTarget();
    this.scope = scope;
    const selector = this.controls?.querySelector('select');
    if (selector) selector.value = scope;
    this.selected = { packageId, pluginId, version };
    this.search('');
    await this.refresh();
    checkTarget();
    if (scope === 'workspace' && !this.workspaceTarget) return;
    if (!this.content?.querySelector('.service-plugin-details')) throw new Error(`所选插件版本已不可用：${packageId}/${pluginId}@${version}`);
  }

  search(query: string): void {
    this.query = query;
    if (this.searchInput) this.searchInput.value = query;
    const otherView = /(?:^|\s)@(mcp|agentPlugins)(?:\s|$)/i.test(query);
    const wasVisible = this.content !== undefined && !this.content.hidden;
    if (this.controls) this.controls.hidden = otherView;
    if (this.content) this.content.hidden = otherView;
    if (otherView) { this.refreshRevision++; this.stopRuntimeStatus(); }
    else if (!otherView && !wasVisible && this.isBodyVisible()) void this.refresh();
    for (const section of this.content?.querySelectorAll<HTMLElement>('.service-plugin-package') ?? []) {
      section.hidden = !section.dataset.search?.includes(query.trim().toLocaleLowerCase());
    }
  }

  override dispose(): void {
    this.stopRuntimeStatus();
    this.refreshRevision++;
    this.content = undefined;
    this.workspaceBindings = undefined;
    this.updateDetailsStatus = undefined;
    super.dispose();
  }

  async refresh(): Promise<void> {
    if (!this.content || !this.isBodyVisible() || this.content.hidden) return;
    this.stopRuntimeStatus();
    const revision = ++this.refreshRevision;
    const scope = this.scope;
    const container = this.content;
    this.workspaceBindings = undefined;
    this.updateDetailsStatus = undefined;
    clearNode(container);
    append(container, $('p')).textContent = '正在读取目标插件…';
    try {
      await this.renderPackages(container, revision, scope);
    } catch (error) {
      if (revision !== this.refreshRevision || this.content !== container) return;
      this.stopRuntimeStatus();
      if (!this.isBodyVisible() || container.hidden) return;
      clearNode(container);
      append(container, $('p')).textContent = errorsText(error);
      const retry = append(container, $('button'));
      retry.textContent = '重试';
      retry.addEventListener('click', () => void this.refresh());
      this.notifications.error(errorsText(error));
    }
  }

  private async runOperation(operation: () => Promise<void>): Promise<void> {
    const revision = this.refreshRevision;
    try { await operation(); }
    catch (error) { this.notifications.error(errorsText(error)); }
    finally { if (revision === this.refreshRevision) await this.refresh(); }
  }

  private async runTargetOperation(operation: (lifecycle: ReturnType<typeof createManagementTargetLifecycle>) => Promise<void>,
    expectedEntryGatewayId?: string): Promise<void> {
    await this.runOperation(async () => {
      const lifecycle = createManagementTargetLifecycle('原 Gateway 或访问凭据已改变，未开始新的插件管理操作', expectedEntryGatewayId);
      try { lifecycle.check(); await operation(lifecycle); }
      finally { lifecycle.dispose(); }
    });
  }

  private stopRuntimeStatus(): void {
    this.runtimeStatusController?.abort();
    this.runtimeStatusController = undefined;
    this.runtimeStatus = undefined;
  }

  private async observeRuntimeStatus(target: PluginTarget): Promise<PluginRuntimeStatusDTO> {
    this.stopRuntimeStatus();
    const controller = new AbortController();
    const first = new DeferredPromise<PluginRuntimeStatusDTO>();
    controller.signal.addEventListener('abort', () => void first.cancel(), { once: true });
    this.runtimeStatusController = controller;
    void watchPluginRuntimeStatus(target, controller.signal, snapshot => {
      if (this.runtimeStatusController !== controller || controller.signal.aborted
        || snapshot.plugins.some(slot => slot.gateway_id !== target.gateway_id)) return;
      this.runtimeStatus = snapshot;
      void first.complete(snapshot);
      this.updateRuntimeStatusPresentation();
    }).catch(error => {
      if (controller.signal.aborted || this.runtimeStatusController !== controller) return;
      if (!first.isSettled) { void first.error(error); return; }
      this.stopRuntimeStatus();
      this.notifications.error(errorsText(error));
    });
    // 首个完整快照同时用于初始化和后续观察，不再并行读取另一份 /status。
    const snapshot = await raceTimeout(first.p, DEFAULT_API_REQUEST_TIMEOUT_MS);
    if (!snapshot) {
      controller.abort();
      throw new Error('等待插件运行状态首个快照超时，请重新打开管理视图');
    }
    return snapshot;
  }

  private runtimeSlot(packageId: string, pluginId: string, workspaceId: string | undefined): PluginRuntimeStatusDTO['plugins'][number] | undefined {
    return this.runtimeStatus?.plugins.find(slot => slot.binding?.package_id === packageId
      && slot.binding.plugin_id === pluginId
      && (slot.binding.workspace_id ?? undefined) === workspaceId);
  }

  private updateRuntimeStatusPresentation(): void {
    const container = this.content;
    if (!container || !this.runtimeStatus) return;
    for (const row of container.querySelectorAll<HTMLElement>('[data-plugin-runtime-row]')) {
      const packageId = row.dataset.packageId;
      const pluginId = row.dataset.pluginId;
      if (!packageId || !pluginId) continue;
      const workspaceId = row.dataset.workspaceId || undefined;
      const status = this.runtimeSlot(packageId, pluginId, workspaceId);
      const output = row.querySelector<HTMLElement>('[data-plugin-runtime-value]');
      if (output) output.textContent = row.dataset.scope === 'defaults'
        ? row.dataset.initialRuntimeValue ?? ''
        : this.runtimeLabel(packageId, pluginId, workspaceId, status, row.dataset.runtime === 'true');
      const failure = row.querySelector<HTMLElement>('[data-plugin-runtime-error]');
      if (failure) {
        failure.hidden = !status?.error;
        failure.textContent = status?.error ?? '';
      }
    }
    const workspace = container.querySelector<HTMLElement>('[data-plugin-workspace-handoff]');
    if (workspace) {
      const state = this.runtimeStatus.workspace_configurations.find(item => item.workspace_id === workspace.dataset.workspaceId);
      workspace.textContent = `工作区 ${workspace.dataset.workspaceId} 配置交接：${state?.state ?? '尚未收到'}${state?.error ? ` · ${state.error}` : ''}`;
    }
    const details = container.querySelector<HTMLElement>('.service-plugin-details');
    const packageId = details?.dataset.packageId;
    const pluginId = details?.dataset.pluginId;
    if (details && packageId && pluginId) {
      const status = this.runtimeSlot(packageId, pluginId, details.dataset.workspaceId || undefined);
      const state = details.querySelector<HTMLElement>('[data-plugin-runtime-value]');
      if (state) state.textContent = details.dataset.scope === 'defaults'
        ? details.dataset.initialRuntimeValue ?? ''
        : this.runtimeLabel(packageId, pluginId, details.dataset.workspaceId || undefined, status,
          details.dataset.runtime === 'true');
      const versions = details.querySelector<HTMLElement>('[data-plugin-runtime-version]');
      if (versions && details.dataset.scope !== 'defaults') {
        versions.textContent = `配置：${this.runtimeConfigurationLabel(packageId, pluginId,
          details.dataset.workspaceId || undefined)} · `
          + this.runtimeActualLabel(status, details.dataset.runtime === 'true');
      }
      const handoff = details.querySelector<HTMLElement>('[data-plugin-workspace-handoff]');
      if (handoff) {
        const state = this.runtimeStatus.workspace_configurations.find(item => item.workspace_id === handoff.dataset.workspaceId);
        handoff.textContent = `工作区 ${handoff.dataset.workspaceId} 配置交接：${state?.state ?? '尚未收到'}${state?.error ? ` · ${state.error}` : ''}`;
      }
      const failure = details.querySelector<HTMLElement>('[data-plugin-runtime-error]');
      if (failure) {
        failure.hidden = !status?.error;
        failure.textContent = status?.error ?? '';
      }
    }
    this.updateDetailsStatus?.();
  }

  private configuredBinding(packageId: string, pluginId: string, workspaceId: string | undefined): PluginBinding | undefined {
    // 工作区来源期望与 Gateway 的 active 交接分别展示；旧交接不能覆盖 pending 期望。
    const bindings = workspaceId ? this.workspaceBindings : this.runtimeStatus?.configured_bindings;
    return bindings?.find(item => item.package_id === packageId
      && item.plugin_id === pluginId && (item.workspace_id ?? undefined) === workspaceId);
  }

  private runtimeConfigurationLabel(packageId: string, pluginId: string, workspaceId: string | undefined): string {
    const binding = this.configuredBinding(packageId, pluginId, workspaceId);
    return binding ? `${binding.enabled ? '启用' : '停用'} @${binding.package_version}` : '未绑定';
  }

  private runtimeLabel(packageId: string, pluginId: string, workspaceId: string | undefined,
    status: PluginRuntimeStatusDTO['plugins'][number] | undefined, runtime: boolean): string {
    return `${this.runtimeConfigurationLabel(packageId, pluginId, workspaceId)} · ${this.runtimeActualLabel(status, runtime)}`;
  }

  private runtimeActualLabel(status: PluginRuntimeStatusDTO['plugins'][number] | undefined, runtime: boolean): string {
    return status?.instance
      ? `实际：${status.state} @${status.instance.package_version}`
      : runtime ? `实际：${status?.state ?? '未运行'} · 尚无 ready 实例回执` : '声明式，无运行实例';
  }

  private async renderPackages(container: HTMLElement, revision: number, scope: Scope): Promise<void> {
    await workspaceContext.ensureReady();
    if (revision !== this.refreshRevision) return;
    const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!entryGatewayId) throw new Error('Gateway 身份尚未完成初始化，无法读取插件管理目标');
    const target = managementGateway('extensions');
    const workspace = scope === 'workspace' ? (this.workspaceTarget ?? await choosePluginWorkspace(target, this.quickInput)) : undefined;
    if (revision !== this.refreshRevision) return;
    this.workspaceTarget = workspace;
    if (scope === 'workspace' && !workspace) { clearNode(container); append(container, $('p')).textContent = '请选择目标工作区后查看插件。'; return; }
    const pluginTarget: PluginTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
    const [inventory, runtime, scopeBindings] = await Promise.all([
      gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      scope === 'defaults' ? undefined : this.observeRuntimeStatus(pluginTarget),
      scope === 'defaults' ? readWorkspaceDefaults(target) : workspace ? readWorkspaceBindings(workspace) : undefined,
    ]);
    if (this.content !== container || revision !== this.refreshRevision) return;
    this.workspaceBindings = workspace ? scopeBindings : undefined;
    clearNode(container);
    append(container, $('p')).textContent = inventory.sources?.message ?? '插件源尚未配置';
    if (workspace) {
      const handoff = runtime!.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      const summary = append(container, $('p'));
      summary.setAttribute('aria-label', '工作区插件配置交接');
      summary.dataset.pluginWorkspaceHandoff = 'true';
      summary.dataset.workspaceId = workspace.workspace_id;
      summary.textContent = `工作区 ${workspace.workspace_id} 配置交接：${handoff?.state ?? '尚未收到'}${handoff?.error ? ` · ${handoff.error}` : ''}`;
    }
    const pluginScope = scope === 'gateway' ? 'gateway' : 'workspace';
    const bindings = (scopeBindings ?? runtime!.configured_bindings).filter(item => scope === 'workspace'
      ? item.workspace_id === workspace?.workspace_id : item.workspace_id == null);
    for (const pkg of inventory.packages) {
      const manifest = pkg.manifest;
      if (!manifest) continue;
      const matching = manifest.plugins.filter(plugin => plugin.instance_scope === pluginScope);
      if (!matching.length) continue;
      const packageSection = append(container, $('section.service-plugin-package'));
      packageSection.dataset.search = `${manifest.package_id} ${manifest.name} ${manifest.description ?? ''} ${matching.map(plugin => `${plugin.id} ${plugin.name}`).join(' ')}`.toLocaleLowerCase();
      append(packageSection, $('h3')).textContent = `${manifest.name} · ${manifest.package_id}@${manifest.version}`;
      append(packageSection, $('p')).textContent = manifest.description ?? '';
      const packageActions = append(packageSection, $('.service-plugin-package-actions'));
      const batchButtons = [true, false].map(enabled => {
        const button = append(packageActions, $('button')) as HTMLButtonElement;
        button.textContent = enabled ? '全部启用此范围' : '全部停用此范围';
        button.addEventListener('click', () => void this.runTargetOperation(async lifecycle => {
          batchButtons.forEach(item => { item.disabled = true; });
          try {
            const excluded = manifest.plugins.filter(plugin => plugin.instance_scope !== pluginScope);
            const owner = `Gateway ${target.gateway_id}${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''} · ${scope}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''}`;
            const confirmation = await this.dialogs.confirm({ message: `${enabled ? '启用' : '停用'} ${manifest.package_id}@${manifest.version} 的当前范围插件？`,
              detail: `${owner}\n纳入：${matching.map(plugin => plugin.id).join('、')}\n排除（固定范围不匹配）：${excluded.map(plugin => `${plugin.id}（${plugin.instance_scope}）`).join('、') || '无'}\n`
                + (scope === 'defaults' ? '仅修改以后新建工作区的默认值，不操作当前实例。' : '每个插件独立提交，失败不回滚已完成的操作。'),
              primaryButton: enabled ? '全部启用此范围' : '全部停用此范围' });
            if (!confirmation.confirmed) return;
            lifecycle.check();
            const results = await setPluginBindings(scope, target, workspace, this.quickInput,
              matching.map(plugin => ({ package_id: manifest.package_id, plugin_id: plugin.id, package_version: manifest.version, enabled })),
              enabled, lifecycle.check, lifecycle.token, lifecycle.signal);
            if (!results) return;
            let current: PluginRuntimeStatusDTO | undefined, readError: string | undefined;
            if (scope !== 'defaults') {
              try {
                lifecycle.check();
                current = await gateway.request<PluginRuntimeStatusDTO>('/api/gateway/service-plugins/status',
                  { method: 'POST', body: JSON.stringify(pluginTarget) });
                lifecycle.check();
              }
              catch (error) { readError = errorsText(error); }
            }
            const lines = results.map(({ binding: item, configuration }) => {
              const actual = current?.plugins.find(slot => slot.gateway_id === target.gateway_id && slot.binding?.package_id === item.package_id
                && slot.binding.plugin_id === item.plugin_id && slot.binding.package_version === item.package_version
                && (scope === 'workspace' ? slot.binding.workspace_id === workspace?.workspace_id : slot.binding.workspace_id == null));
              const state = scope === 'defaults' ? '仅修改新建默认值，未操作当前实例' : readError ? `运行状态读取失败：${readError}`
                : actual ? `${actual.binding?.enabled ? '启用' : '停用'} · ${actual.declarative ? '声明式，无运行实例' : actual.state}${actual.error ? ` · ${actual.error}` : ''}${actual.instance ? ` · 实例 ${actual.instance.instance_id}` : ''}` : '未发布运行状态';
              return `${owner} · ${item.package_id}/${item.plugin_id}@${item.package_version}\n配置：${configuration}\n实际：${state}`;
            });
            await this.dialogs.info('插件批量操作结果', lines.join('\n\n'));
          } finally { batchButtons.forEach(item => { item.disabled = false; }); }
        }, entryGatewayId));
        return button;
      });
      const installInput = append(packageActions, $('button'));
      installInput.textContent = '重新安装/更新';
      installInput.addEventListener('click', () => void this.runOperation(() => installPackageFromView(
        this.quickInput, this.notifications, this.progress, pluginTarget, entryGatewayId)));
      const uninstall = append(packageActions, $('button')) as HTMLButtonElement;
      uninstall.textContent = '卸载整个包';
      uninstall.title = '由目标 Gateway 检查在用引用；有引用时显示具体原因';
      uninstall.addEventListener('click', () => {
        uninstall.disabled = true;
        void this.runTargetOperation(lifecycle => uninstallPackageFromView(this.quickInput, this.notifications,
          pluginTarget, manifest.package_id, manifest.version, lifecycle), entryGatewayId);
      });
      for (const plugin of matching) {
        const binding = bindings.find(item => item.package_id === manifest.package_id && item.plugin_id === plugin.id);
        const status = runtime?.plugins.find(item => item.gateway_id === target.gateway_id
          && item.binding?.package_id === manifest.package_id && item.binding.plugin_id === plugin.id
          && (scope === 'workspace' ? item.binding.workspace_id === workspace?.workspace_id : item.binding.workspace_id == null));
        const row = append(packageSection, $('article'));
        row.dataset.pluginRuntimeRow = 'true';
        row.dataset.packageId = manifest.package_id;
        row.dataset.pluginId = plugin.id;
        row.dataset.workspaceId = scope === 'workspace' ? workspace?.workspace_id ?? '' : '';
        row.dataset.scope = scope;
        row.dataset.runtime = String(Boolean(plugin.runtime));
        const initialRuntimeValue = scope === 'defaults'
          ? `${binding?.enabled ? '新建时启用' : binding ? '新建时停用' : '未加入默认值'}${binding ? ` @${binding.package_version}` : ''} · 不代表已运行`
          : this.runtimeLabel(manifest.package_id, plugin.id, scope === 'workspace' ? workspace?.workspace_id : undefined,
            status, Boolean(plugin.runtime));
        row.dataset.initialRuntimeValue = initialRuntimeValue;
        const open = append(row, $('button'));
        open.textContent = `${plugin.name} · ${plugin.id}`;
        open.addEventListener('click', () => { this.selected = { packageId: manifest.package_id, pluginId: plugin.id, version: manifest.version };
          this.showDetails(container, scope, target, workspace, manifest, plugin, binding, status, entryGatewayId); });
        const runtimeValue = append(row, $('span'));
        runtimeValue.dataset.pluginRuntimeValue = 'true';
        runtimeValue.textContent = initialRuntimeValue;
        if (revision === this.refreshRevision && this.selected?.packageId === manifest.package_id && this.selected.pluginId === plugin.id && this.selected.version === manifest.version) {
          this.showDetails(container, scope, target, workspace, manifest, plugin, binding, status, entryGatewayId);
        }
      }
    }
    for (const binding of bindings) {
      const installed = inventory.packages.some(pkg => pkg.manifest?.package_id === binding.package_id
        && pkg.manifest.version === binding.package_version
        && pkg.manifest.plugins.some(plugin => plugin.id === binding.plugin_id && plugin.instance_scope === pluginScope));
      if (installed) continue;
      const section = append(container, $('section.service-plugin-package'));
      section.dataset.search = `${binding.package_id} ${binding.plugin_id} ${binding.package_version}`.toLocaleLowerCase();
      section.setAttribute('aria-label', '安装声明不可用的插件绑定');
      append(section, $('h3')).textContent = `${binding.package_id}/${binding.plugin_id}@${binding.package_version}`;
      append(section, $('p')).textContent = `${pluginGatewayLabel(target)} · ${scope}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''}`;
      append(section, $('p')).textContent = '绑定的确切包版本或插件声明不可用。请补装原版本，或解除绑定；缺少原声明时不能编辑参数表单。';
      const row = append(section, $('article'));
      row.dataset.pluginRuntimeRow = 'true';
      row.dataset.packageId = binding.package_id;
      row.dataset.pluginId = binding.plugin_id;
      row.dataset.workspaceId = workspace?.workspace_id ?? '';
      row.dataset.scope = scope;
      // 未取得原声明时不能将插件误报为纯声明插件。
      row.dataset.runtime = 'true';
      row.dataset.initialRuntimeValue = `${binding.enabled ? '新建时启用' : '新建时停用'} @${binding.package_version} · 不代表已运行`;
      const status = this.runtimeSlot(binding.package_id, binding.plugin_id, workspace?.workspace_id);
      const state = append(row, $('p'));
      state.dataset.pluginRuntimeValue = 'true';
      state.textContent = scope === 'defaults' ? row.dataset.initialRuntimeValue
        : this.runtimeLabel(binding.package_id, binding.plugin_id, workspace?.workspace_id, status, true);
      const failure = append(row, $('p'));
      failure.dataset.pluginRuntimeError = 'true';
      failure.hidden = !status?.error;
      failure.textContent = status?.error ?? '';
      this.renderInstanceLog(row, target, status?.log_id);
      const install = append(section, $('button'));
      install.textContent = '补装插件包';
      install.addEventListener('click', () => void this.runOperation(() => installPackageFromView(
        this.quickInput, this.notifications, this.progress, pluginTarget, entryGatewayId)));
      const unbind = append(section, $('button')) as HTMLButtonElement;
      unbind.textContent = scope === 'defaults' ? '从新建默认值移除' : '解除此范围绑定';
      unbind.addEventListener('click', () => {
        unbind.disabled = true;
        void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace, this.quickInput, this.notifications,
          binding, null, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
      });
    }
    this.search(this.query);
    this.updateRuntimeStatusPresentation();
    if (!inventory.packages.length && !bindings.length) append(container, $('p')).textContent = `尚无插件包。${inventory.sources?.message ?? ''}`;
  }

  private renderInstanceLog(container: HTMLElement, target: PluginTarget, logId: string | undefined): void {
    if (!logId) return;
    const logs = append(container, $('button')) as HTMLButtonElement;
    logs.textContent = '查看实例日志';
    const output = append(container, $('pre'));
    output.setAttribute('aria-label', '插件实例日志');
    logs.addEventListener('click', () => {
      logs.disabled = true;
      void readPluginInstanceLog(target, logId).then(text => { output.textContent = text; })
        .catch(error => { output.textContent = errorsText(error); this.notifications.error(errorsText(error)); })
        .finally(() => { logs.disabled = false; });
    });
  }

  private showDetails(container: HTMLElement, scope: Scope, target: ReturnType<typeof managementGateway>, workspace: GatewayWorkspaceRequestTarget | undefined,
    manifest: NonNullable<PluginInventoryResultDTO['packages'][number]['manifest']>, plugin: NonNullable<typeof manifest.plugins[number]>,
    binding: PluginBinding | undefined, status: PluginRuntimeStatusDTO['plugins'][number] | undefined, entryGatewayId: string): void {
    if (scope !== 'defaults' && this.runtimeStatus) {
      binding = this.configuredBinding(manifest.package_id, plugin.id, workspace?.workspace_id);
      status = this.runtimeSlot(manifest.package_id, plugin.id, workspace?.workspace_id);
    }
    const old = container.querySelector('.service-plugin-details');
    old?.remove();
    const details = append(container, $('section.service-plugin-details'));
    details.dataset.packageId = manifest.package_id;
    details.dataset.pluginId = plugin.id;
    details.dataset.workspaceId = scope === 'workspace' ? workspace?.workspace_id ?? '' : '';
    details.dataset.scope = scope;
    details.dataset.runtime = String(Boolean(plugin.runtime));
    details.dataset.initialRuntimeValue = scope === 'defaults'
      ? `${plugin.instance_scope} · 新建工作区默认值，不会在此 Gateway 启动插件`
      : this.runtimeLabel(manifest.package_id, plugin.id, scope === 'workspace' ? workspace?.workspace_id : undefined,
          status, Boolean(plugin.runtime));
    append(details, $('h3')).textContent = `${manifest.package_id}/${plugin.id}`;
    const detailsState = append(details, $('p'));
    detailsState.dataset.pluginRuntimeValue = 'true';
    detailsState.textContent = details.dataset.initialRuntimeValue;
    const identity = append(details, $('p'));
    const ownerLabel = `Gateway ${target.gateway_id}${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''}${workspace ? ` · 工作区 ${workspace.workspace_id}` : ''} · ${manifest.version}`;
    identity.textContent = `${ownerLabel}${status?.instance ? ` · 实例 ${status.instance.instance_id}` : ''}`;
    if (workspace) {
      const handoff = append(details, $('p'));
      handoff.setAttribute('aria-label', '工作区插件配置交接');
      handoff.dataset.pluginWorkspaceHandoff = 'true';
      handoff.dataset.workspaceId = workspace.workspace_id;
      const state = this.runtimeStatus?.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      handoff.textContent = `工作区 ${workspace.workspace_id} 配置交接：${state?.state ?? '尚未收到'}${state?.error ? ` · ${state.error}` : ''}`;
    }
    const versions = append(details, $('p'));
    versions.setAttribute('aria-label', '插件配置与运行版本');
    versions.dataset.pluginRuntimeVersion = 'true';
    versions.textContent = `配置：${binding ? `${binding.enabled ? '启用' : '停用'} @${binding.package_version}` : '未绑定'} · `
      + (scope === 'defaults' ? '仅新建默认值，未操作当前实例'
        : this.runtimeActualLabel(status, Boolean(plugin.runtime)));
    const failure = append(details, $('p'));
    failure.setAttribute('aria-label', '插件运行失败原因');
    failure.dataset.pluginRuntimeError = 'true';
    failure.hidden = !status?.error;
    failure.textContent = status?.error ?? '';
    const instanceLog = append(details, $('div'));
    this.renderInstanceLog(instanceLog, target, status?.log_id);
    const actions = append(details, $('.service-plugin-detail-actions'));
    const renderActions = (binding: PluginBinding | undefined, status: PluginRuntimeStatusDTO['plugins'][number] | undefined) => {
      clearNode(actions);
      const bindingValue: ConfigurationBinding = { package_id: manifest.package_id, plugin_id: plugin.id, package_version: manifest.version, enabled: true };
      const otherVersion = binding && binding.package_version !== manifest.version;
      if (otherVersion) {
        const switchVersion = append(actions, $('button')) as HTMLButtonElement;
        switchVersion.textContent = '切换到此版本（保留开关）';
        switchVersion.addEventListener('click', () => {
          switchVersion.disabled = true;
          void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace, this.quickInput, this.notifications,
            bindingValue, 'preserve', false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
        });
      } else {
        const toggle = append(actions, $('button'));
        toggle.textContent = scope === 'defaults'
          ? binding?.enabled ? '设为新建时停用' : '设为新建时启用'
          : binding?.enabled ? '停用此范围' : '启用此范围';
        toggle.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
          this.quickInput, this.notifications, bindingValue, !binding?.enabled, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
      }
      if (binding && !otherVersion) {
        const unbind = append(actions, $('button'));
        unbind.textContent = scope === 'defaults' ? '从新建默认值移除' : '解除此范围绑定';
        unbind.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
          this.quickInput, this.notifications, bindingValue, null, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
      }
      if (scope !== 'defaults') {
        append(actions, $('p')).textContent = binding
          ? `当前绑定 ${manifest.package_id}/${plugin.id}@${binding.package_version}；保存与实际运行分别报告。`
          : '插件未绑定，参数仅留在原来源且未生效；绑定时按确切版本完整校验。';
        if (otherVersion) append(actions, $('p')).textContent = '切换绑定后才能按此版本编辑参数；安装新版本不会自动切换。';
        else {
          const parameters = append(actions, $('button'));
          parameters.textContent = '编辑参数 JSON';
          parameters.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
            this.quickInput, this.notifications, bindingValue, null, true, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
        }
        const privateFields = otherVersion ? [] : writeOnlyFields(plugin.contributes?.configuration);
        for (const field of privateFields) {
          const secret = append(actions, $('button')) as HTMLButtonElement;
          secret.textContent = field.pointer ? `管理秘密参数 ${field.pointer}` : '按位置管理秘密参数';
          secret.addEventListener('click', () => {
            secret.disabled = true;
            void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
              this.quickInput, this.notifications, bindingValue, null, false, field, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
          });
        }
      }
      if (status?.revision && plugin.runtime && binding?.enabled && !otherVersion && status.binding?.package_version === manifest.version) {
        const restart = append(actions, $('button')) as HTMLButtonElement;
        restart.textContent = '重启实例';
        restart.addEventListener('click', () => {
          restart.disabled = true;
          void this.runTargetOperation(async lifecycle => {
            lifecycle.check();
            await gateway.request('/api/gateway/service-plugins/restart', { method: 'POST', body: JSON.stringify({ gateway_id: target.gateway_id,
              gateway_connection_id: target.gateway_connection_id, package_id: manifest.package_id, package_version: manifest.version,
              plugin_id: plugin.id, revision: status.revision, ...(workspace ? { workspace_id: workspace.workspace_id } : {}) }) });
          }, entryGatewayId);
        });
      }
    };
    this.updateDetailsStatus = () => {
      if (scope === 'defaults') return;
      const nextBinding = this.configuredBinding(manifest.package_id, plugin.id, workspace?.workspace_id);
      const nextStatus = this.runtimeSlot(manifest.package_id, plugin.id, workspace?.workspace_id);
      identity.textContent = `${ownerLabel}${nextStatus?.instance ? ` · 实例 ${nextStatus.instance.instance_id}` : ''}`;
      if (status?.log_id !== nextStatus?.log_id) {
        clearNode(instanceLog);
        this.renderInstanceLog(instanceLog, target, nextStatus?.log_id);
      }
      // 只在动作依据变化时重建；兄弟实例或进度更新不打断已开始的输入和按钮状态。
      const actionsChanged = binding?.enabled !== nextBinding?.enabled || binding?.package_version !== nextBinding?.package_version
        || status?.revision !== nextStatus?.revision;
      binding = nextBinding; status = nextStatus;
      if (actionsChanged) renderActions(binding, status);
    };
    renderActions(binding, status);
    if (scope !== 'defaults' && plugin.contributes?.configuration) {
      const schema = append(details, $('pre'));
      schema.setAttribute('aria-label', '此安装版本的参数 schema');
      schema.textContent = JSON.stringify(plugin.contributes.configuration, null, 2);
    }
  }
}

async function readPluginInstanceLog(target: PluginTarget, logId: string): Promise<string> {
  const query = new URLSearchParams({ log_id: logId, tail_lines: '300' });
  if (target.gateway_connection_id) query.set('gateway_connection_id', target.gateway_connection_id);
  const diagnostics = await gateway.request<GatewayDiagnosticsDTO>(`/api/gateway/diagnostics?${query}`, {
    headers: { 'X-BoxTeam-Target-Gateway-Id': target.gateway_id },
  });
  if (diagnostics.gateway_id !== target.gateway_id || diagnostics.selected_log_id !== logId) throw new Error('实例日志回执不属于原 Gateway 或日志');
  const log = diagnostics.logs.find(item => item.log_id === logId);
  if (!log) throw new Error('原实例日志不在目标诊断回执中');
  if (log.status === 'empty') return '当前实例日志为空。';
  if (log.status !== 'available') throw new Error(log.error ?? '原实例日志当前不可读');
  return `${log.truncated ? '仅显示最近的日志尾部。\n' : ''}${log.tail}`;
}

async function installPackageFromView(quickInput: IQuickInputService, notifications: INotificationService,
  progress: IProgressService, target: PluginTarget, expectedEntryGatewayId?: string): Promise<void> {
  target = { ...target };
  const lifecycle = createManagementTargetLifecycle('原 Gateway 或访问凭据已改变，请重新打开插件安装', expectedEntryGatewayId);
  const { check, signal, token } = lifecycle;
  try {
    const type = await quickInput.pick([{ label: 'ZIP 文件', kind: 'zip' }, { label: '开发目录', kind: 'directory' },
      { label: '自有来源', kind: 'source' }, { label: '查询安装操作', kind: 'query' }],
      { title: `插件安装 · ${pluginGatewayLabel(target)}`, canPickMany: false }, token);
    check();
    if (!type) return;
    if (type.kind === 'query') {
      const operationId = await quickInput.input({ title: '输入原安装操作 ID', prompt: target.gateway_id }, token);
      check();
      if (operationId) await observeInstallation(progress, notifications, target, operationId);
      return;
    }
    let acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>;
    if (type.kind === 'source') {
      const inventory = await gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory',
        { method: 'POST', body: JSON.stringify(target), signal });
      check();
      const sources = inventory.sources?.entries.filter(source => source.enabled) ?? [];
      if (!sources.length) { notifications.info(inventory.sources?.message ?? '插件源尚未配置'); return; }
      const source = await quickInput.pick(sources.map(source => ({ label: source.source_id, source })),
        { title: '选择自有来源', canPickMany: false }, token);
      check();
      if (!source) return;
      const query = await quickInput.input({ title: '搜索自有插件包', prompt: source.source.source_id }, token);
      check();
      if (query === undefined) return;
      let cursor: string | undefined, selected: PluginSourceCatalogDTO['items'][number] | undefined, revision: string | undefined;
      while (!selected) {
        const page = await gateway.request<PluginSourceCatalogDTO>('/api/gateway/service-plugins/source-catalog',
          { method: 'POST', body: JSON.stringify({ ...target, source_id: source.source.source_id, query, cursor }), signal });
        check();
        if (page.gateway_id !== target.gateway_id || page.source_id !== source.source.source_id) {
          throw new Error('插件来源目录回执不属于原 Gateway 或 source_id');
        }
        if (revision !== undefined && page.catalog_revision !== revision) {
          throw new Error(`插件目录分页期间版本已变化：${revision} → ${page.catalog_revision}；请重新查询`);
        }
        revision = page.catalog_revision;
        const items = page.items.map(artifact => ({ label: `${artifact.package_id}@${artifact.package_version}`,
          description: artifact.target ? `${artifact.target.os}/${artifact.target.architecture} · ${artifact.target.system_baseline}` : '纯声明插件包', artifact }));
        const choice = await quickInput.pick([...items, ...(page.next_cursor ? [{ label: '下一页', description: '', artifact: undefined }] : [])],
          { title: items.length ? '选择确切版本与目标制品' : '此页无匹配插件包', canPickMany: false }, token);
        check();
        if (!choice) return;
        if (choice.artifact) selected = choice.artifact;
        else cursor = page.next_cursor;
      }
      if (!revision) throw new Error('插件来源目录缺少 catalog_revision');
      acquisition = { source_id: source.source.source_id, catalog_revision: revision, artifact: selected,
        development_directory: false, path: '' };
    } else {
      const path = await quickInput.input({ title: '输入目标 Gateway 上的原生绝对路径', prompt: pluginGatewayLabel(target),
        placeHolder: type.kind === 'directory' ? '/path/to/plugin-directory' : '/path/to/plugin.zip', validateInput: async value =>
          value.trim() && !value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value) && !/^\\\\/.test(value) ? '请输入目标机器原生绝对路径' : null }, token);
      check();
      if (!path) return;
      acquisition = { path, development_directory: type.kind === 'directory', artifact: undefined };
    }
    check();
    await runPluginInstallation(target, acquisition, notifications, async () => {
      // 受理前核最后一次资格；受理后只追踪原操作，不撤销或重放安装。
      check();
      const operationId = generateUuid();
      let operation: PluginInstallOperationDTO;
      try {
        operation = requireInstallationOwner(target, operationId, await gateway.request<PluginInstallOperationDTO>(
          '/api/gateway/service-plugins/install',
          { method: 'POST', body: JSON.stringify({ ...target, ...acquisition, operation_id: operationId }) }));
      } catch (error) {
        if (error instanceof GatewayRequestError && error.status >= 400 && error.status < 500 && error.status !== 408) throw error;
        // 明确拒绝保留原原因；响应未确认时仅查询原操作，不能另发安装。
        try { operation = await readInstallation(target, operationId); }
        catch (queryError) {
          throw new Error(`安装响应未确认：${errorsText(error)}。原操作查询失败：${errorsText(queryError)}。请查询原操作 ${operationId}（Gateway ${target.gateway_id}）。`);
        }
      }
      await observeInstallation(progress, notifications, target, operationId, operation);
    });
  } finally {
    lifecycle.dispose();
  }
}

function installationKey(target: PluginTarget, acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>): string {
  const artifact = acquisition.artifact;
  return JSON.stringify([
    target.gateway_id, target.gateway_connection_id ?? null,
    acquisition.source_id ?? null, acquisition.catalog_revision ?? null,
    artifact ?? [acquisition.development_directory, acquisition.path, acquisition.expected_digest ?? null],
  ]);
}

async function runPluginInstallation(target: PluginTarget,
  acquisition: Omit<InstallPluginRequest, keyof PluginTarget | 'operation_id'>,
  notifications: INotificationService, submit: () => Promise<void>): Promise<void> {
  const key = installationKey(target, acquisition);
  let pending = activePluginInstallations.get(key);
  if (!pending) {
    pending = Promise.resolve().then(submit);
    activePluginInstallations.set(key, pending);
  } else {
    notifications.info('相同插件制品的原安装操作仍在进行，正在等待该操作');
  }
  try { await pending; }
  finally {
    if (activePluginInstallations.get(key) === pending) activePluginInstallations.delete(key);
  }
}

async function readInstallation(target: PluginTarget, operationId: string): Promise<PluginInstallOperationDTO> {
  const operation = await gateway.request<PluginInstallOperationDTO>('/api/gateway/service-plugins/install-operation',
    { method: 'POST', body: JSON.stringify({ ...target, operation_id: operationId }) });
  return requireInstallationOwner(target, operationId, operation);
}

function installationFailureText(target: PluginTarget, operation: PluginInstallOperationDTO): string {
  const artifact = operation.artifact;
  const status = operation.status === 'cancelled' ? '已取消'
    : operation.status === 'unknown' ? '结果未知' : '失败';
  if (!artifact) {
    return `插件安装${status}，但 Gateway 回执缺少必需制品详情（artifact）`
      + `\n目标原生 Gateway：${target.gateway_id}${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''}`
      + `\n原操作：${operation.operation_id}\nGateway 返回错误或环境详情：${operation.error?.trim() || 'Gateway 未返回详情。'}`
      + '\n请重新查询原操作；若仍缺少制品详情，检查目标 Gateway 的安装操作记录。';
  }
  const packageIdentity = `${artifact.package_id}@${artifact.package_version}`;
  const runtimeTarget = artifact.target;
  const requiredEnvironment = runtimeTarget
    ? `${runtimeTarget.os}/${runtimeTarget.architecture} · ${runtimeTarget.system_baseline}`
      + (runtimeTarget.node ? ` · Node ${runtimeTarget.node.version}/ABI ${runtimeTarget.node.abi}` : '')
      + (runtimeTarget.python ? ` · Python ${runtimeTarget.python.version}/ABI ${runtimeTarget.python.abi}` : '')
    : '未声明代码运行环境';
  const error = operation.error?.trim();
  const nextStep = operation.status === 'unknown'
    ? '先检查目标 Gateway 当前插件包，再查询此原操作；确认发布状态前不要重新安装，不会自动重放。'
    : operation.status === 'cancelled'
      ? '确认目标 Gateway 未发布此版本后，可以重新安装。'
      : '请根据 Gateway 错误修复问题后，再对该包版本重新安装。';
  return `插件安装${status}：${packageIdentity}\n目标原生 Gateway：${target.gateway_id}`
    + `${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''}`
    + `\n原操作：${operation.operation_id}\n所需运行环境：${requiredEnvironment}`
    + `\nGateway 返回错误或环境详情：${error || 'Gateway 未返回详情。'}\n${nextStep}`;
}

async function observeInstallation(progress: IProgressService, notifications: INotificationService,
  target: PluginTarget, operationId: string, initial?: PluginInstallOperationDTO): Promise<void> {
  await progress.withProgress({ location: ProgressLocation.Notification,
    title: `安装插件包 · ${pluginGatewayLabel(target)} · ${operationId}`, cancellable: true }, async reporter => {
    const deadline = performance.now() + DEFAULT_API_REQUEST_TIMEOUT_MS;
    let operation = initial ?? await readInstallation(target, operationId);
    while (operation.status === 'pending' || operation.status === 'running') {
      reporter.report({ message: operation.cancel_requested ? '正在结清取消请求…' : '正在安装…' });
      if (performance.now() >= deadline) throw new Error(`等待安装结果超时，请查询原操作 ${operationId}；不会重新安装。`);
      await timeout(300);
      operation = await readInstallation(target, operationId);
    }
    if (operation.status !== 'succeeded') throw new Error(installationFailureText(target, operation));
    if (!operation.artifact?.package_id || !operation.artifact.package_version) {
      throw new Error(`安装成功回执缺少制品身份，无法确认目标包版本：${pluginGatewayLabel(target)} · ${operationId}`);
    }
    notifications.info(`已在 ${pluginGatewayLabel(target)} 安装 ${operation.artifact.package_id}@${operation.artifact.package_version}`);
  }, () => { void gateway.request('/api/gateway/service-plugins/cancel-install',
    { method: 'POST', body: JSON.stringify({ ...target, operation_id: operationId }) })
    .catch(error => notifications.error(`取消未确认，请查询原操作 ${operationId}：${errorsText(error)}`)); });
}

export async function installServicePluginPackage(accessor: ServicesAccessor, explicitTarget?: PluginTarget): Promise<void> {
  const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
  const progress = accessor.get(IProgressService);
  const frozen = explicitTarget ? { ...explicitTarget } : undefined;
  await workspaceContext.ensureReady();
  const target = frozen ?? managementGateway('extensions');
  await installPackageFromView(quickInput, notifications, progress,
    { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id });
}

async function uninstallPackageFromView(quickInput: IQuickInputService, notifications: INotificationService, target: PluginTarget,
  packageId: string, version: string, lifecycle: ReturnType<typeof createManagementTargetLifecycle>): Promise<void> {
  target = { ...target };
  lifecycle.check();
  const confirm = await quickInput.pick([{ label: '取消', accept: false }, { label: '卸载整包', accept: true,
    description: `${packageId}@${version} · ${pluginGatewayLabel(target)} · 有原 owner 引用时会拒绝并显示引用` }],
  { title: `卸载插件包 · ${pluginGatewayLabel(target)}`, canPickMany: false }, lifecycle.token);
  lifecycle.check();
  if (!confirm?.accept) return;
  const result = await gateway.request<PluginUninstallResultDTO>('/api/gateway/service-plugins/uninstall', {
    method: 'POST', body: JSON.stringify({ ...target, package_id: packageId, package_version: version }),
  });
  if (!result.uninstalled || result.package_id !== packageId || result.package_version !== version) {
    throw new Error(`卸载回执未确认所选包版本：${pluginGatewayLabel(target)} · ${packageId}@${version}`);
  }
  notifications.info(`已从 ${pluginGatewayLabel(target)} 卸载 ${packageId}@${version}`);
}
