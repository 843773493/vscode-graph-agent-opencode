import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { Registry } from '../../upstream/src/vs/platform/registry/common/platform.js';
import { Extensions, type IQuickAccessRegistry } from '../../upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { WorkspaceQuickAccess } from './quickAccess.js';
import type { NameSearchCategory } from './quickAccessQuery.js';

/** 专用模式只限定原查询类别；范围、分页、历史与打开继续共用默认 Provider。 */
class SessionQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'session ';
  protected override get category(): NameSearchCategory { return '会话'; }
}

class ThreadQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'thread ';
  protected override get category(): NameSearchCategory { return 'Thread'; }
}

class ContentQuickAccess extends WorkspaceQuickAccess {
  static override readonly PREFIX = 'content ';
  protected override get contentMode(): boolean { return true; }
}

for (const [ctor, id, title, order] of [
  [SessionQuickAccess, 'boxteam.quickAccess.sessions', '查找会话和目录', 41],
  [ThreadQuickAccess, 'boxteam.quickAccess.threads', '查找 Thread', 42],
  [ContentQuickAccess, 'boxteam.quickAccess.content', '搜索正文', 43],
] as const) {
  Registry.as<IQuickAccessRegistry>(Extensions.Quickaccess).registerQuickAccessProvider({
    ctor, prefix: ctor.PREFIX, placeholder: `${title} · 当前明确工作区`,
    helpEntries: [{ description: title, commandId: id, commandCenterOrder: order }],
  });
  registerAction2(class extends Action2 {
    constructor() { super({ id, title: { value: title, original: title }, f1: true }); }
    run(accessor: ServicesAccessor): void { accessor.get(IQuickInputService).quickAccess.show(ctor.PREFIX); }
  });
}
