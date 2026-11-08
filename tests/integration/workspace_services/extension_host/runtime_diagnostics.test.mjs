import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/runtime_diagnostics.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'runtime-diagnostics.fixture.mjs'), checks = [], convertedSources = [];
const sourcePaths = [
  'tests/integration/workspace_services/extension_host/runtime_diagnostics.test.mjs',
  ...['src/extensions/runtimeDiagnostics.ts', 'src/extensions/commands.ts', 'src/extensions/contribution.ts',
    'scripts/sourceTransform.mjs', 'scripts/extensionHostStateTransform.mjs',
    'scripts/extensions/extensionOwnerTransform.mjs', 'scripts/extensions/extensionRunningTransform.mjs',
    'scripts/extensions/extensionInstanceTransform.mjs',
    'upstream/src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
    'upstream/src/vs/workbench/services/extensions/common/extensionDescriptionRegistry.ts',
    'upstream/src/vs/workbench/services/extensions/common/extensionRunningLocationTracker.ts',
    'upstream/src/vs/workbench/services/extensions/common/lazyCreateExtensionHostManager.ts',
    'upstream/src/vs/workbench/services/extensions/common/extensions.ts',
    'upstream/src/vs/workbench/services/commands/common/commandService.ts',
  ].map(path => 'src/clients/workbench/' + path),
];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path =>
  [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint();
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
await writeFile(join(run.artifactsDir, 'test-source.mjs'), await readFile(import.meta.path, 'utf8'));
const built = await build({ stdin: { resolveDir: resolve('.'), contents: `
export { AbstractExtensionService } from '${upstream}workbench/services/extensions/common/abstractExtensionService.js';
export { LazyCreateExtensionHostManager } from '${upstream}workbench/services/extensions/common/lazyCreateExtensionHostManager.js';
export { ExtensionHostManager } from '${upstream}workbench/services/extensions/common/extensionHostManager.js';
export { RemoteRunningLocation, LocalWebWorkerRunningLocation } from '${upstream}workbench/services/extensions/common/extensionRunningLocation.js';
export { ExtensionHostExtensions, IExtensionService } from '${upstream}workbench/services/extensions/common/extensions.js';
export { CommandService } from '${upstream}workbench/services/commands/common/commandService.js';
export { CommandsRegistry, ICommandService } from '${upstream}platform/commands/common/commands.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { SyncDescriptor } from '${upstream}platform/instantiation/common/descriptors.js';
export { IProductService } from '${upstream}platform/product/common/productService.js';
export { IQuickInputService } from '${upstream}platform/quickinput/common/quickInput.js';
export { IEditorService } from '${upstream}workbench/services/editor/common/editorService.js';
export { NullLogService, ILogService } from '${upstream}platform/log/common/log.js';
export { ExtensionIdentifier, extensionInstanceKey } from '${upstream}platform/extensions/common/extensions.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { URI } from '${upstream}base/common/uri.js';
export { extensionRuntimeDiagnostics, extensionRuntimeDiagnosticsCommand } from './src/clients/workbench/src/extensions/runtimeDiagnostics.js';
export { executeHostCommand, registerTargetCommand } from './src/clients/workbench/src/extensions/commands.js';
import './src/clients/workbench/src/extensions/contribution.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024', metafile: true,
  plugins: [{ name: 'explicit-network-and-window-timer-boundaries', setup(builder) {
    // 网络只提供禁止派发的具名边界；不替换扩展 owner、Registry、Tracker 或命令服务。
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({ contents: `
      export const gateway = new Proxy({}, { get(_target, name) { return (...args) => {
        globalThis.__runtimeDiagnosticsBoundary.network.push({ name: String(name), args });
        throw new Error('能力查询不应访问 Gateway：' + String(name));
      }; } });
      export class GatewayRequestError extends Error {}
    `, loader: 'ts' }));
    // 本组没有 DOM，也不验窗口性能服务；只保留该原服务的 DI 标识。
    builder.onLoad({ filter: /\/services\/timer\/browser\/timerService\.ts$/ }, args => ({
      contents: "import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js'; export const ITimerService = createDecorator('timerService');",
      loader: 'ts', resolveDir: dirname(args.path),
    }));
  } }, buildSourcePlugin(root, async (path, source) => {
    const converted = await transform(path, source);
    if (sourcePaths.includes(path.slice(resolve('.').length + 1))) {
      convertedSources.push({ path: path.slice(resolve('.').length + 1),
        original: createHash('sha256').update(source).digest('hex'), converted: createHash('sha256').update(converted).digest('hex') });
      await writeFile(join(run.artifactsDir, path.slice(root.length + 1).replaceAll('/', '__')), converted);
    }
    return converted;
  })], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile));
const m = await import(pathToFileURL(bundle).href);
const targetA = { kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' };
const targetB = { kind: 'backend', gatewayId: 'gateway-b', backendId: '22222222-2222-4222-8222-222222222222' };
const targetC = { kind: 'backend', gatewayId: 'gateway-c', backendId: '33333333-3333-4333-8333-333333333333' };
const product = { version: '1.105.0-test', commit: pinnedUpstreamCommit };
const unsupported = ['terminal', 'tasks', 'debug', 'secrets', 'stateSync'];
const partial = ['commands', 'documentsLanguageSearch', 'workspaceFiles', 'configuration', 'viewsWebviewsPorts', 'memento', 'dependenciesNativeModulesProposedApi'];

function description(target, name, version, entry) {
  return { identifier: new m.ExtensionIdentifier(name), name, publisher: 'probe', version, engines: { vscode: '*' },
    extensionLocation: m.URI.file('/extensions/' + target.gatewayId + '/' + name), installationTarget: target,
    isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined',
    ...entry };
}

async function fixture({ ordinaryActivation = false, registered = true } = {}) {
  const effects = { starts: [], activation: [], activationRequests: [], rpcActivations: [], registrationReads: 0,
    network: [], managementChanges: [], editorCreations: 0, pickerCreations: 0 };
  globalThis.__runtimeDiagnosticsBoundary = effects;
  const management = { onDidChangeProfile: m.Event.None, onDidEnableExtensions: m.Event.None,
    onDidInstallExtensions: m.Event.None, onDidUninstallExtension: m.Event.None, selectedTarget: targetB,
    selectTarget(target) { effects.managementChanges.push(target); this.selectedTarget = target; } };
  const services = {
    proposedApi: {}, hostFactory: { createExtensionHost() { throw new Error('查询不能创建宿主'); } },
    hostKindPicker: {}, ownerInstantiation: {}, notifications: { error(error) { throw new Error(String(error)); } },
    environment: { isBuilt: false, isExtensionDevelopment: false }, telemetry: { publicLog2() {} },
    enablement: { onEnablementChanged: m.Event.None }, files: { onWillActivateFileSystemProvider: m.Event.None },
    management, workspace: {}, configuration: {}, manifest: {}, logs: new m.NullLogService(),
    remoteAgent: {}, remoteScanner: {}, lifecycle: { onWillShutdown: m.Event.None }, remoteResolver: {}, dialogs: {},
  };
  const owner = new m.AbstractExtensionService({ hasLocalProcess: false, allowRemoteExtensionsInLocalWebWorker: false },
    services.proposedApi, services.hostFactory, services.hostKindPicker, services.ownerInstantiation, services.notifications,
    services.environment, services.telemetry, services.enablement, services.files, product, services.management,
    services.workspace, services.configuration, services.manifest, services.logs, services.remoteAgent,
    services.remoteScanner, services.lifecycle, services.remoteResolver, services.dialogs);
  const a = description(targetA, 'probe.shared', '1.0.0', { main: './node.js', extensionDependencies: ['probe.dependency'] });
  const web = description(targetA, 'probe.dependency', '2.0.0', { browser: './web.js',
    activationEvents: ordinaryActivation ? ['onCommand:probe.dependencyCommand'] : [] });
  const b = description(targetB, 'probe.shared', '9.0.0', { main: './node.js' });
  const views = [
    { installationTarget: targetA, viewId: 'view-a', workspaceIds: ['a2', 'a1'], capabilities: { installation_manage: true, host_manage: true },
      nodeRuntime: { channel_state: 'available', platform: 'linux', runtime: { commit: pinnedUpstreamCommit, vscode_version: product.version, node_version: '24.0.0' } } },
    { installationTarget: targetB, viewId: 'view-b', workspaceIds: ['b1'], capabilities: { installation_manage: false, host_manage: true },
      nodeRuntime: { channel_state: 'available', platform: 'windows', runtime: { commit: pinnedUpstreamCommit, vscode_version: product.version, node_version: '24.0.0' } } },
    { installationTarget: targetC, viewId: 'installation-only', workspaceIds: [], capabilities: { installation_manage: true, host_manage: false } },
  ];
  owner._setExtensionWorkspaceViews(views);
  const lock = await owner._registry.acquireLock('runtime diagnostics fixture descriptions');
  try { owner._registry.deltaExtensions(lock, [a, web, b], []); } finally { lock.dispose(); }
  const cleanup = new m.DisposableStore();
  cleanup.add(owner.onWillActivateByEvent(event => effects.activation.push(event.event)));
  // 只观察原方法，不替换其注册等待、激活或状态逻辑。
  const whenRegistered = owner.whenInstalledExtensionsRegistered.bind(owner), activateByEvent = owner.activateByEvent.bind(owner);
  owner.whenInstalledExtensionsRegistered = (...args) => { effects.registrationReads++; return whenRegistered(...args); };
  owner.activateByEvent = (...args) => {
    effects.activationRequests.push({ event: args[0], host: args[2] });
    return activateByEvent(...args);
  };
  const makeHost = (location, all, mine, generation, activationRpc = false) => {
    const exit = cleanup.add(new m.Emitter());
    const host = { runningLocation: location, startup: 0, extensions: new m.ExtensionHostExtensions(1, all, mine.map(value => value.identifier)),
      onExit: exit.event, targetContext: undefined,
      async start() {
        effects.starts.push(generation);
        if (!activationRpc) throw new Error('能力查询不能启动宿主');
        return {};
      },
      async disconnect() {}, dispose() {} };
    const internal = owner._acquireInternalAPI(host), subscriptions = new m.DisposableStore();
    subscriptions.add(internal);
    // 普通命令组运行原 eager Manager；仅它的进程／RPC 客户端是具名接收边界。
    class ActivationRpcBoundaryManager extends m.ExtensionHostManager {
      _createExtensionHostCustomers() { return { activateByEvent: async event => effects.rpcActivations.push({ generation, event }) }; }
    }
    const manager = activationRpc
      ? new ActivationRpcBoundaryManager(host, [], internal, {}, services.environment, services.telemetry, services.logs)
      : new m.LazyCreateExtensionHostManager(host, [], internal, {}, services.logs);
    owner._extensionHostManagers.add(manager, subscriptions);
    for (const extension of mine) owner._runningLocations.set(extension, location);
    return { host, manager, internal, mine, generation };
  };
  const nodeA = makeHost(new m.RemoteRunningLocation(targetA, 'view-a', ['a2', 'a1']), [a, web], [a], 'node-a-1');
  const webA = makeHost(new m.LocalWebWorkerRunningLocation(0, targetA, 'view-a', ['a2', 'a1']), [a, web], [web], 'web-a-1', ordinaryActivation);
  const nodeB = makeHost(new m.RemoteRunningLocation(targetB, 'view-b', ['b1']), [b], [b], 'node-b-1');
  const bind = entry => {
    const location = entry.host.runningLocation, kind = location.kind === nodeA.host.runningLocation.kind ? 'node' : 'web';
    // 认证／IPC 是具名边界；诊断读取原 host.targetContext，不从 channel 或测试状态表推导绑定。
    const connection = { protocol_version: 1, gateway_id: location.installationTarget.gatewayId,
      backend_id: location.installationTarget.backendId, view_id: location.viewId, workspace_ids: [...location.workspaceIds],
      client_id: 'client-' + entry.generation, generation: entry.generation, request_id: 'request-' + entry.generation };
    entry.host.targetContext = { kind, connection };
    return connection;
  };
  bind(webA); bind(nodeB);
  if (registered) owner._installedExtensionsReady.open();
  if (ordinaryActivation) await webA.manager.ready();
  class EditorServiceBoundary { constructor() { effects.editorCreations++; this.activeEditor = undefined; } }
  class QuickInputBoundary { constructor() { effects.pickerCreations++; } async pick() { throw new Error('能力查询不能选择管理或运行目标'); } }
  const instantiation = new m.InstantiationService(new m.ServiceCollection(
    [m.ICommandService, new m.SyncDescriptor(m.CommandService)], [m.IExtensionService, owner],
    [m.IProductService, product], [m.ILogService, services.logs],
    [m.IEditorService, new m.SyncDescriptor(EditorServiceBoundary)], [m.IQuickInputService, new m.SyncDescriptor(QuickInputBoundary)],
  ), true);
  const commands = instantiation.invokeFunction(accessor => accessor.get(m.ICommandService));
  await Promise.resolve();
  // 构造器原有的注册订阅及具名 eager 进程准备不算查询副作用。
  effects.registrationReads = 0; effects.starts.length = 0;
  const originalFetch = globalThis.fetch, OriginalWebSocket = globalThis.WebSocket;
  globalThis.fetch = (...args) => { effects.network.push({ name: 'fetch', args }); throw new Error('能力查询不能访问网络'); };
  globalThis.WebSocket = class { constructor(...args) { effects.network.push({ name: 'WebSocket', args }); throw new Error('能力查询不能建立通道'); } };
  return { owner, commands, effects, views, management, nodeA, webA, nodeB, a, web, b, bind, makeHost,
    async close() {
      globalThis.fetch = originalFetch; globalThis.WebSocket = OriginalWebSocket;
      owner._installedExtensionsReady.open();
      try { await owner._extensionHostManagers.stopAllInReverse(); }
      finally { instantiation.dispose(); owner.dispose(); cleanup.dispose(); delete globalThis.__runtimeDiagnosticsBoundary; }
    } };
}

async function scenario(name, body, options = {}) {
  const f = await fixture(options);
  try {
    await body(f);
    expect(f.effects.starts).toEqual([]);
    if (options.ordinaryActivation) {
      expect(f.effects.registrationReads).toBeGreaterThan(0);
      expect(f.effects.activationRequests).toEqual([{ event: 'onCommand:probe.dependencyCommand', host: f.webA.host }]);
      expect(f.effects.activation).toEqual(['onCommand:probe.dependencyCommand']);
      expect(f.effects.rpcActivations).toEqual([{ generation: 'web-a-1', event: 'onCommand:probe.dependencyCommand' }]);
    } else {
      expect(f.effects.registrationReads).toBe(0);
      expect(f.effects.activationRequests).toEqual([]);
      expect(f.effects.activation).toEqual([]);
      expect(f.effects.rpcActivations).toEqual([]);
    }
    expect(f.effects.network).toEqual([]);
    expect(f.effects.managementChanges).toEqual([]);
    expect(f.management.selectedTarget).toBe(targetB);
    checks.push(name);
  } finally { await f.close(); }
}

test('窗口命令从原 owner 查询多目标／有序 view，通道可用不升级 API 支持', async () => scenario('原窗口命令、管理授权／通道／实现支持分离', async f => {
  expect(m.CommandsRegistry.getCommand(m.extensionRuntimeDiagnosticsCommand)).toBeDefined();
  const result = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  expect(result.scope).toBe('window');
  expect({ version: result.version, commit: result.commit }).toEqual(product);
  expect(result.targets.map(target => [target.installationTarget, target.viewId, target.workspaceIds])).toEqual(
    f.views.map(view => [view.installationTarget, view.viewId, view.workspaceIds]));
  expect(result.targets.map(target => target.hosts.length)).toEqual([2, 1, 0]);
  expect(result.targets[0].management).toEqual({ installation_manage: true, host_manage: true });
  expect(result.targets[1].management).toEqual({ installation_manage: false, host_manage: true });
  expect(result.targets[0].nodeChannel.state).toBe('available');
  expect(result.targets[2].nodeChannel).toBeNull();
  expect(result.apiSupport.map(entry => [entry.id, entry.state]).sort()).toEqual([
    ...unsupported.map(id => [id, 'unsupported']), ...partial.map(id => [id, 'partial']),
  ].sort());
  expect(result.apiSupport.every(entry => entry.detail.length > 0)).toBe(true);
  expect(result.targets.every(target => !Object.hasOwn(target, 'apiSupport'))).toBe(true);
  expect(f.effects.editorCreations).toBe(0); expect(f.effects.pickerCreations).toBe(0);
}));

test('宿主绑定与激活分别由原 context 和 owner 回调产生，不把 created 当目标进程可用', async () => scenario('原绑定、激活及运行错误来源分开', async f => {
  let result = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  const created = result.targets[0].hosts[0], worker = result.targets[0].hosts[1];
  expect(created).toMatchObject({ kind: 'node', phase: 'created', binding: null, processApi: { state: 'unavailable' },
    extensions: [{ id: 'probe.shared', version: '1.0.0', activationStarted: false, activated: false, runtimeErrors: [] }] });
  expect(worker).toMatchObject({ kind: 'web', phase: 'bound', processApi: { state: 'unsupported' },
    binding: { clientId: 'client-web-a-1', generation: 'web-a-1', requestId: 'request-web-a-1' } });
  const binding = f.bind(f.nodeA);
  result = await m.executeHostCommand(f.commands, f.nodeA.host, m.extensionRuntimeDiagnosticsCommand, []);
  expect(result.targets[0].hosts[0]).toMatchObject({ phase: 'bound', processApi: { state: 'partial' },
    binding: { clientId: binding.client_id, generation: binding.generation, requestId: binding.request_id },
    extensions: [{ activationStarted: false, activated: false }] });
  f.nodeA.internal._onWillActivateExtension(f.a.identifier);
  result = m.extensionRuntimeDiagnostics(f.owner, product, f.nodeA.host);
  expect(result.targets[0].hosts[0].extensions[0]).toMatchObject({ activationStarted: true, activated: false });
  f.nodeA.internal._onDidActivateExtension(f.a.identifier, 1, 2, 3,
    { startup: false, extensionId: f.a.identifier, activationEvent: 'fixture:completed' });
  f.nodeA.internal._onExtensionRuntimeError(f.a.identifier, new Error('only A runtime failure'));
  result = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  expect(result.targets[0].hosts[0].extensions[0]).toMatchObject({ activationStarted: true, activated: true, runtimeErrors: ['only A runtime failure'] });
  expect(result.targets[0].hosts[1].extensions[0]).toMatchObject({ activationStarted: false, activated: false, runtimeErrors: [] });
  expect(result.targets[1].hosts[0].extensions[0]).toMatchObject({ id: 'probe.shared', version: '9.0.0', activationStarted: false, activated: false, runtimeErrors: [] });
}));

test('真实 executeHostCommand 经原 CommandService 保留来源，只返回该代际且不泄露同 view peer', async () => scenario('原命令私有来源与宿主隔离', async f => {
  for (const entry of [f.nodeA, f.webA, f.nodeB]) {
    const result = await m.executeHostCommand(f.commands, entry.host, m.extensionRuntimeDiagnosticsCommand, []);
    expect(result.scope).toBe('caller'); expect(result.targets).toHaveLength(1);
    expect(result.targets[0].installationTarget).toEqual(entry.host.runningLocation.installationTarget);
    expect(result.targets[0].viewId).toBe(entry.host.runningLocation.viewId);
    expect(result.targets[0].hosts).toHaveLength(1);
    expect(result.targets[0].hosts[0].extensions.map(extension => [extension.id, extension.version])).toEqual(
      entry.mine.map(extension => [extension.identifier.value, extension.version]));
  }
  // 普通参数不能伪造私有来源包装；窗口调用仍完整显示原 owner 视图。
  const window = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand, { host: f.nodeA.host, args: [] });
  expect(window.scope).toBe('window'); expect(window.targets).toHaveLength(3);
  expect(f.effects.editorCreations).toBe(0); expect(f.effects.pickerCreations).toBe(0);
}));

test('core 查询在原注册屏障尚未打开时立即返回，不等待注册或发起星号激活', async () => scenario('未完成注册仍可只读查询', async f => {
  expect(f.owner._installedExtensionsReady.isOpen()).toBe(false);
  const window = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  const caller = await m.executeHostCommand(f.commands, f.nodeA.host, m.extensionRuntimeDiagnosticsCommand, []);
  expect(window.scope).toBe('window'); expect(window.targets).toHaveLength(3);
  expect(caller.scope).toBe('caller'); expect(caller.targets[0].hosts).toHaveLength(1);
  expect(f.owner._installedExtensionsReady.isOpen()).toBe(false);
  expect(f.effects.editorCreations).toBe(0); expect(f.effects.pickerCreations).toBe(0);
}, { registered: false }));

test('原 collection 关闭旧宿主后拒绝其查询，同位置替代代际不会接管旧来源', async () => scenario('原 collection 关闭与同位置换代', async f => {
  f.bind(f.nodeA);
  const old = await m.executeHostCommand(f.commands, f.nodeA.host, m.extensionRuntimeDiagnosticsCommand, []);
  expect(old.targets[0].hosts[0].binding.generation).toBe('node-a-1');
  await f.owner._extensionHostManagers.stopOne(f.nodeA.manager);
  const replacement = f.makeHost(f.nodeA.host.runningLocation, [f.a, f.web], [f.a], 'node-a-2');
  f.bind(replacement);
  await expect(m.executeHostCommand(f.commands, f.nodeA.host, m.extensionRuntimeDiagnosticsCommand, [])).rejects.toThrow('代际已经关闭');
  expect(() => m.extensionRuntimeDiagnostics(f.owner, product, f.nodeA.host)).toThrow('代际已经关闭');
  const current = await m.executeHostCommand(f.commands, replacement.host, m.extensionRuntimeDiagnosticsCommand, []);
  expect(current.targets[0].hosts).toHaveLength(1);
  expect(current.targets[0].hosts[0].binding.generation).toBe('node-a-2');
  expect(f.owner.getExtensionHosts()).not.toContain(f.nodeA.host);
  expect(f.owner.getExtensionHosts()).toContain(f.webA.host);
}));

test('查询输出全部可变对象与原 owner 隔离，后续查询反映真实 owner 新事件', async () => scenario('输出不回写 owner 且不缓存运行事实', async f => {
  f.bind(f.nodeA);
  const before = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  const result = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand), target = result.targets[0];
  result.apiSupport[0].state = 'supported'; result.apiSupport[0].detail = 'mutated'; result.apiSupport.push({ id: 'invented' });
  target.installationTarget.gatewayId = 'mutated'; target.workspaceIds.reverse(); target.management.host_manage = false;
  target.nodeChannel.state = 'unavailable'; target.nodeChannel.runtime.commit = 'mutated';
  target.hosts[0].binding.generation = 'mutated'; target.hosts[0].processApi.state = 'supported';
  target.hosts[0].extensions[0].version = 'mutated'; target.hosts[0].extensions[0].runtimeErrors.push('invented');
  target.hosts.push({}); result.targets.push({});
  expect(await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand)).toEqual(before);
  expect(f.owner.getExtensionWorkspaceViews()).toEqual(f.views);
  expect(f.nodeA.host.targetContext.connection.generation).toBe('node-a-1');
  expect(f.owner.getExtensionsStatus()[m.extensionInstanceKey(f.a)].runtimeErrors).toEqual([]);
  f.nodeA.internal._onExtensionRuntimeError(f.a.identifier, new Error('new owner event'));
  const fresh = await f.commands.executeCommand(m.extensionRuntimeDiagnosticsCommand);
  expect(fresh.targets[0].hosts[0].extensions[0].runtimeErrors).toEqual(['new owner event']);
  expect(before.targets[0].hosts[0].extensions[0].runtimeErrors).toEqual([]);
}));

test('普通目标命令仍按合法依赖可见集跨 host 派发，不受 core 诊断来源捷径影响', async () => scenario('普通命令合法依赖路由回归', async f => {
  const calls = [], registrations = [
    m.registerTargetCommand(f.owner, f.webA.host, 'probe.dependencyCommand', async (...args) => { calls.push({ host: 'web-a', args }); return 'dependency-A'; }),
    m.registerTargetCommand(f.owner, f.nodeB.host, 'probe.dependencyCommand', async (...args) => { calls.push({ host: 'node-b', args }); return 'foreign-B'; }),
  ];
  try {
    expect(f.nodeA.host.extensions.allExtensions).toContain(f.web);
    expect(f.nodeA.host.extensions.containsExtension(f.web.identifier)).toBe(false);
    expect(await m.executeHostCommand(f.commands, f.nodeA.host, 'probe.dependencyCommand', ['opaque', { value: 7 }])).toBe('dependency-A');
    expect(calls).toEqual([{ host: 'web-a', args: ['opaque', { value: 7 }] }]);
    const diagnostic = await m.executeHostCommand(f.commands, f.nodeA.host, m.extensionRuntimeDiagnosticsCommand, []);
    expect(diagnostic.targets[0].hosts).toHaveLength(1);
    expect(diagnostic.targets[0].hosts[0].kind).toBe('node');
    expect(f.effects.pickerCreations).toBe(0);
  } finally { registrations.forEach(registration => registration.dispose()); }
}, { ordinaryActivation: true }));

afterAll(async () => {
  try {
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, fingerprintsAfter: await fingerprint(), convertedSources, checks,
      boundary: '真实原 AbstractExtensionService／Lockable Registry／Tracker／LazyCreateExtensionHostManager／collection／内部激活回调、CommandService／DI／命令注册和 contribution 入口；认证 binding、进程／IPC、Gateway 网络、Editor／QuickInput 与未使用的 owner 服务为具名装具。没有真实 Node 进程、Gateway、完整 Workbench 或目标 API 业务执行，支持表断言只证明诊断如实表达既有拒绝／partial。' }, null, 2));
  } finally { await run.close(); }
});
