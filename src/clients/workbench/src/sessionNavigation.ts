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
import { ICommandService } from '../upstream/src/vs/platform/commands/common/commands.js';
import { NavigationSurface } from './react/navigationView.js';
import type { GatewayWorkspaceRequestTarget } from '../../web/src/api/workspaceIdentity.js';
import type { WorkspaceAction } from './search/workspaceActions.js';
import type { SessionAction } from './search/sessionActions.js';
import type { SessionForkSourceSelection } from '../../web/src/hooks/shell/useSessionForkDialog.js';
import { t } from '../../shared/core/i18n/index.js';
import type { AddRemoteGatewayConnectionRequest, GatewayRuntimeGroupRequest } from '../../web/src/types/backend.js';
import type { SessionNavigationActions } from '../../web/src/components/panels/sessionPanels/AgentSessionsPanel.js';
import { Action2, MenuId, registerAction2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';

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
    @ICommandService private readonly commands: ICommandService,
  ) {
    super(options, keybindings, menus, configuration, contexts, views, instantiation, opener, themes, hover);
  }
  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    this.surface = new NavigationSurface(container, this.editors, this.groups, this.notifications, this.storage, this.commands);
  }
  override focus(): void { super.focus(); this.surface?.focus(); }
  runNavigationAction(action: keyof SessionNavigationActions): Promise<void> {
    if (!this.surface) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
    return this.surface.runNavigationAction(action);
  }
  runWorkspaceAction(action: WorkspaceAction, workspaceKey: string, assertAccess: () => void, request?: GatewayRuntimeGroupRequest): Promise<void> {
    if (!this.surface) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
    return this.surface.runWorkspaceAction(action, workspaceKey, assertAccess, request);
  }
  runGatewayAction(action: 'refresh' | AddRemoteGatewayConnectionRequest, assertAccess: () => void): Promise<void> {
    if (!this.surface) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
    return this.surface.runGatewayAction(action, assertAccess);
  }
  runSessionAction(action: SessionAction, sessionId: string, title: string, workspaceKey: string, assertAccess: () => void, source?: SessionForkSourceSelection): Promise<void> {
    if (!this.surface) throw new Error(t('workbench.sessionAction.navigationUnavailable'));
    return this.surface.runSessionAction(action, sessionId, title, workspaceKey, assertAccess, source);
  }
  revealFolder(target: GatewayWorkspaceRequestTarget, nodeId: string, focus: boolean): Promise<void> {
    if (!this.surface) throw new Error(t('quickAccess.sessionNavigationUnavailable'));
    return this.surface.revealFolder(target, nodeId, focus);
  }
  override dispose(): void { this.surface?.dispose(); super.dispose(); }
}

// 原生标题工具栏和右键菜单共用命令；树内不再重复绘制标题或操作行。
for (const [action, titleKey, icon, primary] of [
  ['createSession', 'navigation.sessions.new', Codicon.newFile, true],
  ['newWorkspaceFolder', 'navigation.workspace.newFolder', Codicon.newFolder, true],
  ['search', 'navigation.search.sessions', Codicon.search, true],
  ['addWorkspace', 'navigation.workspace.add', Codicon.add, false],
  ['refresh', 'navigation.catalog.refresh', Codicon.refresh, false],
  ['collapseAll', 'navigation.filter.collapseAll', Codicon.collapseAll, false],
] as const) {
  registerAction2(class extends Action2 {
    constructor() {
      super({ id: `boxteam.sessions.${action}`, title: t(titleKey), icon,
        menu: [MenuId.ViewTitle, MenuId.ViewTitleContext].map(id => ({ id,
          when: ContextKeyExpr.equals('view', 'boxteam.sessions'),
          group: id === MenuId.ViewTitle && primary ? 'navigation' : '1_sessions',
          order: ['createSession', 'newWorkspaceFolder', 'search', 'addWorkspace', 'refresh', 'collapseAll'].indexOf(action),
        })),
      });
    }
    async run(accessor: ServicesAccessor): Promise<void> {
      const view = await accessor.get(IViewsService).openView<SessionNavigation>('boxteam.sessions', false);
      if (!view) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
      await view.runNavigationAction(action);
    }
  });
}
