import { ViewPane, IViewPaneOptions } from '../upstream/src/vs/workbench/browser/parts/views/viewPane.js';
import { IKeybindingService } from '../upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IContextMenuService } from '../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IConfigurationService } from '../upstream/src/vs/platform/configuration/common/configuration.js';
import { IContextKeyService } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../upstream/src/vs/workbench/common/views.js';
import { IInstantiationService } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IOpenerService } from '../upstream/src/vs/platform/opener/common/opener.js';
import { IThemeService } from '../upstream/src/vs/platform/theme/common/themeService.js';
import { IHoverService } from '../upstream/src/vs/platform/hover/browser/hover.js';
import { IEditorService } from '../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IEditorGroupsService } from '../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IStorageService } from '../upstream/src/vs/platform/storage/common/storage.js';
import { NavigationSurface } from './react/navigationView.js';

/** 原版视图只管理布局；完整导航及菜单复用既有 React 产品。 */
export class SessionNavigation extends ViewPane {
  private surface?: NavigationSurface;
  constructor(options: IViewPaneOptions,
    @IKeybindingService keybindings: IKeybindingService,
    @IContextMenuService menus: IContextMenuService,
    @IConfigurationService configuration: IConfigurationService,
    @IContextKeyService contexts: IContextKeyService,
    @IViewDescriptorService views: IViewDescriptorService,
    @IInstantiationService instantiation: IInstantiationService,
    @IOpenerService opener: IOpenerService,
    @IThemeService themes: IThemeService,
    @IHoverService hover: IHoverService,
    @IEditorService private readonly editors: IEditorService,
    @IEditorGroupsService private readonly groups: IEditorGroupsService,
    @INotificationService private readonly notifications: INotificationService,
    @IStorageService private readonly storage: IStorageService,
  ) { super(options, keybindings, menus, configuration, contexts, views, instantiation, opener, themes, hover); }
  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    this.surface = new NavigationSurface(container, this.editors, this.groups, this.notifications, this.storage);
  }
  override focus(): void { this.surface?.focus(); }
  override dispose(): void { this.surface?.dispose(); super.dispose(); }
}
