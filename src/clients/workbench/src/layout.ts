import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IWorkbenchLayoutService, Parts } from '../upstream/src/vs/workbench/services/layout/browser/layoutService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';

/** 小窗口沿原版侧栏显隐和尺寸记忆操作，不再把编辑器挤到不可用宽度。 */
class CompactWorkbench extends Disposable {
  private compact = false;
  private changing = false;
  private readonly folded = new Set<Parts.SIDEBAR_PART | Parts.AUXILIARYBAR_PART>();
  private readonly wideSizes = new Map<Parts.SIDEBAR_PART | Parts.AUXILIARYBAR_PART, { width: number; height: number }>();

  constructor(@IWorkbenchLayoutService private readonly layout: IWorkbenchLayoutService) {
    super();
    this._register(layout.onDidLayoutMainContainer(dimension => this.update(dimension.width)));
    this._register(layout.onDidChangePartVisibility(() => {
      if (!this.compact || this.changing) return;
      // 窄窗口中用户手动重新打开后，由用户接管，不在变宽时覆盖其选择。
      for (const part of this.folded) if (layout.isVisible(part)) this.folded.delete(part);
    }));
    this.update(layout.mainContainerDimension.width);
  }

  private update(width: number): void {
    if (this.changing) return;
    const compact = width < 600;
    if (!compact && !this.compact) {
      for (const part of [Parts.SIDEBAR_PART, Parts.AUXILIARYBAR_PART] as const) {
        if (this.layout.isVisible(part)) this.wideSizes.set(part, this.layout.getSize(part));
      }
      return;
    }
    if (compact && this.compact) return;
    this.compact = compact;
    this.changing = true;
    if (compact) {
      for (const part of [Parts.SIDEBAR_PART, Parts.AUXILIARYBAR_PART] as const) {
        if (this.layout.isVisible(part)) {
          this.folded.add(part);
          this.layout.setPartHidden(true, part);
        }
      }
    } else {
      for (const part of this.folded) {
        this.layout.setPartHidden(false, part);
        const size = this.wideSizes.get(part);
        if (size) this.layout.setSize(part, size);
      }
      this.folded.clear();
    }
    this.changing = false;
  }
}

registerWorkbenchContribution2('boxteam.compactWorkbench', CompactWorkbench, WorkbenchPhase.AfterRestored);
