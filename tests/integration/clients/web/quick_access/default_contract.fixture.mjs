import '../../../../harness/js/workbench-locale.ts';
import { WorkspaceQuickAccess } from '../../../../../src/clients/workbench/src/search/quickAccess.js';
import { ThreadInput } from '../../../../../src/clients/workbench/src/threadInput.js';
import { ThreadPane } from '../../../../../src/clients/workbench/src/threadPane.js';
import { InMemoryStorageService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/storage/common/storage.js';
import { NotificationService } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/services/notification/common/notificationService.js';
import { FileService } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/fileService.js';
import { FileOperationError, FileOperationResult } from '../../../../../src/clients/workbench/upstream/src/vs/platform/files/common/files.js';
import { TextFileSaveErrorHandler } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/contrib/files/browser/editors/textFileSaveErrorHandler.js';
import { URI } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { VSBuffer } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/buffer.js';
import { ReportedError, ReportedAggregateError } from '../../../../../src/clients/web/src/utils/errorMessage.js';
import { HttpRequestError } from '../../../../../src/clients/web/src/api/http.js';
import { CancellationToken } from '../../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { EditorPaneSelectionChangeReason } from '../../../../../src/clients/workbench/upstream/src/vs/workbench/common/editor.js';
import { ChatSurface } from '../../../../../src/clients/workbench/src/react/chatView.js';
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
import { invalidateGatewayToken } from '../../../../../src/clients/web/src/api/http.js';
import { workspaceContext } from '../../../../../src/clients/workbench/src/workspaceContext.js';
export { contentBranchContract, nameBranchContract, fileBranchContract, cursorRejectionContract, branchStateContract } from './federated_branches.fixture.mjs';
export { contentSelectionContract } from './content_selection.fixture.mjs';
export { quickAccessControllerContract } from './quick_access_controller.fixture.mjs';
export { typingSearchContract } from './typing_performance.fixture.mjs';
export { canonicalSearchContract } from './canonical_search.fixture.mjs';

/** 原生通知模型与真实转换出口；仅窗口日志collector为显式宿主边界。 */
export async function notificationFailureContract() {
  const storage = new InMemoryStorageService(), logs = [];
  const service = new NotificationService(storage, { error: message => logs.push(message) });
  try {
    const cause = new HttpRequestError(409, 'Conflict', { message: `主操作拒绝 token=private-test-token ${'长原因'.repeat(100)}` }, '/original/operation', 'original-request');
    const reported = new ReportedError('已有行内反馈', cause);
    service.error(reported);
    const afterReported = service.model.notifications.length;
    service.error(new AggregateError([reported, new Error('状态重读拒绝')], '操作与状态重读部分失败'));
    const item = service.model.notifications[0];
    const visible = { count: service.model.notifications.length, message: item.message.raw,
      actions: item.actions.secondary.map(action => action.id) };
    await item.actions.secondary.find(action => action.id === 'boxteam.notification.copyDiagnostic').run();
    const copied = await navigator.clipboard.readText();
    service.error(new ReportedAggregateError([reported, cause], '整体已展示'));
    const afterAggregate = service.model.notifications.length;
    service.error(cause);
    const independent = service.model.notifications[0].message.raw;
    return { afterReported, visible, copied, afterAggregate, independent, count: service.model.notifications.length, logs,
      originalMessage: cause.message, originalDetail: cause.detail.message, retainedCause: reported.cause === cause };
  } finally { service.dispose(); storage.dispose(); }
}

/** 编译后的原FileService与保存handler；provider和宿主动作创建为显式边界。 */
export async function configurationSaveFailureContract() {
  const storage = new InMemoryStorageService(), logs = [], closes = [];
  const service = new NotificationService(storage, { error: message => logs.push(message) });
  const resource = URI.parse('boxteam-config-source://original-gateway/workspace/original-workspace/source.jsonc');
  const http = new HttpRequestError(422, 'Unprocessable Content', { message: '原配置字段拒绝',
    details: { source_uri: 'boxteam://workspace/original/resources/config/source', issues: [{ path: ['field'], message: '必须是字符串', path_exact: true }] } },
    '/api/v1/config/sources/source/preflight', 'original-source-request');
  const source = new Error('配置修改失败，草稿已保留。原配置字段拒绝', { cause: http });
  const provider = { capabilities: 2, writeFile: async () => { throw source; } };
  const options = { create: true, overwrite: true };
  const files = Object.assign(Object.create(FileService.prototype), {
    withWriteProvider: async () => provider, throwIfFileSystemIsReadonly: value => value,
    getExtUri: () => ({ providerExtUri: {} }), validateWriteFile: async () => ({ stat: {} }),
    writeQueue: { queueFor: (_resource, operation) => operation() },
    resourceForError: uri => uri.toString(), _onDidRunOperation: { fire() { throw new Error('失败不能发布写成功事件'); } },
  });
  try {
    let failure;
    try { await files.writeFile(resource, VSBuffer.fromString('{}'), options); } catch (error) { failure = error; }
    if (!(failure instanceof FileOperationError)) throw new Error('原FileService未返回文件操作错误');
    const actions = [];
    const handler = Object.assign(Object.create(TextFileSaveErrorHandler.prototype), {
      notificationService: service, messages: { set(uri, handle) { closes.push({ uri: uri.toString(), handle: Boolean(handle) }); } },
      instantiationService: { createInstance(ctor) { const action = { id: ctor.name, dispose() {} }; actions.push(action); return action; } },
    });
    handler.onSaveError(failure, { resource }, {});
    const item = service.model.notifications[0];
    const visible = { count: service.model.notifications.length, message: item.message.raw,
      actions: item.actions.primary.map(action => action.id) };
    await item.actions.secondary.find(action => action.id === 'boxteam.notification.copyDiagnostic').run();
    return { visible, copied: await navigator.clipboard.readText(), logs, closes, causePreserved: failure.cause === source,
      originalOptions: failure.options === options, result: failure.fileOperationResult, expectedResult: FileOperationResult.FILE_OTHER_ERROR };
  } finally { service.dispose(); storage.dispose(); }
}

/** 真实恢复入口与具名阅读控制器；原生恢复不得成为主动展示切换。 */
export function viewportRestoreContract(ready) {
  class Surface extends ChatSurface { render() {} }
  const container = document.body.appendChild(document.createElement('div'));
  const moves = [], shows = [], reasons = [];
  const controller = { capture: () => undefined, navigate: (position, preview) => moves.push({ position, preview }),
    showConversation: preview => shows.push(preview ?? false), cancel() {} };
  const instantiation = { createInstance: () => ({ dispose() {}, hide() {} }),
    invokeFunction: callback => callback({ get: () => ({ error() {} }) }) };
  const surface = new Surface(container, {}, {}, true, () => {}, {}, {}, [], instantiation, 7, async () => {}, reason => reasons.push(reason));
  const location = { kind: 'viewport', position: { reading_anchor: null, follow_latest: true } };
  try {
    if (ready) surface.onViewportReady(controller);
    surface.revealLocation(location, EditorPaneSelectionChangeReason.PROGRAMMATIC);
    if (!ready) surface.onViewportReady(controller);
    const restored = { moves: [...moves], shows: [...shows], location: surface.getLocation() };
    surface.revealLocation(location);
    return { restored, moves, shows, reasons };
  } finally { surface.dispose(); container.remove(); }
}

/** 实际 Pane setInput／clearInput 和原 memento；仅路由解析及宿主服务为具名边界。 */
export async function threadViewStateContract() {
  const storage = new InMemoryStorageService(), reasons = [];
  const theme = { getColorTheme: () => ({}), onDidColorThemeChange: Event.None };
  let restore = true;
  const configuration = { onDidChangeConfiguration: Event.None,
    getValue: (_resource, key) => key === 'workbench.editor.restoreViewState' ? restore : false };
  const group = { id: 21, contains: () => true }, otherGroup = { id: 22, contains: () => true };
  const groups = { getGroup: id => [group, otherGroup].find(item => item.id === id) };
  const instantiation = { createInstance: () => ({ dispose() {}, hide() {} }),
    invokeFunction: callback => callback({ get: () => ({ error() {} }) }) };
  class Input extends ThreadInput { async resolveOwner() { return { owner: this.owner, workspaces: [] }; } }
  class Pane extends ThreadPane { mount(parent) { this.createEditor(parent); } }
  const makeInput = () => new Input({ gatewayId: 'gateway-a', workspaceId: 'workspace-a',
    workspaceKey: '', sessionId: 'state-session', threadId: 'state-thread', title: '原生视图态' });
  const makePane = selectedGroup => new Pane(selectedGroup, {}, theme, storage, configuration, {}, {}, groups, instantiation, {}, {}, {});
  const container = document.body.appendChild(document.createElement('div'));
  const pane = makePane(group), other = makePane(otherGroup), inputs = [makeInput(), makeInput(), makeInput()];
  pane.mount(container); other.mount(container.appendChild(document.createElement('div')));
  const listener = pane.onDidChangeSelection(event => reasons.push(event.reason));
  const first = { reading_anchor: null, follow_latest: true }, second = { reading_anchor: null, follow_latest: false };
  const open = (target, input, options, newInGroup = false) => target.setInput(input, options, { newInGroup }, CancellationToken.None);
  try {
    await open(pane, inputs[0], { boxteamViewport: first });
    pane.clearInput();
    await open(pane, inputs[0]);
    const switchedBack = pane.getViewState().threadLocation;
    await open(other, inputs[1], { boxteamViewport: second }); other.clearInput();
    await open(other, inputs[1]);
    const separateGroup = other.getViewState().threadLocation;
    pane.clearInput(); inputs[0].dispose();
    await open(pane, inputs[2], inputs[2].toUntyped().options, true);
    const reopened = pane.getViewState().threadLocation;
    pane.clearInput();
    await open(pane, inputs[2], { viewState: { resourcesVisible: true, resourceTab: 'debug' } });
    const partialOptions = pane.getViewState();
    pane.clearInput(); restore = false;
    await open(pane, inputs[2], undefined, false);
    const existingWithRestoreDisabled = pane.getViewState().threadLocation;
    pane.clearInput();
    await open(pane, inputs[2], undefined, true);
    const newWithRestoreDisabled = pane.getViewState().threadLocation;
    pane.clearInput();
    await open(pane, inputs[2], { viewState: { threadLocation: { kind: 'viewport', position: second } }, boxteamViewport: first });
    return { switchedBack, separateGroup, reopened, partialOptions, newWithRestoreDisabled: newWithRestoreDisabled ?? null,
      existingWithRestoreDisabled, explicit: pane.getViewState().threadLocation, reasons };
  } finally {
    listener.dispose(); pane.dispose(); other.dispose();
    for (const input of inputs) input.dispose(); storage.dispose(); container.remove();
  }
}

/** 真实宿主的阅读控制器接线；渲染和控制器是具名边界，不代替完整页面。 */
export function loadingPreviewRestoreContract(accept) {
  class Surface extends ChatSurface { render() {} }
  const container = document.body.appendChild(document.createElement('div'));
  const events = [], moves = [];
  const original = { reading_anchor: null, follow_latest: true };
  const controller = { capture: () => original, capturePresentation: () => () => events.push('presentation'),
    navigate: (position, preview) => moves.push({ position, preview }), cancel: () => events.push('cancel'),
    showConversation: preview => events.push(preview ? 'preview-presentation' : 'conversation') };
  const instantiation = { createInstance: () => ({ dispose() {}, hide() {} }), invokeFunction: callback => callback({ get: () => ({ error() {} }) }) };
  const surface = new Surface(container, {}, {}, true, () => {}, {}, {}, [], instantiation, 7, async () => {}, () => events.push('selection'));
  try {
    surface.onViewportReady(controller);
    const restore = surface.captureViewState();
    const preview = surface.revealTurn('preview-turn', true);
    // around 读取使真实 Hook 暂时卸下控制器；Esc 在重新挂载之前到达。
    surface.onViewportReady(undefined);
    restore.dispose();
    const afterCancel = [...events];
    preview.dispose();
    if (accept) surface.revealLocation({ kind: 'viewport', position: { reading_anchor: null, follow_latest: false } });
    surface.onViewportReady(controller);
    surface.onViewportReady(undefined);
    surface.onViewportReady(controller);
    const observed = { afterCancel, events: [...events], moves };
    surface.onCloseNavigation();
    return { ...observed, closedLocation: surface.getLocation() };
  } finally { surface.dispose(); container.remove(); }
}

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
  editors = { openEditor() { throw new Error('只选择调用方不应打开编辑器'); } }, quickNavigate = false, instantiation,
  notifications = { error(error) { throw error; } }, quickInput = {}, configuration = { getValue: () => undefined }) {
  const changes = new Emitter(), accepts = new Emitter(), buttons = new Emitter(), cancellation = new CancellationTokenSource();
  const picker = { value: '', items: [], activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false },
    buttons: [], quickNavigate: quickNavigate ? { keybindings: [] } : undefined, onDidTriggerButton: buttons.event,
    onDidHide: Event.None, onDidChangeActive: Event.None, onDidChangeValue: changes.event, onDidAccept: accepts.event,
    onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None, hide() { this.hidden = true; if (!this.selectedItems.length) cancellation.cancel(); lifetime.dispose(); } };
  const group = { id: 1 }, groups = { groups: [], activeGroup: group, getGroup: id => id === group.id ? group : undefined,
    findGroup: () => undefined, addGroup() { const added = { id: this.groups.length + 2 }; this.groups.push(added); return added; } };
  const provider = new WorkspaceQuickAccess(instantiation ?? { createInstance: () => new PickerEditorState(editors, groups) }, editors, groups,
    { getHistory: () => history, removeFromHistory(editor) { history.splice(history.indexOf(editor), 1); } },
    configuration, quickInput, {}, { lookupKeybinding: () => undefined }, opener, notifications, { enabled: false }, {});
  const lifetime = provider.provide(picker, cancellation.token, options);
  return { picker, groups, accepts, triggerButton: button => buttons.fire(button), change(value) { picker.value = value; changes.fire(); },
    dispose() { cancellation.dispose(true); lifetime.dispose(); provider.dispose(); changes.dispose(); accepts.dispose(); buttons.dispose(); } };
}

/** 只替换语言服务与模型引用，核真实 Provider 的模式转换和引用释放。 */
export async function fileSymbolContract(mode) {
  const patterns = [], previews = [], symbols = [], releases = [], opened = [], sequence = [], restoredStates = [];
  const files = [uri('symbol-a.json'), uri('symbol-b.json')];
  const symbolProvider = { getSymbolPicks(model, filter) {
    symbols.push({ resource: model.uri.toString(), filter });
    return Promise.resolve(filter === 'missing' ? [] : [{ index: 0, label: 'beta',
      range: { selection: { startLineNumber: 3, startColumn: 3, endLineNumber: 3, endColumn: 7 },
        decoration: { startLineNumber: 3, startColumn: 1, endLineNumber: 4, endColumn: 1 } } }]);
  } };
  const editors = { activeEditorPane: { input: {}, getControl() {}, getViewState: () => ({ position: "before-preview" }), group: {
    async openEditor(_editor, options) { restoredStates.push(options.viewState); sequence.push('restore-start'); await Promise.resolve(); sequence.push('restore-end'); },
  } }, openEditor(input, group) {
    (input.options.transient ? previews : opened).push({ resource: input.resource.toString(), options: input.options, group });
    if (!input.options.transient) sequence.push('open');
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
    f.picker.keyMods.alt = mode === 'side';
    f.accepts.fire({ inBackground: false });
    await settle();
  } finally { f.dispose(); }
  return { ...result, patterns, previews, symbols, releases, opened, sequence, restoredStates, target: files[1].toString() };
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

export async function uriContract() {
  const calls = [];
  for (const uri of [root + '/name%40target.txt', root + '/name%40target.txt#L2', 'https://example.test/file',
    'https://example.test:8042', 'https://example.test/file:2:3', 'https://example.test/#7', 'https://example.test/report,12']) {
    const f = defaultAccess({ searchOwner: owner }, [], { open(resource, options) {
      calls.push({ uri: resource, group: options.editorOptions?.sourceGroup, side: options.openToSide,
        pinned: options.editorOptions.pinned, preserveFocus: options.editorOptions.preserveFocus });
      return Promise.resolve(true);
    } });
    try {
      f.change(uri);
      f.picker.keyMods.alt = true;
      f.picker.activeItems = f.picker.selectedItems = [f.picker.items[0]];
      f.accepts.fire({ inBackground: true });
      await new Promise(resolve => setTimeout(resolve, 0));
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
  for (const kind of ['user', 'gateway', 'gateway-null-returned']) {
    const before = workspaceContext.getSnapshot();
    workspaceContext.publish({ ...before, gatewayId: 'gateway-a' });
    const calls = [];
    const f = defaultAccess({ fileSearchDelegate: { search(pattern, token) {
      return new Promise(resolve => calls.push({ pattern, token, resolve }));
    } } });
    try {
      if (kind === 'user') invalidateGatewayToken(0);
      else if (kind === 'gateway-null-returned') {
        workspaceContext.publish({ ...before, gatewayId: null });
        workspaceContext.publish({ ...before, gatewayId: 'gateway-a' });
      } else workspaceContext.publish({ ...before, gatewayId: 'gateway-b' });
      const cancelled = calls[0].token.isCancellationRequested;
      calls[0].resolve({ results: [{ resource: uri('old-user.txt') }], messages: [] });
      await Promise.resolve(); await Promise.resolve();
      f.change('new query');
      results.push({ kind, cancelled, calls: calls.length, busy: f.picker.busy,
        labels: f.picker.items.map(item => item.label), selected: f.picker.selectedItems.length });
    } finally { f.dispose(); workspaceContext.publish(before); }
  }
  // 已释放 Provider 不再订阅失效通知。
  invalidateGatewayToken(0);
  return results;
}

/** 保留旧候选回调，验证失效后连展示动作和空分屏也不再执行。 */
export async function historyIdentityContract(kind) {
  const before = workspaceContext.getSnapshot(), opened = [], errors = [];
  workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
  const history = [{ resource: uri('history.txt'), label: '真实历史入口' }];
  const f = defaultAccess({ searchOwner: owner }, history, undefined,
    { async openEditor(input) { opened.push(input.resource.toString()); } }, true, undefined,
    { error(error) { errors.push(String(error)); } });
  try {
    const pick = f.picker.items.find(item => item.id === history[0].resource.toString());
    if (!pick) throw new Error('原最近历史未公布候选');
    if (kind === 'credential') invalidateGatewayToken(0);
    else {
      workspaceContext.publish({ ...before, gatewayId: 'gateway-b' });
      if (kind === 'gateway-returned') workspaceContext.publish({ ...before, gatewayId: owner.gatewayId });
    }
    try { pick.trigger(1, { alt: false, ctrlCmd: false }); } catch (cause) { errors.push(String(cause)); }
    try { pick.trigger(0, { alt: false, ctrlCmd: false }); } catch (cause) { errors.push(String(cause)); }
    pick.accept({ alt: true, ctrlCmd: false }, { inBackground: false });
    await new Promise(resolve => setTimeout(resolve, 0));
    return { opened, errors, historyCount: history.length, addedGroups: f.groups.groups.length,
      selected: f.picker.selectedItems.length, labels: f.picker.items.map(item => item.label) };
  } finally { f.dispose(); workspaceContext.publish(before); }
}

export async function historyScopeIdentityContract(kind) {
  const before = workspaceContext.getSnapshot(), pending = Promise.withResolvers(), errors = [], reopened = [];
  workspaceContext.publish({ ...before, gatewayId: owner.gatewayId, loading: false, error: null,
    workspaces: [{ gateway_id: owner.gatewayId, workspace_id: owner.workspaceId, name: '原历史范围', status: 'ready' }] });
  let selection;
  const quick = { quickAccess: { show: (...args) => reopened.push(args) },
    pick(items, _options, token) { selection = { items, token }; return pending.promise; } };
  const f = defaultAccess({ searchOwner: owner }, [{ resource: uri('history.txt') }], undefined, undefined, false, undefined,
    { error: cause => errors.push(String(cause)) }, quick);
  try {
    f.triggerButton(f.picker.buttons[0]);
    if (!selection) throw new Error('范围选择没有进入原Picker');
    // 原 Quick Input 切换 Provider 后释放旧 run，范围选择仍须拥有独立身份生命周期。
    f.dispose();
    if (kind === 'credential') invalidateGatewayToken(0);
    else workspaceContext.publish({ ...before, gatewayId: 'gateway-b' });
    pending.resolve(selection.items);
    await new Promise(resolve => setTimeout(resolve, 0));
    return { reopened, errors, cancelled: selection.token?.isCancellationRequested ?? false };
  } finally { pending.resolve(undefined); f.dispose(); workspaceContext.publish(before); }
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
  const provider = new WorkspaceSymbolsQuickAccess({}, {}, {}, { getValue: () => ({}) }, {}, {},
    { getWorkspace: () => ({ folders: ['workspace-a', 'workspace-b'].map(workspaceId => ({ uri: workspaceFileUri({ ...owner, workspaceId },
      `boxteam://workspace/${workspaceId}/resources/files/root`, `boxteam://workspace/${workspaceId}/resources/files/root`) })) }) }, { activeGroup: { id: 7 } });
  const changes = new Emitter(), accepts = new Emitter(), itemButtons = new Emitter(), token = new CancellationTokenSource();
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
    lifetime.push(provider.provide(picker, token.token, { searchOwners: [owner, { ...owner, workspaceId: 'workspace-c' }] }));
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
    provider.dispose(); registration.dispose(); changes.dispose(); accepts.dispose(); itemButtons.dispose();
  }
}

/** 运行转换后的上游聚合器与真实 Quick Access Provider，替身仅控制注册的 Provider。 */
export async function symbolProviderFailuresContract(mode) {
  const calls = [], changes = new Emitter(), accepts = new Emitter(), cancellation = new CancellationTokenSource();
  let releaseLate;
  const providers = mode.startsWith('partial') ? [
    { provideWorkspaceSymbols(query) { calls.push({ provider: 'success', query }); return [{ name: 'NeedleSymbol', kind: 5,
      location: { uri: uri('symbol-owner.ts'), range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 } } }]; } },
    { provideWorkspaceSymbols(query) { calls.push({ provider: 'failure', query });
      if (mode === 'partial-sync') throw new Error('provider-sync-owner-A');
      return Promise.reject(new Error('provider-async-owner-A')); } },
  ] : mode.startsWith('full') ? [0, 1].map(index => ({ provideWorkspaceSymbols(query) {
    calls.push({ provider: `failure-${index}`, query });
    if (mode === 'full-sync') throw new Error(`provider-sync-owner-${index}`);
    return Promise.reject(new Error(`provider-async-owner-${index}`));
  } })) : [{ provideWorkspaceSymbols(query) {
    calls.push({ provider: 'late', query });
    return new Promise((_, reject) => { releaseLate = () => reject(new Error('late-provider-rejection')); });
  } }];
  const registrations = providers.map(provider => WorkspaceSymbolProviderRegistry.register(provider));
  const provider = new WorkspaceSymbolsQuickAccess({ getUriLabel: () => 'symbol-owner.ts' }, {}, {}, { getValue: () => ({}) }, {}, {},
    { getWorkspace: () => ({ folders: ['workspace-a', 'workspace-b'].map(workspaceId => ({ uri: workspaceFileUri({ ...owner, workspaceId },
      `boxteam://workspace/${workspaceId}/resources/files/root`, `boxteam://workspace/${workspaceId}/resources/files/root`) })) }) }, { activeGroup: { id: 7 } });
  const picker = { value: '#needle', items: [], activeItems: [], keyMods: { alt: false, ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerItemButton: Event.None, onDidTriggerSeparatorButton: Event.None };
  let lifetime;
  try {
    lifetime = provider.provide(picker, cancellation.token, { searchOwners: [
      { ...owner, gatewayId: owner.gatewayId, workspaceId: 'workspace-a' },
      { ...owner, gatewayId: owner.gatewayId, workspaceId: 'workspace-b' },
    ] });
    if (mode === 'cancel-late') {
      await new Promise(resolve => setTimeout(resolve, 250));
      const beforeDispose = picker.items.map(item => item.label);
      lifetime.dispose();
      const afterDispose = picker.items.map(item => item.label);
      releaseLate();
      await new Promise(resolve => setTimeout(resolve, 0));
      return { calls, beforeDispose, afterDispose, afterLate: picker.items.map(item => item.label), busy: picker.busy };
    }
    await new Promise(resolve => setTimeout(resolve, 250));
    return { calls, items: picker.items.map(item => ({ label: item.label, description: item.description })), busy: picker.busy };
  } finally {
    lifetime?.dispose(); cancellation.dispose(true); provider.dispose(); changes.dispose(); accepts.dispose();
    registrations.forEach(registration => registration.dispose());
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


/** 真实符号 Provider、异步 resolve 与原 finder 共用同一发起组。 */
export async function symbolGroupContract(mode) {
  const unscoped = mode.startsWith('unscoped-');
  const action = unscoped ? mode.slice('unscoped-'.length) : mode;
  const button = action === 'button' || action === 'button-cancel';
  const sequence = [];
  const { findGroup } = await import('../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorGroupFinder.js');
  const { IEditorGroupsService } = await import('../../../../../src/clients/workbench/upstream/src/vs/workbench/services/editor/common/editorGroupsService.js');
  const source = { id: 7, editors: [], isLocked: !unscoped }, other = { id: 9, editors: [], isLocked: false };
  const neighbor = { id: 8, editors: [], isLocked: action === 'side', isActive: () => false };
  const created = { id: 10, editors: [], isLocked: false };
  const groups = { activeGroup: other, getGroup: id => [source, other, neighbor, created].find(group => group.id === id),
    getGroups: () => [other, source, neighbor], findGroup: (_location, from) => {
      if (from !== source) throw new Error('符号侧开丢失发起组');
      return neighbor;
    }, addGroup: from => {
      if (from !== source) throw new Error('符号建组丢失发起组');
      return created;
    } };
  const opened = [], opening = Promise.withResolvers(), resolving = Promise.withResolvers(), resolved = Promise.withResolvers();
  const configuration = { getValue: key => key === undefined ? { workbench: { editor: { enablePreview: true, enablePreviewFromQuickOpen: true } } }
    : key === 'workbench.editor.useModal' ? 'off' : key === 'workbench.editor.openSideBySideDirection' ? 'right' : false };
  const symbol = { name: 'group-symbol', kind: 1, location: { uri: uri('symbol.ts') } };
  const registration = WorkspaceSymbolProviderRegistry.register({ provideWorkspaceSymbols: () => [symbol],
    resolveWorkspaceSymbol: async () => { resolving.resolve(); await resolved.promise; return symbol; } });
  const provider = new WorkspaceSymbolsQuickAccess({ getUriLabel: () => 'symbol.ts' }, {}, {
    openEditor: async (input, preferred) => {
      const [target, activation] = await findGroup({ get: service => service === IEditorGroupsService ? groups : configuration }, input, preferred);
      opened.push({ target: target.id, activation, sourceGroup: input.options.sourceGroup }); sequence.push("open"); opening.resolve();
    },
  }, configuration, {}, {}, { getWorkspace: () => ({ folders: [{ uri: uri('') }] }) }, groups);
  const changes = new Emitter(), accepts = new Emitter(), itemButtons = new Emitter(), token = new CancellationTokenSource();
  const picker = { value: '#group', items: [], activeItems: [], selectedItems: [], keyMods: { alt: action !== 'open', ctrlCmd: false },
    onDidChangeValue: changes.event, onDidAccept: accepts.event, onDidTriggerItemButton: itemButtons.event, onDidTriggerSeparatorButton: Event.None,
    hide() { sequence.push("hide"); if (!this.selectedItems.length) token.cancel(); lifetime.dispose(); } };
  const lifetime = provider.provide(picker, token.token, { searchGroup: source.id, ...(unscoped ? {} : { searchOwners: [owner] }) });
  try {
    for (let index = 0; index < 100 && !picker.items.some(item => item.symbol); index++) await new Promise(resolve => setTimeout(resolve, 10));
    const pick = picker.items.find(item => item.symbol);
    if (!pick) throw new Error('原符号 Provider 未生成候选');
    picker.activeItems = [pick];
    if (button) itemButtons.fire({ item: pick, button: pick.buttons[0] });
    else { picker.selectedItems = [pick]; accepts.fire({ inBackground: action === 'background-side' }); }
    await resolving.promise;
    groups.activeGroup = other;
    if (action === 'button-cancel') picker.hide();
    resolved.resolve();
    if (action !== 'button-cancel') await opening.promise;
    await new Promise(resolve => setTimeout(resolve, 0));
    return button ? { opened, sequence } : opened;
  } finally {
    resolved.resolve(); lifetime.dispose(); token.dispose(true); provider.dispose(); registration.dispose(); changes.dispose(); accepts.dispose(); itemButtons.dispose();
  }
}


export async function sideDirectionContract(direction) {
  const f = defaultAccess({ searchOwner: owner }, [{ resource: uri('side.txt') }], undefined, undefined,
    true, undefined, undefined, {}, { getValue: key => key === 'workbench.editor.openSideBySideDirection' ? direction : undefined });
  try { return f.picker.items.find(item => item.resource)?.buttons[0]; }
  finally { f.dispose(); }
}
