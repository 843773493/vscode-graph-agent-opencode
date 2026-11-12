import { t } from '../../../shared/core/i18n/index.js';
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

import { manageConfig, setPluginBindings, readPluginBindings, choosePluginWorkspace, writeOnlyFields, type Scope } from '../settings/pluginConfiguration.js';

type ConfigurationBinding = Omit<PluginBinding, 'workspace_id'>;
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);
const activePluginInstallations = new Map<string, Promise<void>>();
const pluginGatewayLabel = (target: PluginTarget) => t('plugins.nativeGateway', target.gateway_id, target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : '');

function pluginStateLabel(state: string): string {
  switch (state) {
    case 'stopped': return t('plugins.state.stopped');
    case 'starting': return t('plugins.state.starting');
    case 'ready': return t('plugins.state.ready');
    case 'stopping': return t('plugins.state.stopping');
    case 'failed': return t('plugins.state.failed');
    case 'waiting': return t('plugins.state.waiting');
    default: return state;
  }
}

function requireInstallationOwner(target: PluginTarget, operationId: string, operation: PluginInstallOperationDTO): PluginInstallOperationDTO {
  if (operation.gateway_id !== target.gateway_id || operation.operation_id !== operationId) {
    throw new Error(t('plugins.installationOwnerMismatch', operationId));
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
  private configuredBindings: readonly PluginBinding[] | undefined;
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
    this.searchInput.setAttribute('aria-label', t('plugins.searchInstalled'));
    this.searchInput.placeholder = t('plugins.searchInstalled');
    this.searchInput.value = this.query;
    this.searchInput.addEventListener('input', () => void this.commands.executeCommand('workbench.extensions.search', this.searchInput!.value)
      .catch(error => this.notifications.error(errorsText(error))));
    this.controls = append(container, $('div'));
    const selector = append(this.controls, $('select')) as HTMLSelectElement;
    selector.setAttribute('aria-label', t('plugins.managementScope'));
    for (const [value, key] of [['gateway', 'plugins.scopeGateway'], ['workspace', 'plugins.scopeWorkspace'], ['defaults', 'plugins.scopeDefaults']] as const) {
      const option = append(selector, $('option')) as HTMLOptionElement;
      option.value = value;
      option.textContent = t(key);
    }
    append(this.controls, $('p')).textContent = t('plugins.defaultsExplanation');
    selector.addEventListener('change', () => {
      if (selector.value === 'gateway' || selector.value === 'workspace' || selector.value === 'defaults') this.scope = selector.value;
      this.workspaceTarget = undefined;
      this.selected = undefined;
      this.refreshRevision++;
      void this.refresh();
    });
    const install = append(this.controls, $('button'));
    install.textContent = t('plugins.installPackage');
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
        throw new Error(t('plugins.managementTargetChanged'));
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
    if (!this.content?.querySelector('.service-plugin-details')) throw new Error(t('plugins.versionUnavailable', packageId, pluginId, version));
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
    this.configuredBindings = undefined;
    this.updateDetailsStatus = undefined;
    super.dispose();
  }

  async refresh(): Promise<void> {
    if (!this.content || !this.isBodyVisible() || this.content.hidden) return;
    this.stopRuntimeStatus();
    const revision = ++this.refreshRevision;
    const scope = this.scope;
    const container = this.content;
    this.configuredBindings = undefined;
    this.updateDetailsStatus = undefined;
    clearNode(container);
    append(container, $('p')).textContent = t('plugins.loading');
    try {
      await this.renderPackages(container, revision, scope);
    } catch (error) {
      if (revision !== this.refreshRevision || this.content !== container) return;
      this.stopRuntimeStatus();
      if (!this.isBodyVisible() || container.hidden) return;
      clearNode(container);
      append(container, $('p')).textContent = errorsText(error);
      const retry = append(container, $('button'));
      retry.textContent = t('plugins.retry');
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
      const lifecycle = createManagementTargetLifecycle(t('plugins.operationTargetChanged'), expectedEntryGatewayId);
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
      const previous = this.runtimeStatus;
      this.runtimeStatus = snapshot;
      void first.complete(snapshot);
      if (previous) {
        const workspaceId = this.scope === 'workspace' ? this.workspaceTarget?.workspace_id : undefined;
        const bindings = (state: PluginRuntimeStatusDTO) => state.configured_bindings
          .filter(binding => (binding.workspace_id ?? undefined) === workspaceId);
        // 外部配置提交后重读原来源；实例进度和兄弟工作区事件不触发额外请求。
        if (JSON.stringify(bindings(previous)) !== JSON.stringify(bindings(snapshot))) {
          void this.refresh();
          return;
        }
      }
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
      throw new Error(t('plugins.runtimeSnapshotTimeout'));
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
      workspace.textContent = t('plugins.workspaceHandoff', workspace.dataset.workspaceId, state ? pluginStateLabel(state.state) : t('plugins.notReceived'), state?.error ? ` · ${state.error}` : '');
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
        versions.textContent = t('plugins.configurationPrefix', this.runtimeConfigurationLabel(packageId, pluginId,
          details.dataset.workspaceId || undefined))
          + this.runtimeActualLabel(status, details.dataset.runtime === 'true');
      }
      const handoff = details.querySelector<HTMLElement>('[data-plugin-workspace-handoff]');
      if (handoff) {
        const state = this.runtimeStatus.workspace_configurations.find(item => item.workspace_id === handoff.dataset.workspaceId);
        handoff.textContent = t('plugins.workspaceHandoff', handoff.dataset.workspaceId, state ? pluginStateLabel(state.state) : t('plugins.notReceived'), state?.error ? ` · ${state.error}` : '');
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
    // 配置期望统一来自原来源 owner；运行状态流的 active 绑定不能覆盖 pending。
    return this.configuredBindings?.find(item => item.package_id === packageId
      && item.plugin_id === pluginId && (item.workspace_id ?? undefined) === workspaceId);
  }

  private runtimeConfigurationLabel(packageId: string, pluginId: string, workspaceId: string | undefined): string {
    const binding = this.configuredBinding(packageId, pluginId, workspaceId);
    return binding ? `${binding.enabled ? t('plugins.enabled') : t('plugins.disabled')} @${binding.package_version}` : t('plugins.unbound');
  }

  private runtimeLabel(packageId: string, pluginId: string, workspaceId: string | undefined,
    status: PluginRuntimeStatusDTO['plugins'][number] | undefined, runtime: boolean): string {
    return `${this.runtimeConfigurationLabel(packageId, pluginId, workspaceId)} · ${this.runtimeActualLabel(status, runtime)}`;
  }

  private runtimeActualLabel(status: PluginRuntimeStatusDTO['plugins'][number] | undefined, runtime: boolean): string {
    return status?.instance
      ? t('plugins.actualVersion', pluginStateLabel(status.state), status.instance.package_version)
      : runtime ? t('plugins.actualNotReady', status ? pluginStateLabel(status.state) : t('plugins.notRunning')) : t('plugins.declarative');
  }

  private async renderPackages(container: HTMLElement, revision: number, scope: Scope): Promise<void> {
    await workspaceContext.ensureReady();
    if (revision !== this.refreshRevision) return;
    const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!entryGatewayId) throw new Error(t('plugins.gatewayNotReady'));
    const target = managementGateway('extensions');
    const workspace = scope === 'workspace' ? (this.workspaceTarget ?? await choosePluginWorkspace(target, this.quickInput)) : undefined;
    if (revision !== this.refreshRevision) return;
    this.workspaceTarget = workspace;
    if (scope === 'workspace' && !workspace) { clearNode(container); append(container, $('p')).textContent = t('plugins.chooseWorkspaceFirst'); return; }
    const pluginTarget: PluginTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
    const [inventory, runtime, scopeBindings] = await Promise.all([
      gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      scope === 'defaults' ? undefined : this.observeRuntimeStatus(pluginTarget),
      readPluginBindings(scope, workspace ?? target),
    ]);
    if (this.content !== container || revision !== this.refreshRevision) return;
    this.configuredBindings = scopeBindings;
    clearNode(container);
    append(container, $('p')).textContent = inventory.sources?.message ?? t('plugins.sourcesNotConfigured');
    if (workspace) {
      const handoff = runtime!.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      const summary = append(container, $('p'));
      summary.setAttribute('aria-label', t('plugins.workspaceHandoffAria'));
      summary.dataset.pluginWorkspaceHandoff = 'true';
      summary.dataset.workspaceId = workspace.workspace_id;
      summary.textContent = t('plugins.workspaceHandoff', workspace.workspace_id, handoff ? pluginStateLabel(handoff.state) : t('plugins.notReceived'), handoff?.error ? ` · ${handoff.error}` : '');
    }
    const pluginScope = scope === 'gateway' ? 'gateway' : 'workspace';
    const bindings = scopeBindings.filter(item => scope === 'workspace'
      ? item.workspace_id === workspace?.workspace_id : item.workspace_id == null);
    for (const pkg of inventory.packages) {
      const manifest = pkg.manifest;
      if (!manifest) continue;
      const matching = manifest.plugins.filter(plugin => plugin.instance_scope === pluginScope);
      const packageSection = append(container, $('section.service-plugin-package'));
      packageSection.dataset.search = `${manifest.package_id} ${manifest.name} ${manifest.description ?? ''} ${manifest.plugins.map(plugin => `${plugin.id} ${plugin.name}`).join(' ')}`.toLocaleLowerCase();
      append(packageSection, $('h3')).textContent = `${manifest.name} · ${manifest.package_id}@${manifest.version}`;
      append(packageSection, $('p')).textContent = manifest.description ?? '';
      if (!matching.length) append(packageSection, $('p')).textContent = t('plugins.noMatchingScope', manifest.plugins.map(plugin => t('plugins.scopeEntry', plugin.id, plugin.instance_scope)).join(t('plugins.listSeparator')));
      const packageActions = append(packageSection, $('.service-plugin-package-actions'));
      const batchButtons = matching.length ? [true, false].map(enabled => {
        const button = append(packageActions, $('button')) as HTMLButtonElement;
        button.textContent = enabled ? t('plugins.enableAll') : t('plugins.disableAll');
        button.addEventListener('click', () => void this.runTargetOperation(async lifecycle => {
          batchButtons.forEach(item => { item.disabled = true; });
          try {
            const batchBindings = matching.map(plugin => ({ package_id: manifest.package_id, plugin_id: plugin.id,
              package_version: this.configuredBinding(manifest.package_id, plugin.id, workspace?.workspace_id)?.package_version
                ?? manifest.version, enabled }));
            const excluded = manifest.plugins.filter(plugin => plugin.instance_scope !== pluginScope);
            const owner = `Gateway ${target.gateway_id}${target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : ''} · ${scope}${workspace ? t('plugins.workspaceSuffix', workspace.workspace_id) : ''}`;
            const confirmation = await this.dialogs.confirm({ message: t('plugins.confirmBatch', enabled ? t('plugins.enableAction') : t('plugins.disableAction'), manifest.package_id),
              detail: t('plugins.batchDetail', owner, matching.map(plugin => plugin.id).join(t('plugins.listSeparator')), batchBindings.map(binding => `${binding.plugin_id}@${binding.package_version}`).join(t('plugins.listSeparator')), excluded.map(plugin => t('plugins.scopeEntry', plugin.id, plugin.instance_scope)).join(t('plugins.listSeparator')) || t('plugins.none'))
                + (scope === 'defaults' ? t('plugins.defaultsOnly') : t('plugins.independentCommits')),
              primaryButton: enabled ? t('plugins.enableAll') : t('plugins.disableAll') });
            if (!confirmation.confirmed) return;
            lifecycle.check();
            const results = await setPluginBindings(scope, target, workspace, this.quickInput,
              batchBindings,
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
              const state = scope === 'defaults' ? t('plugins.defaultsChanged') : readError ? t('plugins.runtimeReadFailed', readError)
                : actual ? `${actual.binding?.enabled ? t('plugins.enabled') : t('plugins.disabled')} · ${actual.declarative ? t('plugins.declarative') : pluginStateLabel(actual.state)}${actual.error ? ` · ${actual.error}` : ''}${actual.instance ? t('plugins.instanceSuffix', actual.instance.instance_id) : ''}` : t('plugins.runtimeUnpublished');
              return t('plugins.batchResult', owner, item.package_id, item.plugin_id, item.package_version, configuration, state);
            });
            await this.dialogs.info(t('plugins.batchResultsTitle'), lines.join('\n\n'));
          } finally { batchButtons.forEach(item => { item.disabled = false; }); }
        }, entryGatewayId));
        return button;
      }) : [];
      const installInput = append(packageActions, $('button'));
      installInput.textContent = t('plugins.reinstall');
      installInput.addEventListener('click', () => void this.runOperation(() => installPackageFromView(
        this.quickInput, this.notifications, this.progress, pluginTarget, entryGatewayId)));
      const uninstall = append(packageActions, $('button')) as HTMLButtonElement;
      uninstall.textContent = t('plugins.uninstallWholePackage');
      uninstall.title = t('plugins.uninstallReferences');
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
          ? t('plugins.defaultRuntimeValue', binding?.enabled ? t('plugins.enabledOnCreate') : binding ? t('plugins.disabledOnCreate') : t('plugins.notInDefaults'), binding ? ` @${binding.package_version}` : '')
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
      section.setAttribute('aria-label', t('plugins.missingManifestAria'));
      append(section, $('h3')).textContent = `${binding.package_id}/${binding.plugin_id}@${binding.package_version}`;
      append(section, $('p')).textContent = `${pluginGatewayLabel(target)} · ${scope}${workspace ? t('plugins.workspaceSuffix', workspace.workspace_id) : ''}`;
      append(section, $('p')).textContent = t('plugins.missingManifest');
      const row = append(section, $('article'));
      row.dataset.pluginRuntimeRow = 'true';
      row.dataset.packageId = binding.package_id;
      row.dataset.pluginId = binding.plugin_id;
      row.dataset.workspaceId = workspace?.workspace_id ?? '';
      row.dataset.scope = scope;
      // 未取得原声明时不能将插件误报为纯声明插件。
      row.dataset.runtime = 'true';
      row.dataset.initialRuntimeValue = t('plugins.defaultRuntimeVersion', binding.enabled ? t('plugins.enabledOnCreate') : t('plugins.disabledOnCreate'), binding.package_version);
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
      install.textContent = t('plugins.installMissing');
      install.addEventListener('click', () => void this.runOperation(() => installPackageFromView(
        this.quickInput, this.notifications, this.progress, pluginTarget, entryGatewayId)));
      const unbind = append(section, $('button')) as HTMLButtonElement;
      unbind.textContent = scope === 'defaults' ? t('plugins.removeFromDefaults') : t('plugins.unbindScope');
      unbind.addEventListener('click', () => {
        unbind.disabled = true;
        void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace, this.quickInput, this.notifications,
          binding, null, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
      });
    }
    this.search(this.query);
    this.updateRuntimeStatusPresentation();
    if (!inventory.packages.length && !bindings.length) append(container, $('p')).textContent = t('plugins.noPackages', inventory.sources?.message ?? '');
  }

  private renderInstanceLog(container: HTMLElement, target: PluginTarget, logId: string | undefined): void {
    if (!logId) return;
    const logs = append(container, $('button')) as HTMLButtonElement;
    logs.textContent = t('plugins.viewInstanceLog');
    const output = append(container, $('pre'));
    output.setAttribute('aria-label', t('plugins.instanceLogAria'));
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
      ? t('plugins.defaultNoGatewayRuntime', plugin.instance_scope)
      : this.runtimeLabel(manifest.package_id, plugin.id, scope === 'workspace' ? workspace?.workspace_id : undefined,
          status, Boolean(plugin.runtime));
    append(details, $('h3')).textContent = `${manifest.package_id}/${plugin.id}`;
    const detailsState = append(details, $('p'));
    detailsState.dataset.pluginRuntimeValue = 'true';
    detailsState.textContent = details.dataset.initialRuntimeValue;
    const identity = append(details, $('p'));
    const ownerLabel = `Gateway ${target.gateway_id}${target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : ''}${workspace ? t('plugins.workspaceSuffix', workspace.workspace_id) : ''} · ${manifest.version}`;
    identity.textContent = `${ownerLabel}${status?.instance ? t('plugins.instanceSuffix', status.instance.instance_id) : ''}`;
    if (workspace) {
      const handoff = append(details, $('p'));
      handoff.setAttribute('aria-label', t('plugins.workspaceHandoffAria'));
      handoff.dataset.pluginWorkspaceHandoff = 'true';
      handoff.dataset.workspaceId = workspace.workspace_id;
      const state = this.runtimeStatus?.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      handoff.textContent = t('plugins.workspaceHandoff', workspace.workspace_id, state ? pluginStateLabel(state.state) : t('plugins.notReceived'), state?.error ? ` · ${state.error}` : '');
    }
    const versions = append(details, $('p'));
    versions.setAttribute('aria-label', t('plugins.versionsAria'));
    versions.dataset.pluginRuntimeVersion = 'true';
    versions.textContent = t('plugins.configurationPrefix', binding ? `${binding.enabled ? t('plugins.enabled') : t('plugins.disabled')} @${binding.package_version}` : t('plugins.unbound'))
      + (scope === 'defaults' ? t('plugins.defaultsNoRuntime')
        : this.runtimeActualLabel(status, Boolean(plugin.runtime)));
    const failure = append(details, $('p'));
    failure.setAttribute('aria-label', t('plugins.runtimeFailureAria'));
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
        switchVersion.textContent = t('plugins.switchVersion');
        switchVersion.addEventListener('click', () => {
          switchVersion.disabled = true;
          void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace, this.quickInput, this.notifications,
            bindingValue, 'preserve', false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
        });
      } else {
        const toggle = append(actions, $('button'));
        toggle.textContent = scope === 'defaults'
          ? binding?.enabled ? t('plugins.disableOnCreate') : t('plugins.enableOnCreate')
          : binding?.enabled ? t('plugins.disableScope') : t('plugins.enableScope');
        toggle.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
          this.quickInput, this.notifications, bindingValue, !binding?.enabled, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
      }
      if (binding && !otherVersion) {
        const unbind = append(actions, $('button'));
        unbind.textContent = scope === 'defaults' ? t('plugins.removeFromDefaults') : t('plugins.unbindScope');
        unbind.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
          this.quickInput, this.notifications, bindingValue, null, false, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
      }
      if (scope !== 'defaults') {
        append(actions, $('p')).textContent = binding
          ? t('plugins.currentBinding', manifest.package_id, plugin.id, binding.package_version)
          : t('plugins.unboundParameters');
        if (otherVersion) append(actions, $('p')).textContent = t('plugins.switchBeforeParameters');
        else {
          const parameters = append(actions, $('button'));
          parameters.textContent = t('plugins.editParametersJson');
          parameters.addEventListener('click', () => void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
            this.quickInput, this.notifications, bindingValue, null, true, undefined, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId));
        }
        const privateFields = otherVersion ? [] : writeOnlyFields(plugin.contributes?.configuration);
        for (const field of privateFields) {
          const secret = append(actions, $('button')) as HTMLButtonElement;
          secret.textContent = field.pointer ? t('plugins.manageSecret', field.pointer) : t('plugins.manageSecretLocation');
          secret.addEventListener('click', () => {
            secret.disabled = true;
            void this.runTargetOperation(lifecycle => manageConfig(scope, target, workspace,
              this.quickInput, this.notifications, bindingValue, null, false, field, lifecycle.check, lifecycle.token, lifecycle.signal), entryGatewayId);
          });
        }
      }
      if (status?.revision && plugin.runtime && binding?.enabled && !otherVersion && status.binding?.package_version === manifest.version) {
        const restart = append(actions, $('button')) as HTMLButtonElement;
        restart.textContent = t('plugins.restartInstance');
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
      identity.textContent = `${ownerLabel}${nextStatus?.instance ? t('plugins.instanceSuffix', nextStatus.instance.instance_id) : ''}`;
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
      schema.setAttribute('aria-label', t('plugins.parameterSchemaAria'));
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
  if (diagnostics.gateway_id !== target.gateway_id || diagnostics.selected_log_id !== logId) throw new Error(t('plugins.logOwnerMismatch'));
  const log = diagnostics.logs.find(item => item.log_id === logId);
  if (!log) throw new Error(t('plugins.logMissing'));
  if (log.status === 'empty') return t('plugins.logEmpty');
  if (log.status !== 'available') throw new Error(log.error ?? t('plugins.logUnreadable'));
  return `${log.truncated ? t('plugins.logTruncated') : ''}${log.tail}`;
}

async function installPackageFromView(quickInput: IQuickInputService, notifications: INotificationService,
  progress: IProgressService, target: PluginTarget, expectedEntryGatewayId?: string): Promise<void> {
  target = { ...target };
  const lifecycle = createManagementTargetLifecycle(t('plugins.installationTargetChanged'), expectedEntryGatewayId);
  const { check, signal, token } = lifecycle;
  try {
    const type = await quickInput.pick([...(target.gateway_id === lifecycle.entryGatewayId
      ? [{ label: t('plugins.zipFile'), kind: 'zip' }, { label: t('plugins.developmentDirectory'), kind: 'directory' }] : []),
      { label: t('plugins.customSource'), kind: 'source' }, { label: t('plugins.queryInstallation'), kind: 'query' }],
      { title: t('plugins.installationTitle', pluginGatewayLabel(target)), canPickMany: false }, token);
    check();
    if (!type) return;
    if (type.kind === 'query') {
      const operationId = await quickInput.input({ title: t('plugins.operationIdPrompt'), prompt: target.gateway_id }, token);
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
      if (!sources.length) { notifications.info(inventory.sources?.message ?? t('plugins.sourcesNotConfigured')); return; }
      const source = await quickInput.pick(sources.map(source => ({ label: source.source_id, source })),
        { title: t('plugins.chooseSource'), canPickMany: false }, token);
      check();
      if (!source) return;
      const query = await quickInput.input({ title: t('plugins.searchPackages'), prompt: source.source.source_id }, token);
      check();
      if (query === undefined) return;
      let cursor: string | undefined, selected: PluginSourceCatalogDTO['items'][number] | undefined, revision: string | undefined;
      while (!selected) {
        const page = await gateway.request<PluginSourceCatalogDTO>('/api/gateway/service-plugins/source-catalog',
          { method: 'POST', body: JSON.stringify({ ...target, source_id: source.source.source_id, query, cursor }), signal });
        check();
        if (page.gateway_id !== target.gateway_id || page.source_id !== source.source.source_id) {
          throw new Error(t('plugins.catalogOwnerMismatch'));
        }
        if (revision !== undefined && page.catalog_revision !== revision) {
          throw new Error(t('plugins.catalogRevisionChanged', revision, page.catalog_revision));
        }
        revision = page.catalog_revision;
        const items = page.items.map(artifact => ({ label: `${artifact.package_id}@${artifact.package_version}`,
          description: artifact.target ? `${artifact.target.os}/${artifact.target.architecture} · ${artifact.target.system_baseline}` : t('plugins.declarativePackage'), artifact }));
        const choice = await quickInput.pick([...items, ...(page.next_cursor ? [{ label: t('plugins.nextPage'), description: '', artifact: undefined }] : [])],
          { title: items.length ? t('plugins.chooseArtifact') : t('plugins.noMatchingPackages'), canPickMany: false }, token);
        check();
        if (!choice) return;
        if (choice.artifact) selected = choice.artifact;
        else cursor = page.next_cursor;
      }
      if (!revision) throw new Error(t('plugins.catalogRevisionMissing'));
      acquisition = { source_id: source.source.source_id, catalog_revision: revision, artifact: selected,
        development_directory: false, path: '' };
    } else {
      const path = await quickInput.input({ title: t('plugins.nativePathPrompt'), prompt: pluginGatewayLabel(target),
        placeHolder: type.kind === 'directory' ? '/path/to/plugin-directory' : '/path/to/plugin.zip', validateInput: async value =>
          value.trim() && !value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value) && !/^\\\\/.test(value) ? t('plugins.nativePathRequired') : null }, token);
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
          throw new Error(t('plugins.installationResponseUnknown', errorsText(error), errorsText(queryError), operationId, target.gateway_id));
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
    notifications.info(t('plugins.installationAlreadyPending'));
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
  const status = operation.status === 'cancelled' ? t('plugins.cancelled')
    : operation.status === 'unknown' ? t('plugins.unknown') : t('plugins.failed');
  if (!artifact) {
    return t('plugins.failureMissingArtifact', status)
      + t('plugins.targetGatewayLine', target.gateway_id, target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : '')
      + t('plugins.operationErrorLines', operation.operation_id, operation.error?.trim() || t('plugins.noGatewayDetails'))
      + t('plugins.queryMissingArtifact');
  }
  const packageIdentity = `${artifact.package_id}@${artifact.package_version}`;
  const runtimeTarget = artifact.target;
  const requiredEnvironment = runtimeTarget
    ? `${runtimeTarget.os}/${runtimeTarget.architecture} · ${runtimeTarget.system_baseline}`
      + (runtimeTarget.node ? ` · Node ${runtimeTarget.node.version}/ABI ${runtimeTarget.node.abi}` : '')
      + (runtimeTarget.python ? ` · Python ${runtimeTarget.python.version}/ABI ${runtimeTarget.python.abi}` : '')
    : t('plugins.noRuntimeDeclared');
  const error = operation.error?.trim();
  const nextStep = operation.status === 'unknown'
    ? t('plugins.unknownInstallationNext')
    : operation.status === 'cancelled'
      ? t('plugins.cancelledInstallationNext')
      : t('plugins.failedInstallationNext');
  return t('plugins.installationFailure', status, packageIdentity, target.gateway_id)
    + `${target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : ''}`
    + t('plugins.operationRuntimeLines', operation.operation_id, requiredEnvironment)
    + t('plugins.errorNextLines', error || t('plugins.noGatewayDetails'), nextStep);
}

async function observeInstallation(progress: IProgressService, notifications: INotificationService,
  target: PluginTarget, operationId: string, initial?: PluginInstallOperationDTO): Promise<void> {
  await progress.withProgress({ location: ProgressLocation.Notification,
    title: t('plugins.installProgressTitle', pluginGatewayLabel(target), operationId), cancellable: true }, async reporter => {
    const deadline = performance.now() + DEFAULT_API_REQUEST_TIMEOUT_MS;
    let operation = initial ?? await readInstallation(target, operationId);
    while (operation.status === 'pending' || operation.status === 'running') {
      reporter.report({ message: operation.cancel_requested ? t('plugins.settlingCancellation') : t('plugins.installing') });
      if (performance.now() >= deadline) throw new Error(t('plugins.installationTimeout', operationId));
      await timeout(300);
      operation = await readInstallation(target, operationId);
    }
    if (operation.status !== 'succeeded') throw new Error(installationFailureText(target, operation));
    if (!operation.artifact?.package_id || !operation.artifact.package_version) {
      throw new Error(t('plugins.successArtifactMissing', pluginGatewayLabel(target), operationId));
    }
    notifications.info(t('plugins.installationSucceeded', pluginGatewayLabel(target), operation.artifact.package_id, operation.artifact.package_version));
  }, () => { void gateway.request('/api/gateway/service-plugins/cancel-install',
    { method: 'POST', body: JSON.stringify({ ...target, operation_id: operationId }) })
    .catch(error => notifications.error(t('plugins.cancellationUnknown', operationId, errorsText(error)))); });
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
  const confirm = await quickInput.pick([{ label: t('plugins.cancel'), accept: false }, { label: t('plugins.uninstallPackage'), accept: true,
    description: t('plugins.uninstallConfirmationDetail', packageId, version, pluginGatewayLabel(target)) }],
  { title: t('plugins.uninstallTitle', pluginGatewayLabel(target)), canPickMany: false }, lifecycle.token);
  lifecycle.check();
  if (!confirm?.accept) return;
  const result = await gateway.request<PluginUninstallResultDTO>('/api/gateway/service-plugins/uninstall', {
    method: 'POST', body: JSON.stringify({ ...target, package_id: packageId, package_version: version }),
  });
  if (!result.uninstalled || result.package_id !== packageId || result.package_version !== version) {
    throw new Error(t('plugins.uninstallReceiptMismatch', pluginGatewayLabel(target), packageId, version));
  }
  notifications.info(t('plugins.uninstalled', pluginGatewayLabel(target), packageId, version));
}
