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
registerColor('boxteam.chat.background', 'editor.background', 'BoxTeam 对话区域的背景颜色。');
registerColor('boxteam.chat.inputBackground', 'input.background', 'BoxTeam 对话输入框的背景颜色。');
registerColor('boxteam.chat.requestBackground', 'chat.requestBackground', 'BoxTeam 用户消息的背景颜色。');

registerThemingParticipant((theme, collector) => {
  collector.addRule(`.monaco-workbench { --boxteam-color-scheme: ${isDark(theme.type) ? 'dark' : 'light'}; }`);
});

Registry.as<IConfigurationRegistry>(Extensions.Configuration).registerConfiguration({
  id: 'boxteam.appearance', title: 'BoxTeam 外观',
  properties: {
    [BACKGROUND_SETTING]: {
      type: 'object', scope: ConfigurationScope.MACHINE, default: DEFAULT_BACKGROUND,
      description: '当前 Workbench profile 的会话区域背景，随 settings.json 保存。图片只作装饰，不改变主题配色。',
      additionalProperties: false,
      properties: {
        image: { type: 'string', default: '', pattern: '^(https?://.+)?$', description: '图片的完整 HTTP(S) URL；空字符串清除图片。Gateway 图片沿用当前用户的访问权限。' },
        position: { type: 'string', default: DEFAULT_BACKGROUND.position, description: 'CSS background-position。' },
        size: { type: 'string', default: DEFAULT_BACKGROUND.size, description: 'CSS background-size。' },
        repeat: { type: 'string', enum: ['no-repeat', 'repeat', 'repeat-x', 'repeat-y', 'space', 'round'], default: DEFAULT_BACKGROUND.repeat },
        overlay: { type: 'string', default: DEFAULT_BACKGROUND.overlay, description: '图片上的 CSS 渐变遮罩；none 表示不加遮罩。' },
      },
    },
  },
});

/** 原生 JSON 编辑与设置页共用的配置值校验。 */
export function readWorkbenchBackground(value: unknown) {
  const background = readAppearanceBackground(value);
  for (const [key, property] of [['position', 'background-position'], ['size', 'background-size'], ['repeat', 'background-repeat'], ['overlay', 'background-image']] as const) {
    if (!CSS.supports(property, background[key])) throw new Error(`${BACKGROUND_SETTING}.${key} 的 CSS 值无效：${background[key]}`);
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
