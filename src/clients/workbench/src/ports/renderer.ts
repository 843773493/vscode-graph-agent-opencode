import * as dom from '../../upstream/src/vs/base/browser/dom.js';
import { ActionBar } from '../../upstream/src/vs/base/browser/ui/actionbar/actionbar.js';
import { InlineEditInputBox } from '../../upstream/src/vs/workbench/browser/parts/views/inlineEditInputBox.js';
import { SelectBox } from '../../upstream/src/vs/base/browser/ui/selectBox/selectBox.js';
import { getDefaultHoverDelegate } from '../../upstream/src/vs/base/browser/ui/hover/hoverDelegateFactory.js';
import type { ITableColumn, ITableRenderer } from '../../upstream/src/vs/base/browser/ui/table/table.js';
import type { IAction } from '../../upstream/src/vs/base/common/actions.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import type { IHoverService } from '../../upstream/src/vs/platform/hover/browser/hover.js';
import type { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { defaultSelectBoxStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { t } from '../../../shared/core/i18n/index.js';
import { ADD, NEW, type Field, type PortModel, type Row } from './model.js';

export type Column = Field | 'state' | 'error';
export const columns: readonly { field: Column; label: string; min: number; weight: number }[] = [
  { field: 'state', label: '', min: 32, weight: 32 },
  { field: 'label', label: 'hostPorts.name', min: 100, weight: 125 },
  { field: 'listenerHost', label: 'hostPorts.listener', min: 110, weight: 130 },
  { field: 'listenerAddress', label: 'hostPorts.listenAddress', min: 110, weight: 125 },
  { field: 'listenerPort', label: 'hostPorts.listenPort', min: 80, weight: 80 },
  { field: 'destinationHost', label: 'hostPorts.destination', min: 110, weight: 130 },
  { field: 'destinationAddress', label: 'hostPorts.serviceAddress', min: 110, weight: 125 },
  { field: 'destinationPort', label: 'hostPorts.servicePort', min: 80, weight: 80 },
  { field: 'protocol', label: 'hostPorts.protocol', min: 70, weight: 75 },
  { field: 'error', label: 'hostPorts.errorColumn', min: 180, weight: 320 },
];
export interface Edit { id: string; field: Field; value: string }
export interface GridDelegate {
  model: PortModel;
  editing(): Edit | undefined;
  beginEdit(id: string, field: Field): void;
  finishEdit(value: string, next: -1 | 0 | 1): void;
  cancelEdit(): void;
  actions(id: string, field: Column): IAction[];
}
interface Template { root: HTMLElement; label: HTMLElement; actions: ActionBar; lifetime: DisposableStore; cell: DisposableStore }

export function gridColumns(): ITableColumn<Row, Row>[] {
  return columns.map(column => ({ label: column.label ? t(column.label) : '', weight: column.weight,
    minimumWidth: column.min, maximumWidth: column.field === 'state' ? 32 : undefined, templateId: `port-${column.field}`,
    project: row => row }));
}

/** 虚拟行只渲染单行控件，复用上游输入、选择器、ActionBar 与 Hover。 */
export class PortRenderer implements ITableRenderer<Row, Template> {
  readonly templateId: string;
  constructor(private readonly field: Column, private readonly owner: GridDelegate, private readonly contexts: IContextViewService, private readonly hover: IHoverService, private readonly instantiation: IInstantiationService) { this.templateId = `port-${field}`; }

  renderTemplate(container: HTMLElement): Template {
    const root = dom.append(container, dom.$('.port-grid-cell'));
    const label = dom.append(root, dom.$('span.port-grid-label'));
    const actionNode = dom.append(root, dom.$('.port-grid-actions'));
    const lifetime = new DisposableStore();
    return { root, label, actions: lifetime.add(new ActionBar(actionNode)), lifetime, cell: lifetime.add(new DisposableStore()) };
  }

  renderElement(row: Row, _index: number, template: Template): void {
    const field = this.field;
    template.cell.clear(); template.actions.clear(); template.label.replaceChildren();
    template.label.classList.remove('select-container');
    const { model } = this.owner;
    template.root.dataset.portId = row.id;
    template.root.dataset.field = field;
    template.root.classList.toggle('port-grid-state', field === 'state');
    template.root.classList.toggle('port-grid-error', field === 'error' && !!model.error(row.id));
    template.root.classList.toggle('port-grid-add', row.id === ADD);
    const edit = this.owner.editing();
    const editing = row.id !== ADD && edit?.id === row.id && edit.field === field;
    template.root.classList.toggle('port-grid-editing', editing);
    if (editing) {
      this.renderEditor(edit, template);
      return;
    }
    let text = '';
    if (row.id === ADD) text = field === 'label' ? t('hostPorts.add') : '';
    else if (field === 'error') text = model.error(row.id);
    else if (field !== 'state') {
      const value = model.values(row.id)[field];
      text = field === 'listenerHost' || field === 'destinationHost' ? model.hostLabel(value)
        : field === 'protocol' ? value.toUpperCase() : value || (field === 'label' ? t(row.id === NEW ? 'hostPorts.unsaved' : 'hostPorts.unnamed') : '—');
    }
    template.label.textContent = text;
    if (text) {
      const host = field === 'listenerHost' || field === 'destinationHost' ? model.hosts.find(host => host.host_id === model.values(row.id)[field]) : undefined;
      const detail = host ? `${text} · ${host.kind === 'local' ? t('hostPorts.gatewayMachine') : host.description}` : text;
      template.cell.add(this.hover.setupManagedHover(getDefaultHoverDelegate('mouse'), template.label, detail));
    }
    if (row.id !== ADD && field !== 'state' && field !== 'error') {
      template.cell.add(dom.addDisposableListener(template.root, 'dblclick', event => {
        event.preventDefault(); event.stopPropagation(); this.owner.beginEdit(row.id, field);
      }));
    }
    const actions = this.owner.actions(row.id, field);
    template.actions.push(actions, { icon: true, label: false });
    if (field === 'state' && row.id !== ADD) {
      const state = model.busy.has(row.id) ? t('hostPorts.starting') : row.id === NEW ? t('hostPorts.unsaved') : t(`hostPorts.${model.item(row.id)!.status}`);
      template.cell.add(this.hover.setupManagedHover(getDefaultHoverDelegate('mouse'), template.root, `${state} · ${t('hostPorts.stateHint')}`));
    }
  }

  private renderEditor(edit: Edit, template: Template): void {
    const label = t(columns.find(column => column.field === edit.field)!.label);
    const host = edit.field === 'listenerHost' || edit.field === 'destinationHost';
    if (host || edit.field === 'protocol') {
      const options = host ? this.owner.model.hosts.map(item => ({ value: item.host_id,
        text: `${item.label}${item.kind === 'local' ? ` · ${t('hostPorts.gatewayMachine')}` : ''}`, description: item.description }))
        : ['tcp', 'http', 'https'].map(value => ({ value, text: value.toUpperCase(), description: '' }));
      const select = template.cell.add(new SelectBox(options, options.findIndex(option => option.value === edit.value), this.contexts, defaultSelectBoxStyles, { ariaLabel: label }));
      select.render(template.label);
      template.cell.add(select.onDidSelect(event => this.owner.finishEdit(options[event.index].value, 0)));
      template.cell.add(dom.addDisposableListener(template.label, 'keydown', event => {
        if (event.key === 'Escape') { event.stopPropagation(); this.owner.cancelEdit(); }
        else if (event.key === 'Tab') { event.preventDefault(); event.stopPropagation(); this.owner.finishEdit(edit.value, event.shiftKey ? -1 : 1); }
      }));
    } else {
      const editor = template.cell.add(this.instantiation.createInstance(InlineEditInputBox, template.label, {
        startingValue: edit.value,
        validationMessage: () => null,
        onFinish: async (value, accepted) => { if (accepted) await this.owner.finishEdit(value, 0); else this.owner.cancelEdit(); },
      }, { ariaLabel: label }));
      const input = editor.inputBox;
      template.cell.add(input.onDidChange(value => { edit.value = value; }));
      template.cell.add(dom.addDisposableListener(input.inputElement, 'keydown', event => {
        if (event.key === 'Tab' && !event.isComposing) {
          event.preventDefault(); event.stopPropagation();
          const value = input.value;
          editor.dispose();
          this.owner.finishEdit(value, event.shiftKey ? -1 : 1);
        }
      }));
    }
  }
  disposeElement(_element: Row, _index: number, template: Template): void { template.cell.clear(); }
  disposeTemplate(template: Template): void { template.lifetime.dispose(); }
}
