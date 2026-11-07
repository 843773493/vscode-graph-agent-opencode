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
import { isUriComponents, URI } from '../upstream/src/vs/base/common/uri.js';
import type { IEditorTabGroupDto, TabOperation } from '../upstream/src/vs/workbench/api/common/extHost.protocol.js';
import { TargetUriTransformer, hostOwnsWorkspaceResource } from '../src/extensions/targetUris.js';
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
      Event, URI, DisposableMap, DisposableStore, isEqual, hostOwnsWorkspaceResource, ...editor, ...grouping, ...positions, ...columns, ...protocol,
      EditorInput, AbstractTextResourceEditorInput, NotebookEditorInput, CustomEditorInput, WebviewInput, TerminalEditorInput,
      ChatEditorInput, SideBySideEditorInput, DiffEditorInput, MergeEditorInput, InteractiveEditorInput, MultiDiffEditorInput,
      ExtHostContext: { ExtHostEditorTabs: {} },
    });
    return { MainThread: owner.MainThreadEditorTabs, ExtHost: receiver.ExtHostEditorTabs,
      changes: editor.GroupModelChangeKind, operations: protocol.TabModelOperationKind } as Runtime;
  })();
}

function makeHost(gatewayId: string, workspaceIds: string[]) {
  const binding = { gateway_id: gatewayId, backend_id: '00000000-0000-4000-8000-000000000001', initialization: {
    os: 'linux', parent_pid: 1, logs_location_uri: 'file:///target/logs',
    environment: { app_root_uri: 'file:///target/app', global_storage_home_uri: 'file:///target/global',
      workspace_storage_home_uri: 'file:///target/storage', profile_id: 'default',
      remote_settings: { native_uri: 'file:///target/profile/settings.json', resource_uri: `boxteam://${gatewayId}/profile/default/settings` } },
    workspace: { id: `${gatewayId}-view`, name: '明确工作区视图', folders: workspaceIds.map(id => ({ workspace_id: id, name: id,
      resource_uri: `boxteam://workspace/${id}/resources/files/root`, native_uri: `file:///target/${id}` })) },
  } };
  const uriTransformer = new TargetUriTransformer(binding as never, () => []);
  return { host: { binding, targetContext: { kind: 'node', connection: binding }, uriTransformer }, roots: uriTransformer.folders.map(folder => folder.uri), uriTransformer };
}
const hostA = makeHost('gateway-tabs', ['workspace-a1', 'workspace-a2']);
const hostB = makeHost('gateway-tabs', ['workspace-b']);
const fileA = (name: string, index = 0) => URI.joinPath(hostA.roots[index], name);
const fileB = (name: string) => URI.joinPath(hostB.roots[0], name);
const text = (label: string, uri = fileA(label)) => new AbstractTextResourceEditorInput(label, uri);

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
function connect(model: EditorModel, target: typeof hostA | undefined) {
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
    return JSON.parse(JSON.stringify(value, (_key, item: unknown) => {
      if (target && isUriComponents(item) && '$mid' in item && item.$mid === 1) {
        return target.uriTransformer.transformOutgoing(item);
      }
      return item;
    }));
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
  owner = new model.runtime.MainThread({ extensionHost: target?.host ?? { binding: undefined }, getProxy: () => proxy },
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

test('真实 owner 到原 receiver：完整 input 按 A1/A2 view 隔离，所有组和真实 active group 保留，Web 看完整窗口', async () => {
  const runtime = await loadRuntime();
  const a1 = text('a1.ts'), a2 = text('a2.ts', fileA('a2.ts', 1)), b = text('b.ts', fileB('b.ts'));
  const merge = (label: string, last: URI) => new MergeEditorInput(label, fileA('base'), { uri: fileA('left') }, { uri: last }, fileA('result'));
  const editors = [b, a1, a2,
    new DiffEditorInput('diff-own', a1, a2), new DiffEditorInput('diff-mixed', a1, b),
    new DiffEditorInput('notebook-own', new NotebookEditorInput('nb1', fileA('1.ipynb')), new NotebookEditorInput('nb2', fileA('2.ipynb', 1))),
    new DiffEditorInput('notebook-mixed', new NotebookEditorInput('nb1', fileA('1.ipynb')), new NotebookEditorInput('nb-b', fileB('2.ipynb'))),
    new CustomEditorInput('custom-own', fileA('image.bin')), new SideBySideEditorInput('side-own', a1, a2),
    new SideBySideEditorInput('side-mixed', a1, b), new InteractiveEditorInput('interactive-own', fileA('interactive'), fileA('input')),
    new InteractiveEditorInput('interactive-mixed', fileA('interactive2'), fileB('input')), merge('merge-own', fileA('right', 1)),
    merge('merge-mixed', fileB('right')), new WebviewInput('webview'), new TerminalEditorInput('terminal'), new ChatEditorInput('chat'),
    text('outside-native', URI.file('/outside/unbound.txt')), text('other-gateway', fileA('same.ts').with({ authority: 'foreign-gateway' }))];
  const model = new EditorModel(runtime, [editors, [text('b-active.ts', fileB('b-active.ts'))], []]);
  model.groups[0].activeEditor = a1; model.activeGroup = model.groups[1];
  const a = connect(model, hostA), other = connect(model, hostB), web = connect(model, undefined);
  try {
    await ready();
    expect(a.receiver.tabGroups.all).toHaveLength(3);
    expect(other.receiver.tabGroups.all).toHaveLength(3);
    expect(labels(a.group(model.groups[0]).apiObject)).toEqual(['a1.ts', 'a2.ts', 'diff-own', 'notebook-own', 'custom-own', 'side-own', 'interactive-own', 'merge-own']);
    expect(labels(other.group(model.groups[0]).apiObject)).toEqual(['b.ts']);
    expect(a.receiver.tabGroups.activeTabGroup).toBe(a.group(model.activeGroup).apiObject);
    expect(a.receiver.tabGroups.activeTabGroup.tabs).toEqual([]);
    expect(a.receiver.tabGroups.activeTabGroup.activeTab).toBeUndefined();
    expect(other.receiver.tabGroups.activeTabGroup.activeTab?.label).toBe('b-active.ts');
    expect(a.group(model.groups[0]).apiObject.tabs[0].input?.uri?.toString()).toBe('file:///target/workspace-a1/a1.ts');
    expect(a.group(model.groups[0]).apiObject.tabs[1].input?.uri?.toString()).toBe('file:///target/workspace-a2/a2.ts');
    expect(labels(web.group(model.groups[0]).apiObject)).toEqual(editors.map(editor => editor.label));
    expect(web.group(model.groups[0]).apiObject.tabs[1].input?.uri?.toString()).toBe(a1.resource!.toString());
    expect(web.receiver.tabGroups.activeTabGroup).toBe(web.group(model.activeGroup).apiObject);
    const projectedIds = a.receiver._extHostTabGroups.flatMap(group => group.tabs.map(tab => tab.tabId));
    expect(projectedIds.every(id => /^\d+~\d+$/.test(id))).toBe(true);
    expect([a.errors, other.errors, web.errors]).toEqual([[], [], []]);
  } finally { a.dispose(); other.dispose(); web.dispose(); model.dispose(); }
});

test('增量 open/close/move 使用 projected index，状态更新及原 Tab/TabGroup/API 身份连续保留', async () => {
  const runtime = await loadRuntime();
  const a1 = text('one.ts'), a2 = text('two.ts'), b0 = text('b0', fileB('b0')), b1 = text('b1', fileB('b1'));
  const model = new EditorModel(runtime, [[b0, a1, b1, a2], [text('b2', fileB('b2'))]]);
  model.groups[0].activeEditor = a1;
  const connection = connect(model, hostA), group = model.groups[0];
  try {
    await ready();
    const api = connection.receiver.tabGroups, originalGroup = connection.group(group), groupApi = originalGroup.apiObject;
    const [originalOne, originalTwo] = originalGroup.tabs, [one, two] = groupApi.tabs;
    const retained = new Map<object, string>([[api, 'api'], [originalGroup, 'group'], [groupApi, 'groupApi'], [originalOne, 'one'], [originalTwo, 'two'], [one, 'oneApi'], [two, 'twoApi']]);
    a2.label = 'two-renamed.ts'; model.emit(group, 'EDITOR_LABEL', a2, 3);
    a2.dirty = true; model.emit(group, 'EDITOR_DIRTY', a2, 3);
    group.sticky.add(a2); model.emit(group, 'EDITOR_STICKY', a2, 3);
    group.pinned.delete(a2); model.emit(group, 'EDITOR_PIN', a2, 3);
    expect(two.label).toBe('two-renamed.ts'); expect(two.isDirty).toBe(true); expect(two.isPinned).toBe(true); expect(two.isPreview).toBe(true);
    expect(operations(connection, runtime.operations.TAB_UPDATE).slice(-4).map(value => value.index)).toEqual([1, 1, 1, 1]);
    group.activate(b1); expect(groupApi.activeTab).toBeUndefined(); expect(one.isActive).toBe(false); expect(two.isActive).toBe(false);
    group.activate(a1); a2.label = 'two-later.ts'; model.emit(group, 'EDITOR_LABEL', a2, group.editors.indexOf(a2));
    expect(groupApi.activeTab).toBe(one); expect(two.isActive).toBe(false);
    const count = connection.packets.length;
    group.open(text('foreign-open', fileB('foreign-open')), 1);
    expect(connection.packets).toHaveLength(count);
    const a3 = text('three.ts'); group.open(a3, 2);
    const three = groupApi.tabs[0];
    expect(labels(groupApi)).toEqual(['three.ts', 'one.ts', 'two-later.ts']);
    expect(operations(connection, runtime.operations.TAB_OPEN).at(-1)?.index).toBe(0);
    group.moveEditor(a2, group, { index: 0 });
    expect(labels(groupApi)).toEqual(['two-later.ts', 'three.ts', 'one.ts']);
    expect(operations(connection, runtime.operations.TAB_MOVE).at(-1)).toMatchObject({ oldIndex: 2, index: 0 });
    expect(groupApi.tabs[0]).toBe(two); expect(groupApi.tabs[2]).toBe(one);
    const beforeForeignClose = connection.packets.length;
    group.closeNow(b0); expect(connection.packets).toHaveLength(beforeForeignClose);
    group.closeNow(a3);
    expect(operations(connection, runtime.operations.TAB_CLOSE).at(-1)?.index).toBe(1);
    expect(connection.tabsChanged.at(-1)?.closed[0]).toBe(three);
    expect(labels(groupApi)).toEqual(['two-later.ts', 'one.ts']);
    expect(connection.group(group)).toBe(originalGroup);
    expect(connection.group(group).tabs[0]).toBe(originalTwo); expect(connection.group(group).tabs[1]).toBe(originalOne);
    expect(retained.get(connection.receiver.tabGroups)).toBe('api'); expect(retained.get(connection.group(group))).toBe('group');
    expect(retained.get(connection.group(group).apiObject)).toBe('groupApi'); expect(retained.get(connection.group(group).tabs[1])).toBe('one');
    expect(retained.get(connection.group(group).tabs[0])).toBe('two');
    expect(retained.get(groupApi.tabs[1])).toBe('oneApi'); expect(retained.get(groupApi.tabs[0])).toBe('twoApi');
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('组 add/remove/move 的完整 resync 保留原 receiver 对象和实际 activeTabGroup', async () => {
  const runtime = await loadRuntime();
  const model = new EditorModel(runtime, [[text('a.ts')], [text('b.ts', fileB('b.ts'))]]);
  const connection = connect(model, hostA);
  try {
    await ready();
    const group = model.groups[0], foreign = model.groups[1], original = connection.group(group);
    const tab = original.tabs[0], groupApi = original.apiObject, tabApi = tab.apiObject;
    const added = model.addGroup(foreign);
    model.activeGroup = foreign; model.emit(foreign, 'GROUP_ACTIVE');
    expect(connection.receiver.tabGroups.activeTabGroup).toBe(connection.group(foreign).apiObject);
    expect(connection.receiver.tabGroups.activeTabGroup.activeTab).toBeUndefined();
    model.moveGroup(foreign, 0);
    expect(groupApi.viewColumn).toBe(2); expect(connection.group(foreign).apiObject.viewColumn).toBe(1);
    model.removeGroup(added);
    expect(connection.group(group)).toBe(original); expect(original.tabs[0]).toBe(tab);
    expect(connection.group(group).apiObject).toBe(groupApi); expect(original.tabs[0].apiObject).toBe(tabApi);
    expect(connection.receiver.tabGroups.activeTabGroup).toBe(connection.group(foreign).apiObject);
    expect(connection.packets.filter(packet => packet.kind === 'model')).toHaveLength(4);
    expect(connection.groupsChanged.some(change => change.opened.includes(groupApi))).toBe(true);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('before-ready 事件和 dispose 后迟到 ready 都零发包', async () => {
  const runtime = await loadRuntime(), gate = Promise.withResolvers<void>();
  const model = new EditorModel(runtime, [[text('a.ts')]], gate.promise), connection = connect(model, hostA);
  try {
    model.groups[0].open(text('before-ready.ts'));
    const added = model.addGroup(model.groups[0]); model.moveGroup(added, 0); model.removeGroup(added);
    expect(connection.packets).toEqual([]);
    connection.owner.dispose(); gate.resolve(); await ready();
    model.groups[0].open(text('after-dispose.ts'));
    expect(connection.packets).toEqual([]); expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('MultiDiff 初始和 open 都监听完整 endpoints，mixed 隐藏仍监听，恢复才 open，真实关闭释放', async () => {
  const runtime = await loadRuntime();
  const pair = { originalUri: fileA('old.ts'), modifiedUri: fileA('new.ts', 1), goToFileUri: fileA('jump.ts') };
  const initial = new MultiDiffEditorInput('initial', [pair]);
  const hidden = new MultiDiffEditorInput('mixed', [{ ...pair, modifiedUri: fileB('foreign.ts') }]);
  const single = new MultiDiffEditorInput('single-owned', [{ modifiedUri: fileA('single.ts') }]);
  const foreignSingle = new MultiDiffEditorInput('single-foreign', [{ originalUri: fileB('single.ts') }]);
  const foreignGoto = new MultiDiffEditorInput('goto-foreign', [{ ...pair, goToFileUri: fileB('jump.ts') }]);
  const empty = new MultiDiffEditorInput('empty', []), unresolved = new MultiDiffEditorInput('unresolved', undefined);
  const initialEditors = [initial, hidden, single, foreignSingle, foreignGoto, empty, unresolved];
  const model = new EditorModel(runtime, [initialEditors]), connection = connect(model, hostA), group = model.groups[0];
  try {
    await ready();
    const groupApi = connection.group(group).apiObject, original = groupApi.tabs[0], singleApi = groupApi.tabs[1];
    expect(labels(groupApi)).toEqual(['initial', 'single-owned']);
    expect(initialEditors.map(editor => editor.listeners)).toEqual([1, 1, 1, 1, 1, 1, 1]);
    expect(singleApi.input?.textDiffs).toEqual([]);
    initial.resources.set([{ ...pair, modifiedUri: fileA('changed.ts', 1) }], undefined);
    expect(groupApi.tabs[0]).toBe(original);
    expect(original.input?.textDiffs?.[0].modified?.toString()).toBe('file:///target/workspace-a2/changed.ts');
    const sourceId = tabId(connection, original);
    expect(sourceId).not.toContain('opaque-initial'); expect(sourceId).not.toContain('multi-diff-editor');
    initial.resources.set([{ ...pair, goToFileUri: fileB('changed.ts') }], undefined);
    expect(labels(groupApi)).toEqual(['single-owned']); expect(initial.listeners).toBe(1);
    expect(connection.tabsChanged.at(-1)?.closed).toEqual([original]);
    const beforeStillMixed = connection.packets.length;
    initial.resources.set([{ originalUri: fileB('still-foreign.ts') }], undefined);
    expect(connection.packets).toHaveLength(beforeStillMixed);
    initial.resources.set([pair], undefined);
    expect(labels(groupApi)).toEqual(['initial', 'single-owned']); expect(groupApi.tabs[0]).not.toBe(original);
    expect(groupApi.tabs[1]).toBe(singleApi); expect(initial.listeners).toBe(1);
    hidden.resources.set([{ originalUri: fileA('one-sided.ts') }], undefined);
    expect(labels(groupApi)).toEqual(['initial', 'mixed', 'single-owned']);
    expect(groupApi.tabs[1].input?.textDiffs).toEqual([]);
    foreignSingle.resources.set([{ originalUri: fileB('still-foreign.ts'), goToFileUri: fileA('owned-jump.ts') }], undefined);
    expect(labels(groupApi)).not.toContain('single-foreign');
    foreignGoto.resources.set([pair], undefined); expect(labels(groupApi)).toContain('goto-foreign');
    const opened = new MultiDiffEditorInput('opened-mixed', [{ ...pair, originalUri: fileB('other.ts') }]);
    group.open(opened, 1);
    expect(opened.listeners).toBe(1); expect(labels(groupApi)).not.toContain('opened-mixed');
    opened.resources.set([pair], undefined);
    expect(labels(groupApi)).toEqual(['initial', 'opened-mixed', 'mixed', 'single-owned', 'goto-foreign']);
    expect(operations(connection, runtime.operations.TAB_OPEN).at(-1)?.index).toBe(1);
    group.closeNow(opened); expect(opened.listeners).toBe(0);
    group.closeNow(foreignSingle); expect(foreignSingle.listeners).toBe(0);
    const afterClose = connection.packets.length; opened.resources.set([pair], undefined); foreignSingle.resources.set([pair], undefined);
    expect(connection.packets).toHaveLength(afterClose);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
  expect(initialEditors.map(editor => editor.listeners)).toEqual([0, 0, 0, 0, 0, 0, 0]);
});

test('Node closeTab 整批预检：未知或归属已变化的后一项不能先关闭前一项，原 receiver close 可成功', async () => {
  const runtime = await loadRuntime(), first = text('first.ts'), second = text('second.ts');
  const model = new EditorModel(runtime, [[first], [second]]), connection = connect(model, hostA);
  try {
    await ready();
    const firstApi = connection.group(model.groups[0]).apiObject.tabs[0], secondApi = connection.group(model.groups[1]).apiObject.tabs[0];
    const firstId = tabId(connection, firstApi), secondId = tabId(connection, secondApi);
    await expect(connection.owner.$closeTab([firstId, 'foreign-opaque-id'])).rejects.toThrow('不属于发起宿主');
    expect(model.groups.map(group => group.closeCalls.length)).toEqual([0, 0]);
    const source = second.resource; second.resource = fileB('now-foreign.ts');
    await expect(connection.owner.$closeTab([firstId, secondId])).rejects.toThrow('不属于发起宿主');
    expect(model.groups.map(group => group.closeCalls.length)).toEqual([0, 0]); second.resource = source;
    expect(await connection.receiver.tabGroups.close([firstApi, secondApi], true)).toBe(true);
    expect(model.groups.map(group => group.closeCalls[0])).toEqual([
      { editors: [first], options: { preserveFocus: true } }, { editors: [second], options: { preserveFocus: true } },
    ]);
    expect(connection.tabsChanged.flatMap(change => change.closed)).toEqual([firstApi, secondApi]);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

test('Node mixed closeGroup 只关闭自有输入且保留 foreign 非空组，Web closeGroup 保持全窗口范围', async () => {
  const runtime = await loadRuntime(), own = text('own.ts'), foreign = text('foreign.ts', fileB('foreign.ts')), ownLast = text('own-last.ts');
  const model = new EditorModel(runtime, [[foreign, own], [text('foreign-only.ts', fileB('foreign-only.ts'))], [ownLast]]);
  const connection = connect(model, hostA);
  try {
    await ready();
    const [mixed, foreignOnly, last] = model.groups, mixedApi = connection.group(mixed).apiObject;
    expect(await connection.receiver.tabGroups.close([mixedApi, connection.group(foreignOnly).apiObject, connection.group(last).apiObject], true)).toBe(true);
    expect(mixed.closeCalls).toEqual([{ editors: [own], options: { preserveFocus: true } }]);
    expect(foreignOnly.closeCalls.every(call => call.editors.length === 0)).toBe(true);
    expect(mixed.editors).toEqual([foreign]); expect(model.groups).toEqual([mixed, foreignOnly]);
    expect(model.removeCalls).toEqual([last]); expect(connection.group(mixed).apiObject).toBe(mixedApi);
    expect(model.groups.every(group => group.closeAllCalls === 0)).toBe(true);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
  const webModel = new EditorModel(runtime, [[text('a.ts'), text('b.ts', fileB('b.ts')), new TerminalEditorInput('terminal')], []]);
  const web = connect(webModel, undefined);
  try {
    await ready();
    const group = webModel.groups[0], originals = [...group.editors];
    expect(await web.receiver.tabGroups.close(web.group(group).apiObject)).toBe(true);
    expect(group.closeCalls[0].editors).toEqual(originals); expect(group.count).toBe(0); expect(webModel.removeCalls).toEqual([group]);
    expect(web.errors).toEqual([]);
  } finally { web.dispose(); webModel.dispose(); }
});

test('move 先核 source 再创建组，目标 projected index 按可见 anchor 转真实索引，同组移动仍保 API 身份', async () => {
  const runtime = await loadRuntime(), source = text('source.ts'), a0 = text('target-zero.ts'), a1 = text('target-one.ts');
  const model = new EditorModel(runtime, [[text('b-source', fileB('b-source')), source],
    [text('b-leading', fileB('b-leading')), a0, text('b-middle', fileB('b-middle')), a1, text('b-trailing', fileB('b-trailing'))]]);
  const connection = connect(model, hostA);
  try {
    await ready();
    const [sourceGroup, target] = model.groups;
    const sourceId = tabId(connection, connection.group(sourceGroup).apiObject.tabs[0]);
    expect(() => connection.owner.$moveTab('unknown-tab', 0, 8)).toThrow('不属于发起宿主');
    expect(model.addCalls).toEqual([]);
    const originalResource = source.resource; source.resource = fileB('source-moved-away.ts');
    expect(() => connection.owner.$moveTab(sourceId, 0, 8)).toThrow('不属于发起宿主');
    expect(model.addCalls).toEqual([]); source.resource = originalResource;
    connection.owner.$moveTab(sourceId, 1, 1, true);
    expect(sourceGroup.moveCalls[0]).toEqual({ editor: source, target, options: { index: 3, preserveFocus: true } });
    expect(labels(connection.group(target).apiObject)).toEqual(['target-zero.ts', 'source.ts', 'target-one.ts']);
    const zero = connection.group(target).apiObject.tabs[0], zeroId = tabId(connection, zero);
    connection.owner.$moveTab(zeroId, 3, 1, false);
    expect(target.moveCalls.at(-1)?.options).toEqual({ index: 5, preserveFocus: false });
    expect(labels(connection.group(target).apiObject)).toEqual(['source.ts', 'target-one.ts', 'target-zero.ts']);
    expect(connection.group(target).apiObject.tabs[2]).toBe(zero);
    connection.owner.$moveTab(zeroId, 0, 1);
    expect(target.moveCalls.at(-1)?.options.index).toBe(2);
    expect(connection.group(target).apiObject.tabs[0]).toBe(zero);
    connection.owner.$moveTab(zeroId, -1, 2);
    expect(model.addCalls).toHaveLength(1);
    expect(target.moveCalls.at(-1)?.target).toBe(model.addCalls[0]);
    expect(target.moveCalls.at(-1)?.options.index).toBe(0);
    expect(labels(connection.group(model.addCalls[0]).apiObject)).toEqual(['target-zero.ts']);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

for (const operation of ['tab', 'group'] as const) test(`${operation} close 的异步等待后 dispose，不触碰第二组也不再发包`, async () => {
  const runtime = await loadRuntime(), gate = Promise.withResolvers<boolean>();
  const model = new EditorModel(runtime, [[text('first.ts')], [text('second.ts')]]), connection = connect(model, hostA);
  try {
    await ready();
    const [first, second] = model.groups; first.closeGate = gate.promise;
    const waiting = operation === 'tab'
      ? connection.owner.$closeTab([tabId(connection, connection.group(first).apiObject.tabs[0]), tabId(connection, connection.group(second).apiObject.tabs[0])])
      : connection.owner.$closeGroup([first.id, second.id]);
    expect(first.closeCalls).toHaveLength(1); expect(second.closeCalls).toEqual([]);
    connection.owner.dispose(); const packets = connection.packets.length; gate.resolve(true);
    await expect(waiting).rejects.toThrow('标签宿主代际已经关闭；前序关闭结果=[true]，未执行后继关闭');
    expect(second.closeCalls).toEqual([]); expect(second.editors.map(editor => editor.label)).toEqual(['second.ts']);
    expect(model.removeCalls).toEqual([]); expect(connection.packets).toHaveLength(packets); expect(connection.errors).toEqual([]);
  } finally { gate.resolve(true); connection.dispose(); model.dispose(); }
});

test('同组相同投影位置为 no-op，不跨过隐藏标签或发送无意义增量', async () => {
  const runtime = await loadRuntime();
  const model = new EditorModel(runtime, [[text('first.ts'), text('foreign.ts', fileB('foreign.ts')), text('last.ts')]]);
  const connection = connect(model, hostA);
  try {
    await ready();
    const group = model.groups[0], before = [...group.editors], packets = connection.packets.length;
    connection.owner.$moveTab(tabId(connection, connection.group(group).apiObject.tabs[0]), 0, 0);
    expect(group.editors).toEqual(before);
    expect(group.moveCalls).toEqual([]);
    expect(connection.packets).toHaveLength(packets);
    expect(connection.errors).toEqual([]);
  } finally { connection.dispose(); model.dispose(); }
});

for (const operation of ['tab', 'group'] as const) for (const result of [true, false]) {
  test(`${operation} 单组 close 已返回 ${result} 后 dispose，保留真实结果且不再移除组或发包`, async () => {
    const runtime = await loadRuntime(), gate = Promise.withResolvers<boolean>();
    const model = new EditorModel(runtime, [[text('only.ts')]]), connection = connect(model, hostA);
    try {
      await ready();
      const group = model.groups[0]; group.closeGate = gate.promise;
      const waiting = operation === 'tab'
        ? connection.owner.$closeTab([tabId(connection, connection.group(group).apiObject.tabs[0])])
        : connection.owner.$closeGroup([group.id]);
      expect(group.closeCalls).toHaveLength(1);
      connection.owner.dispose(); const packets = connection.packets.length; gate.resolve(result);
      expect(await waiting).toBe(result);
      expect(group.editors).toHaveLength(result ? 0 : 1);
      expect(model.removeCalls).toEqual([]);
      expect(connection.packets).toHaveLength(packets);
      expect(connection.errors).toEqual([]);
    } finally { gate.resolve(result); connection.dispose(); model.dispose(); }
  });
}
