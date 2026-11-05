import { afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import ts from 'typescript';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import type { IPaneCompositePartService } from '../upstream/src/vs/workbench/services/panecomposite/browser/panecomposite.js';
import type { IWorkbenchLayoutService } from '../upstream/src/vs/workbench/services/layout/browser/layoutService.js';
import type { IStorageService } from '../upstream/src/vs/platform/storage/common/storage.js';
import type { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import type { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { workspaceContext } from '../src/workspaceContext.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

interface PanelOwner { dispose(): void }
type PanelConstructor = new (panes: IPaneCompositePartService, layout: IWorkbenchLayoutService, storage: IStorageService,
  notifications: INotificationService, views: IViewsService) => PanelOwner;
// 执行完整产品 owner；仅隔离上游的浏览器模块注册与 DI 装饰器，不复制恢复算法。
const path = resolve(process.cwd(), 'src/clients/workbench/src/workspacePanelState.ts');
const source = ts.createSourceFile(path, await readFile(path, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
const statements = source.statements.filter(statement => !ts.isImportDeclaration(statement));
const js = ts.transpileModule(statements.map(statement => statement.getFullText(source)).join('\n'), {
  compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None, experimentalDecorators: true, useDefineForClassFields: false },
}).outputText;
const decorator = () => {};
const dependencies = { Disposable, workspaceContext, StorageScope: { WORKSPACE: 1 }, StorageTarget: { MACHINE: 1 },
  Parts: { PANEL_PART: 'workbench.parts.panel' }, ViewContainerLocation: { Panel: 1 }, WorkbenchPhase: { AfterRestored: 3 },
  IStorageService: decorator, INotificationService: decorator, IPaneCompositePartService: decorator,
  IWorkbenchLayoutService: decorator, IViewsService: decorator, registerWorkbenchContribution2: () => {} };
const WorkspacePanelState = new Function(...Object.keys(dependencies), `${js}\nreturn WorkspacePanelState;`)(...Object.values(dependencies)) as PanelConstructor;

type SavedPanel = { visible: boolean; id: string; size?: { width: number; height: number } };
type Snapshot = ReturnType<typeof workspaceContext.getSnapshot>;
const originalSnapshot = workspaceContext.getSnapshot();
const originalGlobals = new Map(['window', 'location', 'ResizeObserver'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
const owners: PanelOwner[] = [];

beforeEach(() => {
  Object.defineProperties(globalThis, {
    window: { configurable: true, value: globalThis },
    location: { configurable: true, value: { search: '?mcp_workspace=frozen-target' } },
    ResizeObserver: { configurable: true, value: class { observe() {} disconnect() {} } },
  });
});
afterEach(() => {
  owners.splice(0).forEach(owner => owner.dispose());
  workspaceContext.publish(originalSnapshot);
  for (const [key, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else Reflect.deleteProperty(globalThis, key);
  }
});

function setup(options: { saved?: Record<string, SavedPanel>; snapshot?: Partial<Snapshot>; restore?: Promise<void> } = {}) {
  const steps: string[] = [];
  let visible = false;
  let active = 'boxteam.workspace.output';
  let snapshot = { ...originalSnapshot, loading: false, error: null, activeId: 'active-route', ...options.snapshot };
  workspaceContext.publish(snapshot);
  const started = Promise.withResolvers<void>();
  const openPaneComposite = mock(async (id: string) => {
    steps.push(`restore:${id}`);
    started.resolve();
    await options.restore;
    active = id;
    return undefined;
  });
  const setPartHidden = mock((hidden: boolean) => { visible = !hidden; steps.push(`hidden:${hidden}`); });
  const setSize = mock(() => { steps.push('size'); });
  const openView = mock(async (id: string, focus: boolean) => {
    steps.push(`view:${id}:${focus}`);
    setPartHidden(false);
    return null;
  });
  const errors = mock((_message: string) => {});
  const store = mock((_key: string, _state: SavedPanel) => {});
  const owner = new WorkspacePanelState({
    openPaneComposite,
    getActivePaneComposite: () => ({ getId: () => active }),
    getLastActivePaneCompositeId: () => active,
  } as unknown as IPaneCompositePartService, {
    getContainer: () => ({}), isVisible: () => visible,
    getSize: () => ({ width: 500, height: 240 }), setSize, setPartHidden,
  } as unknown as IWorkbenchLayoutService, {
    onWillSaveState: () => ({ dispose() {} }),
    getObject: (key: string) => options.saved?.[key.replace('boxteam.workspacePanel.', '')],
    store,
  } as unknown as IStorageService, { error: errors } as unknown as INotificationService,
  { openView } as unknown as IViewsService);
  owners.push(owner);
  return {
    owner, steps, openPaneComposite, openView, errors, store, setPartHidden, setSize, started: started.promise,
    get visible() { return visible; },
    // 只等待原 owner 已有的恢复队列，不向产品增加测试等待接口。
    settle: () => (owner as unknown as { restoring: Promise<void> }).restoring,
    publish(next: Partial<Snapshot>) { snapshot = { ...snapshot, ...next }; workspaceContext.publish(snapshot); },
  };
}

test('MCP 深链接等待原 closed 面板异步恢复和尺寸应用后再展开，后续普通状态不重开', async () => {
  const restore = Promise.withResolvers<void>();
  const fixture = setup({ restore: restore.promise, saved: {
    'active-route': { visible: false, id: 'boxteam.workspace.output', size: { width: 800, height: 300 } },
  } });
  await fixture.started;
  expect(fixture.openView).not.toHaveBeenCalled();
  restore.resolve();
  await fixture.settle();
  expect(fixture.steps).toEqual(['restore:boxteam.workspace.output', 'size', 'hidden:true', 'view:boxteam.mcp.view:true', 'hidden:false']);
  expect(fixture.visible).toBe(true);
  fixture.setPartHidden(true);
  fixture.publish({});
  await fixture.settle();
  expect(fixture.openView).toHaveBeenCalledTimes(1);
  expect(fixture.visible).toBe(false);
  expect(fixture.errors).not.toHaveBeenCalled();
});

test('普通入口沿用已存 closed 状态，不打开 MCP 视图', async () => {
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { search: '' } });
  const fixture = setup({ saved: { 'active-route': { visible: false, id: 'boxteam.workspace.output' } } });
  await fixture.settle();
  expect(fixture.steps).toEqual(['restore:boxteam.workspace.output', 'hidden:true']);
  expect(fixture.openView).not.toHaveBeenCalled();
  expect(fixture.visible).toBe(false);
});

test.each([{ loading: false, error: 'Gateway 初始化失败' }, { loading: true, error: 'Gateway 初始化失败' }, { loading: false, error: null }])(
  '深链接在无活动路由且初始化已结算 %j 时展示原视图，之后取得路由恢复 closed 仍展开', async state => {
    const fixture = setup({ snapshot: { activeId: null, ...state }, saved: {
      'recovered-route': { visible: false, id: 'boxteam.workspace.output' },
    } });
    await fixture.settle();
    expect(fixture.openPaneComposite).not.toHaveBeenCalled();
    expect(fixture.openView).toHaveBeenCalledWith('boxteam.mcp.view', true);
    expect(fixture.visible).toBe(true);
    fixture.publish({ loading: false, error: null, activeId: 'recovered-route' });
    await fixture.settle();
    expect(fixture.steps.slice(-3)).toEqual(['hidden:true', 'view:boxteam.mcp.view:true', 'hidden:false']);
    expect(fixture.openView).toHaveBeenCalledTimes(2);
    expect(fixture.visible).toBe(true);
  },
);

test('未结算初始化不提前打开，冻结的深链接目标不要求等于全局活动路由', async () => {
  const fixture = setup({ snapshot: { loading: true, activeId: null } });
  await fixture.settle();
  expect(fixture.openView).not.toHaveBeenCalled();
  fixture.publish({ loading: false, activeId: 'different-global-route' });
  await fixture.settle();
  expect(fixture.openView).toHaveBeenCalledTimes(1);
  expect(fixture.visible).toBe(true);
});

test('迟到旧路由恢复不越过新路由，深链接由最后有效恢复展开一次', async () => {
  const restore = Promise.withResolvers<void>();
  const fixture = setup({ restore: restore.promise, saved: {
    'active-route': { visible: false, id: 'boxteam.workspace.output' },
    'next-route': { visible: false, id: 'boxteam.workspace.terminal' },
  } });
  await fixture.started;
  fixture.publish({ activeId: 'next-route' });
  restore.resolve();
  await fixture.settle();
  expect(fixture.steps).toEqual(['restore:boxteam.workspace.output', 'restore:boxteam.workspace.terminal', 'hidden:true', 'view:boxteam.mcp.view:true', 'hidden:false']);
  expect(fixture.openView).toHaveBeenCalledTimes(1);
  expect(fixture.visible).toBe(true);
});

test.each(['active-route', 'next-route'])('恢复中遇到错误使旧回执失效，再取得 %s 时重走原恢复后展开', async recovered => {
  const restore = Promise.withResolvers<void>();
  const fixture = setup({ restore: restore.promise, saved: {
    'active-route': { visible: false, id: 'boxteam.workspace.output', size: { width: 800, height: 300 } },
    'next-route': { visible: false, id: 'boxteam.workspace.terminal' },
  } });
  await fixture.started;
  fixture.publish({ error: '恢复期间 Gateway 读取失败' });
  restore.resolve();
  await fixture.settle();
  expect(fixture.steps).toEqual(['restore:boxteam.workspace.output', 'view:boxteam.mcp.view:true', 'hidden:false']);
  expect(fixture.setSize).not.toHaveBeenCalled();
  fixture.publish({ error: null, activeId: recovered });
  await fixture.settle();
  expect(fixture.openPaneComposite).toHaveBeenCalledTimes(2);
  expect(fixture.openView).toHaveBeenCalledTimes(2);
  expect(fixture.steps.slice(-3)).toEqual(['hidden:true', 'view:boxteam.mcp.view:true', 'hidden:false']);
  expect(fixture.visible).toBe(true);
});

test('释放 owner 后迟到恢复不再展开深链接', async () => {
  const restore = Promise.withResolvers<void>();
  const fixture = setup({ restore: restore.promise, saved: { 'active-route': { visible: false, id: 'boxteam.workspace.output' } } });
  await fixture.started;
  fixture.owner.dispose();
  restore.resolve();
  await fixture.settle();
  expect(fixture.openView).not.toHaveBeenCalled();
  expect(fixture.setPartHidden).not.toHaveBeenCalled();
});

// 执行正式构建使用的原 Layout 方法；焦点必须在 Grid 隐藏 DOM 前读取。
const workbenchRoot = resolve(process.cwd(), 'src/clients/workbench');
const layoutPath = resolve(workbenchRoot, 'upstream/src/vs/workbench/browser/layout.ts');
const layoutSource = await sourceTransform(workbenchRoot, { commit: pinnedUpstreamCommit }, [])(layoutPath, await readFile(layoutPath, 'utf8'));
const layoutAst = ts.createSourceFile(layoutPath, layoutSource, ts.ScriptTarget.Latest, true);
const layoutMethod = layoutAst.statements.filter(ts.isClassDeclaration).flatMap(node => node.members)
  .find(node => ts.isMethodDeclaration(node) && node.name.getText(layoutAst) === 'setPanelHidden');
if (!layoutMethod) throw new Error('正式转换后的 Layout 缺少 setPanelHidden');
const layoutJs = ts.transpileModule(`class PanelLayout { ${layoutMethod.getText(layoutAst)} }`, {
  compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None },
}).outputText;

test.each([
  { origin: 'Quick Input', panelFocus: false, ios: false, auxiliaryMaximized: false, focused: 0 },
  { origin: '底部面板', panelFocus: true, ios: false, auxiliaryMaximized: false, focused: 1 },
  { origin: '最大化底部面板', panelFocus: true, panelMaximized: true, ios: false, auxiliaryMaximized: false, focused: 1 },
  { origin: '最大化面板外的 Quick Input', panelFocus: false, panelMaximized: true, ios: false, auxiliaryMaximized: false, focused: 0 },
  { origin: 'iOS 底部面板', panelFocus: true, ios: true, auxiliaryMaximized: false, focused: 0 },
  { origin: '最大化右侧区域', panelFocus: true, ios: false, auxiliaryMaximized: true, focused: 0 },
])('原生隐藏面板仅交回面板自身焦点：$origin', ({ panelFocus, panelMaximized = false, ios, auxiliaryMaximized, focused }) => {
  const parts = { PANEL_PART: 'panel' };
  const keys = { PANEL_HIDDEN: 'hidden', PANEL_WAS_LAST_MAXIMIZED: 'maximized' };
  const hide = new Function('Parts', 'LayoutStateKeys', 'LayoutClasses', 'ViewContainerLocation', 'isIOS',
    `${layoutJs}\nreturn PanelLayout.prototype.setPanelHidden;`)(parts, keys, { PANEL_HIDDEN: 'hidden' }, { Panel: 1 }, ios) as (hidden: boolean) => void;
  let hasFocus = panelFocus;
  let visible = true;
  const focus = mock(() => {});
  const hideActive = mock(() => {});
  const runtime = new Map<string, boolean>();
  const layout = {
    workbenchGrid: { setViewVisible: (_view: object, shown: boolean) => { visible = shown; hasFocus = false; } },
    panelPartView: {},
    hasFocus: () => hasFocus,
    isVisible: () => visible,
    isPanelMaximized: () => panelMaximized,
    toggleMaximizedPanel: () => { hasFocus = false; },
    isAuxiliaryBarMaximized: () => auxiliaryMaximized,
    stateModel: { setRuntimeValue: (key: string, value: boolean) => runtime.set(key, value) },
    panelOpensMaximized: () => false,
    mainContainer: { classList: { add() {}, remove() {} } },
    paneCompositeService: { getActivePaneComposite: () => ({}), hideActivePaneComposite: hideActive },
    editorGroupService: { mainPart: { activeGroup: { focus } } },
  };
  hide.call(layout, true);
  expect(visible).toBe(false);
  expect(runtime.get('hidden')).toBe(true);
  expect(hideActive).toHaveBeenCalledTimes(1);
  expect(focus).toHaveBeenCalledTimes(focused);
});
