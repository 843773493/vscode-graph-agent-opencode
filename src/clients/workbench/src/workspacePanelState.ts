import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IStorageService, StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IPaneCompositePartService } from '../upstream/src/vs/workbench/services/panecomposite/browser/panecomposite.js';
import { IWorkbenchLayoutService, Parts } from '../upstream/src/vs/workbench/services/layout/browser/layoutService.js';
import { ViewContainerLocation } from '../upstream/src/vs/workbench/common/views.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { workspaceContext } from './workspaceContext.js';

interface PanelState { visible: boolean; id: string; size?: { width: number; height: number } }

/** 仅按 Gateway 路由保存原生面板展示态，业务状态仍由各产品控制器持有。 */
class WorkspacePanelState extends Disposable {
  private route: string | null = null;
  private appliedRoute: string | null = null;
  private restoring = Promise.resolve();
  private revision = 0;
  private disposed = false;
  private size: PanelState['size'];

  constructor(
    @IPaneCompositePartService private readonly panes: IPaneCompositePartService,
    @IWorkbenchLayoutService private readonly layout: IWorkbenchLayoutService,
    @IStorageService private readonly storage: IStorageService,
    @INotificationService private readonly notifications: INotificationService,
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
    if (snapshot.loading || snapshot.error || !snapshot.activeId || snapshot.activeId === this.route) return;
    const first = this.route === null;
    this.save();
    this.route = snapshot.activeId;
    const route = this.route;
    const next = this.storage.getObject<PanelState>(this.key(route), StorageScope.WORKSPACE);
    this.size = next?.size;
    const revision = ++this.revision;
    this.restoring = this.restoring.then(async () => {
      if (this.disposed || revision !== this.revision) return;
      if (!next) {
        if (!first) this.layout.setPartHidden(true, Parts.PANEL_PART);
        this.appliedRoute = route;
        return;
      }
      // 关闭时也恢复选中标签，用户下次展开仍回到此工作区原来的面板。
      if (next.id) await this.panes.openPaneComposite(next.id, ViewContainerLocation.Panel, false);
      if (this.disposed || revision !== this.revision) return;
      if (next.size) this.layout.setSize(Parts.PANEL_PART, next.size);
      this.layout.setPartHidden(!next.visible, Parts.PANEL_PART);
      this.appliedRoute = route;
    }).catch((cause: unknown) => this.notifications.error(`恢复工作区底部面板失败：${String(cause)}`));
  }

  override dispose(): void { this.disposed = true; this.save(); super.dispose(); }
}

registerWorkbenchContribution2('boxteam.workspacePanelState', WorkspacePanelState, WorkbenchPhase.AfterRestored);
