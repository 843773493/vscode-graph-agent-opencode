import * as dom from '../../upstream/src/vs/base/browser/dom.js';
import { DomScrollableElement } from '../../upstream/src/vs/base/browser/ui/scrollbar/scrollableElement.js';
import { ScrollbarVisibility } from '../../upstream/src/vs/base/common/scrollable.js';
import { Separator, SubmenuAction, toAction, type IAction } from '../../upstream/src/vs/base/common/actions.js';
import { ViewPane, type IViewPaneOptions } from '../../upstream/src/vs/workbench/browser/parts/views/viewPane.js';
import { WorkbenchTable } from '../../upstream/src/vs/platform/list/browser/listService.js';
import { IKeybindingService } from '../../upstream/src/vs/platform/keybinding/common/keybinding.js';
import { IContextMenuService, IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IConfigurationService } from '../../upstream/src/vs/platform/configuration/common/configuration.js';
import { IContextKeyService } from '../../upstream/src/vs/platform/contextkey/common/contextkey.js';
import { IViewDescriptorService } from '../../upstream/src/vs/workbench/common/views.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IOpenerService } from '../../upstream/src/vs/platform/opener/common/opener.js';
import { IThemeService } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { IHoverService } from '../../upstream/src/vs/platform/hover/browser/hover.js';
import { IClipboardService } from '../../upstream/src/vs/platform/clipboard/common/clipboardService.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { IProgressRunner } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { t } from '../../../shared/core/i18n/index.js';
import { publishPanelToolbar, releasePanelToolbar } from '../panelToolbar.js';
import { ADD, NEW, PortModel, editableFields, type Field, type Row } from './model.js';
import { PortRenderer, gridColumns, columns, type Column, type Edit, type GridDelegate } from './renderer.js';
import './view.css';

/** 两端 Host 都归规则本身；视图不读取导航工作区或面板范围。 */
export class PortsView extends ViewPane implements GridDelegate {
  readonly model = this._register(new PortModel());
  private table?: WorkbenchTable<Row>;
  private viewport?: HTMLElement;
  private tableNode?: HTMLElement;
  private scroll?: DomScrollableElement;
  private status?: HTMLElement;
  private progress?: IProgressRunner;
  private edit?: Edit;
  private height = 0;
  private width = 0;

  constructor(options: IViewPaneOptions,
    @IKeybindingService keybindings: IKeybindingService,
    @IContextMenuService private readonly menus: IContextMenuService,
    @IConfigurationService configuration: IConfigurationService,
    @IContextKeyService contexts: IContextKeyService,
    @IViewDescriptorService views: IViewDescriptorService,
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @IOpenerService opener: IOpenerService,
    @IThemeService themes: IThemeService,
    @IHoverService private readonly hover: IHoverService,
    @IContextViewService private readonly contextViews: IContextViewService,
    @IClipboardService private readonly clipboard: IClipboardService,
    @INotificationService private readonly notifications: INotificationService,
  ) {
    super(options, keybindings, menus, configuration, contexts, views, instantiation, opener, themes, hover);
    this._register(this.model.onDidChange(() => this.update()));
    this._register(this.onDidChangeBodyVisibility(visible => { if (visible) void this.model.refresh(); }));
    this._register({ dispose: () => { this.progress?.done(); releasePanelToolbar(this); } });
  }

  protected override renderBody(container: HTMLElement): void {
    super.renderBody(container);
    container.classList.add('host-ports-view');
    this.status = dom.append(container, dom.$('.host-ports-status'));
    this.status.setAttribute('role', 'alert');
    this.status.hidden = true;
    this.viewport = dom.$('.host-ports-viewport');
    this.tableNode = dom.append(this.viewport, dom.$('.host-ports-table'));
    this.scroll = this._register(new DomScrollableElement(this.viewport, { horizontal: ScrollbarVisibility.Auto, vertical: ScrollbarVisibility.Hidden }));
    container.appendChild(this.scroll.getDomNode());
    const renderers = columns.map(column => new PortRenderer(column.field, this, this.contextViews, this.hover, this.instantiation));
    this.table = this._register(this.instantiation.createInstance(WorkbenchTable<Row>, 'HostPortForwarding', this.tableNode,
      { headerRowHeight: 26, getHeight: () => 26 }, gridColumns(), renderers, {
        identityProvider: { getId: row => row.id }, multipleSelectionSupport: true,
        accessibilityProvider: { getWidgetAriaLabel: () => t('hostPorts.title'), getAriaLabel: row => row.id === ADD ? t('hostPorts.add')
          : `${this.model.values(row.id).label || t('hostPorts.unnamed')}, ${this.model.error(row.id)}` },
        keyboardNavigationLabelProvider: { getKeyboardNavigationLabel: row => row.id === ADD ? t('hostPorts.add') : this.model.values(row.id).label },
      }));
    this._register(this.table.onContextMenu(event => {
      const id = event.element?.id;
      this.menus.showContextMenu({ getAnchor: () => event.anchor, getActions: () => this.menu(id) });
    }));
    this._register(this.table.onMouseClick(event => { if (event.element?.id === ADD) this.add(); }));
    this._register(dom.addDisposableListener(this.tableNode, 'keydown', event => this.keydown(event)));
    this.update();
    if (this.isBodyVisible()) void this.model.refresh();
  }
  override focus(): void { super.focus(); this.table?.domFocus(); }
  protected override layoutBody(height: number, width: number): void {
    super.layoutBody(height, width); this.height = height; this.width = width; this.layoutGrid();
  }
  private layoutGrid(): void {
    if (!this.table || !this.viewport || !this.tableNode) return;
    const height = Math.max(0, this.height);
    const width = Math.max(this.width, columns.reduce((sum, column) => sum + column.min, 0));
    this.viewport.style.width = `${this.width}px`; this.viewport.style.height = `${height}px`;
    this.tableNode.style.width = `${width}px`; this.tableNode.style.height = `${Math.max(0, height - (width > this.width ? 12 : 0))}px`;
    this.table.layout(Math.max(0, height - (width > this.width ? 12 : 0)), width);
    this.scroll?.scanDomNode();
  }
  private update(): void {
    if (!this.table) return;
    const refocus = !!this.tableNode?.contains(document.activeElement);
    const focused = this.table.getFocusedElements().map(row => row.id);
    const selected = this.table.getSelectedElements().map(row => row.id);
    const scrollTop = this.table.scrollTop;
    const rows = this.model.rows;
    this.table.splice(0, this.table.length, rows);
    this.table.setFocus(rows.flatMap((row, index) => focused.includes(row.id) ? [index] : []));
    this.table.setSelection(rows.flatMap((row, index) => selected.includes(row.id) ? [index] : []));
    this.table.scrollTop = scrollTop;
    if (this.status) {
      this.status.hidden = !this.model.loadError;
      this.status.textContent = this.model.loadError;
    }
    if (this.model.loading) this.progress ??= this.getProgressIndicator().show(true, 800);
    else { this.progress?.done(); this.progress = undefined; }
    publishPanelToolbar(this, this.id, [
      { id: 'add', kind: 'action', primary: true, icon: 'add', label: t('hostPorts.add'), disabled: this.model.hosts.length === 0, run: () => this.add() },
      { id: 'refresh', kind: 'action', primary: true, icon: 'refresh', label: t('hostPorts.refresh'), disabled: this.model.loading || this.model.busy.size > 0, run: () => this.model.refresh() },
    ]);
    this.layoutGrid();
    if (refocus) this.focusEdit();
  }
  editing(): Edit | undefined { return this.edit; }
  private add(): void { this.model.add(); this.beginEdit(NEW, 'listenerHost'); }
  async beginEdit(id: string, field: Field): Promise<void> {
    if (this.model.busy.has(id)) return;
    if (this.edit && (this.edit.id !== id || this.edit.field !== field)) await this.finishEdit(this.edit.value, 0);
    this.edit = { id, field, value: this.model.values(id)[field] };
    const index = this.model.rows.findIndex(row => row.id === id);
    this.table?.setFocus([index]); this.table?.reveal(index);
    this.update(); this.focusEdit();
  }
  private focusEdit(): void {
    if (!this.edit || !this.tableNode) return;
    for (const cell of this.tableNode.querySelectorAll<HTMLElement>('.port-grid-cell')) {
      if (cell.dataset.portId !== this.edit.id || cell.dataset.field !== this.edit.field) continue;
      cell.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const input = cell.querySelector<HTMLInputElement | HTMLSelectElement>('input,select');
      input?.focus();
    }
  }
  async finishEdit(value: string, next: -1 | 0 | 1): Promise<void> {
    const edit = this.edit;
    if (!edit) return;
    this.edit = undefined;
    await this.model.edit(edit.id, edit.field, value);
    this.update();
    if (this.edit) return;
    if (next && !this.model.error(edit.id)) this.beginEdit(edit.id, editableFields[(editableFields.indexOf(edit.field) + next + editableFields.length) % editableFields.length]);
    else this.table?.domFocus();
  }
  cancelEdit(): void { this.edit = undefined; this.update(); this.table?.domFocus(); }
  private action(id: string, label: string, icon: string, run: () => unknown, enabled = true): IAction {
    return toAction({ id: `boxteam.ports.${id}`, label, class: `codicon codicon-${icon}`, enabled,
      run: async () => { await run(); } });
  }
  private startAction(id: string): IAction {
    const item = this.model.item(id), busy = this.model.busy.has(id);
    return this.action('toggle', t(item?.status === 'active' ? 'hostPorts.stop' : 'hostPorts.start'),
      busy ? 'loading codicon-modifier-spin' : item?.status === 'active' ? 'circle-filled' : item?.status === 'error' ? 'error' : 'circle-outline',
      async () => { if (this.edit?.id === id) await this.finishEdit(this.edit.value, 0); await this.model.toggle(id); }, !busy);
  }
  private endpoint(id: string, side: 'listener' | 'destination'): string {
    const values = this.model.values(id), address = values[`${side}Address`];
    return `${this.model.hostLabel(values[`${side}Host`])} · ${address.includes(':') ? `[${address}]` : address}:${values[`${side}Port`]}`;
  }
  private copy(id: string, side?: 'listener' | 'destination'): Promise<void> {
    return this.clipboard.writeText(side ? this.endpoint(id, side) : `${this.endpoint(id, 'listener')} → ${this.endpoint(id, 'destination')}`);
  }
  actions(id: string, field: Column): IAction[] {
    if (id === ADD) return [];
    const enabled = !this.model.busy.has(id);
    if (field === 'state') return [this.startAction(id)];
    if (field === 'label') return id === NEW ? [
      this.action('save', t('hostPorts.saveStopped'), 'check', async () => { if (this.edit?.id === id) await this.finishEdit(this.edit.value, 0); await this.model.saveNew(false); }, enabled),
      this.action('cancel', t('hostPorts.cancel'), 'close', () => { this.cancelEdit(); this.model.cancelNew(); }, enabled),
    ] : [this.action('rename', t('hostPorts.rename'), 'edit', () => this.beginEdit(id, 'label'), enabled)];
    if (field === 'listenerAddress' || field === 'destinationAddress') return [this.action(`copy.${field}`, t('hostPorts.copy'), 'copy', () => this.copy(id, field === 'listenerAddress' ? 'listener' : 'destination')),
      ...(field === 'listenerAddress' && id !== NEW ? [this.action('delete', t('hostPorts.delete'), 'trash', () => this.model.remove(id), enabled)] : [])];
    if (field === 'error' && this.model.error(id)) return [this.action('error.copy', t('hostPorts.copyError'), 'copy', () => this.clipboard.writeText(this.model.error(id))),
      this.action('error.details', t('hostPorts.errorDetails'), 'info', () => this.notifications.error(this.model.error(id)))];
    return [];
  }
  private menu(id?: string): IAction[] {
    const add = this.action('add', t('hostPorts.add'), 'add', () => this.add());
    if (!id || id === ADD) return [add];
    const enabled = !this.model.busy.has(id);
    const edit = (field: Field) => this.action(`edit.${field}`, t(columns.find(column => column.field === field)!.label), 'edit', () => this.beginEdit(id, field), enabled);
    const actions: IAction[] = [this.startAction(id)];
    if (id !== NEW) actions.push(this.action('restart', t('hostPorts.restart'), 'debug-restart', () => this.model.restart(id), enabled));
    actions.push(new Separator(), new SubmenuAction('boxteam.ports.copy', t('hostPorts.copyAddress'), [
      this.action('copy.listener', t('hostPorts.listener'), 'copy', () => this.copy(id, 'listener')),
      this.action('copy.destination', t('hostPorts.destination'), 'copy', () => this.copy(id, 'destination')),
      this.action('copy.rule', t('hostPorts.copyRule'), 'copy', () => this.copy(id)),
    ]), new SubmenuAction('boxteam.ports.listener', t('hostPorts.editListener'), [edit('listenerHost'), edit('listenerAddress'), edit('listenerPort')]),
    new SubmenuAction('boxteam.ports.destination', t('hostPorts.editDestination'), [edit('destinationHost'), edit('destinationAddress'), edit('destinationPort')]),
    new SubmenuAction('boxteam.ports.protocol', t('hostPorts.protocol'), ['tcp', 'http', 'https'].map(protocol => {
      const action = this.action(`protocol.${protocol}`, protocol.toUpperCase(), 'circle-outline', () => this.model.edit(id, 'protocol', protocol), enabled);
      action.checked = this.model.values(id).protocol === protocol; return action;
    })), this.action('rename', t('hostPorts.rename'), 'edit', () => this.beginEdit(id, 'label'), enabled));
    if (this.model.error(id)) actions.push(new Separator(), ...this.actions(id, 'error'));
    if (id === NEW) actions.push(...this.actions(id, 'label'));
    else actions.push(new Separator(), this.action('delete', t('hostPorts.delete'), 'trash', () => this.model.remove(id), enabled));
    return [...actions, new Separator(), add];
  }
  private keydown(event: KeyboardEvent): void {
    if (event.isComposing || (event.target instanceof Element && event.target.closest('input,select,.monaco-select-box'))) return;
    const selected = this.table?.getSelectedElements() ?? [];
    const rows = selected.length ? selected : this.table?.getFocusedElements() ?? [];
    const ids = rows.map(row => row.id).filter(id => id !== ADD);
    if (event.key === 'F2' && ids.length === 1) { event.preventDefault(); this.beginEdit(ids[0], 'label'); }
    else if (event.key === 'Delete' && ids.length) { event.preventDefault(); for (const id of ids) void this.model.remove(id); }
    else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c' && ids.length) { event.preventDefault(); void this.clipboard.writeText(ids.map(id => `${this.endpoint(id, 'listener')} → ${this.endpoint(id, 'destination')}`).join('\n')); }
    else if (event.key === 'Enter' && rows.some(row => row.id === ADD)) { event.preventDefault(); this.add(); }
  }
}
