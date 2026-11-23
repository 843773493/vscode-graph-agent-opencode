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
import { IProgressService, ProgressLocation, type IProgressRunner } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { timeout } from '../../upstream/src/vs/base/common/async.js';
import { generateUuid } from '../../upstream/src/vs/base/common/uuid.js';
import type { InstallPluginRequest, PluginInstallOperationDTO, PluginInventoryResultDTO, PluginRuntimeStatusDTO, PluginSourceCatalogDTO, PluginTarget, PluginUninstallResultDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import { gateway, GatewayRequestError } from '../agentApi.js';
import { createManagementTargetLifecycle, managementGateway, managementTargetDisplayName, listManagementTargets, selectManagementTarget, onDidChangeExtensionManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';

import { manageConfig, setPluginBindings, type Scope } from '../settings/pluginConfiguration.js';

import { PluginScope, pluginActions, pluginStateLabel, pluginIcon, pluginDescription } from './pluginScope.js';
import { installedPluginGroups, type InstalledPlugin } from './pluginInventory.js';
import { PluginEditorInput, readPluginInstanceLog } from './pluginEditor.js';
import { IEditorService, SIDE_GROUP } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { SettingsEditor2Input } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.js';
import { captureSettingsLocation } from '../settings/editor.js';
import type { ISettingsEditorOptions } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { Action } from '../../upstream/src/vs/base/common/actions.js';
import { ActionBar } from '../../upstream/src/vs/base/browser/ui/actionbar/actionbar.js';
import { InputBox } from '../../upstream/src/vs/base/browser/ui/inputbox/inputBox.js';
import { SelectActionViewItem } from '../../upstream/src/vs/base/browser/ui/actionbar/actionViewItems.js';
import { IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { defaultInputBoxStyles, defaultSelectBoxStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { WorkbenchList } from '../../upstream/src/vs/platform/list/browser/listService.js';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extension.css';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extensionsWidgets.css';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extensionActions.css';
import './plugins.css';
const errorsText = (error: unknown) => error instanceof Error ? error.message : String(error);
const activePluginInstallations = new Map<string, Promise<void>>();
const pluginGatewayLabel = (target: PluginTarget) => t('plugins.nativeGateway', target.gateway_id, target.gateway_connection_id ? t('plugins.connectionSuffix', target.gateway_connection_id) : '');

/** 沿用原工具栏选择器；用索引保持同名目标身份，并同步范围的禁用态。 */
class ManagementTargetSelect extends SelectActionViewItem<number> {
  protected override getActionContext(_option: string, index: number): number { return index; }
  setEnabled(enabled: boolean): void { this.action.enabled = enabled; this.selectBox.setEnabled(enabled); }
}

function requireInstallationOwner(target: PluginTarget, operationId: string, operation: PluginInstallOperationDTO): PluginInstallOperationDTO {
  if (operation.gateway_id !== target.gateway_id || operation.operation_id !== operationId) {
    throw new Error(t('plugins.installationOwnerMismatch', operationId));
  }
  return operation;
}

/** 原 Extensions Viewlet 中展示 Gateway 自有插件模型，不伪造 IExtension 或 Gallery 对象。 */
export class ServicePluginPackagesView extends ViewPane {
  private scope: Scope = 'workspace';
  private content: HTMLElement | undefined;
  private feedback: HTMLElement | undefined;
  private workspaceTarget: GatewayWorkspaceRequestTarget | undefined;
  private refreshRevision = 0;
  private refreshProgress: IProgressRunner | undefined;
  private searchInput: InputBox | undefined;
  private query = '';
  private controls: HTMLElement | undefined;
  private state: PluginScope | undefined;
  private readonly packageActions = this._register(new DisposableStore());
  private readonly rememberedWorkspaces = new Map<string, string>();
  private gatewaySelector: ManagementTargetSelect | undefined;
  private workspaceSelector: ManagementTargetSelect | undefined;
  private selectorWorkspaces: string[] = [];
  private readonly lists: WorkbenchList<InstalledPlugin>[] = [];
  private bodyWidth = 0;
  private get runtimeStatus(): PluginRuntimeStatusDTO | undefined { return this.state?.runtime; }

  constructor(options: IViewPaneOptions, @IKeybindingService keybindings: IKeybindingService,
    @IContextMenuService private readonly pluginMenus: IContextMenuService, @IConfigurationService configuration: IConfigurationService,
    @IContextKeyService contextKeys: IContextKeyService, @IViewDescriptorService descriptors: IViewDescriptorService,
    @IInstantiationService instantiation: IInstantiationService, @IOpenerService opener: IOpenerService,
    @IThemeService theme: IThemeService, @IHoverService hover: IHoverService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
    @IProgressService private readonly progress: IProgressService,
    @IDialogService private readonly dialogs: IDialogService,
    @IEditorService private readonly editors: IEditorService,
    @IContextViewService private readonly contextView: IContextViewService) {
    super(options, keybindings, pluginMenus, configuration, contextKeys, descriptors, instantiation, opener, theme, hover);
    this._register(onDidChangeExtensionManagementTarget(() => {
      this.stopRuntimeStatus();
      this.refreshRevision++;
      this.workspaceTarget = undefined;
      if (this.content) clearNode(this.content);
      void this.refresh();
    }));
    this._register(this.onDidChangeBodyVisibility(visible => {
      if (visible) void this.refresh();
      else { this.refreshRevision++; this.stopRuntimeStatus(); this.refreshProgress?.done(); }
    }));
  }

  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    container.classList.add('service-plugin-packages-view');
    this.searchInput = this._register(new InputBox(append(container, $('.service-plugin-search')), this.contextView, {
      ariaLabel: t('plugins.searchInstalled'), placeholder: t('plugins.searchInstalled'), inputBoxStyles: defaultInputBoxStyles,
    }));
    this.searchInput.inputElement.type = 'search';
    this.searchInput.value = this.query;
    this._register(this.searchInput.onDidChange(value => void this.commands.executeCommand('workbench.extensions.search', value)
      .catch(error => this.notifications.error(errorsText(error)))));
    this.controls = append(container, $('div'));
    this.controls.className = 'service-plugin-scope';
    const gatewayAction = this._register(new Action('plugins.gateway', t('plugins.managementGateway'), undefined, true, index => {
      const target = this.gatewayTargets[index as number];
      if (target) selectManagementTarget('extensions', target);
    }));
    const workspaceAction = this._register(new Action('plugins.workspace', t('plugins.managementWorkspace'), undefined, true, index => {
      const target = managementGateway('extensions');
      this.rememberedWorkspaces.set(this.targetKey(target), this.selectorWorkspaces[index as number]);
      this.workspaceTarget = undefined;
      if (this.content) clearNode(this.content);
      this.scope = 'workspace';
      void this.refresh();
    }));
    this.gatewaySelector = new ManagementTargetSelect(undefined, gatewayAction, [], 0, this.contextView, defaultSelectBoxStyles,
      { ariaLabel: t('plugins.managementGateway'), useCustomDrawn: true });
    this.workspaceSelector = new ManagementTargetSelect(undefined, workspaceAction, [], 0, this.contextView, defaultSelectBoxStyles,
      { ariaLabel: t('plugins.managementWorkspace'), useCustomDrawn: true });
    const toolbar = this._register(new ActionBar(this.controls, { focusOnlyEnabledItems: true,
      actionViewItemProvider: action => action === gatewayAction ? this.gatewaySelector : action === workspaceAction ? this.workspaceSelector : undefined,
    }));
    this._register(toolbar.onDidRun(({ error }) => { if (error) this.notifications.error(errorsText(error)); }));
    const moreAction = this._register(new Action('plugins.scope', t('plugins.scopeMenu'), 'codicon codicon-ellipsis', true, () => {
      const target = { ...managementGateway('extensions') };
      const actions = (['workspace', 'gateway', 'defaults'] as const).map(scope => new Action(`plugins.scope.${scope}`,
        t(scope === 'workspace' ? 'plugins.scopeWorkspace' : scope === 'gateway' ? 'plugins.scopeGateway' : 'plugins.scopeDefaults'), undefined, true,
        async () => { if (scope !== this.scope && this.content) clearNode(this.content); this.scope = scope; await this.refresh(); }));
      actions.push(new Action('plugins.install', t('plugins.installPackage'), undefined, true,
        () => this.runOperation(() => this.commands.executeCommand('boxteam.plugins.installPackage', target))));
      actions.push(new Action('plugins.sources', t('plugins.manageSources'), undefined, true,
        () => this.runOperation(() => this.commands.executeCommand('boxteam.plugins.manageSources', target))));
      this.pluginMenus.showContextMenu({ getAnchor: () => toolbar.domNode.querySelector<HTMLElement>('.action-item:last-child')!, getActions: () => actions,
        onHide: () => actions.forEach(action => action.dispose()) });
    }));
    toolbar.push([gatewayAction, workspaceAction, moreAction], { icon: true, label: false });
    this.content = append(container, $('.service-plugin-packages-content'));
    this.feedback = append(container, $('.service-plugin-feedback'));
    this.feedback.setAttribute('role', 'alert');
    this.feedback.hidden = true;
    void this.refresh();
  }

  get searchValue(): string { return this.query; }

  protected override layoutBody(height: number, width: number): void {
    super.layoutBody(height, width);
    this.bodyWidth = width;
    this.searchInput?.layout();
    for (const list of this.lists) list.layout(list.length * 72, width);
  }

  async openPlugin(target: PluginTarget, packageId: string, version: string, pluginId: string, scope: 'gateway' | 'workspace'): Promise<void> {
    const checkTarget = () => {
      const current = managementGateway('extensions');
      if (current.gateway_id !== target.gateway_id || current.gateway_connection_id !== target.gateway_connection_id) {
        throw new Error(t('plugins.managementTargetChanged'));
      }
    };
    checkTarget();
    if (scope !== this.scope && this.content) clearNode(this.content);
    this.scope = scope;
    this.search('');
    await this.refresh();
    checkTarget();
    if (scope === 'workspace' && !this.workspaceTarget) throw new Error(t('plugins.noWorkspaces'));
    await this.editors.openEditor(new PluginEditorInput(target, this.workspaceTarget, scope, packageId, version, pluginId, pluginId));
  }

  search(query: string): void {
    this.query = query;
    if (this.searchInput) this.searchInput.value = query;
    const otherView = /(?:^|\s)@(mcp|agentPlugins)(?:\s|$)/i.test(query);
    const wasVisible = this.content !== undefined && !this.content.hidden;
    if (this.controls) this.controls.hidden = otherView;
    if (this.content) this.content.hidden = otherView;
    if (otherView) { this.refreshRevision++; this.stopRuntimeStatus(); this.refreshProgress?.done(); if (this.feedback) this.feedback.hidden = true; }
    else if (!otherView && !wasVisible && this.isBodyVisible()) void this.refresh();
    for (const section of this.content?.querySelectorAll<HTMLElement>('.service-plugin-package') ?? []) {
      section.hidden = !section.dataset.search?.includes(query.trim().toLocaleLowerCase());
    }
  }

  override dispose(): void {
    this.stopRuntimeStatus();
    this.packageActions.clear();
    this.lists.length = 0;
    this.refreshRevision++;
    this.content = undefined;
    this.refreshProgress?.done();
    this.feedback = undefined;
    super.dispose();
  }

  async refresh(): Promise<void> {
    if (!this.content || !this.isBodyVisible() || this.content.hidden) return;
    this.stopRuntimeStatus();
    const revision = ++this.refreshRevision;
    const scope = this.scope;
    const container = this.content;
    container.inert = true;
    if (this.feedback) this.feedback.hidden = true;
    this.refreshProgress?.done();
    const progress = this.refreshProgress = this.getProgressIndicator().show(true, 800);
    try {
      await this.renderPackages(container, revision, scope);
    } catch (error) {
      if (revision !== this.refreshRevision || this.content !== container) return;
      this.stopRuntimeStatus();
      if (!this.isBodyVisible() || container.hidden) return;
      const feedback = this.feedback!;
      clearNode(feedback);
      feedback.hidden = false;
      append(feedback, $('span')).textContent = errorsText(error);
      const retry = append(feedback, $('button'));
      retry.textContent = t('plugins.retry');
      retry.addEventListener('click', () => void this.refresh());
    } finally {
      if (this.refreshProgress === progress) { progress.done(); this.refreshProgress = undefined; }
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

  private stopRuntimeStatus(): void { this.state?.dispose(); this.state = undefined; }

  private updateRuntimeStatusPresentation(): void {
    const container = this.content;
    if (!container || !this.state) return;
    for (const row of container.querySelectorAll<HTMLElement>('[data-plugin-runtime-row]')) {
      const packageId = row.dataset.packageId;
      const pluginId = row.dataset.pluginId;
      if (!packageId || !pluginId) continue;
      const status = this.state.slot(packageId, pluginId);
      const output = row.querySelector<HTMLElement>('[data-plugin-runtime-value]');
      row.title = [this.state.label(packageId, pluginId, row.dataset.runtime === 'true'), status?.error, this.state.error].filter(Boolean).join('\n');
      if (output) output.textContent = this.state.summary(packageId, pluginId, row.dataset.runtime === 'true');
      const failure = row.querySelector<HTMLElement>('[data-plugin-runtime-error]');
      if (failure) {
        failure.hidden = !(status?.error || this.state.error);
        failure.textContent = status?.error ?? this.state.error ?? '';
      }
    }
    const workspace = container.querySelector<HTMLElement>('[data-plugin-workspace-handoff]');
    if (workspace) {
      const state = this.runtimeStatus?.workspace_configurations.find(item => item.workspace_id === workspace.dataset.workspaceId);
      workspace.hidden = state?.state === 'ready';
      workspace.textContent = t('plugins.workspaceHandoffSummary', state ? pluginStateLabel(state.state) : t('plugins.notReceived'));
      workspace.title = t('plugins.workspaceHandoff', workspace.dataset.workspaceId, state ? pluginStateLabel(state.state) : t('plugins.notReceived'), state?.error ? ` · ${state.error}` : '');
    }
  }

  private gatewayTargets: Awaited<ReturnType<typeof listManagementTargets>> = [];
  private targetKey(target: PluginTarget): string { return JSON.stringify([target.gateway_id, target.gateway_connection_id]); }
  private async renderTargetSelectors(revision: number): Promise<void> {
    const targets = await listManagementTargets();
    if (revision !== this.refreshRevision) return;
    this.gatewayTargets = targets;
    const target = managementGateway('extensions');
    const gatewaySelect = this.gatewaySelector!, workspaceSelect = this.workspaceSelector!;
    gatewaySelect.setOptions(targets.map(item => ({ text: managementTargetDisplayName(item) })),
      targets.findIndex(item => item.gateway_id === target.gateway_id && item.gateway_connection_id === target.gateway_connection_id));
    const workspaces = workspaceContext.getSnapshot().workspaces.filter(item => item.gateway_id === target.gateway_id);
    const remembered = this.rememberedWorkspaces.get(this.targetKey(target));
    const selected = workspaces.find(item => item.workspace_id === remembered) ?? workspaces[0];
    if (selected) this.rememberedWorkspaces.set(this.targetKey(target), selected.workspace_id);
    this.workspaceTarget = this.scope === 'workspace' && selected ? { gateway_id: target.gateway_id,
      gateway_connection_id: target.gateway_connection_id, workspace_id: selected.workspace_id } : undefined;
    this.selectorWorkspaces = workspaces.map(item => item.workspace_id);
    const workspaceScope = this.scope === 'workspace' && workspaces.length > 0;
    workspaceSelect.setOptions(workspaceScope ? workspaces.map(item => ({ text: item.name }))
      : [{ text: t(this.scope === 'gateway' ? 'plugins.scopeGateway' : this.scope === 'defaults' ? 'plugins.scopeDefaults' : 'plugins.noWorkspaces') }],
      workspaceScope ? workspaces.indexOf(selected!) : 0);
    workspaceSelect.setEnabled(workspaceScope);
  }

  private async renderPackages(container: HTMLElement, revision: number, scope: Scope): Promise<void> {
    await workspaceContext.ensureReady();
    if (revision !== this.refreshRevision) return;
    const entryGatewayId = workspaceContext.getSnapshot().gatewayId;
    if (!entryGatewayId) throw new Error(t('plugins.gatewayNotReady'));
    await this.renderTargetSelectors(revision);
    if (revision !== this.refreshRevision) return;
    const target = managementGateway('extensions');
    const workspace = scope === 'workspace' ? this.workspaceTarget : undefined;
    if (revision !== this.refreshRevision) return;
    this.workspaceTarget = workspace;
    if (scope === 'workspace' && !workspace) { this.packageActions.clear(); this.lists.length = 0; clearNode(container); container.inert = false; append(container, $('p')).textContent = t('plugins.chooseWorkspaceFirst'); return; }
    const pluginTarget: PluginTarget = { gateway_id: target.gateway_id, gateway_connection_id: target.gateway_connection_id };
    const state = this.state = new PluginScope(scope, pluginTarget, workspace, this.notifications);
    const [inventory] = await Promise.all([
      gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', { method: 'POST', body: JSON.stringify(pluginTarget) }),
      state.load(),
    ]);
    if (this.content !== container || revision !== this.refreshRevision) return;
    const runtime = state.runtime, scopeBindings = state.bindings;
    const boundVersions = () => JSON.stringify(state.bindings.map(item => [item.package_id, item.plugin_id, item.package_version]));
    const renderedVersions = boundVersions();
    state.onDidChange(() => {
      if (state !== this.state) return;
      if (boundVersions() !== renderedVersions) void this.refresh();
      else this.updateRuntimeStatusPresentation();
    });
    const scrollTop = container.scrollTop;
    this.packageActions.clear();
    this.lists.length = 0;
    clearNode(container);
    container.inert = false;
    if (scope === 'defaults') append(container, $('p')).textContent = t('plugins.defaultsFutureOnly');
    if (workspace) {
      const handoff = runtime!.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
      const summary = append(container, $('p'));
      summary.setAttribute('aria-label', t('plugins.workspaceHandoffAria'));
      summary.dataset.pluginWorkspaceHandoff = 'true';
      summary.dataset.workspaceId = workspace.workspace_id;
      summary.hidden = handoff?.state === 'ready';
      summary.textContent = t('plugins.workspaceHandoffSummary', handoff ? pluginStateLabel(handoff.state) : t('plugins.notReceived'));
      summary.title = t('plugins.workspaceHandoff', workspace.workspace_id, handoff ? pluginStateLabel(handoff.state) : t('plugins.notReceived'), handoff?.error ? ` · ${handoff.error}` : '');
    }
    const pluginScope = scope === 'gateway' ? 'gateway' : 'workspace';
    const bindings = scopeBindings.filter(item => scope === 'workspace'
      ? item.workspace_id === workspace?.workspace_id : item.workspace_id == null);
    for (const group of installedPluginGroups(inventory, bindings, pluginScope)) {
      const { manifest } = group;
      const matching = group.plugins.map(item => item.plugin);
      if (!matching.length) continue;
      const packageSection = append(container, $('section.service-plugin-package'));
      packageSection.dataset.search = `${manifest.package_id} ${manifest.name} ${manifest.description ?? ''} ${group.plugins.map(({ plugin }) => `${plugin.id} ${plugin.name}`).join(' ')}`.toLocaleLowerCase();
      const packageHeading = append(packageSection, $('h3'));
      append(packageHeading, $('span')).textContent = manifest.name;
      packageHeading.title = manifest.package_id;
      const packageMenu = append(packageHeading, $('div'));
      const packageBar = this.packageActions.add(new ActionBar(packageMenu));
      const openVersion = async (pluginId?: string) => {
        const versions = group.versions.filter(item => item.plugins.some(plugin => plugin.instance_scope === pluginScope && (!pluginId || plugin.id === pluginId)));
        const selected = await this.quickInput.pick(versions.map(manifest => ({ label: manifest.version,
          description: bindings.some(binding => binding.package_id === manifest.package_id && binding.package_version === manifest.version && (!pluginId || binding.plugin_id === pluginId)) ? t('plugins.boundVersion') : undefined,
          manifest })), { title: t('plugins.installedVersions'), placeHolder: manifest.name }, state.lifecycle.token);
        if (!selected) return;
        const plugins = selected.manifest.plugins.filter(plugin => plugin.instance_scope === pluginScope && (!pluginId || plugin.id === pluginId));
        const plugin = plugins.length === 1 ? plugins[0] : (await this.quickInput.pick(plugins.map(plugin => ({ label: plugin.name, plugin })),
          { title: manifest.name }, state.lifecycle.token))?.plugin;
        if (!plugin) return;
        state.lifecycle.check();
        await this.editors.openEditor(new PluginEditorInput(pluginTarget, workspace, scope, manifest.package_id, selected.manifest.version, plugin.id, plugin.name));
      };
      const packageActions: Action[] = [];
      if (group.versions.length > 1) packageActions.push(new Action('plugin.package.versions', t('plugins.installedVersions'), undefined, true,
        () => openVersion().catch(error => this.notifications.error(errorsText(error)))));
      const batchButtons = [true, false].map(enabled => {
        const button = new Action(`plugin.package.${enabled}`, enabled ? t('plugins.enableAll') : t('plugins.disableAll'), undefined, true, () => this.runTargetOperation(async lifecycle => {
          batchButtons.forEach(item => { item.enabled = false; });
          try {
            const batchBindings = group.plugins.map(item => ({ package_id: manifest.package_id, plugin_id: item.plugin.id,
              package_version: state.binding(manifest.package_id, item.plugin.id)?.package_version
                ?? item.manifest.version, enabled }));
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
          } finally { batchButtons.forEach(item => { item.enabled = true; }); }
        }, entryGatewayId));
        packageActions.push(button);
        return button;
      });
      packageActions.push(new Action('plugin.package.install', t('plugins.reinstall'), undefined, true, () => this.runOperation(() => installPackageFromView(
        this.quickInput, this.notifications, this.progress, pluginTarget, entryGatewayId))));
      const uninstall = new Action('plugin.package.uninstall', t('plugins.uninstallWholePackage'), undefined, true, async () => {
        uninstall.enabled = false;
        try {
          const selected = group.versions.length === 1 ? group.versions[0] : (await this.quickInput.pick(
            group.versions.map(manifest => ({ label: manifest.version, manifest })), { title: t('plugins.uninstallWholePackage'), placeHolder: manifest.name }, state.lifecycle.token))?.manifest;
          if (selected) await this.runTargetOperation(lifecycle => uninstallPackageFromView(
            this.quickInput, this.notifications, pluginTarget, manifest.package_id, selected.version, lifecycle), entryGatewayId);
        } finally { uninstall.enabled = true; }
      });
      packageActions.push(uninstall);
      const packageAction = this.packageActions.add(new Action('plugins.package.manage', t('plugins.managePlugin', manifest.name), 'codicon codicon-ellipsis', true,
        () => this.pluginMenus.showContextMenu({ getAnchor: () => packageMenu, getActions: () => packageActions })));
      packageBar.push(packageAction, { icon: true, label: false });
      for (const action of packageActions) this.packageActions.add(action);
      const listContainer = append(packageSection, $('.service-plugin-package-list'));
      const list = this.packageActions.add(this.instantiationService.createInstance(WorkbenchList<InstalledPlugin>, 'ServicePluginPackages', listContainer,
        { getHeight: () => 72, getTemplateId: () => 'plugin' }, [{
          templateId: 'plugin',
          renderTemplate: (container: HTMLElement) => ({ container, disposables: new DisposableStore() }),
          renderElement: (item: InstalledPlugin, _index: number, template: { container: HTMLElement; disposables: DisposableStore }) => {
            template.disposables.clear(); clearNode(template.container);
            const { plugin, manifest } = item;
            const row = append(template.container, $('.extension-list-item'));
            const icon = append(append(row, $('.icon-container')), $('.extension-icon'));
            append(icon, $(`span.codicon.${pluginIcon(plugin)}`)).setAttribute('aria-hidden', 'true');
            const body = append(row, $('.details'));
            row.dataset.pluginRuntimeRow = 'true';
            row.dataset.packageId = manifest.package_id;
            row.dataset.pluginId = plugin.id;
            row.dataset.packageVersion = manifest.version;
            row.title = state.label(manifest.package_id, plugin.id, Boolean(plugin.runtime));
            row.dataset.workspaceId = scope === 'workspace' ? workspace?.workspace_id ?? '' : '';
            row.dataset.scope = scope;
            row.dataset.runtime = String(Boolean(plugin.runtime));
            const header = append(append(body, $('.header-container')), $('.header'));
            const open = append(header, $('span.name'));
            open.textContent = plugin.name;
            open.title = `${manifest.package_id}/${plugin.id} @${manifest.version}`;
            append(body, $('.description.ellipsis')).textContent = pluginDescription(plugin, manifest);
            const footer = append(body, $('.footer'));
            const publisher = append(footer, $('.publisher-container.ellipsis'));
            const gearBar = template.disposables.add(new ActionBar(footer));
            const gear = template.disposables.add(new Action('plugins.plugin.manage', t('plugins.managePlugin', plugin.name), 'extension-action icon codicon codicon-gear', true, () => {
              const actions = pluginActions(state, manifest, plugin, this.quickInput, this.notifications);
              if (group.versions.filter(item => item.plugins.some(candidate => candidate.id === plugin.id && candidate.instance_scope === pluginScope)).length > 1) actions.push(new Action('plugin.versions', t('plugins.installedVersions'), undefined, true, () => openVersion(plugin.id).catch(error => this.notifications.error(errorsText(error)))));
              this.pluginMenus.showContextMenu({ getAnchor: () => footer, getActions: () => actions,
                onHide: () => actions.forEach(action => action.dispose()) });
            }));
            gearBar.push(gear, { icon: true, label: false });
            const runtimeValue = append(publisher, $('span.publisher-name'));
            runtimeValue.dataset.pluginRuntimeValue = 'true';
            runtimeValue.textContent = state.summary(manifest.package_id, plugin.id, Boolean(plugin.runtime));
            const failure = append(publisher, $('span'));
            failure.dataset.pluginRuntimeError = 'true';
            failure.hidden = true;
          },
          disposeTemplate: (template: { disposables: DisposableStore }) => template.disposables.dispose(),
        }], { multipleSelectionSupport: false, setRowLineHeight: false, horizontalScrolling: false, openOnSingleClick: true,
          accessibilityProvider: { getWidgetAriaLabel: () => manifest.name, getAriaLabel: (item: InstalledPlugin) => item.plugin.name },
          keyboardNavigationLabelProvider: { getKeyboardNavigationLabel: (item: InstalledPlugin) => item.plugin.name },
        }));
      this.packageActions.add(list.onDidOpen(event => {
        if (!event.element) return;
        const { plugin, manifest } = event.element;
        void this.editors.openEditor(new PluginEditorInput(pluginTarget, workspace, scope, manifest.package_id, manifest.version, plugin.id, plugin.name), event.editorOptions, event.sideBySide ? SIDE_GROUP : undefined)
          .catch(error => this.notifications.error(errorsText(error)));
      }));
      this.lists.push(list);
      list.splice(0, 0, group.plugins);
      list.layout(group.plugins.length * 72, this.bodyWidth || container.clientWidth);
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
      const status = state.slot(binding.package_id, binding.plugin_id);
      const runtimeText = append(row, $('p'));
      runtimeText.dataset.pluginRuntimeValue = 'true';
      runtimeText.textContent = scope === 'defaults' ? row.dataset.initialRuntimeValue
        : state.label(binding.package_id, binding.plugin_id, true);
      const failure = append(row, $('p'));
      failure.dataset.pluginRuntimeError = 'true';
      failure.hidden = !status?.error;
      failure.textContent = status?.error ?? '';
      this.renderInstanceLog(row, target, status?.log_id, state.lifecycle.signal);
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
    container.scrollTop = scrollTop;
    this.updateRuntimeStatusPresentation();
    if (!inventory.packages.length && !bindings.length) append(container, $('p')).textContent = t('plugins.noPackages', '');
  }

  private renderInstanceLog(container: HTMLElement, target: PluginTarget, logId: string | undefined, signal: AbortSignal): void {
    if (!logId) return;
    const logs = append(container, $('button')) as HTMLButtonElement;
    logs.textContent = t('plugins.viewInstanceLog');
    const output = append(container, $('pre'));
    output.hidden = true;
    output.setAttribute('aria-label', t('plugins.instanceLogAria'));
    logs.addEventListener('click', () => {
      logs.disabled = true;
      output.hidden = false;
      void readPluginInstanceLog(target, logId, signal).then(text => { output.textContent = text; })
        .catch(error => { if (!signal.aborted) { output.textContent = errorsText(error); this.notifications.error(errorsText(error)); } })
        .finally(() => { logs.disabled = false; });
    });
  }

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
      if (!sources.length) { notifications.info(t('plugins.emptySources')); return; }
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

/** 下载源仍编辑 Gateway 正式来源；数组覆盖、预检和保存由原 Settings owner 负责。 */
export async function manageServicePluginSources(accessor: ServicesAccessor, explicitTarget?: PluginTarget): Promise<void> {
  const parent = accessor.get(IInstantiationService), editors = accessor.get(IEditorService);
  const groups = accessor.get(IEditorGroupsService), groupId = groups.activeGroup.id;
  const lifecycle = createManagementTargetLifecycle(t('plugins.settingsTargetChanged'));
  const target = explicitTarget ? { ...explicitTarget, name: explicitTarget.gateway_id,
    connection_kind: explicitTarget.gateway_id === lifecycle.entryGatewayId ? 'local' : 'remote_gateway' } : managementGateway('extensions');
  try {
    const captured = await captureSettingsLocation(parent, lifecycle.token, target, {});
    lifecycle.check();
    if (!groups.getGroup(groupId)) throw new Error(t('workbench.settings.originGroupClosed'));
    const options: ISettingsEditorOptions = { pinned: true, query: 'plugins.sources.entries' };
    if (captured) await editors.openEditor(parent.createInstance(SettingsEditor2Input, captured.location, captured.document), { ...options, sourceGroup: groupId });
  } finally { lifecycle.dispose(); }
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
