import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { WorkspaceQuickAccess } from './quickAccess.js';
import type { NameSearchCategory } from './quickAccessQuery.js';
import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';

/** 专用模式只限定原查询类别；范围、分页、历史与打开继续共用默认 Provider。 */
class SessionQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'session ';
  protected override get category(): NameSearchCategory { return '会话'; }
}

class ThreadQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'thread ';
  protected override get category(): NameSearchCategory { return 'Thread'; }
}

export class ContentQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'content ';
  protected override get contentMode(): boolean { return true; }
}

for (const [ctor, id, titleKey, order] of [
  [SessionQuickAccess, 'boxteam.quickAccess.sessions', 'quickAccess.mode.sessions', 41],
  [ThreadQuickAccess, 'boxteam.quickAccess.threads', 'quickAccess.mode.threads', 42],
  [ContentQuickAccess, 'boxteam.quickAccess.content', 'quickAccess.mode.content', 43],
] as const) {
  const title = t(titleKey);
  Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
    ctor, prefix: ctor.PREFIX, placeholder: t('quickAccess.mode.placeholder', title),
    helpEntries: [{ description: title, commandId: id, commandCenterOrder: order }],
  });
  registerAction2(class extends Action2 {
    constructor() { super({ id, title: { value: title, original: actionTitles[titleKey] }, f1: true }); }
    run(accessor: ServicesAccessor): void { accessor.get(IQuickInputService).quickAccess.show(ctor.PREFIX); }
  });
}
