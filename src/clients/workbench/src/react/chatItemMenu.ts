import { addDisposableListener } from '../../upstream/src/vs/base/browser/dom.js';
import { HoverPosition } from '../../upstream/src/vs/base/browser/ui/hover/hoverWidget.js';
import { ActionRunner, toAction } from '../../upstream/src/vs/base/common/actions.js';
import { isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { IContextMenuService, IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IHoverService } from '../../upstream/src/vs/platform/hover/browser/hover.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { ChatItemMenuRequest, ChatHoverRow } from '../../../web/src/components/chat/itemContextMenu.js';
import { t } from '../../../shared/core/i18n/index.js';

/** 内容动作由 React 提供，菜单、主题、键盘导航及 Hover 由 Workbench 承载。 */
export class ChatItemMenu extends Disposable {
  private readonly bindings = this._register(new DisposableStore());
  private readonly runner = this._register(new ActionRunner());
  private opened = false;

  readonly bindHover = (element: HTMLElement, rows: () => readonly ChatHoverRow[]): (() => void) => {
    const binding = this.hover.setupDelayedHover(element, () => {
      const table = element.ownerDocument.createElement('table');
      for (const { label, value } of rows()) {
        const row = table.insertRow();
        const heading = element.ownerDocument.createElement('th');
        heading.scope = 'row'; heading.textContent = label;
        heading.style.textAlign = 'start'; heading.style.paddingRight = '12px';
        row.append(heading);
        const cell = row.insertCell();
        cell.textContent = value; cell.style.whiteSpace = 'pre-line';
        cell.style.overflowWrap = 'anywhere';
      }
      return { content: table, position: { hoverPosition: HoverPosition.ABOVE } };
    });
    return () => binding.dispose();
  };

  constructor(
    @IContextMenuService private readonly menus: IContextMenuService,
    @IContextViewService private readonly views: IContextViewService,
    @IHoverService private readonly hover: IHoverService,
    @INotificationService notifications: INotificationService,
  ) {
    super();
    // 菜单在动作开始时就销毁，错误订阅必须由宿主持有，直到异步动作结束。
    this._register(this.runner.onDidRun(({ error }) => {
      if (error && !isCancellationError(error)) notifications.error(error);
    }));
  }

  readonly show = ({ source, anchor, actions }: ChatItemMenuRequest): void => {
    this.hide();
    source.focus({ preventScroll: true });
    this.menus.showContextMenu({
      actionRunner: this.runner,
      getAnchor: () => anchor,
      getMenuClassName: () => 'boxteam-chat-item-menu',
      getActions: () => actions.map(action => toAction({ ...action, id: `boxteam.chat.${action.id}`,
        run: async () => {
          if (this._store.isDisposed) throw new Error(t('chat.itemMenu.sourceGone'));
          await action.run();
        },
      })),
      onHide: () => {
        this.opened = false;
        this.bindings.clear();
        this.hover.hideHover(true);
      },
    });
    this.opened = true;
    // 原菜单刻意不展示 tooltip；仅为本次菜单的现有行接入原 Hover，不修改全局菜单。
    const container = this.views.getContextViewElement();
    container.querySelectorAll<HTMLElement>('[role="menuitem"]').forEach((row, index) => {
      const content = actions[index].tooltip;
      row.setAttribute('aria-description', content);
      const options = { content, position: { hoverPosition: HoverPosition.RIGHT },
        persistence: { hideOnHover: true, hideOnKeyDown: true } };
      this.bindings.add(this.hover.setupDelayedHover(row, options, { setupKeyboardEvents: false }));
      this.bindings.add(addDisposableListener(row, 'focus', () => this.hover.showDelayedHover({ ...options, target: row }, {})));
    });
  };

  hide(): void {
    if (this.opened) this.views.hideContextView(true);
  }

  override dispose(): void { this.hide(); super.dispose(); }
}
