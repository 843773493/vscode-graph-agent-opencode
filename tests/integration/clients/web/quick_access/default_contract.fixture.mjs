import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { PickerEditorState } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/browser/quickaccess.js';
import { GotoSymbolQuickAccessProvider } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/codeEditor/browser/quickaccess/gotoSymbolQuickAccess.js';
import { Event, Emitter } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationTokenSource } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { workspaceFileUri } from '../../../../../src/clients/workbench/src/workspaceFiles.js';
import { HelpQuickAccessProvider } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/browser/helpQuickAccess.js';
import { Registry } from '../../../../../src/clients/workbench/upstream/src/vs/platform/registry/common/platform.js';
import { Extensions } from '../../../../../src/clients/workbench/upstream/src/vs/platform/quickinput/common/quickAccess.js';
import { WorkspaceSymbolsQuickAccess } from '../../../../../src/clients/workbench/src/search/symbolsQuickAccess.js';
import { WorkspaceSymbolProviderRegistry } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/search/common/search.js';
import { invalidateGatewayUserSession } from '../../../../../src/clients/web/src/api/http.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
export { contentBranchContract, nameBranchContract, fileBranchContract, cursorRejectionContract } from './federated_branches.fixture.mjs';

/** 原非文本 Provider 的分页更新，不替代真实 Thread 页面接线验收。 */
export async function outlineContract(accept) {
  const changes = new Emitter(), accepts = new Emitter(), active = new Emitter(), outlineChange = new Emitter();
  const cancellation = new CancellationTokenSource(), reveals = [], previews = [];
  const first = { id: 'first' }, second = { id: 'second' }, more = { id: 'more' };
  let entries = [second, more], restored = 0, disposed = 0;
  const outline = { onDidChange: outlineChange.event, dispose() { disposed++; },
    captureViewState: () => ({ dispose() { restored++; } }),
    preview: entry => { previews.push(entry.id); return { dispose() { previews.push('cancel:' + entry.id); } }; },
    reveal: entry => { reveals.push(entry.id); if (entry === more) { entries = [first, second]; outlineChange.fire({}); } },
    config: { quickPickDataSource: { getQuickPickElements: () => entries.map(entry => ({
      element: entry, label: entry.id, acceptInPlace: entry === more,
    })) } },
  };
  const provider = new GotoSymbolQuickAccessProvider({ activeEditorPane: { getControl() {} }, onDidActiveEditorChange: Event.None },
    { getValue: () => ({}) }, {}, { canCreateOutline: () => true, createOutline: async () => outline }, {}, {});
  const picker = { value: '@', items: [], selectedItems: [], activeItems: [], onDidChangeValue: changes.event,
    onDidChangeActive: active.event, onDidAccept: accepts.event, hide() { this.hidden = true; } };
  const lifetime = provider.provide(picker, cancellation.token);
  try {
    await new Promise(resolve => setTimeout(resolve, 0));
    picker.activeItems = [picker.items[0]];
    active.fire();
    // 原候选第二轮的身份在前插后仍保留，而不是按行号切成 first。
    entries = [first, second, more]; outlineChange.fire({});
    const retained = picker.activeItems[0].label;
    picker.value = '@missing'; changes.fire();
    const controlVisibleWithoutMatches = picker.items.map(item => item.label);
    picker.selectedItems = [picker.items[0]]; accepts.fire();
    const continuedWithoutHide = !picker.hidden && picker.selectedItems.length === 0;
    picker.value = '@'; changes.fire();
    if (accept) {
      picker.selectedItems = [picker.items[0]]; accepts.fire();
    }
    lifetime.dispose();
    return { retained, controlVisibleWithoutMatches, continuedWithoutHide, labels: picker.items.map(item => item.label),
      reveals, previews, restored, disposed, busy: picker.busy };
  } finally {
    lifetime.dispose(); cancellation.dispose(true);
    for (const event of [changes, accepts, active, outlineChange]) event.dispose();
  }
}

const owner = { gatewayId: 'gateway-a', workspaceId: 'workspace-a' };
const root = 'boxteam://workspace/workspace-a/resources/files/root';
const uri = name => workspaceFileUri(owner, root + '/' + name, root);

/** 仅替原 Picker 事件和宿主服务；实际 Provider、取消及接受逻辑均来自生产源码。 */
function defaultAccess(options, history = [], opener = { open() { throw new Error('delegate 不应被 URI opener 抢占'); } },
  editors = { openEditor() { throw new Error('只选择调用方不应打开编辑器'); } }, quickNavigate = false, instantiation) {
  const changes = new Emitter(), accepts = new Emitter(), cancellation = new CancellationTokenSource();
  const picker = { value: '', items: [], activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false },
    buttons: [], quickNavigate: quickNavigate ? { keybindings: [] } : undefined, onDidTriggerButton: Event.None,
    onDidHide: Event.None, onDidChangeActive: Event.None, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; } };
  const group = { id: 1 }, groups = { groups: [], activeGroup: group, getGroup: id => id === group.id ? group : undefined };
  const provider = new WorkspaceQuickAccess(instantiation ?? { createInstance: () => new PickerEditorState(editors, groups) }, editors, groups,
    { getHistory: () => history }, { getValue: () => undefined }, {}, {}, { lookupKeybinding: () => undefined },
    opener, { error(error) { throw error; } }, { enabled: false }, {});
  const lifetime = provider.provide(picker, cancellation.token, options);
  return { picker, accepts, change(value) { picker.value = value; changes.fire(); },
    dispose() { cancellation.dispose(true); lifetime.dispose(); provider.dispose(); changes.dispose(); accepts.dispose(); } };
}

/** 只替换语言服务与模型引用，核真实 Provider 的模式转换和引用释放。 */
export async function fileSymbolContract() {
  const patterns = [], previews = [], symbols = [], releases = [], opened = [];
  const files = [uri('symbol-a.json'), uri('symbol-b.json')];
  const symbolProvider = { getSymbolPicks(model, filter) {
    symbols.push({ resource: model.uri.toString(), filter });
    return Promise.resolve(filter === 'missing' ? [] : [{ index: 0, label: 'beta',
      range: { selection: { startLineNumber: 3, startColumn: 3, endLineNumber: 3, endColumn: 7 },
        decoration: { startLineNumber: 3, startColumn: 1, endLineNumber: 4, endColumn: 1 } } }]);
  } };
  const editors = { openEditor(input, group) {
    (input.options.transient ? previews : opened).push({ resource: input.resource.toString(), options: input.options, group });
    return Promise.resolve();
  } };
  const instantiation = {
    createInstance(ctor) {
      if (ctor === PickerEditorState) return new PickerEditorState(editors, { groups: [] });
      if (ctor === GotoSymbolQuickAccessProvider) return symbolProvider;
      throw new Error('符号合同出现未登记的宿主服务');
    },
    invokeFunction(callback) { return callback({ get: () => ({ createModelReference: async resource => ({
      object: { textEditorModel: { uri: resource } }, dispose() { releases.push(resource.toString()); },
    }) }) }); },
  };
  const f = defaultAccess({ fileSearchDelegate: { search(pattern) {
    patterns.push(pattern); return Promise.resolve({ results: files.map(resource => ({ resource })), messages: [] });
  } } }, [], undefined, editors, false, instantiation);
  let result;
  try {
    const settle = () => new Promise(resolve => setTimeout(resolve, 0));
    await settle();
    f.change('symbol'); await settle();
    const ids = f.picker.items.map(item => item.id);
    f.picker.activeItems = [f.picker.items[1]];
    f.change('symbol@beta'); await settle();
    f.picker.activeItems = [f.picker.items[0]];
    f.change('symbol@missing'); await settle();
    f.change('symbol'); await settle();
    result = { ids, restoredIds: f.picker.items.map(item => item.id), active: f.picker.activeItems[0]?.id };
    f.change('symbol@beta'); await settle();
    f.picker.activeItems = f.picker.selectedItems = [f.picker.items[0]];
    f.accepts.fire({ inBackground: false });
    await settle();
  } finally { f.dispose(); }
  return { ...result, patterns, previews, symbols, releases, opened, target: files[1].toString() };
}

export async function quickNavigateContract() {
  const calls = [], opened = [], fetch = globalThis.fetch;
  const registry = Registry.as(Extensions.Quickaccess);
  const registration = registry.registerQuickAccessProvider({ prefix: '_navigation-contract ', ctor: class {},
    helpEntries: [{ description: '不能混入历史的帮助', commandCenterOrder: 1 }] });
  globalThis.fetch = async input => { calls.push(String(input)); throw new Error('历史导航不得发起网络查询'); };
  const thread = new ThreadInput({ ...owner, workspaceKey: '', sessionId: 'session-a', threadId: 'thread-a', title: 'needle Thread' });
  const first = { resource: uri('needle-recent.txt'), label: 'needle recent' };
  const history = [first, { resource: workspaceFileUri({ ...owner, workspaceId: 'workspace-b' },
    'boxteam://workspace/workspace-b/resources/files/root/needle-foreign.txt',
    'boxteam://workspace/workspace-b/resources/files/root'), label: 'needle foreign' }, thread,
    { ...first }, { resource: uri('needle-hidden.txt'), label: 'needle hidden' }];
  let f;
  try {
    f = defaultAccess({ searchOwner: owner, includeHelp: true, additionPicks: [{ label: 'needle extra' }],
      filter: item => item.label !== 'needle hidden' }, history, undefined,
      { openEditor(input, group) { opened.push({ resource: input.resource.toString(), group, options: input.options }); return Promise.resolve(); } }, true);
    const empty = f.picker.items.map(item => ({ id: item.id, label: item.label }));
    f.change('needle');
    await new Promise(resolve => setTimeout(resolve, 0));
    const filtered = f.picker.items.map(item => ({ id: item.id, label: item.label }));
    f.picker.activeItems = f.picker.selectedItems = [f.picker.items.find(item => item.id === first.resource.toString())];
    f.accepts.fire({ inBackground: false });
    await Promise.resolve();
    return { empty, filtered, buttons: f.picker.buttons.length, opened, calls,
      resources: [first.resource.toString(), thread.resource.toString()] };
  } finally { f?.dispose(); registration.dispose(); globalThis.fetch = fetch; }
}

export function uriContract() {
  const calls = [];
  for (const uri of [root + '/name%40target.txt', root + '/name%40target.txt#L2', 'https://example.test/file',
    'https://example.test:8042', 'https://example.test/file:2:3', 'https://example.test/#7', 'https://example.test/report,12']) {
    const f = defaultAccess({ searchOwner: owner }, [], { open(resource, options) {
      calls.push({ uri: resource, group: options.boxteamTargetGroup, side: options.openToSide,
        pinned: options.editorOptions.pinned, preserveFocus: options.editorOptions.preserveFocus });
      return Promise.resolve(true);
    } });
    try {
      f.change(uri);
      f.picker.keyMods.alt = true;
      f.picker.activeItems = f.picker.selectedItems = [f.picker.items[0]];
      f.accepts.fire({ inBackground: true });
    } finally { f.dispose(); }
  }
  return calls;
}

export async function fileRangeContract(filesOnly) {
  const patterns = [], opened = [];
  const f = defaultAccess({ filesOnly, includeOpenToSideButton: false, fileSearchDelegate: {
    search(pattern) {
      patterns.push(pattern);
      return Promise.resolve({ results: [{ resource: uri(filesOnly && pattern ? pattern : 'target.txt') }], messages: [] });
    },
  } }, [], undefined, { openEditor(input, group) {
    opened.push({ resource: input.resource.toString(), selection: input.options.selection ?? null, group });
    return Promise.resolve();
  } });
  try {
    await new Promise(resolve => setTimeout(resolve, 0));
    f.change('target.txt:7:3');
    await new Promise(resolve => setTimeout(resolve, 0));
    const first = f.picker.items[0];
    f.change('target.txt:9:2');
    await new Promise(resolve => setTimeout(resolve, 0));
    f.picker.activeItems = f.picker.selectedItems = [f.picker.items[0]];
    f.accepts.fire({ inBackground: false });
    await Promise.resolve();
    return { patterns, samePick: first === f.picker.items[0], opened };
  } finally { f.dispose(); }
}

export async function delegateContract() {
  const calls = [], observed = {};
  const f = defaultAccess({ includeOpenToSideButton: false, fileSearchDelegate: {
    search(pattern, token) { return new Promise(resolve => calls.push({ pattern, token, resolve })); },
    getItemDescription: () => '调用方目录',
  } });
  try {
    observed.initial = calls.map(call => call.pattern);
    f.change('https://example.test/file');
    observed.patterns = calls.map(call => call.pattern);
    observed.cancelledPrevious = calls[0].token.isCancellationRequested;
    f.change('delegate');
    calls[2].resolve({ results: [{ resource: uri('delegate.txt') }], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.picks = f.picker.items.map(item => ({ label: item.label, description: item.description, buttons: item.buttons }));
    calls[0].resolve({ results: [{ resource: uri('old.txt') }], messages: [] });
    calls[1].resolve({ results: [], messages: [] });
    await Promise.resolve(); await Promise.resolve();
    observed.afterLate = f.picker.items.map(item => item.label);
    f.change('cancel');
  } finally { f.dispose(); }
  observed.cancelledOnDispose = calls.at(-1).token.isCancellationRequested;
  calls.at(-1).resolve({ results: [], messages: [] });
  return observed;
}

export async function identityContract() {
  const results = [];
  for (const kind of ['user', 'gateway']) {
    const before = workspaceContext.getSnapshot();
    workspaceContext.publish({ ...before, gatewayId: 'gateway-a' });
    const calls = [];
    const f = defaultAccess({ fileSearchDelegate: { search(pattern, token) {
      return new Promise(resolve => calls.push({ pattern, token, resolve }));
    } } });
    try {
      if (kind === 'user') invalidateGatewayUserSession(0);
      else workspaceContext.publish({ ...before, gatewayId: 'gateway-b' });
      const cancelled = calls[0].token.isCancellationRequested;
      calls[0].resolve({ results: [{ resource: uri('old-user.txt') }], messages: [] });
      await Promise.resolve(); await Promise.resolve();
      f.change('new query');
      results.push({ kind, cancelled, calls: calls.length, busy: f.picker.busy,
        labels: f.picker.items.map(item => item.label), selected: f.picker.selectedItems.length });
    } finally { f.dispose(); workspaceContext.publish(before); }
  }
  // 已释放 Provider 不再订阅失效通知。
  invalidateGatewayUserSession(0);
  return results;
}

export function selectContract() {
  const received = [], observed = {};
  const f = defaultAccess({ searchOwner: owner, additionPicks: [{ label: '附件来源', kind: 'contextPick' }, { label: '隐藏项' }],
    filter: item => item.label !== '隐藏项', handleAccept: (item, background) => received.push({ label: item.label, kind: item.kind, background }) },
  [{ resource: uri('history.txt') }, new ThreadInput({ ...owner, workspaceKey: '', sessionId: 'session-a', threadId: 'thread-a', title: '历史 Thread' })]);
  try {
    observed.labels = f.picker.items.filter(item => item.type !== 'separator').map(item => item.label);
    f.picker.activeItems = [f.picker.items[0]];
    f.accepts.fire({ inBackground: true });
    observed.backgroundKeptOpen = !f.picker.hidden;
    f.accepts.fire({ inBackground: false });
    observed.foregroundClosed = f.picker.hidden;
    observed.received = received;
    return observed;
  } finally { f.dispose(); }
}

export async function symbolContract() {
  const calls = [], queried = Promise.withResolvers();
  const registration = WorkspaceSymbolProviderRegistry.register({ provideWorkspaceSymbols(query) { calls.push(query); queried.resolve(); return []; } });
  const provider = new WorkspaceSymbolsQuickAccess({}, {}, {}, { getValue: () => ({}) }, {}, {});
  const changes = new Emitter(), accepts = new Emitter(), token = new CancellationTokenSource();
  const picker = { value: '#needle', items: [], activeItems: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None };
  const lifetime = [];
  try {
    lifetime.push(provider.provide(picker, token.token, { searchOwner: owner }));
    // 原全局查询有 200ms debounce；等待越过该边界，不能在它尚未执行时宣称未查询。
    await new Promise(resolve => setTimeout(resolve, 250));
    const scopedLabel = picker.items[0].label;
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token, { searchOwnerError: '原 Gateway 身份失效' }));
    await new Promise(resolve => setTimeout(resolve, 250));
    const errorLabel = picker.items[0].label;
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token, { searchOwners: [owner, { ...owner, workspaceId: 'workspace-b' }] }));
    await new Promise(resolve => setTimeout(resolve, 250));
    const multiLabel = picker.items[0].label, multiPlaceholder = picker.placeholder;
    const scopedCalls = [...calls];
    lifetime.pop().dispose();
    lifetime.push(provider.provide(picker, token.token));
    await queried.promise;
    return { scopedLabel, errorLabel, multiLabel, multiPlaceholder, scopedCalls, nativeCalls: calls };
  } finally {
    token.dispose(true);
    lifetime.forEach(item => item.dispose());
    provider.dispose(); registration.dispose(); changes.dispose(); accepts.dispose();
  }
}

export function helpContract() {
  const registry = Registry.as(Extensions.Quickaccess), registrations = [], accepts = new Emitter(), changes = new Emitter();
  const shows = [], handleAccept = () => {}, options = { searchOwner: owner, handleAccept, includeHelp: true };
  for (const prefix of ['', '_contract ']) registrations.push(registry.registerQuickAccessProvider({ prefix,
    ctor: class {}, helpEntries: [{ description: '合同装具' }] }));
  const help = new HelpQuickAccessProvider({ quickAccess: { show: (value, options) => shows.push({ value, options }) } },
    { lookupKeybinding: () => undefined }, {});
  const picker = { onDidAccept: accepts.event, onDidChangeValue: changes.event, selectedItems: [], items: [] };
  const cancellation = new CancellationTokenSource(), lifetime = help.provide(picker, cancellation.token, options);
  try {
    picker.selectedItems = [picker.items.find(item => item.prefix === '')];
    accepts.fire();
    changes.fire('?_contract ');
    return { values: shows.map(show => show.value), preserve: shows.every(show => show.options.preserveValue),
      sameOwner: shows.every(show => show.options.providerOptions.searchOwner === owner),
      sameReceiver: shows.every(show => show.options.providerOptions.handleAccept === handleAccept),
      includeHelp: shows.map(show => show.options.providerOptions.includeHelp), sourceHelpUnchanged: options.includeHelp };
  } finally {
    lifetime.dispose(); cancellation.dispose(true); accepts.dispose(); changes.dispose();
    for (const registration of registrations) registration.dispose();
  }
}
