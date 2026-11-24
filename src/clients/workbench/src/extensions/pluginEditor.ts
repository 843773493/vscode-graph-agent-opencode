import { t } from '../../../shared/core/i18n/index.js';
import { append, $, clearNode, type Dimension } from '../../upstream/src/vs/base/browser/dom.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import type { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { EditorInput } from '../../upstream/src/vs/workbench/common/editor/editorInput.js';
import { EditorPane } from '../../upstream/src/vs/workbench/browser/parts/editor/editorPane.js';
import { EditorPaneDescriptor, type IEditorPaneRegistry } from '../../upstream/src/vs/workbench/browser/editor.js';
import { EditorExtensions, type IEditorOpenContext, type IUntypedEditorInput, type IEditorFactoryRegistry, type IEditorSerializer } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorOptions } from '../../upstream/src/vs/platform/editor/common/editor.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { listManagementTargets, managementTargetDisplayName, createManagementTargetLifecycle } from '../managementTarget.js';
import { SyncDescriptor } from '../../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { IEditorGroup } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { ITelemetryService } from '../../upstream/src/vs/platform/telemetry/common/telemetry.js';
import { IThemeService } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { IStorageService } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ActionBar } from '../../upstream/src/vs/base/browser/ui/actionbar/actionbar.js';
import { Action } from '../../upstream/src/vs/base/common/actions.js';
import { Button } from '../../upstream/src/vs/base/browser/ui/button/button.js';
import { defaultButtonStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import type { PluginInventoryResultDTO, PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { Scope } from '../settings/pluginConfiguration.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { PluginScope, pluginActions, pluginStateLabel, pluginIcon, pluginDescription } from './pluginScope.js';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extensionEditor.css';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extensionsWidgets.css';
import '../../upstream/src/vs/workbench/contrib/extensions/browser/media/extensionActions.css';
import './plugins.css';

/** 这是原编辑器身份，只保存固定范围；切换侧栏目标不会重绑已打开的详情。 */
export class PluginEditorInput extends EditorInput {
  static readonly ID = 'boxteam.editor.servicePlugin';
  readonly resource: URI;
  constructor(readonly target: PluginTarget, readonly workspace: GatewayWorkspaceRequestTarget | undefined,
    readonly scope: Scope, readonly packageId: string, readonly version: string, readonly pluginId: string, readonly name: string) {
    super();
    this.target = { ...target }; this.workspace = workspace ? { ...workspace } : undefined;
    this.resource = URI.from({ scheme: 'boxteam-plugin-editor', path: '/' + encodeURIComponent(JSON.stringify([
      target.gateway_id, workspace?.workspace_id, scope, packageId, version, pluginId,
    ])) });
  }
  get typeId(): string { return PluginEditorInput.ID; }
  override get editorId(): string { return PluginEditorInput.ID; }
  override getName(): string { return this.name; }
  override getDescription(): string | undefined {
    return this.workspace ? workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === this.target.gateway_id && item.workspace_id === this.workspace?.workspace_id)?.name
      : t(this.scope === 'gateway' ? 'plugins.scopeGateway' : 'plugins.scopeDefaults');
  }
  override getTitle(): string { return `${this.name} @${this.version}\n${this.target.gateway_id} · ${this.workspace?.workspace_id ?? this.scope}`; }
  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    return other instanceof PluginEditorInput && this.resource.toString() === other.resource.toString();
  }
}

class PluginEditorPane extends EditorPane {
  static readonly ID = 'boxteam.editor.servicePluginPane';
  private container!: HTMLElement;
  private readonly presentation = this._register(new DisposableStore());
  constructor(group: IEditorGroup, @ITelemetryService telemetry: ITelemetryService,
    @IThemeService themes: IThemeService, @IStorageService storage: IStorageService,
    @IQuickInputService private readonly quickInput: IQuickInputService,
    @INotificationService private readonly notifications: INotificationService,
    @IContextMenuService private readonly menus: IContextMenuService,
    @ICommandService private readonly commands: ICommandService) {
    super(PluginEditorPane.ID, group, telemetry, themes, storage);
  }
  protected override createEditor(parent: HTMLElement): void {
    this.container = append(parent, $('section.extension-editor.service-plugin-editor', { role: 'document', tabIndex: 0 }));
  }
  override async setInput(input: EditorInput, options: IEditorOptions | undefined, context: IEditorOpenContext, token: CancellationToken): Promise<void> {
    this.clearInput();
    await super.setInput(input, options, context, token);
    if (!(input instanceof PluginEditorInput)) throw new Error(t('plugins.managementViewUnavailable'));
    await workspaceContext.ensureReady();
    if (token.isCancellationRequested || this.input !== input) return;
    const access = createManagementTargetLifecycle(t('plugins.operationTargetChanged'));
    this.presentation.add({ dispose: () => access.dispose() });
    // 原 Workbench 在打开操作结算时也取消 token，监听仅覆盖初始化阶段。
    const cancellation = token.onCancellationRequested(() => this.presentation.clear());
    try {
      const targets = (await listManagementTargets(access.signal)).filter(target => target.gateway_id === input.target.gateway_id);
      access.check();
      if (token.isCancellationRequested || this.input !== input) return;
      const route = targets.find(target => target.gateway_connection_id === input.target.gateway_connection_id) ?? (targets.length === 1 ? targets[0] : undefined);
      if (!route) throw new Error(t('plugins.managementTargetChanged'));
      const target: PluginTarget = { gateway_id: route.gateway_id, gateway_connection_id: route.gateway_connection_id };
      const workspace = input.workspace ? { ...input.workspace, gateway_connection_id: target.gateway_connection_id } : undefined;
      const state = this.presentation.add(new PluginScope(input.scope, target, workspace, this.notifications));
      access.dispose();
      const inventory = await gateway.request<PluginInventoryResultDTO>('/api/gateway/service-plugins/inventory', {
        method: 'POST', body: JSON.stringify(state.target), signal: state.lifecycle.signal,
      });
      if (token.isCancellationRequested || this.input !== input) return;
      const manifest = inventory.packages.find(item => item.manifest?.package_id === input.packageId && item.manifest.version === input.version)?.manifest;
      const plugin = manifest?.plugins.find(item => item.id === input.pluginId && item.instance_scope === (input.scope === 'gateway' ? 'gateway' : 'workspace'));
      if (!manifest || !plugin) throw new Error(t('plugins.versionUnavailable', input.packageId, input.pluginId, input.version));
      await state.load();
      if (token.isCancellationRequested || this.input !== input) return;
      clearNode(this.container);
      this.container.dataset.packageId = input.packageId;
      this.container.dataset.pluginId = input.pluginId;
      this.container.dataset.workspaceId = input.workspace?.workspace_id ?? '';
      this.container.dataset.scope = input.scope;
      // 沿用原 ExtensionEditor 的结构与样式，业务模型仍是自有插件。
      const header = append(this.container, $('.header'));
      const icon = append(append(header, $('.icon-container')), $('.extension-icon'));
      append(icon, $(`span.codicon.${pluginIcon(plugin)}`)).setAttribute('aria-hidden', 'true');
      const details = append(header, $('.details'));
      const title = append(details, $('.title'));
      append(title, $('span.name', { role: 'heading', 'aria-level': 1 })).textContent = plugin.name;
      const version = append(title, $('span.version'));
      version.textContent = input.version; version.title = input.version;
      const subtitle = append(details, $('.subtitle'));
      append(subtitle, $('.subtitle-entry')).textContent = manifest.name;
      const owner = append(subtitle, $('.subtitle-entry.last-non-empty'));
      owner.textContent = input.workspace
        ? workspaceContext.getSnapshot().workspaces.find(item => item.gateway_id === input.target.gateway_id && item.workspace_id === input.workspace?.workspace_id)?.name ?? input.workspace.workspace_id
        : t(input.scope === 'gateway' ? 'plugins.scopeGateway' : 'plugins.scopeDefaults');
      owner.title = `${input.target.gateway_id} · ${input.workspace?.workspace_id ?? input.scope}`;
      const description = append(details, $('.description'));
      description.textContent = pluginDescription(plugin, manifest);
      const actionsAndStatus = append(details, $('.actions-status-container.list-layout'));
      const actions = this.presentation.add(new ActionBar(actionsAndStatus, { focusOnlyEnabledItems: true }));
      actions.domNode.classList.add('service-plugin-detail-actions');
      const actionLifetime = this.presentation.add(new DisposableStore());
      const versions = append(append(actionsAndStatus, $('.status')), $('p'));
      versions.setAttribute('aria-label', t('plugins.versionsAria'));
      versions.dataset.pluginRuntimeValue = 'true';
      const body = append(this.container, $('.body'));
      const navbar = this.presentation.add(new ActionBar(append(body, $('.navbar'))));
      const content = append(body, $('.content'));
      const overview = append(content, $('section.service-plugin-document'));
      const contributions = append(content, $('section.service-plugin-document'));
      contributions.hidden = true;
      const tabs: Action[] = [
        { id: 'details', label: t('plugins.detailsTab'), section: overview },
        { id: 'contributions', label: t('plugins.contributionsTab'), section: contributions },
      ].map(tab => this.presentation.add(new Action(tab.id, tab.label, undefined, true, () => {
        for (let index = 0; index < tabs.length; index++) {
          tabs[index].checked = tabs[index].id === tab.id;
          [overview, contributions][index].hidden = tabs[index].id !== tab.id;
        }
      })));
      tabs[0].checked = true;
      navbar.push(tabs, { icon: false, label: true });
      const identity = append(overview, $('p.service-plugin-owner'));
      identity.textContent = `${managementTargetDisplayName(route)} / ${owner.textContent}`;
      const diagnostics = append(overview, $('details.service-plugin-diagnostics'));
      append(diagnostics, $('summary')).textContent = t('plugins.diagnosticDetails');
      const technicalIdentity = append(diagnostics, $('p.service-plugin-owner'));
      const handoff = append(diagnostics, $('p'));
      handoff.setAttribute('aria-label', t('plugins.workspaceHandoffAria'));
      handoff.dataset.pluginWorkspaceHandoff = 'true';
      handoff.hidden = !input.workspace;
      const failure = append(overview, $('p'));
      failure.setAttribute('aria-label', t('plugins.runtimeFailureAria'));
      failure.setAttribute('role', 'alert');
      failure.dataset.pluginRuntimeError = 'true';
      const logActions = append(diagnostics, $('div'));
      const logButton = this.presentation.add(new Button(logActions, { ...defaultButtonStyles, secondary: true }));
      logButton.label = t('plugins.viewInstanceLog');
      this.presentation.add(logButton.onDidClick(() => {
        const logId = state.slot(input.packageId, input.pluginId)?.log_id;
        if (!logId) return;
        logButton.enabled = false;
        void this.commands.executeCommand('boxteam.output.openLog', { ...state.target, log_id: logId })
          .catch(error => { if (!state.lifecycle.signal.aborted) this.notifications.error(error instanceof Error ? error : String(error)); })
          .finally(() => { if (!state.lifecycle.signal.aborted) logButton.enabled = true; });
      }));
      let actionKey: string | undefined;
      const update = () => {
        const status = state.slot(input.packageId, input.pluginId), binding = state.binding(input.packageId, input.pluginId);
        technicalIdentity.textContent = `${input.target.gateway_id} · ${input.workspace?.workspace_id ?? input.scope}${status?.instance ? t('plugins.instanceSuffix', status.instance.instance_id) : ''}`;
        const fullStatus = input.scope === 'defaults'
          ? t('plugins.defaultRuntimeValue', binding?.enabled ? t('plugins.enabledOnCreate') : binding ? t('plugins.disabledOnCreate') : t('plugins.notInDefaults'), binding ? ` @${binding.package_version}` : '')
          : `${t('plugins.configurationPrefix', state.configurationLabel(input.packageId, input.pluginId))}${state.actualLabel(status, Boolean(plugin.runtime))}`;
        versions.title = fullStatus;
        versions.textContent = binding && binding.package_version !== input.version
          ? t('plugins.otherVersion', binding.package_version) : state.summary(input.packageId, input.pluginId, Boolean(plugin.runtime));
        failure.hidden = !(status?.error || state.error);
        failure.textContent = status?.error ?? state.error ?? '';
        logActions.hidden = !status?.log_id;
        const handoffState = state.runtime?.workspace_configurations.find(item => item.workspace_id === input.workspace?.workspace_id);
        handoff.textContent = input.workspace ? t('plugins.workspaceHandoff', input.workspace.workspace_id,
          handoffState ? pluginStateLabel(handoffState.state) : t('plugins.notReceived'), handoffState?.error ? ` · ${handoffState.error}` : '') : '';
        const key = JSON.stringify([binding?.enabled, binding?.package_version, status?.revision]);
        if (key !== actionKey) {
          actionKey = key; actions.clear(); actionLifetime.clear();
          const items = pluginActions(state, manifest, plugin, this.quickInput, this.notifications);
          items.forEach((item, index) => { item.class = `extension-action ${index === 0 ? 'label' : 'text'}`; actionLifetime.add(item); });
          actions.push(items.slice(0, 1), { icon: true, label: true });
          if (items.length > 1) {
            const more = actionLifetime.add(new Action('plugin.manage', t('plugins.managePlugin', plugin.name), 'extension-action icon codicon codicon-gear', true,
              () => this.menus.showContextMenu({ getAnchor: () => actions.domNode, getActions: () => items.slice(1) })));
            actions.push(more, { icon: true, label: false });
          }
        }
      };
      this.presentation.add(state.onDidChange(update)); update();
      if (plugin.contributes?.configuration) {
        append(contributions, $('h2')).textContent = t('plugins.parameterSchemaAria');
        const schema = append(contributions, $('pre'));
        schema.setAttribute('aria-label', t('plugins.parameterSchemaAria'));
        schema.textContent = JSON.stringify(plugin.contributes.configuration, null, 2);
      }
    } finally { cancellation.dispose(); }
  }
  override clearInput(): void {
    this.presentation.clear();
    if (this.container) { clearNode(this.container); for (const key of ['packageId', 'pluginId', 'workspaceId', 'scope']) delete this.container.dataset[key]; }
    super.clearInput();
  }
  override layout(dimension: Dimension): void { this.container.style.height = `${dimension.height}px`; this.container.style.width = `${dimension.width}px`; }
  override focus(): void { this.container.querySelector<HTMLElement>('button,.action-label')?.focus(); }
}
Registry.as<IEditorPaneRegistry>(EditorExtensions.EditorPane).registerEditorPane(
  EditorPaneDescriptor.create(PluginEditorPane, PluginEditorPane.ID, t('workbench.plugins.servicePluginPackages')), [new SyncDescriptor(PluginEditorInput)],
);

class PluginEditorSerializer implements IEditorSerializer {
  canSerialize(input: EditorInput): boolean { return input instanceof PluginEditorInput; }
  serialize(input: EditorInput): string {
    if (!(input instanceof PluginEditorInput)) throw new Error(t('plugins.managementViewUnavailable'));
    return JSON.stringify({ gatewayId: input.target.gateway_id, workspaceId: input.workspace?.workspace_id,
      scope: input.scope, packageId: input.packageId, version: input.version, pluginId: input.pluginId, name: input.name });
  }
  deserialize(_instantiation: IInstantiationService, serialized: string): PluginEditorInput {
    const value: Record<string, unknown> = JSON.parse(serialized);
    if (!value || !['gatewayId', 'packageId', 'version', 'pluginId', 'name'].every(key => typeof value[key] === 'string' && value[key])
      || !['gateway', 'workspace', 'defaults'].includes(String(value.scope))
      || (value.scope === 'workspace' && (typeof value.workspaceId !== 'string' || !value.workspaceId))) throw new Error(t('plugins.managementTargetChanged'));
    const target = { gateway_id: value.gatewayId as string };
    return new PluginEditorInput(target, value.scope === 'workspace' ? { ...target, workspace_id: value.workspaceId as string } : undefined,
      value.scope as Scope, value.packageId as string, value.version as string, value.pluginId as string, value.name as string);
  }
}
Registry.as<IEditorFactoryRegistry>(EditorExtensions.EditorFactory).registerEditorSerializer(PluginEditorInput.ID, PluginEditorSerializer);
