import { t } from '../../../shared/core/i18n/index.js';
import { Disposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ITargetSettingsContext } from './scope.js';
import { sourceStatus } from './sourceOwner.js';

/** 来源和生效状态只读原 owner 回执，不为标题重复请求。 */
export class SettingsContextHeader extends Disposable {
  constructor(private readonly element: HTMLElement, private readonly layout: () => void,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext) {
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
    this.element.textContent = source ? t('settings.editor.header', source.location.gateway_id, source.location.workspace_id ?? 'Gateway', sourceStatus(source.document), source.document.writable ? '' : t('settings.editor.readOnlySuffix'), warning) : t('settings.editor.windowProfile');
    this.layout();
  }
}
