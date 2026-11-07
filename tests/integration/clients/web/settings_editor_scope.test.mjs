import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { Disposable, DisposableStore, dispose, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Event, Emitter, Relay } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { CancellationToken, CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError } from '../../../../src/clients/workbench/upstream/src/vs/base/common/errors.js';
import { URI } from '../../../../src/clients/workbench/upstream/src/vs/base/common/uri.js';
import { assertReturnsDefined } from '../../../../src/clients/workbench/upstream/src/vs/base/common/types.js';
import { LongRunningOperation, IEditorProgressService } from '../../../../src/clients/workbench/upstream/src/vs/platform/progress/common/progress.js';
import { IContextKeyService } from '../../../../src/clients/workbench/upstream/src/vs/platform/contextkey/common/contextkey.js';
import { InstantiationService } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiationService.js';
import { ServiceCollection } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/serviceCollection.js';
import { createDecorator } from '../../../../src/clients/workbench/upstream/src/vs/platform/instantiation/common/instantiation.js';
import { pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/settings_editor_scope.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = join(root, 'upstream/src/vs');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const sources = new Map(), fingerprints = [];
const registered = [], passed = [];

async function declaration(relative, name, environment = {}) {
  if (!sources.has(relative)) {
    const path = join(upstream, relative), original = await readFile(path, 'utf8');
    const converted = await transform(path, original);
    const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
    if (parsed.parseDiagnostics.length) throw new Error(`转换源码不能解析：${relative}`);
    sources.set(relative, parsed);
    fingerprints.push({ path: relative, original: createHash('sha256').update(original).digest('hex'),
      converted: createHash('sha256').update(converted).digest('hex') });
    await writeFile(join(run.artifactsDir, relative.replaceAll('/', '__')), converted);
  }
  const parsed = sources.get(relative);
  const item = parsed.statements.find(node => node.name?.text === name ||
    ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(parsed) === name));
  if (!item) throw new Error(`实际源码缺少声明：${relative}#${name}`);
  // 仅删除装配用参数装饰器，类字段、构造器和所有原方法均执行实际转换源码。
  const stripped = ts.transform(item, [context => {
    const visit = node => ts.isDecorator(node) ? undefined : ts.visitEachChild(node, visit, context);
    return node => ts.visitNode(node, visit);
  }]);
  const code = ts.createPrinter().printNode(ts.EmitHint.Unspecified, stripped.transformed[0], parsed)
    .replace(/^export /gm, '');
  stripped.dispose();
  const javascript = ts.transpile(code, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None,
    useDefineForClassFields: false });
  const bindings = Object.entries(environment).filter(([key]) => key !== name);
  return new Function(...bindings.map(([key]) => key), `${javascript}\nreturn ${name};`)(...bindings.map(([, value]) => value));
}

const common = {};
for (const name of ['EditorInputCapabilities', 'Verbosity', 'SideBySideEditor', 'EditorCloseContext', 'EditorsOrder', 'GroupModelChangeKind']) {
  common[name] = await declaration('workbench/common/editor.ts', name);
}
common.AbstractEditorInput = await declaration('workbench/common/editor.ts', 'AbstractEditorInput', { Disposable });
common.isEditorInput = await declaration('workbench/common/editor.ts', 'isEditorInput', common);
for (const name of ['isDiffEditorInput', 'isResourceDiffEditorInput', 'isResourceSideBySideEditorInput']) {
  common[name] = await declaration('workbench/common/editor.ts', name, common);
}
const Input = await declaration('workbench/common/editor/editorInput.ts', 'EditorInput', {
  ...common, Emitter, DisposableStore, URI,
});
const CompoundInput = await declaration('workbench/common/editor/sideBySideEditorInput.ts', 'SideBySideEditorInput', {
  ...common, EditorInput: Input, Event, localize: (_key, text) => text,
});
const Descriptor = await declaration('workbench/browser/editor.ts', 'EditorPaneDescriptor', { Emitter });
const Target = createDecorator('settingsScopeTestTarget');
const groupModelDependencies = { ...common, Disposable, DisposableStore, dispose, Emitter, Event,
  EditorInput: Input, SideBySideEditorInput: CompoundInput };
groupModelDependencies.isSerializedEditorGroupModel = await declaration('workbench/common/editor/editorGroupModel.ts', 'isSerializedEditorGroupModel');
groupModelDependencies.EditorOpenPositioning = await declaration('workbench/common/editor/editorGroupModel.ts', 'EditorOpenPositioning');
const GroupModel = await declaration('workbench/common/editor/editorGroupModel.ts', 'EditorGroupModel', groupModelDependencies);
const GroupView = await declaration('workbench/browser/parts/editor/editorGroupView.ts', 'EditorGroupView', {
  ...common, Themable: Disposable, SideBySideEditorInput: CompoundInput,
});

function node() {
  return { children: [], parent: undefined, hidden: false, style: {}, classList: { add() {} },
    appendChild(child) { child.remove(); child.parent = this; this.children.push(child); return child; },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = undefined; },
  };
}
function clearNode(container) {
  for (const child of [...container.children]) child.remove();
}

class CompositeBoundary extends Disposable {
  constructor(id) { super(); this.id = id; this.onDidFocus = this._register(new Emitter()).event; }
  create(container) { this.container = container; }
  getContainer() { return this.container; }
  getId() { return this.id; }
  setVisible(value) { this.visible = value; }
  isVisible() { return this.visible; }
  focus() {}
  updateStyles() {}
}
const Pane = await declaration('workbench/browser/parts/editor/editorPane.ts', 'EditorPane', {
  Composite: CompositeBoundary, Emitter, Event, DEFAULT_EDITOR_MIN_DIMENSIONS: { width: 1, height: 1 },
  DEFAULT_EDITOR_MAX_DIMENSIONS: { width: Infinity, height: Infinity },
});
class ViewStateBoundary extends Pane {
  constructor(id, group, _key, telemetry, instantiation, storage, _text, theme) {
    super(id, group, telemetry, theme, storage);
    this.instantiationService = instantiation;
  }
  loadEditorViewState() { return undefined; }
}

class PlainInput extends Input {
  constructor(key) { super(); this.key = key; }
  get typeId() { return 'settings-scope-probe'; }
  get resource() { return URI.parse(`test:/${this.key}`); }
  matches(other) { return other instanceof PlainInput && this.key === other.key; }
}
class ScopedInput extends PlainInput {
  constructor(key) { super(key); this.preparations = []; this.entered = Promise.withResolvers(); }
  get editorPaneScope() { return this; }
  async prepareEditorPane(parent, store, token) {
    this.entered.resolve();
    await this.prepareGate;
    const target = { key: this.key, disposed: 0, order: [], createError: this.createError,
      onConstruct: this.onConstruct, onCreate: this.onCreate, onVisible: this.onVisible };
    store.add(toDisposable(() => { target.disposed++; target.order.push('services'); }));
    const child = parent.createChild(new ServiceCollection([Target, target]), store);
    this.preparations.push({ parent, store, token, target, child });
    if (this.prepareError) throw this.prepareError;
    return child;
  }
}

async function harness(groupId = 'group-A') {
  const created = [], logs = [], moves = [], progress = { show: () => ({ done() {} }) };
  const focus = { active: null };
  const context = { groupId }, groupEvents = new Emitter(), group = { id: groupId, windowId: 1, onDidCloseEditor: groupEvents.event };
  const parent = new InstantiationService(new ServiceCollection([Target, { key: 'default', order: [] }]));
  const groupLifetime = new DisposableStore();
  const groupDI = parent.createChild(new ServiceCollection([IContextKeyService, context], [IEditorProgressService, progress]), groupLifetime);
  class ProbePane extends Pane {
    constructor(owner, target, contextKeys, editorProgress) {
      super('settings-pane', owner, {}, {}, {});
      this.target = target; this.contextKeys = contextKeys; this.editorProgress = editorProgress;
      this.inputs = []; this.appliedOptions = []; this.focuses = 0; this.disposed = 0;
      created.push(this);
      this.target.onConstruct?.();
    }
    createEditor(container) {
      container.appendChild(node());
      this.target.onCreate?.();
      if (this.target.createError) throw this.target.createError;
    }
    async setInput(input, options, context, token) {
      await super.setInput(input, options, context, token);
      this.inputs.push({ input, token });
      input.setEntered?.resolve();
      await input.setGate;
    }
    setOptions(options) { super.setOptions(options); this.appliedOptions.push(options); }
    setEditorVisible(visible) { if (visible) this.target.onVisible?.(); }
    focus() { this.focuses++; }
    dispose() { if (!this._store.isDisposed) { this.disposed++; this.target.order.push('pane'); } super.dispose(); }
    layout() {}
  }
  Target(ProbePane, undefined, 1); IContextKeyService(ProbePane, undefined, 2); IEditorProgressService(ProbePane, undefined, 3);
  const descriptor = Descriptor.create(ProbePane, 'settings-pane', 'Settings probe');
  const errorDescriptor = Descriptor.create(ProbePane, 'error-placeholder', 'Error placeholder');
  const registry = { descriptor, getEditorPane() { return this.descriptor; } };
  const dependencies = {
    ...common, Disposable, DisposableStore, Emitter, Event, Relay, CancellationError, CancellationToken,
    LongRunningOperation, assertReturnsDefined, Registry: { as: () => registry }, EditorExtensions: { EditorPane: 'editor' },
    AbstractEditorWithViewState: ViewStateBoundary, EditorInput: Input, SideBySideEditorInput: CompoundInput,
    ErrorPlaceholderEditor: { DESCRIPTOR: errorDescriptor }, WorkspaceTrustRequiredPlaceholderEditor: { DESCRIPTOR: errorDescriptor },
    isCancellationError, isEditorOpenError: () => false, toErrorMessage: error => error.message,
    EditorOpenSource: { USER: 1 }, Severity: { Error: 3 }, localize: (_key, text) => text,
    getActiveElement: () => focus.active, getWindowById: () => ({ window: { id: 1 } }), isAncestor: () => false,
    isEditableElement: value => !!value.editable, $: node, show: value => { value.hidden = false; }, hide: value => { value.hidden = true; },
    DEFAULT_EDITOR_MIN_DIMENSIONS: { width: 1, height: 1 }, DEFAULT_EDITOR_MAX_DIMENSIONS: { width: Infinity, height: Infinity },
    Dimension: class { constructor(width, height) { this.width = width; this.height = height; } },
    Side: common.SideBySideEditor, Orientation: { HORIZONTAL: 0, VERTICAL: 1 }, SIDE_BY_SIDE_EDITOR_ID: 'side-pane',
    isEditorPaneWithSelection: () => false, clearNode,
    isSideBySideEditorViewState: value => typeof value?.primary === 'object' && typeof value?.secondary === 'object',
  };
  const EditorPanes = await declaration('workbench/browser/parts/editor/editorPanes.ts', 'EditorPanes', dependencies);
  const SidePane = await declaration('workbench/browser/parts/editor/sideBySideEditor.ts', 'SideBySideEditor', dependencies);
  const parentNode = node(), container = node();
  const panes = new EditorPanes(parentNode, container, group, { isRestored: () => true }, groupDI, progress,
    { onDidChangeTrust: Event.None, isWorkspaceTrusted: () => true }, { error: error => logs.push(error) }, {},
    { moveTop: value => moves.push(value) });
  const side = () => {
    const pane = new SidePane(group, {}, groupDI, {}, {}, { getValue: () => 'horizontal', onDidChangeConfiguration: Event.None }, {}, {}, {});
    pane.primaryEditorContainer = node(); pane.secondaryEditorContainer = node();
    pane.splitview = { layout() {}, distributeViewSizes() {}, resizeView() {} };
    pane.setVisible(true);
    return pane;
  };
  return { created, logs, moves, focus, context, progress, descriptor, registry, groupDI, container, panes, side,
    wireModel(model, allModels = [model]) {
      const boundary = { id: groupId, _onWillCloseEditor: new Emitter(), _onDidCloseEditor: groupEvents,
        // 原 GroupView 的 canDispose 会扫描其它组；这里只替换 UI 外围组清单。
        canDispose: editor => !allModels.some(item => item.contains(editor, { strictEquals: true, supportSideBySide: common.SideBySideEditor.ANY })),
        updateContainer() {},
      };
      return model.onDidModelChange(event => {
        if (event.kind === common.GroupModelChangeKind.EDITOR_CLOSE) {
          GroupView.prototype.handleOnDidCloseEditor.call(boundary, event.editor, event.editorIndex, event.context, event.sticky);
        }
      });
    },
    close() { panes.dispose(); groupEvents.dispose(); groupLifetime.dispose(); parent.dispose(); } };
}

function scenario(name, callback) {
  registered.push(name);
  test(name, async () => { await callback(); passed.push(name); });
}

scenario('默认输入保留原 Pane 复用；同输入选项等待在途 setInput 且不撤销它', async () => {
  const f = await harness(), input = new PlainInput('default-A'), next = new PlainInput('default-B');
  const gate = Promise.withResolvers(); input.setGate = gate.promise; input.setEntered = Promise.withResolvers();
  try {
    expect(input.editorPaneScope).toBeUndefined();
    expect(await input.prepareEditorPane(f.groupDI, new DisposableStore(), CancellationToken.None)).toBe(f.groupDI);
    const opening = f.panes.openEditor(input);
    await input.setEntered.promise;
    const pane = f.created[0], token = pane.inputs[0].token;
    const options = { preserveFocus: true, marker: 'latest' };
    const update = f.panes.openEditor(input, options);
    expect(token.isCancellationRequested).toBe(false);
    expect(pane.appliedOptions).toEqual([]);
    gate.resolve();
    expect((await opening).changed).toBe(true);
    expect(await update).toMatchObject({ pane, changed: false, cancelled: false });
    expect(pane.appliedOptions).toEqual([options]);
    expect((await f.panes.openEditor(next)).pane).toBe(pane);
    expect(f.created).toHaveLength(1);
  } finally { gate.resolve(); input.dispose(); next.dispose(); f.close(); }
});

scenario('同 descriptor 的 scope A/B 各有 Pane 与组 child，返回 A 复用；关闭按 owner 清理', async () => {
  const f = await harness(), a = new ScopedInput('A'), b = new ScopedInput('B');
  try {
    const paneA = (await f.panes.openEditor(a)).pane, paneB = (await f.panes.openEditor(b)).pane;
    expect(paneA).not.toBe(paneB);
    for (const pane of [paneA, paneB]) {
      expect(pane.contextKeys).toBe(f.context); expect(pane.editorProgress).toBe(f.progress);
    }
    expect(paneA.target.key).toBe('A'); expect(paneB.target.key).toBe('B');
    expect((await f.panes.openEditor(a)).pane).toBe(paneA);
    expect(a.preparations).toHaveLength(1);
    a.dispose();
    expect(paneA.disposed).toBe(1); expect(paneA.getContainer().parent).toBeUndefined();
    expect(a.preparations[0].target.order).toEqual(['pane', 'services']);
    expect(() => a.preparations[0].child.invokeFunction(() => true)).toThrow('disposed');
    expect(paneB.disposed).toBe(0);
    expect((await f.panes.openEditor(b)).pane).toBe(paneB);
    b.dispose();
    expect(f.panes.activeEditorPane).toBeNull(); expect(f.container.children).toHaveLength(0);
  } finally { a.dispose(); b.dispose(); f.close(); }
});

scenario('原 EditorGroupModel 合并 matches 的候选，原 Pane 继续使用实际已打开 input 的 scope', async () => {
  const f = await harness(), first = new ScopedInput('same-target'), candidate = new ScopedInput('same-target');
  const model = new GroupModel(undefined, f.groupDI, { onDidChangeConfiguration: Event.None, getValue: () => 'right' });
  try {
    const opened = model.openEditor(first, { pinned: true });
    const pane = (await f.panes.openEditor(opened.editor)).pane;
    const merged = model.openEditor(candidate, { pinned: true });
    expect(merged).toEqual({ editor: first, isNew: false });
    expect((await f.panes.openEditor(merged.editor)).pane).toBe(pane);
    expect(candidate.preparations).toHaveLength(0); expect(first.preparations).toHaveLength(1);
    expect(model.count).toBe(1);
  } finally { model.dispose(); first.dispose(); candidate.dispose(); f.close(); }
});

scenario('缓存同时区分 descriptor 身份与 scope，不能仅按 Pane 类型命中', async () => {
  const f = await harness(), input = new ScopedInput('same-scope');
  try {
    const first = (await f.panes.openEditor(input)).pane;
    f.registry.descriptor = Descriptor.create(first.constructor, 'settings-pane', 'Another descriptor');
    const second = (await f.panes.openEditor(input)).pane;
    expect(second).not.toBe(first); expect(f.created).toHaveLength(2);
    input.dispose(); expect(first.disposed).toBe(1); expect(second.disposed).toBe(1);
  } finally { input.dispose(); f.close(); }
});

scenario('同 input 的两组 DI 独立；释放一组不撤销另一组的 Pane 或 input', async () => {
  const left = await harness('left'), right = await harness('right'), input = new ScopedInput('shared');
  try {
    const a = (await left.panes.openEditor(input)).pane, b = (await right.panes.openEditor(input)).pane;
    expect(a.contextKeys.groupId).toBe('left'); expect(b.contextKeys.groupId).toBe('right');
    expect(a.target).not.toBe(b.target);
    left.panes.dispose();
    expect(a.disposed).toBe(1); expect(a.target.disposed).toBe(1); expect(input.isDisposed()).toBe(false);
    expect((await right.panes.openEditor(input)).pane).toBe(b); expect(b.disposed).toBe(0);
    input.dispose(); expect(b.disposed).toBe(1); expect(b.target.disposed).toBe(1);
  } finally { input.dispose(); left.close(); right.close(); }
});

scenario('真实组模型关闭非活动和活动标签均回收本组 scope，另一组同 input 保持可用', async () => {
  const left = await harness('left-close'), right = await harness('right-close');
  const shared = new ScopedInput('shared'), other = new ScopedInput('other');
  const config = { onDidChangeConfiguration: Event.None, getValue: () => 'right' };
  const models = [new GroupModel(undefined, left.groupDI, config), new GroupModel(undefined, right.groupDI, config)];
  const listeners = [left.wireModel(models[0], models), right.wireModel(models[1], models)];
  try {
    for (const [index, f] of [left, right].entries()) {
      models[index].openEditor(shared, { pinned: true });
      await f.panes.openEditor(shared);
    }
    const paneLeft = left.created[0], paneRight = right.created[0];
    models[0].openEditor(other, { pinned: true, active: true });
    const paneOther = (await left.panes.openEditor(other)).pane;
    expect(paneLeft.disposed).toBe(0);
    models[0].closeEditor(shared);
    expect(shared.isDisposed()).toBe(false);
    expect(paneLeft.disposed).toBe(1); expect(paneLeft.target.disposed).toBe(1);
    expect(left.panes.activeEditorPane).toBe(paneOther);
    expect(paneRight.disposed).toBe(0); expect((await right.panes.openEditor(shared)).pane).toBe(paneRight);
    models[0].openEditor(shared, { pinned: true, active: true });
    const reopened = (await left.panes.openEditor(shared)).pane;
    expect(reopened).not.toBe(paneLeft); expect(reopened.contextKeys.groupId).toBe('left-close');
    models[0].closeEditor(shared);
    expect(reopened.disposed).toBe(1); expect(shared.isDisposed()).toBe(false);
    expect((await left.panes.openEditor(other)).pane).toBe(paneOther);
    models[1].closeEditor(shared);
    expect(shared.isDisposed()).toBe(true); expect(paneRight.disposed).toBe(1);
  } finally {
    for (const listener of listeners) listener.dispose(); for (const model of models) model.dispose();
    shared.dispose(); other.dispose(); left.close(); right.close();
  }
});

scenario('默认输入的组关闭继续保留原 Pane 缓存', async () => {
  const f = await harness(), input = new PlainInput('normal-close'), next = new PlainInput('normal-next');
  const model = new GroupModel(undefined, f.groupDI, { onDidChangeConfiguration: Event.None, getValue: () => 'right' });
  const listener = f.wireModel(model);
  try {
    model.openEditor(input, { pinned: true });
    const pane = (await f.panes.openEditor(input)).pane;
    model.closeEditor(input); f.panes.closeEditor(input);
    expect(pane.disposed).toBe(0);
    expect((await f.panes.openEditor(next)).pane).toBe(pane);
  } finally { listener.dispose(); model.dispose(); input.dispose(); next.dispose(); f.close(); }
});

scenario('真实组关闭事件取消在途 prepare，另一组仍持有 input 时也释放迟到 child', async () => {
  const left = await harness('preparing-left'), right = await harness('visible-right'), input = new ScopedInput('shared-pending');
  const config = { onDidChangeConfiguration: Event.None, getValue: () => 'right' }, gate = Promise.withResolvers();
  const models = [new GroupModel(undefined, left.groupDI, config), new GroupModel(undefined, right.groupDI, config)];
  const listeners = [left.wireModel(models[0], models), right.wireModel(models[1], models)];
  try {
    models[1].openEditor(input, { pinned: true });
    const visible = (await right.panes.openEditor(input)).pane;
    input.prepareGate = gate.promise; input.entered = Promise.withResolvers();
    models[0].openEditor(input, { pinned: true });
    const opening = left.panes.openEditor(input); await input.entered.promise;
    models[0].closeEditor(input);
    expect(input.isDisposed()).toBe(false);
    gate.resolve(); expect((await opening).cancelled).toBe(true);
    expect(left.created).toHaveLength(0); expect(input.preparations[1].target.disposed).toBe(1);
    expect(right.panes.activeEditorPane).toBe(visible); expect(visible.disposed).toBe(0);
  } finally {
    gate.resolve(); for (const listener of listeners) listener.dispose(); for (const model of models) model.dispose();
    input.dispose(); left.close(); right.close();
  }
});

scenario('异步 prepare A 迟到时，后发 B 保持活动且 A 的迟到 child 释放', async () => {
  const f = await harness(), a = new ScopedInput('A'), b = new ScopedInput('B'), gate = Promise.withResolvers();
  a.prepareGate = gate.promise;
  try {
    const openingA = f.panes.openEditor(a); await a.entered.promise;
    const paneB = (await f.panes.openEditor(b)).pane;
    gate.resolve();
    expect(await openingA).toEqual({ cancelled: true });
    expect(f.created).toEqual([paneB]); expect(f.panes.activeEditorPane).toBe(paneB); expect(paneB.focuses).toBe(1);
    expect(a.preparations[0].token.isCancellationRequested).toBe(true);
    expect(a.preparations[0].target.disposed).toBe(1);
    expect(() => a.preparations[0].child.invokeFunction(() => true)).toThrow('disposed');
  } finally { gate.resolve(); a.dispose(); b.dispose(); f.close(); }
});

scenario('异步 prepare 期间用户转到其它输入框，仍按原焦点判定保留用户新焦点', async () => {
  const f = await harness(), input = new ScopedInput('focus'), gate = Promise.withResolvers(); input.prepareGate = gate.promise;
  const document = { body: {} }, original = { editable: true, ownerDocument: document }, next = { editable: true, ownerDocument: document };
  try {
    f.focus.active = original;
    const opening = f.panes.openEditor(input); await input.entered.promise;
    f.focus.active = next; gate.resolve();
    const result = await opening;
    expect(result.cancelled).toBe(false); expect(result.pane.focuses).toBe(0); expect(f.focus.active).toBe(next);
  } finally { gate.resolve(); input.dispose(); f.close(); }
});

for (const action of ['close', 'dispose-input', 'dispose-group']) {
  scenario(`prepare 等待中 ${action} 不允许迟到构造或挂载`, async () => {
    const f = await harness(), input = new ScopedInput(action), gate = Promise.withResolvers(); input.prepareGate = gate.promise;
    try {
      const opening = f.panes.openEditor(input); await input.entered.promise;
      if (action === 'close') f.panes.closeEditor(input);
      else if (action === 'dispose-input') input.dispose();
      else f.panes.dispose();
      gate.resolve();
      expect(await opening).toEqual({ cancelled: true }); expect(f.created).toHaveLength(0);
      expect(input.preparations[0].target.disposed).toBe(1); expect(f.container.children).toHaveLength(0);
      if (action === 'close') {
        input.prepareGate = undefined;
        expect((await f.panes.openEditor(input)).cancelled).toBe(false);
        expect(input.preparations).toHaveLength(2);
      }
    } finally { gate.resolve(); input.dispose(); f.close(); }
  });
}

for (const stage of ['onConstruct', 'onCreate', 'onVisible']) {
  scenario(`Pane ${stage} 同步释放 scope 后不能 setInput、挂载或抢焦点`, async () => {
    const f = await harness(), input = new ScopedInput(stage); input[stage] = () => input.dispose();
    try {
      expect((await f.panes.openEditor(input)).cancelled).toBe(true);
      expect(f.created).toHaveLength(1); expect(f.created[0].disposed).toBe(1);
      expect(f.created[0].inputs).toHaveLength(0); expect(f.created[0].focuses).toBe(0);
      expect(input.preparations[0].target.order).toEqual(['pane', 'services']);
      expect(f.container.children).toHaveLength(0); expect(f.panes.activeEditorPane).toBeNull();
    } finally { input.dispose(); f.close(); }
  });
}

scenario('同一 operation 延续到 setInput；旧 setInput 结算不能抢回焦点或覆盖 B', async () => {
  const f = await harness(), a = new ScopedInput('A'), b = new ScopedInput('B'), gate = Promise.withResolvers();
  a.setGate = gate.promise; a.setEntered = Promise.withResolvers();
  try {
    const openingA = f.panes.openEditor(a); await a.setEntered.promise;
    const paneA = f.created[0], paneB = (await f.panes.openEditor(b)).pane;
    expect(paneA.inputs[0].token).toBe(a.preparations[0].token);
    expect(paneA.inputs[0].token.isCancellationRequested).toBe(true);
    gate.resolve();
    expect((await openingA).cancelled).toBe(true); expect(paneA.focuses).toBe(0);
    expect(f.panes.activeEditorPane).toBe(paneB); expect(paneB.focuses).toBe(1);
    expect(paneA.input).toBeUndefined();
  } finally { gate.resolve(); a.dispose(); b.dispose(); f.close(); }
});

scenario('prepare 和 create 失败保留原错误出口，释放未完成 Pane/child 并允许明确重试', async () => {
  const f = await harness(), input = new ScopedInput('failure'), error = new Error('target prepare failed');
  try {
    input.prepareError = error;
    const failed = await f.panes.openEditor(input);
    expect(failed.error).toBe(error); expect(f.logs).toEqual([error]);
    expect(input.preparations).toHaveLength(1); expect(input.preparations[0].target.disposed).toBe(1);
    expect(failed.pane.target.key).toBe('default');
    input.prepareError = undefined; input.createError = new Error('target create failed');
    const createFailed = await f.panes.openEditor(input, { ignoreError: true });
    expect(createFailed.error).toBe(input.createError);
    expect(input.preparations[1].target.disposed).toBe(1);
    expect(f.created.find(pane => pane.target.key === 'failure').disposed).toBe(1);
    input.createError = undefined;
    const retried = await f.panes.openEditor(input);
    expect(retried.cancelled).toBe(false); expect(retried.pane.target.key).toBe('failure');
    expect(input.preparations).toHaveLength(3);
  } finally { input.dispose(); f.close(); }
});

scenario('原 SideBySideEditor 在两侧构造前取得各自组 child；任一输入释放清理两侧', async () => {
  const f = await harness(), a = new ScopedInput('left'), b = new ScopedInput('right'), side = f.side();
  const input = new CompoundInput(undefined, undefined, a, b, {});
  try {
    await side.setInput(input, undefined, {}, CancellationToken.None);
    const secondary = side.getSecondaryEditorPane(), primary = side.getPrimaryEditorPane();
    expect(secondary.target.key).toBe('left'); expect(primary.target.key).toBe('right');
    expect(secondary.contextKeys).toBe(f.context); expect(primary.editorProgress).toBe(f.progress);
    expect(secondary.inputs[0].input).toBe(a); expect(primary.inputs[0].input).toBe(b);
    a.dispose();
    expect(input.isDisposed()).toBe(true); expect(secondary.disposed).toBe(1); expect(primary.disposed).toBe(1);
    expect(secondary.target.order).toEqual(['pane', 'services']); expect(primary.target.order).toEqual(['pane', 'services']);
    expect(side.getPrimaryEditorPane()).toBeUndefined(); expect(side.primaryEditorContainer.children).toHaveLength(0);
  } finally { input.dispose(); a.dispose(); b.dispose(); side.dispose(); f.close(); }
});

scenario('SideBySideEditor 的取消准备不会在后发输入之后创建旧 Pane，迟到服务仍释放', async () => {
  const f = await harness(), side = f.side(), gate = Promise.withResolvers(), token = new CancellationTokenSource();
  const a = new ScopedInput('old-left'), b = new ScopedInput('old-right'); a.prepareGate = gate.promise; b.prepareGate = gate.promise;
  const c = new ScopedInput('new-left'), d = new ScopedInput('new-right');
  const oldInput = new CompoundInput(undefined, undefined, a, b, {}), newInput = new CompoundInput(undefined, undefined, c, d, {});
  try {
    const opening = side.setInput(oldInput, undefined, {}, token.token);
    const rejection = opening.then(() => { throw new Error('旧 SideBySideEditor 应当取消'); }, error => error);
    await Promise.all([a.entered.promise, b.entered.promise]); token.cancel();
    await side.setInput(newInput, undefined, {}, CancellationToken.None);
    const primary = side.getPrimaryEditorPane(), secondary = side.getSecondaryEditorPane();
    gate.resolve(); expect(await rejection).toBeInstanceOf(CancellationError);
    expect(f.created.map(pane => pane.target.key).sort()).toEqual(['new-left', 'new-right']);
    expect(a.preparations[0].target.disposed).toBe(1); expect(b.preparations[0].target.disposed).toBe(1);
    expect(side.getPrimaryEditorPane()).toBe(primary); expect(side.getSecondaryEditorPane()).toBe(secondary);
    expect(primary.disposed).toBe(0); expect(secondary.disposed).toBe(0);
  } finally {
    gate.resolve(); token.dispose(); oldInput.dispose(); newInput.dispose();
    for (const input of [a, b, c, d]) input.dispose(); side.dispose(); f.close();
  }
});

scenario('SideBySideEditor 一侧准备失败后，另一侧迟到结果不能复活 Pane 或泄漏 child', async () => {
  const f = await harness(), side = f.side(), gate = Promise.withResolvers();
  const a = new ScopedInput('failure'), b = new ScopedInput('late');
  a.prepareError = new Error('side prepare failed'); b.prepareGate = gate.promise;
  const input = new CompoundInput(undefined, undefined, a, b, {});
  try {
    await expect(side.setInput(input, undefined, {}, CancellationToken.None)).rejects.toThrow('side prepare failed');
    gate.resolve();
    // 等到真实异步准备结算及其 finally 完成，不伪造第二侧方法的结果。
    await b.entered.promise; await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.created).toHaveLength(0); expect(a.preparations[0].target.disposed).toBe(1); expect(b.preparations[0].target.disposed).toBe(1);
    expect(side.getPrimaryEditorPane()).toBeUndefined(); expect(side.primaryEditorContainer.children).toHaveLength(0);
  } finally { gate.resolve(); input.dispose(); a.dispose(); b.dispose(); side.dispose(); f.close(); }
});

for (const stage of ['onConstruct', 'onCreate']) {
  scenario(`SideBySideEditor 的 ${stage} 同步释放输入不会向已释放 store 登记 Pane`, async () => {
    const f = await harness(), side = f.side(), a = new ScopedInput('closing-left'), b = new ScopedInput('closing-right');
    a[stage] = () => a.dispose();
    const input = new CompoundInput(undefined, undefined, a, b, {});
    try {
      await expect(side.setInput(input, undefined, {}, CancellationToken.None)).rejects.toBeInstanceOf(CancellationError);
      for (const pane of f.created) { expect(pane.disposed).toBe(1); expect(pane.inputs).toHaveLength(0); }
      for (const owner of [a, b]) {
        for (const preparation of owner.preparations) {
          expect(preparation.target.disposed).toBe(1);
          expect(() => preparation.child.invokeFunction(() => true)).toThrow('disposed');
        }
      }
      expect(side.primaryEditorContainer.children).toHaveLength(0); expect(side.secondaryEditorContainer.children).toHaveLength(0);
    } finally { input.dispose(); a.dispose(); b.dispose(); side.dispose(); f.close(); }
  });
}

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'source-fingerprints.json'), JSON.stringify({ upstream: pinnedUpstreamCommit, sources: fingerprints }, null, 2));
  await run.writeResult(registered.length === passed.length ? 'passed' : 'failed', {
    boundary: '执行 sourceTransform 后完整 EditorInput、EditorPanes、SideBySideEditor 类，使用原 EditorPane/SideBySideEditorInput/EditorPaneDescriptor/EditorGroupModel、EditorGroupView.handleOnDidCloseEditor、LongRunningOperation、InstantiationService 和 DisposableStore；仅 DOM、Composite/ViewState UI 父类、目标服务及组外围服务是具名装具。未经过真实浏览器、Gateway 或 SettingsEditor2 业务。',
    registered, passed,
  });
  await run.close();
});
