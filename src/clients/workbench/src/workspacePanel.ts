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
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { WorkspacePanelSurface } from './react/workspacePanelView.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';

export class WorkspacePanel extends ViewPane {
  private surface?: WorkspacePanelSurface;
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
    @INotificationService private readonly notifications: INotificationService,
    @ICommandService private readonly commands: ICommandService,
  ) {
    super(options, keybindings, menus, configuration, contexts, views, instantiation, opener, themes, hover);
    this._register(this.onDidChangeBodyVisibility(visible => this.surface?.setVisible(visible)));
  }
  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    container.style.height = '100%';
    this.surface = new WorkspacePanelSurface(container, this.editors, this.id, this.isBodyVisible(), this.notifications, this.commands);
  }
  override focus(): void { this.surface?.focus(); }
  revealResource(owner: WorkspaceOwner, resourceId: string): void {
    if (!this.surface) throw new Error('工作区资源面板尚未挂载');
    this.surface.revealResource(owner, resourceId);
  }
  override dispose(): void { this.surface?.dispose(); super.dispose(); }
}
