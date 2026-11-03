import { Emitter, Event } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { Disposable } from '../../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { QuickInputType } from '../../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickInput.js';

/** 仅提供原 Controller 所需的宿主事件；候选、前缀和接受逻辑来自生产实现。 */
class Picker extends Disposable {
  type = QuickInputType.QuickPick;
  items = []; activeItems = []; selectedItems = []; buttons = [];
  keyMods = { alt: false, ctrlCmd: false };
  _value = ''; visible = false;
  constructor(host) {
    super();
    this.host = host;
    for (const name of ['DidChangeValue', 'DidAccept', 'WillAccept', 'DidHide']) {
      this['emit' + name] = this._register(new Emitter());
      this['on' + name] = this['emit' + name].event;
    }
    this.onDidChangeActive = this.onDidTriggerItemButton = this.onDidTriggerSeparatorButton = this.onDidTriggerButton = Event.None;
  }
  get value() { return this._value; }
  set value(value) { if (value !== this._value) { this._value = value; this.emitDidChangeValue.fire(value); } }
  show() {
    if (this.host.currentQuickInput !== this) this.host.currentQuickInput?.hide();
    this.host.currentQuickInput = this; this.visible = true;
  }
  hide() {
    if (!this.visible) return;
    this.visible = false;
    if (this.host.currentQuickInput === this) this.host.currentQuickInput = undefined;
    this.emitDidHide.fire({ reason: 1 }); this.host.hidden.fire();
  }
  accept(item, inBackground = false) {
    this.activeItems = this.selectedItems = [item];
    let vetoed = false;
    this.emitWillAccept.fire({ veto() { vetoed = true; } });
    if (!vetoed) this.emitDidAccept.fire({ inBackground });
  }
  dispose() { this.hide(); super.dispose(); }
}

export class PickerHost extends Disposable {
  pickers = []; shows = [];
  hidden = this._register(new Emitter());
  onHide = this.hidden.event;
  createQuickPick() {
    const picker = new Picker(this);
    this.pickers.push(picker);
    return picker;
  }
  connect(controller) {
    this.quickAccess = { show: (value, options) => { this.shows.push({ value, options }); controller.show(value, options); } };
  }
  dispose() { for (const picker of this.pickers) picker.dispose(); super.dispose(); }
}
