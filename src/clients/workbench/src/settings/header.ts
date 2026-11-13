import { clearNode } from '../../upstream/src/vs/base/browser/dom.js';
import { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { t } from '../../../shared/core/i18n/index.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ITargetSettingsContext } from './scope.js';
import { sourceStatus } from './sourceOwner.js';
import type { DefinitionKind } from '../../../web/src/components/settings/ConfigurationEntities.js';

/** 来源和生效状态只读原 owner 回执，不为标题重复请求。 */
export class SettingsContextHeader extends Disposable {
  constructor(private readonly element: HTMLElement, private readonly layout: () => void,
    private readonly showDefinitions: (kind?: DefinitionKind) => void,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext,
    @ICommandService private readonly commands: ICommandService,
    @INotificationService private readonly notifications: INotificationService) {
    super();
    this.element.className = 'settings-context-label';
    Object.assign(this.element.style, { order: '1', flex: '0 0 100%', fontSize: '12px', lineHeight: '20px', overflowWrap: 'anywhere' });
    this.update();
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
    clearNode(this.element);
    const status = document.createElement('div'); this.element.append(status);
    status.textContent = source ? t('settings.editor.header', source.location.gateway_id, source.location.workspace_id ?? 'Gateway', sourceStatus(source.document), source.document.writable ? '' : t('settings.editor.readOnlySuffix'), warning) : t('settings.editor.windowProfile');
    for (const kind of ['providers', 'agents', undefined] as const) {
      if (kind && source && !source.location.workspace_id) continue;
      if (!kind && !source?.location.workspace_id) continue;
      const link = document.createElement('a'); link.href = '#'; link.style.marginRight = '12px';
      link.textContent = t(kind === 'providers' ? 'workbench.settings.modelsProviders' : kind === 'agents' ? 'configuration.entities.agentsTitle' : 'configuration.entities.settings');
      link.addEventListener('click', event => {
        event.preventDefault();
        if (source?.location.workspace_id || !kind) { this.showDefinitions(kind); return; }
        void this.commands.executeCommand(kind === 'providers' ? 'boxteam.settings.modelsProviders' : 'boxteam.settings.agents', source?.location)
          .catch(error => this.notifications.error(error));
      });
      this.element.append(link);
    }
    this.layout();
  }
}
