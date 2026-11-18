import { afterEach, expect, mock, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import ts from 'typescript';
import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { workspaceContext } from '../src/workspaceContext.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget } from '../../web/src/api/workspaceIdentity.js';
import type { GatewayWorkspace } from '../../web/src/types/backend.js';
import { t } from '../../shared/core/i18n/index.js';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

// 执行两个完整产品模块，只隔离浏览器注册与 DI，不复制范围选择或恢复算法。
async function loadProduct(path: string, dependencies: Record<string, unknown>, returned: string) {
  const source = ts.createSourceFile(path, await readFile(path, 'utf8'), ts.ScriptTarget.Latest, true);
  const body = source.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getFullText(source)).join('\n');
  const js = ts.transpileModule(body.replace(/^export /gm, ''), { compilerOptions: {
    target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None, experimentalDecorators: true, useDefineForClassFields: false,
  } }).outputText;
  return new Function('exports', ...Object.keys(dependencies), `${js}\nreturn ${returned};`)({}, ...Object.values(dependencies));
}
const decorator = () => {};
const originalSnapshot = workspaceContext.getSnapshot();
const originalLocation = Object.getOwnPropertyDescriptor(globalThis, 'location');
const owners: { dispose(): void }[] = [];
afterEach(() => {
  owners.splice(0).forEach(owner => owner.dispose());
  workspaceContext.publish(originalSnapshot);
  if (originalLocation) Object.defineProperty(globalThis, 'location', originalLocation);
  else Reflect.deleteProperty(globalThis, 'location');
});

async function setup(search = '') {
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { search } });
  const workspaces = [
    { gateway_id: 'local', workspace_id: 'other', name: 'Other', connection_kind: 'local' },
    { gateway_id: 'local', workspace_id: 'home', name: 'Home', connection_kind: 'local', system_default: true },
    { gateway_id: 'remote', workspace_id: 'remote-workspace', name: 'Remote', connection_kind: 'remote_gateway',
      remote_gateways: [{ gateway_id: 'remote', gateway_connection_id: 'connection-1' }] },
  ].map(item => ({ remote_gateways: [], status: 'ready', ...item })) as GatewayWorkspace[];
  workspaceContext.publish({ ...originalSnapshot, gatewayId: 'local', workspaces, activeId: 'remote/remote-workspace', loading: false, error: null });
  const management = await loadProduct(resolve('src/clients/workbench/src/managementTarget.ts'), {
    Emitter, workspaceContext, t, Action2: class {}, registerAction2: () => {},
  }, '{ managementGateway, managementWorkspace, selectManagementTarget, onDidChangePanelManagementTarget }');
  const product = await loadProduct(resolve('src/clients/workbench/src/workspacePanelState.ts'), {
    ...management, Disposable, workspaceContext, t, gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget,
    StorageScope: { WORKSPACE: 1 }, StorageTarget: { MACHINE: 1 }, WorkbenchPhase: { BlockRestore: 2 },
    IStorageService: decorator, INotificationService: decorator, IViewsService: decorator, registerWorkbenchContribution2: () => {},
  }, '{ WorkspacePanelState, selectPanelWorkspace }');
  const saved = new Map<string, unknown>();
  const errors = mock(() => {}), openView = mock(async () => null);
  const createOwner = () => {
    const owner = new product.WorkspacePanelState({
      getObject: (key: string) => saved.get(key), store: (key: string, value: unknown) => saved.set(key, value), onWillSaveState: () => ({ dispose() {} }),
    }, { error: errors }, { openView });
    owners.push(owner);
    return owner;
  };
  return { ...management, ...product, saved, errors, openView, createOwner, workspaces,
    selected: () => management.managementWorkspace('panel', management.managementGateway('panel')) as GatewayWorkspace };
}

test('首次面板默认本地的默认工作区，无视已激活远端工作区', async () => {
  const fixture = await setup(); fixture.createOwner();
  expect(fixture.selected().workspace_id).toBe('home');
  expect(workspaceContext.getSnapshot().activeId).toBe('remote/remote-workspace');
  expect(fixture.openView).not.toHaveBeenCalled();
});

test('面板手动选工作区与导航激活互不联动，并写入原 Workbench 存储', async () => {
  const fixture = await setup(); fixture.createOwner();
  fixture.managementWorkspace('panel', fixture.managementGateway('panel'), 'other');
  expect(workspaceContext.getSnapshot().activeId).toBe('remote/remote-workspace');
  workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: null });
  expect(fixture.selected().workspace_id).toBe('other');
  expect(fixture.saved.get('boxteam.panel.selection.local')).toMatchObject({ target: { gateway_id: 'local' }, workspaceId: 'other' });
});

test('窗口重载恢复自己的目标和工作区，不恢复第二套尺寸或强制展开面板', async () => {
  const fixture = await setup();
  fixture.saved.set('boxteam.panel.selection.local', { target: { gateway_id: 'remote', gateway_connection_id: 'connection-1', connection_kind: 'remote_gateway', name: 'Remote' }, workspaceId: 'remote-workspace' });
  fixture.createOwner();
  expect(fixture.selected().workspace_id).toBe('remote-workspace');
  expect(fixture.managementGateway('panel').gateway_connection_id).toBe('connection-1');
  expect(fixture.openView).not.toHaveBeenCalled();
});

test('显式定位远端资源选择原 owner，不激活导航工作区', async () => {
  const fixture = await setup(); fixture.createOwner();
  workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: null });
  fixture.selectPanelWorkspace({ gatewayId: 'remote', workspaceId: 'remote-workspace' });
  expect(fixture.selected().workspace_id).toBe('remote-workspace');
  expect(fixture.managementGateway('panel').gateway_connection_id).toBe('connection-1');
  expect(workspaceContext.getSnapshot().activeId).toBeNull();
});

test('授权深链打开精确工作区的 MCP，后续激活变化不再展开', async () => {
  const fixture = await setup('?mcp_workspace=' + encodeURIComponent(JSON.stringify(['local', 'home']))); fixture.createOwner();
  expect(fixture.selected().workspace_id).toBe('home');
  expect(fixture.openView).toHaveBeenCalledWith('boxteam.mcp.view', true);
  workspaceContext.publish({ ...workspaceContext.getSnapshot(), activeId: null });
  expect(fixture.openView).toHaveBeenCalledTimes(1);
});

test('已选工作区消失时明确失败，不自动改投默认工作区', async () => {
  const fixture = await setup(); fixture.createOwner();
  fixture.managementWorkspace('panel', fixture.managementGateway('panel'), 'other');
  workspaceContext.publish({ ...workspaceContext.getSnapshot(), workspaces: fixture.workspaces.filter(item => item.workspace_id !== 'other') });
  expect(fixture.selected).toThrow();
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
