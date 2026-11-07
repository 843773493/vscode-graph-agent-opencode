import { afterEach, expect, spyOn, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import { Disposable, MutableDisposable, toDisposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../upstream/src/vs/base/common/event.js';
import { observableValue } from '../upstream/src/vs/base/common/observable.js';
import { CancellationError } from '../upstream/src/vs/base/common/errors.js';
import { CancellationTokenSource } from '../upstream/src/vs/base/common/cancellation.js';
import * as api from '../../web/src/api/mcpConnections.js';
import { requireGatewayWorkspaceRequestTarget } from '../../web/src/api/workspaceIdentity.js';
import { WorkspaceMcpCatalog, mcpCatalogKey, pendingMcpOperation } from '../src/mcp/catalog.js';
import { WorkspaceMcpServer } from '../src/mcp/server.js';

const source = ts.createSourceFile('service.ts', await readFile('src/clients/workbench/src/mcp/service.ts', 'utf8'), ts.ScriptTarget.Latest, true);
const declaration = source.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'WorkspaceMcpService');
if (!declaration) throw new Error('原 MCP owner 声明缺失');
// 执行完整生产类，只隔离 Workbench DI、计时器与原 editor 宿主。
const javascript = ts.transpileModule(declaration.getText().replace('export class ', 'class ').replace(/@(IEditorService|INotificationService|IQuickInputService)\s+/g, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None, useDefineForClassFields: false },
}).outputText;
const local = { gateway_id: 'gateway-local', workspace_id: 'workspace-local', backend_id: 'backend-local', name: '本地工作区' };
const remote = { gateway_id: 'gateway-remote', workspace_id: 'workspace-remote', backend_id: 'backend-remote', name: '远端工作区' };
const operation: api.McpConnectionOperationDTO = { operation_id: 'original-operation', connection_id: 'runtime-connection', server_id: 'secure',
  action: 'connect', status: 'waiting_user', stage: 'authorize', created_at: 1, updated_at: 1, deadline: 300 };
const catalog = (target: { gateway_id: string; workspace_id: string }) => ({ code: 0, message: 'ok', request_id: 'request', protocol_version: 1,
  target: { kind: 'workspace', gateway_id: target.gateway_id, workspace_id: target.workspace_id }, catalog_revision: 'sha256:' + 'a'.repeat(64), catalog_generation: 1,
  data: [{ server_id: 'secure', transport: 'stdio', enabled: true, status: 'authorization_required', tools: [] }] });
const cleanups: (() => void)[] = [];
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup(); });

function prepare(search = '', connections = ['original-route']) {
  let snapshot = { gatewayId: local.gateway_id, activeId: JSON.stringify([remote.gateway_id, remote.workspace_id]), loading: false, error: null,
    workspaces: [local, { ...local, workspace_id: 'local-second' }, remote].map(workspace => ({ ...workspace, status: 'ready' })) };
  let selection = { gateway_id: local.gateway_id, name: '本地', connection_kind: 'local', gateway_connection_id: undefined as string | undefined };
  const listeners = new Set<() => void>(), changed = new Emitter<void>();
  let invalidate: () => void = () => {}, timer: () => void = () => {};
  const opened: WorkspaceMcpServer[] = [], errors: unknown[] = [];
  const read = spyOn(api, 'getMcpCatalog').mockImplementation(async (_port, target) => catalog(target));
  const operations = spyOn(api, 'listMcpOperations').mockResolvedValue([operation]);
  const submit = spyOn(api, 'submitMcpOperation').mockResolvedValue(operation);
  cleanups.push(() => read.mockRestore(), () => operations.mockRestore(), () => submit.mockRestore());
  const context = { getSnapshot: () => snapshot, refresh: async () => {}, subscribe: (listener: () => void) => {
    listeners.add(listener); return () => { listeners.delete(listener); };
  } };
  const dependencies = { Disposable, Emitter, observableValue, LazyCollectionState: { AllKnown: 1 }, ContributionEnablementState: { DisabledWorkspace: 0 },
    workspaceContext: context, toDisposable, onDidChangeMcpManagementTarget: changed.event,
    onGatewayUserSessionInvalidated: (_port: number, listener: () => void) => { invalidate = listener; return () => {}; },
    window: { setInterval: (callback: () => void) => { timer = callback; return 1; }, clearInterval() {} }, location: { search },
    WorkspaceMcpCatalog, mcpCatalogKey, WorkspaceMcpServer, managementGateway: () => ({ ...selection }), CancellationError,
    pendingMcpOperation, CancellationTokenSource, requireGatewayWorkspaceRequestTarget,
    gateway: { request: async () => ({ local_gateway_id: local.gateway_id,
      items: connections.map(gateway_connection_id => ({ gateway_id: remote.gateway_id, connection_kind: 'remote_gateway', gateway_connection_id })) }) },
    McpServerEditorInput: class { constructor(readonly mcpServer: WorkspaceMcpServer) {} } };
  const Subject = new Function(...Object.keys(dependencies), `${javascript}\nreturn WorkspaceMcpService;`)(...Object.values(dependencies));
  const owner = new Subject({ openEditor: async (input: { mcpServer: WorkspaceMcpServer }) => { opened.push(input.mcpServer); } },
    { error: (error: unknown) => { errors.push(error); } }, { pick: async () => { throw new Error('不应出现隐式对象选择'); } });
  cleanups.push(() => owner.dispose(), () => changed.dispose());
  return { owner, read, operations, submit, opened, errors, invalidate: () => invalidate(), tick: () => timer(),
    update: (change: Partial<typeof snapshot>) => { snapshot = { ...snapshot, ...change }; for (const listener of listeners) listener(); },
    select: (target: typeof selection) => { selection = { ...target }; changed.fire(); } };
}

test('普通MCP视图默认聚合本地Gateway，活动工作区焦点不改变原对象或安装选择', async () => {
  const fixture = prepare();
  const initial = await fixture.owner.queryLocal() as WorkspaceMcpServer[];
  expect(initial.map(server => server.catalog.target.workspace_id)).toEqual(['workspace-local', 'local-second']);
  expect(new Set(initial.map(server => server.id)).size).toBe(2);
  fixture.update({ activeId: 'another-focus' });
  expect(fixture.owner.local).toEqual(initial);
  expect(fixture.owner.installationTarget().target.gateway_id).toBe(local.gateway_id);
  fixture.update({ workspaces: [] });
  expect(await fixture.owner.queryLocal()).toEqual([]);
  expect(fixture.owner.installationTarget().target.gateway_id).toBe(local.gateway_id);
});

test('同Gateway的连接选择改变后，迟到查询不覆盖新视图，旧详情仍只操作原连接', async () => {
  const fixture = prepare();
  fixture.operations.mockResolvedValue([]);
  fixture.select({ gateway_id: remote.gateway_id, name: '远端', connection_kind: 'remote_gateway', gateway_connection_id: 'original-route' });
  const [original] = await fixture.owner.queryLocal() as WorkspaceMcpServer[];
  const delayed = Promise.withResolvers<ReturnType<typeof catalog>>();
  fixture.read.mockImplementationOnce(() => delayed.promise);
  const pending = fixture.owner.queryLocal().catch((error: unknown) => error);
  fixture.select({ gateway_id: remote.gateway_id, name: '远端', connection_kind: 'remote_gateway', gateway_connection_id: 'new-route' });
  const [current] = await fixture.owner.queryLocal() as WorkspaceMcpServer[];
  delayed.resolve(catalog(remote));
  expect(await pending).toBeInstanceOf(CancellationError);
  expect(fixture.owner.local).toEqual([current]); expect(original!.id).not.toBe(current!.id);
  await original!.start();
  expect(fixture.submit.mock.calls[0]![1]).toMatchObject({ gateway_id: remote.gateway_id, workspace_id: remote.workspace_id,
    backend_id: remote.backend_id, gateway_connection_id: 'original-route' });
  fixture.invalidate();
  expect(fixture.owner.local).toEqual([]);
  await expect(original!.start()).rejects.toThrow('不可发起连接');
});

test('授权深链独立打开原远端workspace对象并冻结连接，普通列表与管理目标仍属于本地Gateway', async () => {
  const selection = JSON.stringify([remote.gateway_id, remote.workspace_id]);
  const fixture = prepare(`?${new URLSearchParams({ mcp_workspace: selection })}`);
  await fixture.owner.queryLocal();
  await Promise.resolve(); await Promise.resolve();
  expect(fixture.opened).toHaveLength(1);
  expect(fixture.opened[0]!.catalog.target).toMatchObject({ gateway_id: remote.gateway_id, workspace_id: remote.workspace_id,
    backend_id: remote.backend_id, gateway_connection_id: 'original-route' });
  expect(fixture.owner.local.every((server: WorkspaceMcpServer) => server.catalog.target.gateway_id === local.gateway_id)).toBe(true);
  expect(fixture.owner.installationTarget().target.gateway_id).toBe(local.gateway_id);
  expect(fixture.errors).toEqual([]);
});

test('授权深链多连接歧义明确拒绝，不自动选择第一条路由', async () => {
  const fixture = prepare(`?${new URLSearchParams({ mcp_workspace: JSON.stringify([remote.gateway_id, remote.workspace_id]) })}`, ['route-a', 'route-b']);
  await fixture.owner.queryLocal(); await Promise.resolve();
  expect(fixture.opened).toEqual([]);
  expect(String(fixture.errors[0])).toContain('多个或缺失');
  expect(fixture.owner.installationTarget().target.gateway_id).toBe(local.gateway_id);
});

test('授权深链显式connection只打开该连接，仍不改变管理选择', async () => {
  const fixture = prepare(`?${new URLSearchParams({ mcp_workspace: JSON.stringify([remote.gateway_id, remote.workspace_id]), gateway_connection_id: 'route-b' })}`, ['route-a', 'route-b']);
  await fixture.owner.queryLocal(); await Promise.resolve(); await Promise.resolve();
  expect(fixture.opened[0]!.catalog.target.gateway_connection_id).toBe('route-b');
  expect(fixture.owner.installationTarget().target.gateway_id).toBe(local.gateway_id);
  expect(fixture.errors).toEqual([]);
});

test('MCP标题选择与扩展及设置各自独立，多安装域必须明确选择且不访问工作区', async () => {
  const module = ts.createSourceFile('managementTarget.ts', await readFile('src/clients/workbench/src/managementTarget.ts', 'utf8'), ts.ScriptTarget.Latest, true);
  const executable = ts.transpileModule(module.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText()).join('\n').replace(/^export /gm, ''), {
    compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.None, useDefineForClassFields: false },
  }).outputText;
  let snapshot = { gatewayId: local.gateway_id, activeId: 'unrelated-focus', workspaces: [] };
  const listeners = new Set<() => void>();
  const first = { gateway_id: remote.gateway_id, name: '远端第一连接', connection_kind: 'remote_gateway', gateway_connection_id: 'route-a' },
    second = { ...first, name: '远端第二连接', gateway_connection_id: 'route-b' };
  const domains = ['backend-a', 'backend-b'].map(backend_id => ({ kind: 'backend', gateway_id: remote.gateway_id, backend_id }));
  const discovered: unknown[] = [], actions = new Map<string, { run(accessor: unknown): Promise<void> }>();
  const dependencies = { CancellationTokenSource, MutableDisposable, Emitter, Action2: class { constructor(readonly desc: { id: string }) {} },
    registerAction2: (Ctor: new () => { desc: { id: string }; run(accessor: unknown): Promise<void> }) => { const action = new Ctor(); actions.set(action.desc.id, action); },
    MenuId: { ViewTitle: 'view', ViewContainerTitle: 'container' }, MenuRegistry: { appendMenuItem: () => ({ dispose() {} }) },
    ContextKeyExpr: { equals: () => ({}), or: () => ({}) }, VIEWLET_ID: 'extensions', IQuickInputService: 'quick',
    onGatewayUserSessionInvalidated: () => () => {},
    workspaceContext: { getSnapshot: () => snapshot, subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; } },
    gateway: { request: async (path: string) => {
      expect(path).toBe('/api/gateway/management/gateways'); return { local_gateway_id: local.gateway_id, items: [first, second] };
    } }, listMcpInstallationTargets: async (_port: number, target: unknown) => { discovered.push(target); return domains; } };
  const owner = new Function(...Object.keys(dependencies), `${executable}\nreturn { managementGateway, mcpManagementTarget };`)(...Object.values(dependencies));
  expect(owner.managementGateway('mcp').gateway_id).toBe(local.gateway_id);
  await expect(owner.mcpManagementTarget(first, new AbortController().signal)).rejects.toThrow('多个安装域');
  const choices = [1, 1];
  await actions.get('boxteam.mcp.selectTarget')!.run({ get: () => ({ pick: async (items: unknown[]) => items[choices.shift()!] }) });
  expect(owner.managementGateway('mcp')).toEqual({ ...second, backend_id: 'backend-b' });
  expect(discovered.at(-1)).toEqual(second);
  snapshot = { ...snapshot, activeId: 'new-focus' }; for (const listener of listeners) listener();
  expect(owner.managementGateway('mcp')).toEqual({ ...second, backend_id: 'backend-b' });
  expect(owner.managementGateway('extensions').gateway_id).toBe(local.gateway_id);
  expect(owner.managementGateway('settings').gateway_id).toBe(local.gateway_id);
});
