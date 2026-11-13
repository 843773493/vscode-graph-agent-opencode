import { clearNode } from '../../upstream/src/vs/base/browser/dom.js';
import { SelectBox } from '../../upstream/src/vs/base/browser/ui/selectBox/selectBox.js';
import { IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { defaultSelectBoxStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { SettingsEditor2Input } from '../../upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.js';
import type { ISettingsEditorOptions } from '../../upstream/src/vs/workbench/services/preferences/common/preferences.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { t } from '../../../shared/core/i18n/index.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ITargetSettingsContext } from './scope.js';
import { sourceStatus } from './sourceOwner.js';
import { captureSettingsLocation } from './editor.js';
import type { DefinitionEditorOptions } from './entities.js';
import { workspaceContext } from '../workspaceContext.js';
import { createManagementTargetLifecycle, listManagementTargets, managementGateway, managementWorkspace,
  selectManagementTarget, type ManagementTarget } from '../managementTarget.js';
import type { DefinitionKind } from '../../../web/src/components/settings/ConfigurationEntities.js';

/** 来源和生效状态只读原 owner 回执，不为标题重复请求。 */
export class SettingsContextHeader extends Disposable {
  private readonly content: HTMLElement;
  private readonly gatewaySelector: SelectBox;
  private readonly workspaceSelector: SelectBox;
  private readonly sourceSelector: SelectBox;
  private targets: ManagementTarget[] = [];
  private workspaceIds: (string | undefined)[] = [];
  private lifetime?: ReturnType<typeof createManagementTargetLifecycle>;
  private busy = false;
  constructor(private readonly element: HTMLElement, private readonly layout: () => void,
    private readonly showDefinitions: (kind?: DefinitionKind) => void,
    private readonly groupId: number, private readonly selection: () => DefinitionEditorOptions,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext,
    @ICommandService private readonly commands: ICommandService,
    @INotificationService private readonly notifications: INotificationService,
    @IContextViewService contextView: IContextViewService,
    @IInstantiationService private readonly parent: IInstantiationService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService) {
    super();
    this.element.className = 'settings-context-label';
    Object.assign(this.element.style, { order: '1', flex: '0 0 100%', fontSize: '12px', lineHeight: '20px', overflowWrap: 'anywhere' });
    const controls = document.createElement('div');
    controls.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;padding:4px 0';
    this.element.append(controls);
    const selector = (label: string) => {
      const container = document.createElement('div'); controls.append(container);
      const select = this._register(new SelectBox([], 0, contextView, defaultSelectBoxStyles, { ariaLabel: label, useCustomDrawn: true }));
      select.render(container); return select;
    };
    this.gatewaySelector = selector(t('settings.editor.managementGateway'));
    this.workspaceSelector = selector(t('settings.editor.managementScope'));
    this.sourceSelector = selector(t('settings.editor.managementSource'));
    this.content = document.createElement('div'); this.element.append(this.content);
    this._register(this.gatewaySelector.onDidSelect(({ index }) => {
      const target = this.targets[index];
      if (target) this.run(async () => {
        selectManagementTarget('settings', target);
        await this.openTarget(target, this.context.target?.location.workspace_id || this.selection().definitionKind
          ? managementWorkspace('settings', target).workspace_id : undefined);
      });
    }));
    this._register(this.workspaceSelector.onDidSelect(({ index }) => {
      const target = this.currentTarget();
      const workspaceId = this.workspaceIds[index];
      this.run(async () => {
        if (workspaceId) managementWorkspace('settings', target, workspaceId);
        await this.openTarget(target, workspaceId);
      });
    }));
    this._register(this.sourceSelector.onDidSelect(({ index }) => {
      const source = this.context.target;
      const layer = source?.document.layers[index];
      if (source && layer) this.run(() => this.openTarget(this.currentTarget(), source.location.workspace_id, layer.source_key));
    }));
    this._register({ dispose: workspaceContext.subscribe(() => this.updateSelectors()) });
    // 只在打开此设置输入时读原目标目录；正文变化不重读，工作区沿原镜像更新。
    this.run(async () => {
      await workspaceContext.ensureReady();
      if (this._store.isDisposed) return;
      this.lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
      this.targets = await listManagementTargets(this.lifetime.signal);
      this.lifetime.check();
    });
    this.update();
  }
  private currentTarget(): ManagementTarget {
    const location = this.context.target?.location;
    return location ? this.targets.find(target => target.gateway_id === location.gateway_id
      && target.gateway_connection_id === location.gateway_connection_id)
      ?? { gateway_id: location.gateway_id, gateway_connection_id: location.gateway_connection_id,
        name: location.gateway_id, connection_kind: location.gateway_connection_id ? 'remote_gateway' : 'local' }
      : managementGateway('settings');
  }
  private updateSelectors(): void {
    if (this._store.isDisposed) return;
    if (!workspaceContext.getSnapshot().gatewayId) {
      for (const selector of [this.gatewaySelector, this.workspaceSelector, this.sourceSelector]) selector.setEnabled(false);
      return;
    }
    const target = this.currentTarget(), source = this.context.target;
    const targets = this.targets.some(item => item.gateway_id === target.gateway_id && item.gateway_connection_id === target.gateway_connection_id)
      ? this.targets : [...this.targets, target];
    this.gatewaySelector.setOptions(targets.map(item => ({ text: `${item.name} · ${item.gateway_id}`,
      description: item.connection_error ?? item.gateway_connection_id })), targets.findIndex(item => item.gateway_id === target.gateway_id
        && item.gateway_connection_id === target.gateway_connection_id));
    this.targets = targets;
    const workspaces = workspaceContext.getSnapshot().workspaces.filter(item => item.gateway_id === target.gateway_id);
    this.workspaceIds = [undefined, ...workspaces.map(item => item.workspace_id)];
    this.workspaceSelector.setOptions([{ text: t('settings.editor.gatewayConfiguration'), isDisabled: Boolean(this.selection().definitionKind) },
      ...workspaces.map(item => ({ text: `${item.name} · ${item.workspace_id}`, description: item.status }))],
    this.workspaceIds.indexOf(source?.location.workspace_id));
    this.sourceSelector.setOptions(source?.document.layers.map(layer => ({ text: `${layer.source_key} · ${layer.layer}` })) ?? [],
      source?.document.layers.findIndex(layer => layer.source_key === source.location.source_key));
    this.gatewaySelector.setEnabled(!this.busy && Boolean(this.lifetime));
    this.workspaceSelector.setEnabled(!this.busy && Boolean(this.lifetime));
    this.sourceSelector.setEnabled(!this.busy && Boolean(source) && Boolean(this.lifetime));
  }
  private run(action: () => Promise<void>): void {
    this.busy = true; this.updateSelectors();
    action().catch(error => { if (!this._store.isDisposed) this.notifications.error(error); })
      .finally(() => { this.busy = false; this.updateSelectors(); this.layout(); });
  }
  private async openTarget(target: ManagementTarget, workspaceId?: string, sourceKey?: string): Promise<void> {
    const lifetime = this.lifetime;
    if (!lifetime) throw new Error(t('workbench.settings.gatewayPending'));
    const selected = this.selection(), source = this.context.target;
    const kind = workspaceId ? selected.definitionKind : undefined;
    const captured = await captureSettingsLocation(this.parent, lifetime.token, target, { workspace_id: workspaceId,
      source_key: sourceKey ?? (Boolean(source?.location.workspace_id) === Boolean(workspaceId) ? source?.location.source_key : undefined) });
    lifetime.check();
    if (!captured) return;
    if (this._store.isDisposed || !this.groups.getGroup(this.groupId)) throw new Error(t('workbench.settings.originGroupClosed'));
    const options: ISettingsEditorOptions & DefinitionEditorOptions = { pinned: true, definitionKind: kind };
    await this.editors.openEditor(this.parent.createInstance(SettingsEditor2Input, captured.location, captured.document), options, this.groupId);
  }
  update(): void {
    const source = this.context.target;
    const unbound = source?.unboundPluginParameters()?.map(plugin => {
      const identity = `${plugin.packageId}/${plugin.pluginId}`;
      if (plugin.installation === 'declared') return t('settings.editor.installedUnbound', identity);
      if (plugin.installation === 'unknown') return t('settings.editor.unboundManifestUnavailable', identity);
      return t('settings.editor.declarationUnavailable', identity);
    });
    const warning = source && unbound === undefined ? t('settings.editor.invalidSourceUnbound')
      : unbound?.length ? t('settings.editor.unboundEditHint', unbound.join(t('settings.source.observationSeparator'))) : '';
    clearNode(this.content);
    const status = document.createElement('div'); this.content.append(status);
    status.textContent = source ? t('settings.editor.header', source.location.gateway_id, source.location.workspace_id ?? 'Gateway', sourceStatus(source.document), source.document.writable ? '' : t('settings.editor.readOnlySuffix'), warning) : t('settings.editor.windowProfile');
    for (const kind of ['providers', 'agents', undefined] as const) {
      if (kind && source && !source.location.workspace_id) continue;
      if (!kind && !source?.location.workspace_id) continue;
      const link = document.createElement('a'); link.href = '#'; link.style.marginRight = '12px';
      link.textContent = t(kind === 'providers' ? 'workbench.settings.modelsProviders' : kind === 'agents' ? 'configuration.entities.agentsTitle' : 'configuration.entities.settings');
      link.addEventListener('click', event => {
        event.preventDefault();
        if (source?.location.workspace_id || !kind) { this.showDefinitions(kind); this.updateSelectors(); return; }
        void this.commands.executeCommand(kind === 'providers' ? 'boxteam.settings.modelsProviders' : 'boxteam.settings.agents', source?.location)
          .catch(error => this.notifications.error(error));
      });
      this.content.append(link);
    }
    this.updateSelectors();
    this.layout();
  }
}
