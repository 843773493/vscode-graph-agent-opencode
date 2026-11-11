import { Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { DisposableStore, toDisposable } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Registry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { Extensions } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { QuickAccessController } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/quickAccess.js';

/** 运行同一源码转换后的原 Controller；只替换 Picker 事件和读取服务边界。 */
export async function quickAccessControllerContract({ action, canPickMany }) {
  const lifetime = new DisposableStore(), changes = lifetime.add(new Emitter()), hides = lifetime.add(new Emitter());
  const accepts = lifetime.add(new Emitter()), willAccept = lifetime.add(new Emitter());
  let readerCancelled = 0, providerDisposed = 0, executions = 0;
  const items = [{ label: 'alpha.txt' }, { label: 'beta.txt' }];
  const picker = {
    value: '', items: [], selectedItems: [], activeItems: [], canSelectMany: false,
    onDidChangeValue: changes.event, onDidHide: hides.event, onDidAccept: accepts.event, onWillAccept: willAccept.event,
    show() {}, hide() { hides.fire(); }, dispose() {},
    accept() {
      let vetoed = false;
      willAccept.fire({ veto() { vetoed = true; } });
      if (!vetoed) accepts.fire({ inBackground: false });
    },
  };
  const prefix = '_controller-contract ';
  class Provider {
    provide(picker, token) {
      const reader = new DisposableStore();
      reader.add(token.onCancellationRequested(() => { readerCancelled++; }));
      reader.add(picker.onDidAccept(() => { executions++; picker.hide(); }));
      reader.add(toDisposable(() => { providerDisposed++; }));
      picker.items = items;
      return reader;
    }
  }
  lifetime.add(Registry.as(Extensions.Quickaccess).registerQuickAccessProvider({ prefix, ctor: Provider }));
  const controller = lifetime.add(new QuickAccessController({ createQuickPick: () => picker },
    { createInstance: ctor => new ctor() }, { contextMatchesRules: () => true, getContextKeyValue: () => undefined }, {}));
  const options = { canPickMany, preserveValue: true, enabledProviderPrefixes: [prefix], providerOptions: { fileSearchDelegate: {} } };
  try {
    const pending = action === 'show' ? controller.show(prefix, options) : controller.pick(prefix, options);
    picker.activeItems = [items[0]];
    picker.selectedItems = action === 'cancel' && !canPickMany ? [] : canPickMany && action !== 'show' ? items : [items[0]];
    if (action === 'cancel') picker.hide();
    else picker.accept();
    const selected = await pending;
    return { labels: selected?.map(item => item.label), canSelectMany: picker.canSelectMany,
      readerCancelled, providerDisposed, executions };
  } finally { lifetime.dispose(); }
}
