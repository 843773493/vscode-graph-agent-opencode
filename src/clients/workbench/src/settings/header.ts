import { SelectBox } from '../../upstream/src/vs/base/browser/ui/selectBox/selectBox.js';
import { IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { defaultSelectBoxStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { t } from '../../../shared/core/i18n/index.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { Link } from '../../upstream/src/vs/platform/opener/browser/link.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ITargetSettingsContext } from './scope.js';
import { ConfigurationSourceOwner, sourceStatus } from './sourceOwner.js';
import { createManagementTargetLifecycle } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';

/** 原设置树之外只补充业务来源与保存状态；窗口设置不渲染此区域。 */
export class SettingsContextHeader extends Disposable {
  private readonly statusDetail = document.createElement('div');
  private readonly warning = document.createElement('div');
  private readonly sourceSelector?: SelectBox;
  constructor(private readonly element: HTMLElement, private readonly layout: () => void,
    private readonly selectSource: (sourceKey: string) => Promise<void>,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext,
    @INotificationService private readonly notifications: INotificationService,
    @IQuickInputService private readonly quick: IQuickInputService,
    @IContextViewService contextView: IContextViewService,
    @IInstantiationService private readonly instantiation: IInstantiationService) {
    super();
    this.element.className = 'settings-context-label';
    this.element.style.cssText = 'font-size:13px;line-height:20px;overflow-wrap:anywhere';
    const source = this.context.target;
    if (source) {
      const controls = document.createElement('div');
      controls.style.cssText = 'display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:4px 0';
      this.sourceSelector = this._register(new SelectBox([], 0, contextView, defaultSelectBoxStyles,
        { ariaLabel: t('settings.editor.managementSource'), useCustomDrawn: true }));
      const sourceContainer = document.createElement('div');
      sourceContainer.style.cssText = 'flex:0 1 260px;min-width:0;max-width:100%';
      controls.append(sourceContainer);
      this.sourceSelector.render(sourceContainer);
      this._register(this.sourceSelector.onDidSelect(({ index }) => {
        const layer = source.document.layers[index];
        if (layer) this.run(() => {
          return this.selectSource(layer.source_key);
        });
      }));
      const content = document.createElement('div');
      this.element.append(content);
      const status = document.createElement('details'); content.append(status, this.warning);
      status.title = `${source.location.gateway_id} · ${source.location.workspace_id ?? 'Gateway'} · ${source.location.source_key}`;
      const summary = document.createElement('summary');
      summary.title = t('workbench.settings.configurationStatus');
      summary.setAttribute('aria-label', summary.title);
      summary.style.cssText = 'display:flex;align-items:center;gap:4px;cursor:pointer;list-style:none;color:var(--vscode-descriptionForeground)';
      const chevron = document.createElement('span'); chevron.className = 'codicon codicon-chevron-right';
      const label = document.createElement('span'); label.textContent = t('workbench.settings.configurationStatus');
      summary.append(chevron, label);
      status.addEventListener('toggle', () => {
        chevron.className = status.open ? 'codicon codicon-chevron-down' : 'codicon codicon-chevron-right';
        this.layout();
      });
      status.append(summary, controls, this.statusDetail);
      const location = { ...source.location };
      this._register(this.instantiation.createInstance(Link, status, { label: t('settings.editor.sourceLocations'), href: '#' }, {
        opener: () => this.run(async () => {
          await workspaceContext.ensureReady();
          if (this._store.isDisposed) return;
          const lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
          try {
            const listed = await new ConfigurationSourceOwner(location).list(lifetime.signal);
            lifetime.check();
            await this.quick.pick([
              { label: location.source_key, detail: listed.sources.find(item => item.source_key === location.source_key)?.uri ?? t('gateway.storage.unreported') },
              { label: t('settings.editor.sourceSchema'), detail: listed.schema_uri ?? t('gateway.storage.unreported') },
            ], { title: t('settings.editor.sourceLocations'), prompt: t('gateway.storage.sourceBoundary', location.gateway_id,
              location.workspace_id ?? 'Gateway') }, lifetime.token);
          } finally { this._store.delete(lifetime); }
        }),
      }));
    } else this.element.style.display = 'none';
    this.update();
  }
  private run(action: () => Promise<unknown>): void {
    this.sourceSelector?.setEnabled(false);
    action().catch(error => { if (!this._store.isDisposed) this.notifications.error(error); })
      .finally(() => { if (!this._store.isDisposed) this.sourceSelector?.setEnabled(true); });
  }
  update(): void {
    const source = this.context.target;
    if (!source) return;
    const labels: Record<string, string> = { inline: t('workbench.settings.builtInSource'), source: t('workbench.settings.layer.source'),
      user: t(source.location.workspace_id ? 'workbench.settings.backendSettings' : 'workbench.settings.gatewaySettings'),
      user_local: t('workbench.settings.localSource'), workspace: t('workbench.settings.workspaceSettings'),
      runtime_override: t('workbench.settings.runtimeSource') };
    this.sourceSelector?.setOptions(source.document.layers.map(layer => ({ text: labels[layer.layer] ?? layer.source_key,
      description: layer.source_key })), source.document.layers.findIndex(layer => layer.source_key === source.location.source_key));
    this.statusDetail.textContent = t('settings.editor.header', source.location.gateway_id, source.location.workspace_id ?? 'Gateway',
      sourceStatus(source.document), source.document.writable ? '' : t('settings.editor.readOnlySuffix'), '');
    const unbound = source.unboundPluginParameters()?.map(plugin => {
      const identity = `${plugin.packageId}/${plugin.pluginId}`;
      return t(plugin.installation === 'declared' ? 'settings.editor.installedUnbound'
        : plugin.installation === 'unknown' ? 'settings.editor.unboundManifestUnavailable' : 'settings.editor.declarationUnavailable', identity);
    });
    const warning = unbound === undefined ? t('settings.editor.invalidSourceUnbound')
      : unbound.length ? t('settings.editor.unboundEditHint', unbound.join(t('settings.source.observationSeparator'))) : '';
    this.warning.textContent = warning;
    this.warning.hidden = !warning;
    this.layout();
  }
}
