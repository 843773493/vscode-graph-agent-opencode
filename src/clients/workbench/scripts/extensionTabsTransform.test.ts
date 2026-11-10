import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { diffSets } from '../upstream/src/vs/base/common/collections.js';
import { Emitter, Event } from '../upstream/src/vs/base/common/event.js';
import { DisposableMap, DisposableStore } from '../upstream/src/vs/base/common/lifecycle.js';
import { observableValue } from '../upstream/src/vs/base/common/observable.js';
import { isEqual } from '../upstream/src/vs/base/common/resources.js';
import { assertReturnsDefined } from '../upstream/src/vs/base/common/types.js';
import { URI } from '../upstream/src/vs/base/common/uri.js';
import type { IEditorTabGroupDto, TabOperation } from '../upstream/src/vs/workbench/api/common/extHost.protocol.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);

interface TabInput {
  uri?: URI;
  original?: URI;
  modified?: URI;
  textDiffs?: readonly TabInput[];
}
interface Tab {
  readonly label: string;
  readonly input?: TabInput;
  readonly isActive: boolean;
  readonly isDirty: boolean;
  readonly isPinned: boolean;
  readonly isPreview: boolean;
  readonly group: TabGroup;
}
interface TabGroup {
  readonly tabs: readonly Tab[];
  readonly activeTab: Tab | undefined;
  readonly isActive: boolean;
  readonly viewColumn: number;
}
interface Change<T> { opened: readonly T[]; closed: readonly T[]; changed: readonly T[] }
interface ReceiverTab { readonly tabId: string; readonly apiObject: Tab }
interface ReceiverGroup { readonly groupId: number; readonly apiObject: TabGroup; readonly tabs: readonly ReceiverTab[] }
interface Receiver {
  readonly _extHostTabGroups: readonly ReceiverGroup[];
  readonly tabGroups: {
    readonly all: readonly TabGroup[];
    readonly activeTabGroup: TabGroup;
    readonly onDidChangeTabs: Event<Change<Tab>>;
    readonly onDidChangeTabGroups: Event<Change<TabGroup>>;
    close(value: Tab | TabGroup | readonly Tab[] | readonly TabGroup[], preserveFocus?: boolean): Promise<boolean>;
  };
  $acceptEditorTabModel(groups: IEditorTabGroupDto[]): void;
  $acceptTabGroupUpdate(group: IEditorTabGroupDto): void;
  $acceptTabOperation(operation: TabOperation): void;
}
interface MainThread {
  dispose(): void;
  $closeTab(ids: string[], preserveFocus?: boolean): Promise<boolean>;
  $closeGroup(ids: number[], preserveFocus?: boolean): Promise<boolean>;
  $moveTab(id: string, index: number, column: number, preserveFocus?: boolean): void;
}
interface Runtime {
  MainThread: new (...args: unknown[]) => MainThread;
  ExtHost: new (context: object) => Receiver;
  changes: Record<string, number>;
  operations: Record<string, number>;
}

// 只替代具体 EditorInput 的可观察接口；投影、索引和 receiver 均执行真实源码。
class EditorInput {
  readonly editorId = 'test.editor';
  readonly typeId = this.constructor.name;
  dirty = false;
  constructor(public label: string, public resource?: URI) {}
  getName() { return this.label; }
  isDirty() { return this.dirty; }
}
class AbstractTextResourceEditorInput extends EditorInput {}
class NotebookEditorInput extends EditorInput { readonly viewType = 'test.notebook'; }
class CustomEditorInput extends EditorInput { readonly viewType = 'test.custom'; }
class WebviewInput extends EditorInput { readonly viewType = 'test.webview'; }
class TerminalEditorInput extends EditorInput {}
class ChatEditorInput extends EditorInput {}
class SideBySideEditorInput extends EditorInput {
  constructor(label: string, readonly primary: EditorInput, readonly secondary: EditorInput) { super(label, primary.resource); }
}
class DiffEditorInput extends SideBySideEditorInput {
  constructor(label: string, readonly original: EditorInput, readonly modified: EditorInput) { super(label, modified, original); }
}
class MergeEditorInput extends EditorInput {
  constructor(label: string, readonly base: URI, readonly input1: { uri: URI }, readonly input2: { uri: URI }, result: URI) { super(label, result); }
}
class InteractiveEditorInput extends EditorInput {
  constructor(label: string, resource: URI, readonly inputResource: URI) { super(label, resource); }
}
interface MultiResource { originalUri?: URI; modifiedUri?: URI; goToFileUri?: URI }
class MultiDiffEditorInput extends EditorInput {
  readonly resources;
  constructor(label: string, resources: readonly MultiResource[] | undefined) {
    super(label, URI.parse(`multi-diff-editor:/opaque-${label}`));
    this.resources = observableValue<readonly MultiResource[] | undefined>(label, resources);
  }
  get listeners() {
    return (this.resources as typeof this.resources & { debugGetObservers(): ReadonlySet<unknown> }).debugGetObservers().size;
  }
}

async function declarations(relativePath: string, names: readonly string[], converted = false): Promise<string> {
  const path = join(root, 'upstream', relativePath);
  const source = await readFile(path, 'utf8');
  const parsed = ts.createSourceFile(path, converted ? await transform(path, source) : source, ts.ScriptTarget.Latest, true);
  const missing = new Set(names);
  const selected = parsed.statements.filter(statement => {
    const declared = ts.isVariableStatement(statement)
      ? statement.declarationList.declarations.map(value => value.name.getText(parsed))
      : (ts.isClassDeclaration(statement) || ts.isFunctionDeclaration(statement) || ts.isEnumDeclaration(statement) || ts.isModuleDeclaration(statement)) && statement.name
        ? [statement.name.getText(parsed)] : [];
    const found = declared.filter(name => missing.has(name));
    for (const name of found) missing.delete(name);
    return found.length > 0;
  });
  if (missing.size) throw new Error(`实际源码缺少声明 ${relativePath}: ${[...missing].join(', ')}`);
  return selected.map(statement => statement.getText(parsed)).join('\n')
    .replace(/^@extHostNamedCustomer[^\n]*\n/gm, '').replace(/@I\w+\s+/g, '')
    .replace(/^export\s+(?=(?:class|function|const|let|enum|namespace)\b)/gm, '');
}

function evaluate(code: string, names: readonly string[], dependencies: Record<string, unknown>): Record<string, unknown> {
  const javascript = ts.transpileModule(code, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None, useDefineForClassFields: false,
  } }).outputText;
  return new Function(...Object.keys(dependencies), `${javascript}\nreturn { ${names.join(', ')} };`)(...Object.values(dependencies));
}

let runtime: Promise<Runtime> | undefined;
function loadRuntime(): Promise<Runtime> {
  return runtime ??= (async () => {
    const editorNames = ['GroupModelChangeKind', 'SideBySideEditor', 'EditorResourceAccessorImpl', 'EditorResourceAccessor',
      'isEditorInput', 'isEditorInputWithPreferredResource', 'isResourceDiffEditorInput', 'isResourceMultiDiffEditorInput',
      'isResourceSideBySideEditorInput', 'isResourceMergeEditorInput', 'isSideBySideEditorInput', 'isDiffEditorInput'];
    const inputNames = ['TextTabInput', 'TextDiffTabInput', 'TextMergeTabInput', 'CustomEditorTabInput', 'WebviewEditorTabInput',
      'NotebookEditorTabInput', 'NotebookDiffEditorTabInput', 'TerminalEditorTabInput', 'InteractiveWindowInput', 'ChatEditorTabInput', 'TextMultiDiffTabInput'];
    const protocol = evaluate(await declarations('src/vs/workbench/api/common/extHost.protocol.ts', ['TabInputKind', 'TabModelOperationKind']),
      ['TabInputKind', 'TabModelOperationKind'], {});
    const editor = evaluate(await declarations('src/vs/workbench/common/editor.ts', editorNames), editorNames,
      { URI, AbstractEditorInput: EditorInput });
    const grouping = evaluate(await declarations('src/vs/workbench/services/editor/common/editorGroupsService.ts',
      ['GroupsOrder', 'GroupDirection', 'preferredSideBySideGroupDirection']), ['GroupsOrder', 'GroupDirection', 'preferredSideBySideGroupDirection'], {});
    const positions = evaluate(await declarations('src/vs/workbench/services/editor/common/editorService.ts',
      ['ACTIVE_GROUP', 'SIDE_GROUP', 'AUX_WINDOW_GROUP', 'MODAL_GROUP']), ['ACTIVE_GROUP', 'SIDE_GROUP', 'AUX_WINDOW_GROUP', 'MODAL_GROUP'], {});
    const columns = evaluate(await declarations('src/vs/workbench/services/editor/common/editorGroupColumn.ts',
      ['columnToEditorGroup', 'editorGroupToColumn']), ['columnToEditorGroup', 'editorGroupToColumn'], { ...grouping, ...positions });
    const types = evaluate(await declarations('src/vs/workbench/api/common/extHostTypes.ts', inputNames), inputNames, {});
    const viewColumn = evaluate(await declarations('src/vs/platform/editor/common/editor.ts', ['ViewColumn']), ['ViewColumn'], {});
    const typeConverters = evaluate(await declarations('src/vs/workbench/api/common/extHostTypeConverters.ts', ['ViewColumn']),
      ['ViewColumn'], { types: viewColumn, ...positions });
    const receiverNames = ['ExtHostEditorTab', 'ExtHostEditorTabGroup', 'ExtHostEditorTabs', 'isTabGroup'];
    const receiver = evaluate(await declarations('src/vs/workbench/api/common/extHostEditorTabs.ts', receiverNames), receiverNames,
      { Emitter, URI, diffSets, assertReturnsDefined, typeConverters, ...types, ...protocol, MainContext: { MainThreadEditorTabs: {} } });
    const owner = evaluate(await declarations('src/vs/workbench/api/browser/mainThreadEditorTabs.ts', ['MainThreadEditorTabs'], true), ['MainThreadEditorTabs'], {
      Event, URI, DisposableMap, DisposableStore, isEqual, ...editor, ...grouping, ...positions, ...columns, ...protocol,
      EditorInput, AbstractTextResourceEditorInput, NotebookEditorInput, CustomEditorInput, WebviewInput, TerminalEditorInput,
      ChatEditorInput, SideBySideEditorInput, DiffEditorInput, MergeEditorInput, InteractiveEditorInput, MultiDiffEditorInput,
      ExtHostContext: { ExtHostEditorTabs: {} },
    });
    return { MainThread: owner.MainThreadEditorTabs, ExtHost: receiver.ExtHostEditorTabs,
      changes: editor.GroupModelChangeKind, operations: protocol.TabModelOperationKind } as Runtime;
  })();
}

const fileOne = (name: string, index = 0) => URI.file(`/workspaces/one-${index + 1}/${name}`);
const fileTwo = (name: string) => URI.file(`/workspaces/two/${name}`);
const text = (label: string, uri = fileOne(label)) => new AbstractTextResourceEditorInput(label, uri);

interface EditorChange { groupId: number; event: { kind: number; editor?: EditorInput; editorIndex?: number; oldEditorIndex?: number } }
class EditorGroup {
  activeEditor: EditorInput | undefined;
  readonly sticky = new Set<EditorInput>();
  readonly pinned: Set<EditorInput>;
  readonly closeCalls: { editors: EditorInput[]; options: { preserveFocus?: boolean } }[] = [];
  readonly moveCalls: { editor: EditorInput; target: EditorGroup; options: { index: number; preserveFocus?: boolean } }[] = [];
  closeGate: Promise<boolean> | undefined;
  closeAllCalls = 0;
  constructor(readonly id: number, readonly editors: EditorInput[], private readonly model: EditorModel) {
    this.activeEditor = editors[0]; this.pinned = new Set(editors);
  }
  get count() { return this.editors.length; }
  isSticky(index: number) { return this.sticky.has(this.editors[index]); }
  isPinned(index: number) { return this.pinned.has(this.editors[index]); }
  isActive(editor: EditorInput) { return this.activeEditor === editor; }
  open(editor: EditorInput, index = this.editors.length) {
    this.editors.splice(index, 0, editor); this.pinned.add(editor);
    this.model.emit(this, 'EDITOR_OPEN', editor, index);
  }
  closeNow(editor: EditorInput) {
    const index = this.editors.indexOf(editor);
    if (index < 0) throw new Error('测试编辑器不在原组中');
    this.editors.splice(index, 1);
    if (this.activeEditor === editor) this.activeEditor = this.editors[0];
    this.model.emit(this, 'EDITOR_CLOSE', editor, index);
  }
  activate(editor: EditorInput) { this.activeEditor = editor; this.model.emit(this, 'EDITOR_ACTIVE', editor, this.editors.indexOf(editor)); }
  async closeEditors(editors: EditorInput[], options: { preserveFocus?: boolean } = {}) {
    this.closeCalls.push({ editors: [...editors], options });
    const closed = await (this.closeGate ?? Promise.resolve(true));
    if (closed) for (const editor of editors) this.closeNow(editor);
    return closed;
  }
  async closeAllEditors() { this.closeAllCalls++; return this.closeEditors([...this.editors]); }
  moveEditor(editor: EditorInput, target: EditorGroup, options: { index: number; preserveFocus?: boolean }) {
    this.moveCalls.push({ editor, target, options: { ...options } });
    const oldIndex = this.editors.indexOf(editor);
    if (oldIndex < 0) throw new Error('测试移动缺少原输入');
    this.editors.splice(oldIndex, 1); target.editors.splice(options.index, 0, editor);
    if (target === this) this.model.emit(this, 'EDITOR_MOVE', editor, options.index, oldIndex);
    else {
      if (this.activeEditor === editor) this.activeEditor = this.editors[0];
      this.model.emit(this, 'EDITOR_CLOSE', editor, oldIndex);
      this.model.emit(target, 'EDITOR_OPEN', editor, options.index);
    }
  }
}
class EditorModel {
  readonly changed = new Emitter<EditorChange>();
  readonly added = new Emitter<EditorGroup>();
  readonly removed = new Emitter<EditorGroup>();
  readonly moved = new Emitter<EditorGroup>();
  readonly onDidEditorsChange = this.changed.event;
  readonly onDidAddGroup = this.added.event;
  readonly onDidRemoveGroup = this.removed.event;
  readonly onDidMoveGroup = this.moved.event;
  readonly groups: EditorGroup[];
  readonly addCalls: EditorGroup[] = [];
  readonly removeCalls: EditorGroup[] = [];
  activeGroup: EditorGroup;
  readonly whenReady: Promise<void>;
  constructor(readonly runtime: Runtime, editors: EditorInput[][], ready = Promise.resolve()) {
    this.groups = editors.map((values, index) => new EditorGroup(index + 11, [...values], this));
    this.activeGroup = this.groups[0]; this.whenReady = ready;
  }
  getGroup(id: number) { return this.groups.find(group => group.id === id); }
  getGroups() { return this.groups; }
  addGroup(reference: EditorGroup) {
    const group = new EditorGroup(Math.max(...this.groups.map(value => value.id)) + 1, [], this);
    this.groups.splice(this.groups.indexOf(reference) + 1, 0, group); this.addCalls.push(group); this.added.fire(group); return group;
  }
  removeGroup(group: EditorGroup) {
    this.groups.splice(this.groups.indexOf(group), 1); this.removeCalls.push(group);
    if (this.activeGroup === group) this.activeGroup = this.groups[0];
    this.removed.fire(group);
  }
  moveGroup(group: EditorGroup, index: number) {
    this.groups.splice(this.groups.indexOf(group), 1); this.groups.splice(index, 0, group); this.moved.fire(group);
  }
  emit(group: EditorGroup, kind: string, editor?: EditorInput, editorIndex?: number, oldEditorIndex?: number) {
    this.changed.fire({ groupId: group.id, event: { kind: this.runtime.changes[kind], editor, editorIndex, oldEditorIndex } });
  }
  dispose() { this.changed.dispose(); this.added.dispose(); this.removed.dispose(); this.moved.dispose(); }
}

type Packet = { kind: 'model'; value: IEditorTabGroupDto[] } | { kind: 'group'; value: IEditorTabGroupDto } | { kind: 'operation'; value: TabOperation };
function connect(model: EditorModel) {
  let owner!: MainThread;
  const receiver = new model.runtime.ExtHost({ getProxy: () => ({
    $closeTab: (ids: string[], focus?: boolean) => owner.$closeTab(ids, focus),
    $closeGroup: (ids: number[], focus?: boolean) => owner.$closeGroup(ids, focus),
  }) });
  const packets: Packet[] = [], errors: unknown[] = [];
  const tabsChanged: Change<Tab>[] = [], groupsChanged: Change<TabGroup>[] = [];
  const subscriptions = new DisposableStore();
  subscriptions.add(receiver.tabGroups.onDidChangeTabs(event => tabsChanged.push(event)));
  subscriptions.add(receiver.tabGroups.onDidChangeTabGroups(event => groupsChanged.push(event)));
  function wire<T>(value: T): T {
    return JSON.parse(JSON.stringify(value));
  }
  const proxy = {
    $acceptEditorTabModel(value: IEditorTabGroupDto[]) {
      packets.push({ kind: 'model', value: JSON.parse(JSON.stringify(value)) });
      try { receiver.$acceptEditorTabModel(wire(value)); } catch (error) { errors.push(error); throw error; }
    },
    $acceptTabGroupUpdate(value: IEditorTabGroupDto) {
      packets.push({ kind: 'group', value: JSON.parse(JSON.stringify(value)) });
      try { receiver.$acceptTabGroupUpdate(wire(value)); } catch (error) { errors.push(error); throw error; }
    },
    $acceptTabOperation(value: TabOperation) {
      packets.push({ kind: 'operation', value: JSON.parse(JSON.stringify(value)) });
      try { receiver.$acceptTabOperation(wire(value)); } catch (error) { errors.push(error); throw error; }
    },
  };
  owner = new model.runtime.MainThread({ getProxy: () => proxy },
    model, { getValue: () => 'right' }, model);
  return { owner, receiver, packets, errors, tabsChanged, groupsChanged,
    group: (group: EditorGroup) => assertReturnsDefined(receiver._extHostTabGroups.find(value => value.groupId === group.id)),
    dispose() { owner.dispose(); subscriptions.dispose(); } };
}
type Connection = ReturnType<typeof connect>;
const labels = (group: TabGroup) => group.tabs.map(tab => tab.label);
function tabId(connection: Connection, tab: Tab) {
  return assertReturnsDefined(connection.receiver._extHostTabGroups.flatMap(group => group.tabs).find(value => value.apiObject === tab)).tabId;
}
function operations(connection: Connection, kind: number) {
  return connection.packets.filter((packet): packet is Extract<Packet, { kind: 'operation' }> => packet.kind === 'operation')
    .map(packet => packet.value).filter(operation => operation.kind === kind);
}
async function ready() { await Promise.resolve(); await Promise.resolve(); }

test('真实 owner 到 receiver：完整编辑器窗口模型和真实 active group 全部保留', async () => {
  const runtime = await loadRuntime();
  const a1 = text('a1.ts'), a2 = text('a2.ts', fileOne('a2.ts', 1)), b = text('b.ts', fileTwo('b.ts'));
  const editors = [b, a1, a2,
    new DiffEditorInput('diff', a1, a2),
    new DiffEditorInput('notebook-diff', new NotebookEditorInput('nb1', fileOne('1.ipynb')), new NotebookEditorInput('nb2', fileTwo('2.ipynb'))),
    new CustomEditorInput('custom', fileOne('image.bin')), new SideBySideEditorInput('side', a1, a2),
    new InteractiveEditorInput('interactive', fileOne('interactive'), fileTwo('input')),
    new MergeEditorInput('merge', fileOne('base'), { uri: fileOne('left') }, { uri: fileTwo('right') }, fileOne('result')),
    new WebviewInput('webview'), new TerminalEditorInput('terminal'), new ChatEditorInput('chat'),
    text('outside-native', URI.file('/outside/unbound.txt')), text('other-scheme', URI.parse('boxteam://workspace/files/same.ts'))];
  const model = new EditorModel(runtime, [editors, [text('active.ts', fileTwo('active.ts'))], []]);
  model.groups[0].activeEditor = a1; model.activeGroup = model.groups[1];
  const connection = connect(model);
  try {
    await ready();
    expect(connection.receiver.tabGroups.all).toHaveLength(3);
    expect(labels(connection.group(model.groups[0]).apiObject)).toEqual(editors.map(editor => editor.label));
    expect(connection.receiver.tabGroups.activeTabGroup).toBe(connection.group(model.activeGroup).apiObject);
    expect(connection.receiver.tabGroups.activeTabGroup.activeTab?.label).toBe('active.ts');
    expect(connection.group(model.groups[0]).apiObject.tabs[1].input?.uri?.toString()).toBe('file:///workspaces/one-1/a1.ts');
    expect(connection.group(model.groups[0]).apiObject.tabs[2].input?.uri?.toString()).toBe('file:///workspaces/one-2/a2.ts');
    expect(connection.receiver._extHostTabGroups.flatMap(group => group.tabs.map(tab => tab.tabId)).every(id => /^\d+~\d+$/.test(id))).toBe(true);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('增量 open/close/move 使用真实索引并保留 Tab、TabGroup 和 API 对象身份', async () => {
  const runtime = await loadRuntime(), one = text('one.ts'), two = text('two.ts');
  const model = new EditorModel(runtime, [[one, two]]), connection = connect(model), group = model.groups[0];
  model.groups[0].activeEditor = one;
  try {
    await ready();
    const api = connection.receiver.tabGroups, originalGroup = connection.group(group), groupApi = originalGroup.apiObject;
    const [originalOne, originalTwo] = originalGroup.tabs, [oneApi, twoApi] = groupApi.tabs;
    two.label = 'two-renamed.ts'; model.emit(group, 'EDITOR_LABEL', two, 1);
    two.dirty = true; model.emit(group, 'EDITOR_DIRTY', two, 1);
    group.sticky.add(two); model.emit(group, 'EDITOR_STICKY', two, 1);
    group.pinned.delete(two); model.emit(group, 'EDITOR_PIN', two, 1);
    expect([twoApi.label, twoApi.isDirty, twoApi.isPinned, twoApi.isPreview]).toEqual(['two-renamed.ts', true, true, true]);
    expect(operations(connection, runtime.operations.TAB_UPDATE).slice(-4).map(value => value.index)).toEqual([1, 1, 1, 1]);
    group.activate(two); expect(groupApi.activeTab).toBe(twoApi); expect(oneApi.isActive).toBe(false);
    group.activate(one); expect(groupApi.activeTab).toBe(oneApi);

    const three = text('three.ts'); group.open(three, 1); const threeApi = groupApi.tabs[1];
    expect(labels(groupApi)).toEqual(['one.ts', 'three.ts', 'two-renamed.ts']);
    expect(operations(connection, runtime.operations.TAB_OPEN).at(-1)?.index).toBe(1);
    group.moveEditor(two, group, { index: 0 });
    expect(labels(groupApi)).toEqual(['two-renamed.ts', 'one.ts', 'three.ts']);
    expect(operations(connection, runtime.operations.TAB_MOVE).at(-1)).toMatchObject({ oldIndex: 2, index: 0 });
    expect(groupApi.tabs[0]).toBe(twoApi); expect(groupApi.tabs[1]).toBe(oneApi);
    group.closeNow(three);
    expect(operations(connection, runtime.operations.TAB_CLOSE).at(-1)?.index).toBe(2);
    expect(connection.tabsChanged.at(-1)?.closed[0]).toBe(threeApi);
    expect(labels(groupApi)).toEqual(['two-renamed.ts', 'one.ts']);
    expect(connection.group(group)).toBe(originalGroup);
    expect(connection.group(group).tabs[0]).toBe(originalTwo); expect(connection.group(group).tabs[1]).toBe(originalOne);
    expect(connection.receiver.tabGroups).toBe(api);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('组结构 resync 保留原 receiver 对象，迟到 ready 不会在 dispose 后发送', async () => {
  const runtime = await loadRuntime(), gate = Promise.withResolvers<void>();
  const model = new EditorModel(runtime, [[text('a.ts')], [text('b.ts', fileTwo('b.ts'))]], gate.promise);
  const connection = connect(model);
  try {
    model.groups[0].open(text('before-ready.ts'));
    const added = model.addGroup(model.groups[0]); model.moveGroup(added, 0); model.removeGroup(added);
    expect(connection.packets).toEqual([]);
    connection.owner.dispose(); gate.resolve(); await ready();
    model.groups[0].open(text('after-dispose.ts'));
    expect(connection.packets).toEqual([]); expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('MultiDiff 初始和异步更新复用标签对象，关闭及 dispose 释放订阅', async () => {
  const runtime = await loadRuntime(), pair = { originalUri: fileOne('old.ts'), modifiedUri: fileTwo('new.ts') };
  const initial = new MultiDiffEditorInput('initial', [pair]);
  const empty = new MultiDiffEditorInput('empty', []), unresolved = new MultiDiffEditorInput('unresolved', undefined);
  const editors = [initial, empty, unresolved], model = new EditorModel(runtime, [editors]), connection = connect(model), group = model.groups[0];
  try {
    await ready();
    const groupApi = connection.group(group).apiObject, original = groupApi.tabs[0];
    expect(labels(groupApi)).toEqual(['initial', 'empty', 'unresolved']);
    expect(editors.map(editor => editor.listeners)).toEqual([1, 1, 1]);
    initial.resources.set([{ ...pair, modifiedUri: fileOne('changed.ts', 1) }], undefined);
    expect(groupApi.tabs[0]).toBe(original);
    expect(original.input?.textDiffs?.[0].modified?.toString()).toBe('file:///workspaces/one-2/changed.ts');
    expect(tabId(connection, original)).not.toContain('opaque-initial');

    const opened = new MultiDiffEditorInput('opened', [pair]); group.open(opened, 1);
    const openedApi = groupApi.tabs[1];
    opened.resources.set([{ ...pair, originalUri: fileTwo('opened.ts') }], undefined);
    expect(openedApi.input?.textDiffs?.[0].original?.toString()).toBe('file:///workspaces/two/opened.ts');
    group.closeNow(opened); expect(opened.listeners).toBe(0);
    group.closeNow(initial); expect(initial.listeners).toBe(0);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
  expect(editors.map(editor => editor.listeners)).toEqual([0, 0, 0]);
});

test('异步 close 等待期间标签被关闭重开时，旧请求不会关闭新标签', async () => {
  const runtime = await loadRuntime(), gate = Promise.withResolvers<boolean>();
  const model = new EditorModel(runtime, [[text('first.ts')], [text('second.ts')]]), connection = connect(model);
  try {
    await ready();
    const [first, second] = model.groups, editor = second.editors[0];
    const firstApi = connection.group(first).apiObject.tabs[0], oldApi = connection.group(second).apiObject.tabs[0];
    const id = tabId(connection, oldApi);
    first.closeGate = gate.promise;
    const waiting = connection.receiver.tabGroups.close([firstApi, oldApi], true);
    expect(first.closeCalls).toHaveLength(1); expect(second.closeCalls).toEqual([]);
    second.closeNow(editor); second.open(editor);
    const currentApi = connection.group(second).apiObject.tabs[0];
    expect(currentApi).not.toBe(oldApi); expect(tabId(connection, currentApi)).toBe(id);
    gate.resolve(true);
    await expect(waiting).rejects.toThrow('前序关闭结果=[true]，未执行后继关闭');
    expect(first.editors).toEqual([]); expect(second.closeCalls).toEqual([]);
    expect(second.editors).toEqual([editor]); expect(connection.group(second).apiObject.tabs[0]).toBe(currentApi);
    expect(connection.errors).toEqual([]);
  } finally { gate.resolve(true); connection.dispose(); model.dispose(); }
});

test('closeGroup 等待期间后继组被同 ID 组替换，不关闭替代组', async () => {
  const runtime = await loadRuntime(), gate = Promise.withResolvers<boolean>();
  const model = new EditorModel(runtime, [[text('first.ts')], [text('second.ts')]]), connection = connect(model);
  try {
    await ready();
    const [first, second] = model.groups;
    first.closeGate = gate.promise;
    const waiting = connection.receiver.tabGroups.close([connection.group(first).apiObject, connection.group(second).apiObject]);
    const current = new EditorGroup(second.id, [...second.editors], model);
    model.groups.splice(1, 1, current); model.removed.fire(second); model.added.fire(current);
    gate.resolve(true);
    await expect(waiting).rejects.toThrow('前序关闭结果=[true]，未执行后继关闭');
    expect(first.editors).toEqual([]); expect(model.removeCalls).toEqual([first]);
    expect(current.closeCalls).toEqual([]); expect(second.closeCalls).toEqual([]);
    expect(model.groups).toEqual([current]); expect(current.editors.map(editor => editor.label)).toEqual(['second.ts']);
    expect(connection.errors).toEqual([]);
  } finally { gate.resolve(true); connection.dispose(); model.dispose(); }
});

test('moveTab 在完整窗口索引上移动标签并保留组内身份', async () => {
  const runtime = await loadRuntime(), source = text('source.ts'), zero = text('zero.ts'), one = text('one.ts');
  const model = new EditorModel(runtime, [[source], [zero, one]]), connection = connect(model);
  try {
    await ready();
    const [sourceGroup, target] = model.groups, sourceApi = connection.group(sourceGroup).apiObject.tabs[0];
    const sourceId = tabId(connection, sourceApi);
    connection.owner.$moveTab(sourceId, 1, 1, true);
    expect(sourceGroup.moveCalls[0]).toEqual({ editor: source, target, options: { index: 1, preserveFocus: true } });
    expect(labels(connection.group(target).apiObject)).toEqual(['zero.ts', 'source.ts', 'one.ts']);
    const movedApi = connection.group(target).apiObject.tabs[1];
    expect(movedApi).not.toBe(sourceApi);
    expect(movedApi.label).toBe('source.ts');
    expect(tabId(connection, movedApi)).not.toBe(sourceId);
    const zeroApi = connection.group(target).apiObject.tabs[0], zeroId = tabId(connection, zeroApi);
    connection.owner.$moveTab(zeroId, 2, 1, false);
    expect(target.moveCalls.at(-1)?.options).toEqual({ index: 2, preserveFocus: false });
    expect(labels(connection.group(target).apiObject)).toEqual(['source.ts', 'one.ts', 'zero.ts']);
    expect(connection.group(target).apiObject.tabs[2]).toBe(zeroApi);
    connection.owner.$moveTab(zeroId, 0, 1);
    expect(target.moveCalls.at(-1)?.options.index).toBe(0);
    expect(connection.group(target).apiObject.tabs[0]).toBe(zeroApi);
    connection.owner.$moveTab(zeroId, -1, 2);
    expect(model.addCalls).toHaveLength(1);
    expect(target.moveCalls.at(-1)?.target).toBe(model.addCalls[0]);
    expect(target.moveCalls.at(-1)?.options.index).toBe(0);
    expect(labels(connection.group(model.addCalls[0]).apiObject)).toEqual(['zero.ts']);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

for (const operation of ['tab', 'group'] as const) {
  test(operation + ' 多组 close 等待期间 dispose，不再执行后继组', async () => {
    const runtime = await loadRuntime(), gate = Promise.withResolvers<boolean>();
    const model = new EditorModel(runtime, [[text('first.ts')], [text('second.ts')]]), connection = connect(model);
    try {
      await ready();
      const [first, second] = model.groups, firstApi = connection.group(first).apiObject.tabs[0];
      const secondApi = connection.group(second).apiObject.tabs[0];
      first.closeGate = gate.promise;
      const waiting = operation === 'tab'
        ? connection.owner.$closeTab([tabId(connection, firstApi), tabId(connection, secondApi)])
        : connection.owner.$closeGroup([first.id, second.id]);
      expect(first.closeCalls).toHaveLength(1); expect(second.closeCalls).toEqual([]);
      connection.owner.dispose(); const packets = connection.packets.length; gate.resolve(true);
      await expect(waiting).rejects.toThrow('标签同步已经释放；前序关闭结果=[true]，未执行后继关闭');
      expect(second.closeCalls).toEqual([]); expect(second.editors.map(editor => editor.label)).toEqual(['second.ts']);
      expect(model.removeCalls).toEqual([]); expect(connection.packets).toHaveLength(packets);
      expect(connection.errors).toEqual([]);
    } finally { gate.resolve(true); connection.dispose(); model.dispose(); }
  });
}
