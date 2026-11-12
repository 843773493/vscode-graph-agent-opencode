import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IStorageService, StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IPaneCompositePartService } from '../upstream/src/vs/workbench/services/panecomposite/browser/panecomposite.js';
import { IWorkbenchLayoutService, Parts } from '../upstream/src/vs/workbench/services/layout/browser/layoutService.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { ViewContainerLocation } from '../upstream/src/vs/workbench/common/views.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { workspaceContext } from './workspaceContext.js';

interface PanelState { visible: boolean; id: string; size?: { width: number; height: number } }

/** 仅按 Gateway 路由保存原生面板展示态，业务状态仍由各产品控制器持有。 */
export class WorkspacePanelState extends Disposable {
  static readonly ID = 'boxteam.workspacePanelState';
  private route: string | null = null;
  private appliedRoute: string | null = null;
  private restoring = Promise.resolve();
  private revision = 0;
  private disposed = false;
  private size: PanelState['size'];
  private openMcpAfterRestore = Boolean(new URLSearchParams(location.search).get('mcp_workspace'));

  constructor(
    @IPaneCompositePartService private readonly panes: IPaneCompositePartService,
    @IWorkbenchLayoutService private readonly layout: IWorkbenchLayoutService,
    @IStorageService private readonly storage: IStorageService,
    @INotificationService private readonly notifications: INotificationService,
    @IViewsService private readonly views: IViewsService,
  ) {
    super();
    const unsubscribe = workspaceContext.subscribe(() => this.selectWorkspace());
    this._register({ dispose: unsubscribe });
    this._register(storage.onWillSaveState(() => this.save()));
    const panel = layout.getContainer(window, Parts.PANEL_PART);
    if (!panel) throw new Error('工作区底部面板尚未创建');
    const observer = new ResizeObserver(() => this.captureSize());
    observer.observe(panel);
    this._register({ dispose: () => observer.disconnect() });
    this.selectWorkspace();
  }

  private key(route: string): string { return `boxteam.workspacePanel.${route}`; }
  async whenRestored(route: string): Promise<void> {
    this.selectWorkspace();
    await this.restoring;
    if (this.disposed || this.appliedRoute !== route) throw new Error('工作区底部面板尚未恢复到所选目标');
  }
  private captureSize(): void {
    if (this.route !== this.appliedRoute || !this.layout.isVisible(Parts.PANEL_PART)) return;
    this.size = this.layout.getSize(Parts.PANEL_PART);
  }

  private save(): void {
    if (!this.route || this.route !== this.appliedRoute) return;
    // 隐藏面板的 Grid 尺寸为零；保存最后的可见尺寸，首次未展开则沿用原版默认值。
    this.captureSize();
    const state: PanelState = {
      visible: this.layout.isVisible(Parts.PANEL_PART),
      id: this.panes.getActivePaneComposite(ViewContainerLocation.Panel)?.getId()
        ?? this.panes.getLastActivePaneCompositeId(ViewContainerLocation.Panel),
      size: this.size,
    };
    this.storage.store(this.key(this.route), state, StorageScope.WORKSPACE, StorageTarget.MACHINE);
  }

  private selectWorkspace(): void {
    const snapshot = workspaceContext.getSnapshot();
    const selectedRoute = !snapshot.loading && !snapshot.error ? snapshot.activeId : null;
    if (this.openMcpAfterRestore && !selectedRoute) {
      ++this.revision;
      this.route = this.appliedRoute;
    }
    if (snapshot.loading && !snapshot.error) return;
    const route = selectedRoute !== this.route ? selectedRoute : null;
    if (!route && !this.openMcpAfterRestore) return;
    const first = this.route === null;
    const next = route ? this.storage.getObject<PanelState>(this.key(route), StorageScope.WORKSPACE) : undefined;
    if (route) {
      this.save();
      this.route = route;
      ++this.revision;
    }
    const revision = this.revision;
    this.restoring = this.restoring.then(async () => {
      if (this.disposed || revision !== this.revision) return;
      if (route) {
        if (next) {
          // 关闭时也恢复选中标签，用户下次展开仍回到此工作区原来的面板。
          if (next.id) await this.panes.openPaneComposite(next.id, ViewContainerLocation.Panel, false);
          if (this.disposed || revision !== this.revision) return;
          if (next.size) this.layout.setSize(Parts.PANEL_PART, next.size);
          this.layout.setPartHidden(!next.visible, Parts.PANEL_PART);
        } else if (!first) {
          this.layout.setPartHidden(true, Parts.PANEL_PART);
        }
        this.size = next?.size;
        this.appliedRoute = route;
      }
      // 显式入口在同一 owner 恢复完成后打开；无活动工作区或初始化失败也交给原 MCP 视图显示真实错误。
      if (this.openMcpAfterRestore) {
        if (selectedRoute && this.appliedRoute === selectedRoute) this.openMcpAfterRestore = false;
        await this.views.openView('boxteam.mcp.view', true);
      }
    }).catch((cause: unknown) => this.notifications.error(`恢复工作区底部面板失败：${String(cause)}`));
  }

  override dispose(): void { this.disposed = true; this.save(); super.dispose(); }
}

registerWorkbenchContribution2(WorkspacePanelState.ID, WorkspacePanelState, WorkbenchPhase.AfterRestored);
