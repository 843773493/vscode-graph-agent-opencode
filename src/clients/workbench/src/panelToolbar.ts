import { Disposable, DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { ThemeIcon } from '../upstream/src/vs/base/common/themables.js';
import { CommandsRegistry } from '../upstream/src/vs/platform/commands/common/commands.js';
import { MenuId, MenuRegistry, MenuItemAction } from '../upstream/src/vs/platform/actions/common/actions.js';
import { IActionViewItemService } from '../upstream/src/vs/platform/actions/browser/actionViewItemService.js';
import { MenuEntryActionViewItem } from '../upstream/src/vs/platform/actions/browser/menuEntryActionViewItem.js';
import { ContextKeyExpr } from '../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IQuickInputService } from '../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { t } from '../../shared/core/i18n/index.js';
import type { PanelToolbarControl } from '../../web/src/components/panels/bottomPanel/PanelToolbar.js';
import { workspaceContext } from './workspaceContext.js';
import { managementGateway, managementWorkspace, managementTargetDisplayName, onDidChangePanelManagementTarget, selectPanelManagementTargetCommand } from './managementTarget.js';
import './panelToolbar.css';

const changed = new Emitter<string>();
const products = new Map<string, { owner: object; controls: readonly PanelToolbarControl[] }>();

/** 产品公布当前动作；原生菜单在结构或状态变化时更新。 */
export function publishPanelToolbar(owner: object, viewId: string, controls: readonly PanelToolbarControl[]): void {
  products.set(viewId, { owner, controls }); changed.fire(viewId);
}
export function releasePanelToolbar(owner: object): void {
  for (const [viewId, product] of products) {
    if (product.owner === owner) { products.delete(viewId); changed.fire(viewId); }
  }
}
const selectWorkspaceCommand = 'boxteam.panel.selectWorkspace';
// 原生窗口、插件等通道有明确来源，不为它们增加与当前内容无关的范围选择。
const scopeWhen = ContextKeyExpr.or(
  ...['terminal', 'boxteam.browser', 'boxteam.automation', 'boxteam.mcp', 'boxteam.tools']
    .map(id => ContextKeyExpr.equals('activePanel', id)),
  ContextKeyExpr.and(ContextKeyExpr.equals('activePanel', 'workbench.panel.output'),
    ContextKeyExpr.equals('boxteamOutputWorkspaceScoped', true)),
);

/** 保留原生菜单动作和悬停，只为新增范围标签提供像素截断。 */
class ScopeActionViewItem extends MenuEntryActionViewItem {
  protected override updateLabel(): void {
    super.updateLabel();
    if (!this.label) return;
    this.label.classList.add('boxteam-panel-scope');
    const icon = document.createElement('span');
    icon.className = `scope-icon codicon codicon-${this.action.id === selectWorkspaceCommand ? 'folder' : 'remote'}`;
    const text = document.createElement('span'); text.className = 'scope-label'; text.textContent = this.action.label;
    const arrow = document.createElement('span'); arrow.className = 'codicon codicon-chevron-down';
    this.label.replaceChildren(icon, text, arrow);
  }
}

/** 面板范围走 PanelTitle，产品走各自 ViewTitle；选择器与分体按钮由原版菜单工厂生成。 */
class PanelToolbar extends Disposable {
  private readonly scopeMenu = this._register(new DisposableStore());
  private readonly productMenus = new Map<string, { owner: object; signature: string; menu: DisposableStore }>();
  private scopeSignature = '';
  constructor(
    @IQuickInputService quickInput: IQuickInputService,
    @IActionViewItemService actionViews: IActionViewItemService,
  ) {
    super();
    this._register(changed.event(viewId => this.updateProduct(viewId)));
    this._register({ dispose: () => { for (const item of this.productMenus.values()) item.menu.dispose(); this.productMenus.clear(); } });
    this._register(onDidChangePanelManagementTarget(() => this.updateScope()));
    this._register({ dispose: workspaceContext.subscribe(() => this.updateScope()) });
    for (const id of [selectPanelManagementTargetCommand, selectWorkspaceCommand]) this._register(actionViews.register(MenuId.PanelTitle, id,
      (action, options, instantiation) => action instanceof MenuItemAction ? instantiation.createInstance(ScopeActionViewItem, action, options) : undefined));
    this._register(CommandsRegistry.registerCommand(selectWorkspaceCommand, async () => {
      const target = managementGateway('panel'), entry = workspaceContext.getSnapshot().gatewayId;
      let selected: ReturnType<typeof managementWorkspace> | undefined;
      try { selected = managementWorkspace('panel', target); } catch { /* 已移除的选择仍允许用户重新选择。 */ }
      const picked = await quickInput.pick(workspaceContext.getSnapshot().workspaces.filter(item => item.gateway_id === target.gateway_id)
        .map(item => ({ label: item.name, description: item.status, detail: item.workspace_id, picked: item.workspace_id === selected?.workspace_id, item })),
      { title: t('workbench.panel.selectWorkspace'), placeHolder: managementTargetDisplayName(target) });
      if (!picked) return;
      if (workspaceContext.getSnapshot().gatewayId !== entry) throw new Error(t('workbench.managementTarget.accessChanged'));
      managementWorkspace('panel', target, picked.item.workspace_id);
    }));
    this.updateScope();
    for (const viewId of products.keys()) this.updateProduct(viewId);
  }

  private updateScope(): void {
    const snapshot = workspaceContext.getSnapshot();
    if (!snapshot.gatewayId) return;
    const target = managementGateway('panel');
    let workspace;
    try { workspace = managementWorkspace('panel', target); } catch { /* 内容区显示范围错误，标题保留选择入口。 */ }
    const targetLabel = managementTargetDisplayName(target), workspaceLabel = workspace?.name ?? t('workbench.panel.workspace');
    const available = snapshot.workspaces.some(item => item.gateway_id === target.gateway_id);
    const signature = JSON.stringify([target, workspace?.workspace_id, workspaceLabel, available]);
    if (signature === this.scopeSignature) return;
    this.scopeSignature = signature; this.scopeMenu.clear();
    this.scopeMenu.add(MenuRegistry.appendMenuItem(MenuId.PanelTitle, {
      command: { id: selectPanelManagementTargetCommand, title: targetLabel,
        tooltip: `${t('workbench.panel.selectTarget')} · ${targetLabel} / ${workspaceLabel}` }, when: scopeWhen, group: 'navigation', order: -2,
    }));
    this.scopeMenu.add(MenuRegistry.appendMenuItem(MenuId.PanelTitle, {
      command: { id: selectWorkspaceCommand, title: workspaceLabel,
        tooltip: `${t('workbench.panel.selectWorkspace')} · ${workspaceLabel}\n${workspace?.workspace_id ?? ''}`,
        precondition: available ? undefined : ContextKeyExpr.false() }, when: scopeWhen, group: 'navigation', order: -1,
    }));
  }

  private updateProduct(viewId: string): void {
    const current = products.get(viewId);
    // 函数由原产品持有；只有菜单结构／状态变化才重建原生动作。
    const signature = JSON.stringify(current?.controls);
    const previous = this.productMenus.get(viewId);
    if (current && previous?.owner === current.owner && signature === previous.signature) return;
    previous?.menu.dispose(); this.productMenus.delete(viewId);
    if (!current) return;
    const productMenu = new DisposableStore();
    this.productMenus.set(viewId, { owner: current.owner, signature, menu: productMenu });
    for (const [index, control] of current.controls.entries()) {
      const id = `${viewId}.product.${control.id}`;
      const activeControl = () => { const latest = products.get(viewId); return latest?.owner === current.owner ? latest.controls.find(item => item.id === control.id) : undefined; };
      const group = control.primary ? 'navigation' : '2_product';
      const when = ContextKeyExpr.equals('view', viewId);
      if (control.kind === 'select' || control.alternatives?.length) {
        const submenu = MenuId.for(`${id}.choices`);
        productMenu.add(MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
          submenu, title: control.label, isSelection: control.kind === 'select',
          isSplitButton: control.kind === 'action' ? true : undefined,
          when, group, order: index,
        }));
        const options = control.kind === 'select' ? control.options : [{ label: control.label }, ...control.alternatives!];
        options.forEach((option, optionIndex) => {
          const optionId = `${id}.${'value' in option ? encodeURIComponent(option.value) : optionIndex}`;
          productMenu.add(CommandsRegistry.registerCommand(optionId, () => {
            const latest = activeControl();
            if (!latest || latest.disabled) return;
            if (latest.kind === 'select' && 'value' in option) return latest.onChange(option.value);
            if (latest.kind === 'action') return optionIndex === 0 ? latest.run() : latest.alternatives?.[optionIndex - 1]?.run();
          }));
          productMenu.add(MenuRegistry.appendMenuItem(submenu, { command: {
            id: optionId, title: option.label, icon: control.kind === 'action' && optionIndex === 0 ? ThemeIcon.fromId(control.icon ?? 'add') : undefined,
            precondition: control.disabled ? ContextKeyExpr.false() : undefined,
            toggled: control.kind === 'select' && 'value' in option ? { condition: option.value === control.value ? ContextKeyExpr.true() : ContextKeyExpr.false() } : undefined,
          }, group: 'navigation', order: optionIndex }));
        });
      } else {
        productMenu.add(CommandsRegistry.registerCommand(id, () => { const latest = activeControl(); if (latest?.kind === 'action' && !latest.disabled) return latest.run(); }));
        productMenu.add(MenuRegistry.appendMenuItem(MenuId.ViewTitle, {
          command: { id, title: control.label, icon: ThemeIcon.fromId(control.icon ?? 'gear'), precondition: control.disabled ? ContextKeyExpr.false() : undefined },
          when, group, order: index,
        }));
      }
    }
  }
}
registerWorkbenchContribution2('boxteam.panelToolbar', PanelToolbar, WorkbenchPhase.AfterRestored);
