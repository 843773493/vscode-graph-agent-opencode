import { t } from '../../shared/core/i18n/index.js';
import { isDark } from '../upstream/src/vs/platform/theme/common/theme.js';
import { registerColor } from '../upstream/src/vs/platform/theme/common/colorRegistry.js';
import { registerThemingParticipant } from '../upstream/src/vs/platform/theme/common/themeService.js';

import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { ConfigurationScope, Extensions, type IConfigurationRegistry } from '../upstream/src/vs/platform/configuration/common/configurationRegistry.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IWorkbenchLayoutService } from '../upstream/src/vs/workbench/services/layout/browser/layoutService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { BACKGROUND_SETTING, DEFAULT_BACKGROUND, readAppearanceBackground, loadAndApplyBackground, applyBackground } from '../../web/src/theme/theme.js';
import { errorMessage } from '../../web/src/utils/errorMessage.js';

// 聊天专属颜色仍由原主题扩展和 workbench.colorCustomizations 提供。
registerColor('boxteam.chat.background', 'editor.background', t('appearance.chatBackground'));
registerColor('boxteam.chat.inputBackground', 'input.background', t('appearance.inputBackground'));
registerColor('boxteam.chat.requestBackground', 'chat.requestBackground', t('appearance.requestBackground'));

registerThemingParticipant((theme, collector) => {
  collector.addRule(`.monaco-workbench { --boxteam-color-scheme: ${isDark(theme.type) ? 'dark' : 'light'}; }`);
});

Registry.as<IConfigurationRegistry>(Extensions.Configuration).registerConfiguration({
  id: 'boxteam.appearance', title: t('appearance.title'),
  properties: {
    [BACKGROUND_SETTING]: {
      type: 'object', scope: ConfigurationScope.MACHINE, default: DEFAULT_BACKGROUND,
      description: t('appearance.description'),
      additionalProperties: false,
      properties: {
        image: { type: 'string', default: '', pattern: '^(https?://.+)?$', description: t('appearance.imageDescription') },
        position: { type: 'string', default: DEFAULT_BACKGROUND.position, description: t('appearance.positionDescription') },
        size: { type: 'string', default: DEFAULT_BACKGROUND.size, description: t('appearance.sizeDescription') },
        repeat: { type: 'string', enum: ['no-repeat', 'repeat', 'repeat-x', 'repeat-y', 'space', 'round'], default: DEFAULT_BACKGROUND.repeat },
        overlay: { type: 'string', default: DEFAULT_BACKGROUND.overlay, description: t('appearance.overlayDescription') },
      },
    },
  },
});

/** 原生 JSON 编辑与设置页共用的配置值校验。 */
export function readWorkbenchBackground(value: unknown) {
  const background = readAppearanceBackground(value);
  for (const [key, property] of [['position', 'background-position'], ['size', 'background-size'], ['repeat', 'background-repeat'], ['overlay', 'background-image']] as const) {
    if (!CSS.supports(property, background[key])) throw new Error(t('appearance.invalidCss', BACKGROUND_SETTING, key, background[key]));
  }
  return background;
}

/** 唯一背景监听与预载；新建的 React ShadowRoot 直接继承 Workbench 变量。 */
class AppearanceContribution extends Disposable {
  private load?: AbortController;
  constructor(
    @IConfigurationService private readonly configuration: IConfigurationService,
    @IWorkbenchLayoutService private readonly layout: IWorkbenchLayoutService,
    @INotificationService private readonly notifications: INotificationService,
  ) {
    super();
    this._register(configuration.onDidChangeConfiguration(event => { if (event.affectsConfiguration(BACKGROUND_SETTING)) this.apply(); }));
    this.apply();
  }
  private apply(): void {
    this.load?.abort();
    const load = this.load = new AbortController();
    try {
      const background = readWorkbenchBackground(this.configuration.getValue(BACKGROUND_SETTING));
      void loadAndApplyBackground(background, { root: this.layout.mainContainer, signal: load.signal }).then(result => {
        if (result.backgroundWarning) this.notifications.warn(result.backgroundWarning);
      }, cause => { if (!load.signal.aborted) this.notifications.error(errorMessage(cause)); });
    } catch (cause) {
      applyBackground(DEFAULT_BACKGROUND, this.layout.mainContainer);
      this.notifications.error(errorMessage(cause));
    }
  }
  override dispose(): void { this.load?.abort(); super.dispose(); }
}
registerWorkbenchContribution2('boxteam.appearance', AppearanceContribution, WorkbenchPhase.BlockRestore);
