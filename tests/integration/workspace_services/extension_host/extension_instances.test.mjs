import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_instances.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'extension-instances.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const sourcePaths = ['tests/integration/workspace_services/extension_host/extension_instances.test.mjs',
  ...['src/extensions/inventory.ts', 'src/extensions/nativeInventory.ts', 'src/extensions/webConnection.ts', 'src/extensions/targetContext.ts',
    'src/extensions/commands.ts', 'src/extensions/contribution.ts', 'scripts/sourceTransform.mjs',
    'scripts/extensions/extensionOwnerTransform.mjs', 'scripts/extensions/extensionInstanceTransform.mjs',
    'scripts/extensions/extensionRunningTransform.mjs', 'scripts/extensions/extensionEnablementTransform.mjs',
    'upstream/src/vs/workbench/services/extensionManagement/browser/extensionEnablementService.ts',
    'upstream/src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
    'upstream/src/vs/workbench/services/extensions/common/extensionRunningLocationTracker.ts',
    'upstream/src/vs/workbench/services/extensions/common/extensionRunningLocation.ts'].map(path => 'src/clients/workbench/' + path)];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint();
const requests = [];
let request;
globalThis.__extensionInventoryBoundary = { request: (...args) => { requests.push(args); return request(...args); } };
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const built = await build({ stdin: { contents: `
export * from '${upstream}workbench/services/extensions/common/extensionDescriptionRegistry.js';
export { ExtensionRunningLocationTracker } from '${upstream}workbench/services/extensions/common/extensionRunningLocationTracker.js';
export { ExtensionHostExtensions, toExtension } from '${upstream}workbench/services/extensions/common/extensions.js';
export { RemoteRunningLocation, LocalWebWorkerRunningLocation } from '${upstream}workbench/services/extensions/common/extensionRunningLocation.js';
export { ExtensionIdentifier, extensionInstanceKey, installationTargetKey } from '${upstream}platform/extensions/common/extensions.js';
export { ImplicitActivationEvents } from '${upstream}platform/extensionManagement/common/implicitActivationEvents.js';
export { StorageManager, GlobalExtensionEnablementService, areSameEnablementIdentifiers } from '${upstream}platform/extensionManagement/common/extensionEnablementService.js';
export { ExtensionEnablementService } from '${upstream}workbench/services/extensionManagement/browser/extensionEnablementService.js';
export { checkEnabledAndProposedAPI } from '${upstream}workbench/services/extensions/common/abstractExtensionService.js';
export { EnablementState, ExtensionInstallLocation } from '${upstream}workbench/services/extensionManagement/common/extensionManagement.js';
export { MainThreadDocumentAndEditorStateComputer } from '${upstream}workbench/api/browser/mainThreadDocumentsAndEditors.js';
export { MainThreadDocuments } from '${upstream}workbench/api/browser/mainThreadDocuments.js';
export { MainThreadFileSystem } from '${upstream}workbench/api/browser/mainThreadFileSystem.js';
export { ExtHostFileSystemInfo } from '${upstream}workbench/api/common/extHostFileSystemInfo.js';
export { RPCProtocol, parseJsonAndRestoreBufferRefs } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
export { CommandService } from '${upstream}workbench/services/commands/common/commandService.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { SyncDescriptor } from '${upstream}platform/instantiation/common/descriptors.js';
export { IExtensionService } from '${upstream}workbench/services/extensions/common/extensions.js';
export { ICommandService } from '${upstream}platform/commands/common/commands.js';
export { ILogService, NullLogService } from '${upstream}platform/log/common/log.js';
export { IQuickInputService } from '${upstream}platform/quickinput/common/quickInput.js';
export { IEditorService } from '${upstream}workbench/services/editor/common/editorService.js';
export { IWorkspaceEditingService } from '${upstream}workbench/services/workspaces/common/workspaceEditing.js';
export { BrowserWorkspacesService } from '${upstream}workbench/services/workspaces/browser/workspacesService.js';
export { getStoredWorkspaceFolder, toWorkspaceFolders } from '${upstream}platform/workspaces/common/workspaces.js';
export { extUri } from '${upstream}base/common/resources.js';
export { URI } from '${upstream}base/common/uri.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { scanTargetExtensions } from './src/clients/workbench/src/extensions/inventory.js';
export { workspaceFileUri, workspaceFileSource } from './src/clients/workbench/src/workspaceFiles.js';
export { TargetUriTransformer, hostOwnsWorkspaceResource } from './src/clients/workbench/src/extensions/targetUris.js';
export * from './src/clients/workbench/src/extensions/commands.js';
import './src/clients/workbench/src/extensions/contribution.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024', metafile: true,
  plugins: [{ name: 'explicit-gateway-http-boundary', setup(builder) {
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({ contents: 'export const gateway = globalThis.__extensionInventoryBoundary; export class GatewayRequestError extends Error {}', loader: 'ts' }));
    // 仅导出原文件内部类以运行真实方法；不替换文档状态计算器或其状态转换。
    builder.onLoad({ filter: /\/api\/browser\/mainThreadDocumentsAndEditors\.ts$/ }, async args => ({
      contents: await transform(args.path, await readFile(args.path, 'utf8')) + '\nexport { MainThreadDocumentAndEditorStateComputer };',
      loader: 'ts', resolveDir: dirname(args.path),
    }));
    // 不构造 DOM Editor Pane；仅文档状态计算器和 MainThreadDocuments 是本片被测 owner。
    builder.onLoad({ filter: /\/browser\/parts\/editor\/textEditor\.ts$/ }, () => ({ contents: 'export class AbstractTextEditor {}', loader: 'ts' }));
    builder.onLoad({ filter: /\/api\/browser\/mainThreadEditor\.ts$/ }, () => ({ contents: 'export class MainThreadTextEditor {}', loader: 'ts' }));
    builder.onLoad({ filter: /\/api\/browser\/mainThreadEditors\.ts$/ }, () => ({ contents: 'export class MainThreadTextEditors {}', loader: 'ts' }));
    builder.onLoad({ filter: /\/contrib\/scm\/browser\/quickDiffModel\.ts$/ }, args => ({
      contents: "import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js'; export const IQuickDiffModelService = createDecorator('quickDiffModel');",
      loader: 'ts', resolveDir: dirname(args.path),
    }));
    builder.onLoad({ filter: /\/services\/timer\/browser\/timerService\.ts$/ }, args => ({
      contents: "import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js'; export const ITimerService = createDecorator('timerService');", loader: 'ts', resolveDir: dirname(args.path),
    }));
  } }, buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile));
const m = await import(pathToFileURL(bundle).href);
const targetA = { kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' };
const targetB = { kind: 'backend', gatewayId: 'gateway-b', backendId: '22222222-2222-4222-8222-222222222222' };
const web = { kind: 'web', profileId: 'web-profile' };
const fullCapabilities = { installation_manage: true, host_manage: true };
const installationOnly = { installation_manage: true, host_manage: false };
const hostOnly = { installation_manage: false, host_manage: true };
const id = value => new m.ExtensionIdentifier(value);
function extension(target, version = '1.0.0', overrides = {}) {
  return { identifier: id('probe.shared'), name: 'shared', publisher: 'probe', version, engines: { vscode: '*' },
    main: './extension.js', extensionLocation: m.URI.file('/extensions/shared-' + version), installationTarget: target,
    isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined',
    activationEvents: ['onCommand:probe.shared'], contributes: { commands: [{ command: 'probe.shared', title: 'Shared' }] }, ...overrides };
}
const reader = { readActivationEvents: extension => m.ImplicitActivationEvents.readActivationEvents(extension) };
const receipt = { boundary: '真实 Registry、Tracker、原 profile StorageManager／GlobalEnablement、原 CommandsRegistry、目标发现/库存扫描业务入口、原Web资格校验及 typed URI；HTTP、token、WebSocket字节与picker是明确边界装具，不代替真实 Gateway 权限判定／浏览器或全扩展 API 验收。', checks: [], scans: [] };

test('挂接原命令只进入原工作区 owner，持久文件重读保留完整不透明根和有序集合', async () => {
  const routes = [{ gateway_id: 'gateway-a', workspace_id: 'a1', name: 'A1', status: 'ready' },
    { gateway_id: 'gateway-b', workspace_id: 'b1', name: 'B1', status: 'ready' }];
  globalThis.__extensionInventoryBoundary.workspaces = async () => ({ items: routes });
  request = async (path, _options, key) => {
    expect(path).toBe('/api/v1/workspace/files/roots');
    return { workspace_uri: 'boxteam://workspace/' + JSON.parse(key)[1] + '/resources/files/root' };
  };
  const writes = [], entries = [];
  const service = new m.BrowserWorkspacesService({ onDidChangeValue: () => m.Event.None },
    { getWorkspace: () => ({ folders: [] }), getWorkbenchState: () => 1, onDidChangeWorkspaceFolders: m.Event.None },
    new m.NullLogService(), { writeFile: async (uri, data) => writes.push({ uri, data: data.toString() }) },
    { untitledWorkspacesHome: m.URI.parse('vscode-userdata:/Workspaces') }, { extUri: m.extUri });
  try {
    const values = new Map([[m.IQuickInputService, { pick: async items => [items[1], items[0]] }],
      [m.IWorkspaceEditingService, { createAndEnterWorkspace: async folders => entries.push(await service.createUntitledWorkspace(folders)) }]]);
    await m.CommandsRegistry.getCommand('boxteam.extensions.attachWorkspaceViews').handler({ get: id => {
      if (!values.has(id)) throw new Error('挂接命令不应读取额外 UI／reload 服务');
      return values.get(id);
    } });
    expect(entries).toHaveLength(1); expect(writes).toHaveLength(1);
    const restored = m.toWorkspaceFolders(JSON.parse(writes[0].data).folders, entries[0].configPath, m.extUri);
    expect(restored.map(folder => m.workspaceFileSource(folder.uri))).toEqual([routes[1], routes[0]].map(route => ({
      gatewayId: route.gateway_id, workspaceId: route.workspace_id,
      root: 'boxteam://workspace/' + route.workspace_id + '/resources/files/root',
      path: 'boxteam://workspace/' + route.workspace_id + '/resources/files/root',
    })));
    expect(restored.map(folder => folder.name)).toEqual(['B1', 'A1']);
    const source = m.workspaceFileSource(restored[0].uri);
    const sameScheme = m.getStoredWorkspaceFolder(restored[0].uri, false, 'B1', restored[1].uri, m.extUri);
    expect(m.workspaceFileSource(m.toWorkspaceFolders([sameScheme], restored[1].uri, m.extUri)[0].uri)).toEqual(source);
  } finally { service.dispose(); delete globalThis.__extensionInventoryBoundary.workspaces; }
  receipt.checks.push('真实挂接Action仅一次createAndEnterWorkspace；原BrowserWorkspacesService实写workspace正文并原toWorkspaceFolders重读，有序根及不透明URI身份不变');
});

test('原文件系统能力标记经真实 RPC 只投影scheme，未绑定真实文件仍拒绝', async () => {
  const binding = { initialization: { os: 'linux', logs_location_uri: 'file:///host/logs',
    environment: { app_root_uri: 'file:///host/app', global_storage_home_uri: 'file:///host/global', workspace_storage_home_uri: 'file:///host/workspace', profile_id: 'profile-target',
      remote_settings: { native_uri: 'file:///host/profile/settings.json', resource_uri: 'boxteam://gateway-a/gateway/gateway-a/resources/config/extension-host/backend-a/profiles/profile-target/settings' } },
    workspace: { folders: [] } } };
  const transformer = new m.TargetUriTransformer(binding, () => []);
  const toMain = new m.Emitter(), toHost = new m.Emitter(), registrations = new m.Emitter(), capabilities = new m.Emitter();
  const main = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)) }, null, transformer);
  const host = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)) });
  const info = new m.ExtHostFileSystemInfo(), received = [];
  let next = Promise.withResolvers();
  host.set(m.ExtHostContext.ExtHostFileSystemInfo, { $acceptProviderInfos: (uri, value) => {
    info.$acceptProviderInfos(uri, value); received.push(uri); next.resolve();
  } });
  const owner = new m.MainThreadFileSystem({ extensionHost: { uriTransformer: transformer }, getProxy: id => main.getProxy(id) },
    { listCapabilities: () => [{ scheme: 'boxteam-file', capabilities: 1024 }],
      onDidChangeFileSystemProviderRegistrations: registrations.event, onDidChangeFileSystemProviderCapabilities: capabilities.event });
  try {
    await next.promise;
    expect(info.getCapabilities('file')).toBe(1024); expect(received).toEqual([{ scheme: 'file', path: '/dummy' }]);
    next = Promise.withResolvers(); capabilities.fire({ scheme: 'boxteam-file', provider: { capabilities: 2048 } }); await next.promise;
    expect(info.getCapabilities('file')).toBe(2048);
    next = Promise.withResolvers(); registrations.fire({ scheme: 'boxteam-file' }); await next.promise;
    expect(info.getCapabilities('file')).toBeUndefined();
    expect(() => transformer.transformOutgoing(m.URI.file('/dummy').toJSON())).toThrow('未绑定');
  } finally { owner.dispose(); main.dispose(); host.dispose(); toMain.dispose(); toHost.dispose(); registrations.dispose(); capabilities.dispose(); }
  receipt.checks.push('原MainThreadFileSystem构造及provider能力/注销事件经过真实RPC至原ExtHostFileSystemInfo；scheme元数据不触发路径准入，真实未知file仍拒绝');
});


test('真实 Registry 构造保留类边界，同公开ID按目标查询／删除，窗口贡献只投影一次', async () => {
  const a = extension(targetA), b = extension(targetB, '2.0.0');
  const raw = new m.ExtensionDescriptionRegistry(reader, [a, b], true);
  expect(raw.getAllExtensionDescriptions()).toHaveLength(2);
  expect(raw.getWindowExtensionDescriptions()).toHaveLength(1);
  expect(raw.getExtensionDescription('PROBE.SHARED', targetB)).toBe(b);
  expect(() => raw.getExtensionDescription('probe.shared')).toThrow('Ambiguous');
  expect(() => raw.deltaExtensions([], [a.identifier])).toThrow('Ambiguous');
  raw.deltaExtensions([], [a]);
  expect(raw.getAllExtensionDescriptions()).toEqual([b]);
  const owner = new m.LockableExtensionDescriptionRegistry(reader), lock = await owner.acquireLock('instance test');
  try {
    expect(() => owner.deltaExtensions(lock, [{ ...a, installationTarget: undefined }], [])).toThrow('unbound');
    owner.deltaExtensions(lock, [a, b], []);
    expect(owner.getAllExtensionInstances()).toHaveLength(2);
    const before = owner.getSnapshot();
    const conflicting = { ...b, contributes: { commands: [{ command: 'probe.shared', title: 'Changed' }] } };
    expect(() => owner.deltaExtensions(lock, [conflicting], [b])).toThrow('Conflicting');
    expect(owner.getSnapshot()).toEqual(before);
  } finally { lock.dispose(); raw.dispose(); }
  receipt.checks.push('真实构造边界、无目标歧义零删除、精确实例删除及候选贡献冲突原子拒绝');
});

test('相同声明的相对资源不因同版本被错误共享，循环图只连接同目标', () => {
  const a = extension(targetA, '1.0.0', { contributes: { commands: [{ command: 'probe.shared', title: 'Shared', icon: 'icon.svg' }] } });
  const b = { ...a, installationTarget: targetB, extensionLocation: m.URI.file('/other/shared') };
  expect(() => new m.ExtensionDescriptionRegistry(reader, [a, b], true)).toThrow('Conflicting');
  const root = extension(targetA, '1.0.0', { contributes: undefined, extensionDependencies: ['probe.dep'] });
  const depA = extension(targetA, '1.0.0', { contributes: undefined, identifier: id('probe.dep'), extensionDependencies: ['probe.shared'] });
  const depB = { ...depA, installationTarget: targetB, extensionDependencies: [] };
  const registry = new m.ExtensionDescriptionRegistry(reader, [], true);
  expect(registry.deltaExtensions([root, depA, depB], []).removedDueToLooping).toEqual([root, depA]);
  expect(registry.getAllExtensionDescriptions()).toEqual([depB]);
  expect(() => registry.deltaExtensions([root, root], [])).toThrow('Duplicate');
  registry.dispose();
  receipt.checks.push('相对资源 base 不等价拒绝、Backend B 的同名依赖不能打破 A 的循环、循环前重复校验');
});

test('相同设置声明在窗口只登记一次，schema及信任限制冲突仍拒绝', () => {
  const a = extension(targetA, '1.0.0', { contributes: { configuration: { title: 'Shared', properties: {
    'probe.setting': { type: 'string', scope: 'language-overridable', default: 'default' },
  } } }, capabilities: { untrustedWorkspaces: { supported: true } } });
  const b = { ...a, version: '2.0.0', installationTarget: targetB, extensionLocation: m.URI.file('/other/shared') };
  const registry = new m.ExtensionDescriptionRegistry(reader, [a, b], true);
  expect(registry.getAllExtensionDescriptions()).toEqual([b, a]);
  expect(registry.getWindowExtensionDescriptions()).toEqual([b]);
  const before = registry.getSnapshot();
  const changed = { ...b, contributes: { configuration: { title: 'Shared', properties: {
    'probe.setting': { type: 'string', scope: 'machine', default: 'changed' },
  } } } };
  expect(() => registry.deltaExtensions([changed], [b])).toThrow('Conflicting');
  expect(registry.getSnapshot()).toEqual(before);
  expect(() => registry.deltaExtensions([{ ...b, capabilities: { untrustedWorkspaces: {
    supported: 'limited', restrictedConfigurations: ['probe.setting'],
  } } }], [b])).toThrow('Conflicting');
  expect(registry.getSnapshot()).toEqual(before);
  expect(() => registry.deltaExtensions([{ ...b, extensionLocation: a.extensionLocation,
    capabilities: { untrustedWorkspaces: { supported: false } },
  }], [b])).toThrow('Conflicting');
  expect(() => registry.deltaExtensions([{ ...b, extensionLocation: a.extensionLocation,
    enabledApiProposals: ['configurationDefaults'],
  }], [b])).toThrow('Conflicting');
  expect(registry.getSnapshot()).toEqual(before);
  registry.dispose();
  receipt.checks.push('原窗口相同configuration声明共享一次，schema／scope／默认值及信任限制冲突仍候选原子拒绝');
});

function tracker(registry, readKinds = extension => extension.installationTarget.kind === 'backend' ? ['workspace'] : ['web']) {
  return new m.ExtensionRunningLocationTracker(registry, { pickExtensionHostKind: (_id, _kinds, local, remote) => remote ? 3 : local ? 2 : null }, {},
    { getValue: () => ({}) }, { info() {}, warn() {}, error() {} }, { getExtensionKind: readKinds });
}
test('原 Tracker 与 host snapshot 按实例分组，A/B 同ID版本和更新互不覆盖', () => {
  const a = extension(targetA), b = extension(targetB, '2.0.0'), local = extension(web, '3.0.0');
  const registry = new m.ExtensionDescriptionRegistry(reader, [a, b, local], true), running = tracker(registry);
  const views = [targetA, targetB].map((installationTarget, index) => ({ installationTarget, viewId: 'default', workspaceIds: ['workspace-' + index], capabilities: fullCapabilities, nodeRuntime: { channel_state: 'available' } }));
  running.setRemoteViews(views); running.initializeRunningLocation([local], [a, b]);
  const locations = running.remoteRunningLocations;
  expect(locations[0].equals(locations[1])).toBe(false);
  expect(running.getRunningLocation(a).equals(locations[0])).toBe(true);
  expect(running.getRunningLocation(b).equals(locations[1])).toBe(true);
  expect(() => running.getRunningLocation(a.identifier)).toThrow('Ambiguous');
  for (const [index, expected] of [a, b].entries()) {
    const all = running.getHostExtensions(registry.getAllExtensionDescriptions(), locations[index]);
    const mine = running.filterByRunningLocation(all, locations[index]);
    expect(new m.ExtensionHostExtensions(1, all, mine.map(extension => extension.identifier)).allExtensions).toEqual([expected]);
  }
  const next = { ...a, version: '4.0.0' };
  registry.deltaExtensions([next], [a]); running.deltaExtensions([next], [a]);
  expect(running.getHostExtensions(registry.getAllExtensionDescriptions(), locations[1])).toEqual([b]);
  expect(() => running.setRemoteViews(views)).toThrow('reload'); registry.dispose();
  receipt.checks.push('真实 Tracker 及 HostExtensions 唯一公开ID快照、实例更新只影响 A、集合变化要求reload');
});

test('原Tracker按manifest有序偏好分配目标Web/Node，无Node运行时仍分配browser且不混窗口affinity', () => {
  const cases = [
    { kinds: ['workspace', 'web'], node: true, expected: 3 },
    { kinds: ['workspace', 'web'], node: false, expected: 2 },
    { kinds: ['web', 'workspace'], node: true, expected: 2 },
    { kinds: ['workspace'], node: false, expected: null },
    { kinds: ['web'], node: false, expected: 2, main: undefined },
    { kinds: ['workspace'], node: false, expected: null, browser: undefined },
    { kinds: ['ui', 'workspace', 'web'], node: false, expected: 2, main: undefined, browser: undefined },
    { kinds: ['ui', 'workspace', 'web'], node: true, expected: 2, main: undefined, browser: undefined },
    { kinds: ['workspace'], node: true, expected: null, main: undefined, browser: undefined },
  ];
  for (const value of cases) {
    const item = extension(targetA, '1.0.0', { main: './node.js', browser: './browser.js', extensionKind: value.kinds,
      ...('main' in value ? { main: value.main } : {}), ...('browser' in value ? { browser: value.browser } : {}) });
    const registry = new m.ExtensionDescriptionRegistry(reader, [item], true), running = tracker(registry, extension => extension.extensionKind);
    const view = { installationTarget: targetA, viewId: 'default', workspaceIds: value.node ? ['a1'] : [], capabilities: fullCapabilities,
      ...(value.node ? { nodeRuntime: { channel_state: 'available' } } : {}), webHost: { fixtureAdmission: true } };
    try {
      running.setRemoteViews([view]); running.initializeRunningLocation([], [item]);
      const location = running.getRunningLocation(item);
      expect(location?.kind ?? null).toBe(value.expected);
      if (value.expected === 2) {
        expect(location.equals(running.targetWebRunningLocations[0])).toBe(true);
        expect(location.equals(new m.LocalWebWorkerRunningLocation(0))).toBe(false);
        expect(location.workspaceIds).toEqual(view.workspaceIds);
      }
    } finally { registry.dispose(); }
  }
  receipt.checks.push('原Tracker按manifest偏好和Node可用性单路分配，空根目标Web与窗口affinity位置严格区分');
});

test('原 enablement storage 保存目标与视图，A 禁用不改变 B 或原 Web profile 记录', async () => {
  const values = new Map(), storage = { onDidChangeValue: () => m.Event.None, get: (key, _scope, fallback) => values.get(key) ?? fallback,
    store: (key, value) => values.set(key, value), remove: key => values.delete(key) };
  const service = new m.GlobalExtensionEnablementService(storage, { onDidInstallExtensions: m.Event.None });
  const a = { id: 'probe.shared', enablementTarget: targetA }, b = { id: 'probe.shared', enablementTarget: targetB };
  await service.disableExtension(a); await service.disableExtension({ id: 'probe.shared' });
  expect(service.getDisabledExtensions()).toEqual([a, { id: 'probe.shared' }]);
  expect(await service.enableExtension(b)).toBe(false);
  const workspace = new m.StorageManager(storage);
  workspace.set('workspace-test', [{ ...a, workspaceView: '["default",["a1","a2"]]' }, b], 1);
  expect(JSON.parse(values.get('workspace-test'))).toEqual([{ ...a, workspaceView: '["default",["a1","a2"]]' }, b]);
  expect(m.areSameEnablementIdentifiers({ ...a, workspaceView: 'first' }, { ...a, workspaceView: 'second' })).toBe(false);
  workspace.dispose(); service.dispose(); receipt.checks.push('原 storage 序列化与禁用比较均消费稳定target/view，不剥离字段或串改另一个目标');
});

function workbenchEnablement(all) {
  const values = new Map(), storage = { onDidChangeValue: () => m.Event.None, get: (key, scope, fallback) => values.get(key + '@' + scope) ?? fallback,
    getBoolean: () => undefined, getObject: (_key, _scope, fallback) => fallback,
    store: (key, value, scope) => values.set(key + '@' + scope, value), remove: (key, scope) => values.delete(key + '@' + scope) };
  const global = new m.GlobalExtensionEnablementService(storage, { onDidInstallExtensions: m.Event.None });
  const webServer = {}, manager = { extensions: all, whenInitialized: async () => {}, onDidChangeExtensions: m.Event.None, dispose() {} };
  // 本片不运行控制清单轮询；扩展管理及计时器是外部边界，启用计算和写入仍走原 owner。
  class Enablement extends m.ExtensionEnablementService { loopCheckForMaliciousExtensions() {} }
  const service = new Enablement(storage, global, { getWorkbenchState: () => 3, getWorkspace: () => ({ folders: [{ uri: m.URI.parse('boxteam-file:/root') }] }) },
    {}, { onDidUninstallExtension: m.Event.None }, { getValue: () => false, onDidChangeConfiguration: m.Event.None },
    { webExtensionManagementServer: webServer, getExtensionManagementServer: extension => extension.installationTarget?.kind === 'web' ? webServer : {},
      getExtensionInstallLocation: extension => extension.installationTarget?.kind === 'web' ? m.ExtensionInstallLocation.Web : m.ExtensionInstallLocation.Remote },
    {}, {}, {}, {}, {}, {}, { isDisabledByBisect: () => false }, { isAllowed: () => true, onDidChangeAllowedExtensionsConfigValue: m.Event.None },
    { isWorkspaceTrusted: () => true }, {}, { getExtensionKind: manifest => manifest.extensionKind,
      getExtensionVirtualWorkspaceSupportType: () => false, canExecuteOnWeb: () => true }, {}, { createInstance: () => manager }, { debug() {} }, {});
  return { service, global };
}

test('原 Workbench enablement 入口保留扫描无效、Node kind及同目标依赖启用行为', async () => {
  const a = m.toExtension(extension(targetA, '1.0.0', { extensionKind: ['workspace'], enablementView: 'view-a' }));
  const b = m.toExtension(extension(targetB, '2.0.0', { extensionKind: ['workspace'], enablementView: 'view-b' }));
  const local = m.toExtension(extension(web, '3.0.0', { browser: './web.js', extensionKind: ['web'] }));
  const dep = target => m.toExtension(extension(target, '1.0.0', { identifier: id('probe.dep'), name: 'dep', extensionKind: ['workspace'] }));
  const depA = dep(targetA), depB = dep(targetB);
  const { service, global } = workbenchEnablement([a, b, local, depA, depB]);
  const changed = [], listener = service.onEnablementChanged(extensions => changed.push(extensions));
  try {
    await Promise.resolve();
    expect(service.getEnablementStates([a, b, local])).toEqual(Array(3).fill(m.EnablementState.EnabledGlobally));
    expect(service.getEnablementStates([{ ...a, isValid: false }])).toEqual([m.EnablementState.DisabledByInvalidExtension]);
    expect(service.getEnablementStates([{ ...a, manifest: { ...a.manifest, main: undefined, browser: './web.js', extensionKind: ['web'] } }]))
      .toEqual([m.EnablementState.EnabledGlobally]);
    expect(service.getEnablementStates([{ ...a, manifest: { ...a.manifest, main: undefined, browser: './web.js' } },
      { ...a, manifest: { ...a.manifest, extensionKind: ['ui'] } }])).toEqual(Array(2).fill(m.EnablementState.DisabledByExtensionKind));
    await service.setEnablement([a], m.EnablementState.DisabledWorkspace);
    expect(service.getEnablementStates([a, b, local])).toEqual([m.EnablementState.DisabledWorkspace, m.EnablementState.EnabledGlobally, m.EnablementState.EnabledGlobally]);
    expect(service.getEnablementStates([{ ...a, enablementView: 'other-view' }])).toEqual([m.EnablementState.EnabledGlobally]);
    await service.setEnablement([a], m.EnablementState.EnabledGlobally);
    await global.disableExtension({ id: a.identifier.id, enablementTarget: targetA });
    await Promise.resolve();
    expect(changed.at(-1)).toEqual([a]);
    expect(service.getEnablementStates([a, b, local])).toEqual([m.EnablementState.DisabledGlobally, m.EnablementState.EnabledGlobally, m.EnablementState.EnabledGlobally]);
    await global.enableExtension({ id: a.identifier.id, enablementTarget: targetA });
    await global.disableExtension({ id: 'probe.dep', enablementTarget: targetA });
    await global.disableExtension({ id: 'probe.dep', enablementTarget: targetB });
    const dependent = { ...a, manifest: { ...a.manifest, extensionDependencies: ['probe.dep'] } };
    expect(service.getEnablementStates([dependent, depA, depB])[0]).toBe(m.EnablementState.DisabledByExtensionDependency);
    const pack = { ...a, manifest: { ...a.manifest, extensionPack: ['probe.dep'] } };
    await service.setEnablement([pack], m.EnablementState.EnabledGlobally);
    expect(service.getEnablementStates([dependent, depA, depB])).toEqual([m.EnablementState.EnabledGlobally, m.EnablementState.EnabledGlobally, m.EnablementState.DisabledGlobally]);
  } finally { listener.dispose(); service.dispose(); global.dispose(); }
  receipt.checks.push('原 Workbench 启用入口消费 invalid及kind；workspace view、全局变更事件、依赖计算与扩展包递归按目标，不启用另一个Backend同名项');
});

function model(resource, name) {
  return { id: name, uri: resource, isTooLargeForSyncing: () => false, isForSimpleWidget: false, isDisposed: () => false,
    getVersionId: () => 1, onDidChangeContent: m.Event.None };
}
test('原文档状态计算器初始与增量只暴露所属视图，外部焦点无activeEditor', () => {
  const resource = (target, workspaceId, name) => m.URI.joinPath(m.workspaceFileUri({ gatewayId: target.gatewayId, workspaceId }, 'root', 'root'), name);
  const own = model(resource(targetA, 'a1', 'own.ts'), 'own'), foreign = model(resource(targetB, 'b1', 'foreign.ts'), 'foreign');
  const added = new m.Emitter(), removed = new m.Emitter(), active = new m.Emitter();
  const models = [own, foreign], editors = models.map(item => ({ getId: () => item.id, getModel: () => item, hasModel: () => true,
    hasTextFocus: () => false, hasWidgetFocus: () => false, onDidChangeModel: m.Event.None, onDidFocusEditorText: m.Event.None, onDidFocusEditorWidget: m.Event.None }));
  const modelService = { getModels: () => models, getModel: uri => models.find(item => item.uri.toString() === uri.toString()), onModelAdded: added.event, onModelRemoved: removed.event };
  const editorService = { activeTextEditorControl: editors[1], onDidActiveEditorChange: active.event, onDidVisibleEditorsChange: m.Event.None }, deltas = [];
  const binding = { gateway_id: targetA.gatewayId, backend_id: targetA.backendId, initialization: { os: 'linux', logs_location_uri: 'file:///host/logs',
    environment: { app_root_uri: 'file:///host/app', global_storage_home_uri: 'file:///host/global', workspace_storage_home_uri: 'file:///host/workspace', profile_id: 'profile-target',
      remote_settings: { native_uri: 'file:///host/profile/settings.json', resource_uri: 'boxteam://gateway-a/gateway/gateway-a/resources/config/extension-host/backend-a/profiles/profile-target/settings' } },
    workspace: { folders: [{ workspace_id: 'a1', name: 'A1', resource_uri: 'root', native_uri: 'file:///work/a' }] } } };
  const host = { runningLocation: new m.RemoteRunningLocation(targetA, 'default', ['a1']), binding, targetContext: { kind: 'node', connection: binding }, uriTransformer: new m.TargetUriTransformer(binding, () => []) };
  const computer = new m.MainThreadDocumentAndEditorStateComputer(delta => deltas.push(delta), uri => m.hostOwnsWorkspaceResource(host, uri), modelService,
    { listCodeEditors: () => editors, onCodeEditorAdd: m.Event.None, onCodeEditorRemove: m.Event.None }, editorService,
    { onDidPaneCompositeOpen: m.Event.None, onDidPaneCompositeClose: m.Event.None, getActivePaneComposite: () => undefined });
  try {
    expect(deltas.flatMap(delta => delta.addedDocuments)).toEqual([own]);
    expect(deltas.flatMap(delta => delta.addedEditors).map(editor => editor.id)).toEqual(['own,own']);
    expect(deltas.at(-1).newActiveEditor).toBeNull();
    const outside = model(resource(targetB, 'b1', 'outside.ts'), 'outside');
    models.push(outside); const before = deltas.length; added.fire(outside); expect(deltas).toHaveLength(before);
    const inside = model(resource(targetA, 'a1', 'inside.ts'), 'inside');
    models.push(inside); added.fire(inside); expect(deltas.at(-1).addedDocuments).toEqual([inside]);
    editorService.activeTextEditorControl = editors[0]; active.fire(); expect(deltas.at(-1).newActiveEditor).toBe('own,own');
    editorService.activeTextEditorControl = editors[1]; active.fire(); expect(deltas.at(-1).newActiveEditor).toBeNull();
    models.splice(models.indexOf(own), 1); removed.fire(own); expect(deltas.at(-1).removedDocuments).toEqual([own]);
  } finally { computer.dispose(); added.dispose(); removed.dispose(); active.dispose(); }
  receipt.checks.push('原文档状态计算器初始/增量/移除过滤、外部编辑器焦点不泄露、内部焦点可用');
});

test('原 MainThreadDocuments 的 save/dirty/encoding 事件只发给已跟踪模型', () => {
  const own = model(m.URI.file('/own.ts'), 'own'), foreign = model(m.URI.file('/foreign.ts'), 'foreign');
  const saved = new m.Emitter(), dirty = new m.Emitter(), encoding = new m.Emitter(), calls = [];
  const documents = new m.MainThreadDocuments({ getProxy: () => ({ $acceptModelSaved: uri => calls.push(['save', uri]),
    $acceptDirtyStateChanged: uri => calls.push(['dirty', uri]), $acceptEncodingChanged: uri => calls.push(['encoding', uri]) }) },
    { getModel: uri => [own, foreign].find(item => item.uri.toString() === uri.toString()), onModelLanguageChanged: m.Event.None },
    { files: { onDidSave: saved.event, onDidChangeDirty: dirty.event, onDidChangeEncoding: encoding.event }, untitled: { onDidChangeEncoding: m.Event.None } },
    {}, {}, {}, { extUri: {} }, { onDidRunWorkingCopyFileOperation: m.Event.None }, {});
  try {
    documents.handleModelAdded(own);
    for (const item of [own, foreign]) {
      saved.fire({ model: { resource: item.uri } }); dirty.fire({ resource: item.uri, isDirty: () => true });
      encoding.fire({ resource: item.uri, getEncoding: () => 'utf8' });
    }
    expect(calls).toEqual([['save', own.uri], ['dirty', own.uri], ['encoding', own.uri]]);
  } finally { documents.dispose(); saved.dispose(); dirty.dispose(); encoding.dispose(); }
  receipt.checks.push('原 MainThreadDocuments 的 save/dirty/encoding 消费跟踪集合，不广播外部模型');
});

const scanLifetime = new m.DisposableStore();
const scanWarnings = [];
const scanKinds = extension => extension.extensionKind ?? (extension.installationTarget.kind === 'backend' ? ['workspace'] : ['web']);
const scan = (context, entryGatewayId = targetA.gatewayId) => m.scanTargetExtensions(context, scanKinds, scanLifetime,
  entryGatewayId, { warn: (...values) => scanWarnings.push(values.map(String).join(' ')) });
function context(owners) { return { getCompleteWorkspace: async () => ({ folders: [{ uri: m.URI.parse('boxteam-workspace:/') }, ...owners.map(owner => ({ uri: m.workspaceFileUri(owner, 'boxteam://' + owner.gatewayId + '/root/' + owner.workspaceId, 'boxteam://' + owner.gatewayId + '/root/' + owner.workspaceId) }))] }) }; }
const targetListPath = '/api/gateway/extension-targets';
const targetResolvePath = '/api/gateway/extension-targets/resolve';
const hostResolvePath = '/api/gateway/extension-hosts/resolve';
const inventoryPath = '/api/gateway/extension-installations/inventory';
const ownerA = workspaceId => ({ gatewayId: targetA.gatewayId, workspaceId });
function targetReply(payload, capabilities = fullCapabilities) {
  return { ...payload, backend_id: payload.gateway_id === targetA.gatewayId ? targetA.backendId : targetB.backendId, source: 'live', capabilities: { ...capabilities } };
}
function directoryReply(payload, capabilities = fullCapabilities) {
  return { ...payload, source: 'live', targets: [targetReply(payload, capabilities)] };
}
function hostReply(payload) { return { ...payload, source: 'live', channel_state: 'available', runtime: { commit: pinnedUpstreamCommit } }; }
function inventoryReply(payload) {
  return { ...payload, profile_scope: 'default', inventory: { profile_id: 'native-default', target_platform: 'linux-x64', extensions: [{ source: 'profile', extension_key: 'publisher.probe-1.0.0', is_valid: false,
    validations: [{ severity: 3, message: 'manifest invalid' }], description: JSON.parse(JSON.stringify(extension(targetB))) }] } };
}
const scannedRequests = () => requests.map(([path, options]) => ({ path, method: options.method, body: JSON.parse(options.body) }));
function scanResponse(path, options) {
  const payload = JSON.parse(options.body);
  if (path === targetListPath) return directoryReply(payload);
  if (path === targetResolvePath) return targetReply(payload);
  if (path === hostResolvePath) return hostReply(payload);
  if (path === inventoryPath) return inventoryReply(payload);
  throw new Error('扫描不应请求其它入口：' + path);
}

test('库存真实业务入口只发现明确根并核完整集合，包正文不能覆写target或invalid诊断', async () => {
  requests.length = 0;
  request = async (...args) => scanResponse(...args);
  const snapshot = await scan(context([ownerA('a1'), ownerA('a2')]));
  expect(snapshot.views).toEqual([{ installationTarget: targetA, viewId: 'default', workspaceIds: ['a1', 'a2'], capabilities: fullCapabilities }]);
  expect(snapshot.extensions[0].installationTarget).toEqual(targetA);
  expect(m.toExtension(snapshot.extensions[0])).toMatchObject({ isValid: false, validations: [[3, 'manifest invalid']], installationTarget: targetA });
  expect(requests.map(([path]) => path)).toEqual([targetResolvePath, targetResolvePath, targetResolvePath, inventoryPath]);
  const complete = { protocol_version: 1, gateway_id: targetA.gatewayId, backend_id: targetA.backendId, workspace_ids: ['a1', 'a2'] };
  expect(scannedRequests().slice(2)).toEqual([
    { path: targetResolvePath, method: 'POST', body: complete },
    { path: inventoryPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId, backend_id: targetA.backendId } },
  ]);
  receipt.scans.push({ name: '原库存及invalid诊断', requests: scannedRequests(), views: snapshot.views });
  const previous = request;
  request = async (...args) => args[0].endsWith('/inventory') ? { ...await previous(...args), backend_id: targetB.backendId } : previous(...args);
  await expect(scan(context([ownerA('a1')]))).rejects.toThrow('不匹配');
  request = async () => { throw new Error('offline route'); };
  await expect(scan(context([ownerA('a1')]))).rejects.toThrow('offline route');
  receipt.checks.push('扫描真实入口取 envelope 目标、完整有序集合准入、保留invalid诊断、路由误指与离线不空回退');
});

test('真实scanner仅安装目标不发现host或读取库存，原Tracker保留view而不生成宿主位置', async () => {
  requests.length = 0;
  request = async (path, options) => {
    if (path !== targetResolvePath) throw new Error('仅安装权限不能进入宿主发现或库存入口');
    return targetReply(JSON.parse(options.body), installationOnly);
  };
  const snapshot = await scan(context([ownerA('a1')]));
  expect(snapshot.views).toEqual([{ installationTarget: targetA, viewId: 'default', workspaceIds: ['a1'], capabilities: installationOnly }]);
  expect(snapshot.extensions).toEqual([]);
  expect(scannedRequests()).toEqual([{ path: targetResolvePath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId, workspace_ids: ['a1'] } }]);
  const registry = new m.ExtensionDescriptionRegistry(reader, [], true), running = tracker(registry);
  try {
    running.setRemoteViews(snapshot.views); running.initializeRunningLocation([], snapshot.extensions);
    expect(running.workspaceViews).toEqual(snapshot.views); expect(running.workspaceViews[0]).toBe(snapshot.views[0]);
    expect(running.remoteRunningLocations).toEqual([]);
  } finally { registry.dispose(); }
  receipt.scans.push({ name: '单根仅安装权限', requests: scannedRequests(), views: snapshot.views });
});

test('真实scanner混合两权限，完整组回执决定权限且保留根顺序，host-only仍读原库存', async () => {
  requests.length = 0;
  request = async (path, options) => {
    const payload = JSON.parse(options.body);
    if (path === targetResolvePath) return targetReply(payload, payload.gateway_id === targetA.gatewayId
      ? payload.workspace_ids.length === 1 ? fullCapabilities : installationOnly : hostOnly);
    const reply = scanResponse(path, options);
    if (path === inventoryPath) { reply.inventory.extensions[0].is_valid = true; reply.inventory.extensions[0].validations = []; }
    return reply;
  };
  const snapshot = await scan(context([ownerA('a2'), { gatewayId: targetB.gatewayId, workspaceId: 'b1' }, ownerA('a1')]));
  expect(snapshot.views).toEqual([{ installationTarget: targetA, viewId: 'default', workspaceIds: ['a2', 'a1'], capabilities: installationOnly },
    { installationTarget: targetB, viewId: 'default', workspaceIds: ['b1'], capabilities: hostOnly, nodeRuntime: hostReply({ protocol_version: 1,
      gateway_id: targetB.gatewayId, backend_id: targetB.backendId, workspace_ids: ['b1'] }) }]);
  expect(snapshot.extensions).toHaveLength(1); expect(snapshot.extensions[0].installationTarget).toEqual(targetB);
  const a = { protocol_version: 1, gateway_id: targetA.gatewayId, backend_id: targetA.backendId, workspace_ids: ['a2', 'a1'] };
  const b = { protocol_version: 1, gateway_id: targetB.gatewayId, backend_id: targetB.backendId, workspace_ids: ['b1'] };
  expect(scannedRequests()).toEqual([
    { path: targetResolvePath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId, workspace_ids: ['a2'] } },
    { path: targetResolvePath, method: 'POST', body: { protocol_version: 1, gateway_id: targetB.gatewayId, workspace_ids: ['b1'] } },
    { path: targetResolvePath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId, workspace_ids: ['a1'] } },
    { path: targetResolvePath, method: 'POST', body: a },
    { path: inventoryPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetB.gatewayId, backend_id: targetB.backendId } },
    { path: hostResolvePath, method: 'POST', body: b },
  ]);
  const registry = new m.ExtensionDescriptionRegistry(reader, snapshot.extensions, true), running = tracker(registry);
  try {
    running.setRemoteViews(snapshot.views); running.initializeRunningLocation([], snapshot.extensions);
    expect(running.workspaceViews).toEqual(snapshot.views);
    expect(running.workspaceViews[0]).toBe(snapshot.views[0]); expect(running.workspaceViews[1]).toBe(snapshot.views[1]);
    expect(running.remoteRunningLocations.map(location => [location.installationTarget, location.workspaceIds])).toEqual([[targetB, ['b1']]]);
    expect(running.getRunningLocation(snapshot.extensions[0]).equals(running.remoteRunningLocations[0])).toBe(true);
  } finally { registry.dispose(); }
  receipt.scans.push({ name: '混合权限完整有序组', requests: scannedRequests(), views: snapshot.views });
});

for (const invalid of ['protocol', 'gateway', 'backend', 'order', 'source', 'capabilities', 'missing-capabilities', 'denied']) test(`真实scanner完整组${invalid}回执拒绝，不越权发现host或读取库存`, async () => {
  requests.length = 0;
  request = async (path, options) => {
    if (path !== targetResolvePath) throw new Error('非法目标回执不能继续其它入口');
    const payload = JSON.parse(options.body), result = targetReply(payload);
    if (payload.workspace_ids.length === 1) return result;
    if (invalid === 'protocol') result.protocol_version = 2;
    if (invalid === 'gateway') result.gateway_id = targetB.gatewayId;
    if (invalid === 'backend') result.backend_id = targetB.backendId;
    if (invalid === 'order') result.workspace_ids = [...payload.workspace_ids].reverse();
    if (invalid === 'source') result.source = 'cached';
    if (invalid === 'capabilities') result.capabilities = { installation_manage: 'true', host_manage: false };
    if (invalid === 'missing-capabilities') delete result.capabilities;
    if (invalid === 'denied') result.capabilities = { installation_manage: false, host_manage: false };
    return result;
  };
  await expect(scan(context([ownerA('a1'), ownerA('a2')]))).rejects.toThrow();
  expect(requests.map(([path]) => path)).toEqual([targetResolvePath, targetResolvePath, targetResolvePath]);
  receipt.scans.push({ name: '拒绝完整组-' + invalid, requests: scannedRequests() });
});

test('真实scanner前置目标授权拒绝保留原错误，不回退宿主发现或空根目录', async () => {
  requests.length = 0;
  const denied = new Error('extension targets forbidden: original-authorization-request');
  request = async () => { throw denied; };
  await expect(scan(context([ownerA('a1')]))).rejects.toBe(denied);
  expect(requests.map(([path]) => path)).toEqual([targetResolvePath]);
  receipt.checks.push('目标授权失败保留原错误，不借宿主管理权或空根目录回退');
});

test('空根scanner让browser、hybrid及纯声明式贡献共用原Web资格，Node-only和不支持Web的kind不取得位置', async () => {
  requests.length = 0; scanWarnings.length = 0;
  const opens = [], sockets = [], originals = Object.fromEntries(['location', 'fetch', 'WebSocket'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const entries = [
    extension(targetB, '1.0.0', { identifier: id('probe.browser'), name: 'browser', main: undefined, browser: './browser.js', extensionKind: ['web'] }),
    extension(targetB, '1.0.0', { identifier: id('probe.hybrid'), name: 'hybrid', browser: './browser.js', extensionKind: ['workspace', 'web'] }),
    extension(targetB, '1.0.0', { identifier: id('probe.node-only'), name: 'node-only', extensionKind: ['workspace'] }),
    ...[
      { name: 'theme', contributes: { themes: [{ label: '目标主题', uiTheme: 'vs-dark', path: './theme.json' }] } },
      { name: 'grammar', contributes: { grammars: [{ language: 'target-probe', scopeName: 'source.target-probe', path: './grammar.json' }] } },
      { name: 'snippet', contributes: { snippets: [{ language: 'target-probe', path: './snippets.json' }] } },
      { name: 'no-web', contributes: { themes: [{ label: '禁止Web的主题', uiTheme: 'vs-dark', path: './no-web.json' }] } },
    ].map(value => extension(targetB, '1.0.0', { ...value, identifier: id('probe.' + value.name), main: undefined, browser: undefined,
      activationEvents: [], extensionKind: value.name === 'no-web' ? ['workspace'] : ['ui', 'workspace', 'web'] })),
  ];
  const native = payload => ({ ...payload, profile_scope: 'default', inventory: { profile_id: 'native-default', target_platform: 'linux-x64',
    extensions: entries.map(value => ({ source: 'profile', extension_key: value.identifier.value + '-1.0.0', is_valid: true,
      validations: [], description: JSON.parse(JSON.stringify(value)) })) } });
  // HTTP和WebSocket字节是具名边界；扫描、原资格校验、包位置绑定及Tracker均执行生产算法。
  class QualificationSocket extends EventTarget {
    constructor(url) {
      super(); sockets.push(this); expect(new URL(url).pathname).toBe('/api/gateway/extension-web-hosts/channel');
      queueMicrotask(() => this.dispatchEvent(new Event('open')));
    }
    send(data) {
      const payload = JSON.parse(data); opens.push(payload);
      const inventory = native(payload).inventory;
      inventory.extensions = payload.extension_keys.map(key => inventory.extensions.find(entry => entry.extension_key === key));
      const value = { ...payload, request_id: 'empty-web-request', client_id: 'empty-web-client', generation: 'empty-web-generation', inventory,
        context: { os: 'linux', remote_settings: { resource_uri: 'boxteam://profile/settings' },
          workspace: { id: 'empty-workspace', name: 'Empty', folders: [] } } };
      queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })));
    }
    close() { this.dispatchEvent(new Event('close')); }
  }
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { href: 'http://entry-gateway.test/' } });
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: QualificationSocket });
  globalThis.fetch = async url => {
    expect(new URL(url).pathname).toBe('/api/gateway/auth/local-credential');
    return new Response(JSON.stringify({ data: { token: 'qualification-boundary-token' } }));
  };
  request = async (path, options) => {
    const payload = JSON.parse(options.body);
    if (path === targetListPath) return directoryReply(payload);
    if (path === inventoryPath) return native(payload);
    throw new Error('空根不能进入Node或按工作区发现入口：' + path);
  };
  try {
    const snapshot = await scan(context([])), [view] = snapshot.views;
    expect(scannedRequests()).toEqual([
      { path: targetListPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId } },
      { path: inventoryPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId, backend_id: targetA.backendId } },
    ]);
    expect(view.installationTarget).toEqual(targetA); expect(view.workspaceIds).toEqual([]); expect(view.nodeRuntime).toBeUndefined();
    expect(snapshot.extensions).toHaveLength(7);
    expect(snapshot.extensions.every(value => value.installationTarget.gatewayId === targetA.gatewayId)).toBe(true);
    expect(opens).toEqual([{ protocol_version: 1, gateway_id: targetA.gatewayId, backend_id: targetA.backendId,
      workspace_ids: [], view_id: 'default', profile_id: 'native-default',
      extension_keys: ['probe.browser-1.0.0', 'probe.hybrid-1.0.0', 'probe.theme-1.0.0', 'probe.grammar-1.0.0', 'probe.snippet-1.0.0'] }]);
    expect(sockets).toHaveLength(1); expect(scanWarnings).toHaveLength(1);
    expect(scanWarnings[0]).toContain('probe.node-only'); expect(scanWarnings[0]).toMatch(/零根.*运行宿主/);
    const { service, global } = workbenchEnablement(snapshot.extensions.map(value => m.toExtension(value)));
    await Promise.resolve();
    const enabled = m.checkEnabledAndProposedAPI(new m.NullLogService(), service, { updateEnabledApiProposals() {} }, snapshot.extensions, false);
    const registry = new m.ExtensionDescriptionRegistry(reader, [...enabled], true), running = tracker(registry, scanKinds);
    try {
      expect(service.getEnablementStates(snapshot.extensions.map(value => m.toExtension(value))))
        .toEqual([...Array(6).fill(m.EnablementState.EnabledGlobally), m.EnablementState.DisabledByExtensionKind]);
      expect(enabled.map(value => value.name)).toEqual(['browser', 'hybrid', 'node-only', 'theme', 'grammar', 'snippet']);
      running.setRemoteViews(snapshot.views); running.initializeRunningLocation([], enabled);
      expect(running.remoteRunningLocations).toEqual([]); expect(running.targetWebRunningLocations).toHaveLength(1);
      expect(running.targetWebRunningLocations[0].workspaceIds).toEqual([]);
      for (const value of snapshot.extensions) {
        if (value.name === 'node-only' || value.name === 'no-web') {
          expect(running.getRunningLocation(value)).toBeNull(); expect(value.extensionLocation.scheme).toBe('file');
        } else {
          expect(running.getRunningLocation(value).equals(running.targetWebRunningLocations[0])).toBe(true);
          expect(value.extensionLocation.toString()).toBe(view.webHost.packageUri(value.extensionKey, value.identifier.value).toString());
          expect(value.extensionLocation.toString()).toContain('/api/gateway/extension-packages/content/gateway-a/' + targetA.backendId
            + '/native-default/default/empty-web-generation/' + value.extensionKey + '/' + value.identifier.value + '/');
        }
      }
      const contributions = new m.ExtensionDescriptionRegistry(reader, running.filterByExtensionHostKind(enabled, 2), true);
      try {
        expect(contributions.getWindowExtensionDescriptions().map(value => value.name)).toEqual(['browser', 'grammar', 'hybrid', 'snippet', 'theme']);
        for (const name of ['theme', 'grammar', 'snippet']) {
          const value = contributions.getExtensionDescription('probe.' + name, targetA);
          expect(value.main).toBeUndefined(); expect(value.browser).toBeUndefined();
          expect(value.contributes).toEqual(entries.find(entry => entry.name === name).contributes);
        }
      } finally { contributions.dispose(); }
    } finally { registry.dispose(); service.dispose(); global.dispose(); }
    receipt.scans.push({ name: '空根唯一域的原Web资格、纯声明式启用及窗口贡献', requests: scannedRequests(), opens, warnings: [...scanWarnings] });
  } finally {
    scanLifetime.clear();
    for (const [name, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name];
    }
  }
});

test('空根仅安装权限保留空view，目录之后零库存及零宿主准入', async () => {
  requests.length = 0; scanWarnings.length = 0;
  request = async (path, options) => {
    if (path !== targetListPath) throw new Error('仅安装权限空根不得读取启动库存或进入宿主');
    return directoryReply(JSON.parse(options.body), installationOnly);
  };
  const snapshot = await scan(context([]));
  expect(snapshot).toEqual({ views: [{ installationTarget: targetA, viewId: 'default', workspaceIds: [], capabilities: installationOnly }], extensions: [] });
  expect(scannedRequests()).toEqual([{ path: targetListPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId } }]);
  expect(scanWarnings).toEqual([]);
  const registry = new m.ExtensionDescriptionRegistry(reader, [], true), running = tracker(registry);
  try {
    running.setRemoteViews(snapshot.views); running.initializeRunningLocation([], snapshot.extensions);
    expect(running.remoteRunningLocations).toEqual([]); expect(running.targetWebRunningLocations).toEqual([]);
  } finally { registry.dispose(); }
  receipt.scans.push({ name: '空根仅安装权限', requests: scannedRequests(), views: snapshot.views });
});

test('空根合法无管理组件目录明确诊断，零库存或宿主请求且返回空目标清单', async () => {
  requests.length = 0; scanWarnings.length = 0;
  request = async (path, options) => {
    if (path !== targetListPath) throw new Error('无管理组件不能继续库存或宿主入口');
    const reply = directoryReply(JSON.parse(options.body)); reply.targets = []; return reply;
  };
  expect(await scan(context([]))).toEqual({ views: [], extensions: [] });
  expect(scannedRequests()).toEqual([{ path: targetListPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId } }]);
  expect(scanWarnings).toHaveLength(1); expect(scanWarnings[0]).toMatch(/扩展|目标|安装/);
  receipt.scans.push({ name: '空根合法无管理组件', requests: scannedRequests(), warnings: [...scanWarnings] });
});

for (const invalid of ['multiple', 'protocol', 'gateway', 'source', 'target-protocol', 'target-gateway', 'target-source', 'backend', 'capabilities', 'missing-capabilities', 'denied']) test(`空根目录${invalid}拒绝，不回退工作区或管理标题目标`, async () => {
  requests.length = 0;
  request = async (path, options) => {
    if (path !== targetListPath) throw new Error('非法空根目录不能继续其它入口');
    const reply = directoryReply(JSON.parse(options.body)), target = reply.targets[0];
    if (invalid === 'multiple') reply.targets.push({ ...target, backend_id: targetB.backendId });
    if (invalid === 'protocol') reply.protocol_version = 2;
    if (invalid === 'gateway') reply.gateway_id = targetB.gatewayId;
    if (invalid === 'source') reply.source = 'cached';
    if (invalid === 'target-protocol') target.protocol_version = 2;
    if (invalid === 'target-gateway') target.gateway_id = targetB.gatewayId;
    if (invalid === 'target-source') target.source = 'cached';
    if (invalid === 'backend') target.backend_id = '';
    if (invalid === 'capabilities') target.capabilities = { installation_manage: true, host_manage: 'true' };
    if (invalid === 'missing-capabilities') delete target.capabilities;
    if (invalid === 'denied') target.capabilities = { installation_manage: false, host_manage: false };
    return reply;
  };
  await expect(scan(context([]))).rejects.toThrow();
  expect(scannedRequests()).toEqual([{ path: targetListPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId } }]);
  receipt.scans.push({ name: '空根拒绝-' + invalid, requests: scannedRequests() });
});

for (const status of ['offline', 403, 503]) test(`空根目录${status}保留原错误，零库存和零Node解析`, async () => {
  requests.length = 0;
  const error = Object.assign(new Error('empty directory ' + status + ': original-entry-request'), { status });
  request = async () => { throw error; };
  await expect(scan(context([]))).rejects.toBe(error);
  expect(scannedRequests()).toEqual([{ path: targetListPath, method: 'POST', body: { protocol_version: 1, gateway_id: targetA.gatewayId } }]);
  receipt.checks.push('空根目录' + status + '不吞成无组件目录，不回退其它Gateway、工作区或Node宿主');
});

test('原 CommandsRegistry 单入口按编辑器／原host冻结调用，dispose仅撤自己的generation', async () => {
  const a = extension(targetA), b = extension(targetB), hosts = [a, b].map((extension, index) => ({
    runningLocation: new m.RemoteRunningLocation(extension.installationTarget, 'default', [index ? 'b1' : 'a1']),
    extensions: new m.ExtensionHostExtensions(1, [extension], [extension.identifier]),
  }));
  let live = [...hosts], picks = 0;
  const service = { getExtensionHosts: () => live, getHostExtensions: host => host.extensions.allExtensions,
    whenInstalledExtensionsRegistered: async () => true }, calls = [];
  const registrations = hosts.map((host, index) => m.registerTargetCommand(service, host, 'probe.shared', async () => { calls.push(index); return index; }));
  const picker = { pick: async values => { picks++; return values[1]; } };
  const resource = m.workspaceFileUri({ gatewayId: targetB.gatewayId, workspaceId: 'b1' }, 'root', 'root');
  try {
    const captured = await m.captureCommandHost(service, 'probe.shared', () => resource, () => picker);
    expect(captured).toBe(hosts[1]);
    await m.executeCommandForHost(m.CommandsRegistry.getCommand('probe.shared'), { get: () => ({ activeEditor: undefined }) }, [], captured);
    expect(calls).toEqual([1]); expect(picks).toBe(0);
    expect(await m.captureCommandHost(service, 'probe.shared', () => undefined, () => picker, hosts[1])).toBe(hosts[1]);
    expect(await m.captureCommandHost(service, 'probe.shared', () => undefined, () => picker)).toBe(hosts[1]); expect(picks).toBe(1);
    registrations[0].dispose(); expect(m.CommandsRegistry.getCommand('probe.shared')).toBeDefined();
    await m.executeCommandForHost(m.CommandsRegistry.getCommand('probe.shared'), { get: () => ({ activeEditor: undefined }) }, [], captured);
    live = [];
    await expect(m.executeCommandForHost(m.CommandsRegistry.getCommand('probe.shared'), { get: () => ({ activeEditor: undefined }) }, [], captured)).rejects.toThrow('关闭');
    expect(calls).toEqual([1, 1]);
  } finally { registrations.forEach(registration => registration.dispose()); }
  expect(m.CommandsRegistry.getCommand('probe.shared')).toBeUndefined(); receipt.checks.push('单窗口原命令入口、发起目标冻结、无上下文明确选择、来源撤销及旧代际拒绝');
});

test('普通目标命令从发起宿主的依赖可见集路由，不触达其它目标同名扩展', async () => {
  const dependencyA = extension(targetA, '1.0.0', { identifier: id('probe.dep'), contributes: { commands: [
    { command: 'probe.dependencyCommand', title: '依赖命令' },
  ] } });
  const dependencyB = { ...dependencyA, installationTarget: targetB, extensionLocation: m.URI.file('/extensions/B/dep') };
  const root = extension(targetA, '1.0.0', { identifier: id('probe.root'), contributes: undefined,
    extensionDependencies: ['probe.dep'] });
  const source = {
    runningLocation: new m.RemoteRunningLocation(targetA, 'default', ['a1']),
    extensions: new m.ExtensionHostExtensions(1, [root, dependencyA], [root.identifier]),
  };
  const target = {
    runningLocation: new m.RemoteRunningLocation(targetA, 'default', ['a1']),
    extensions: new m.ExtensionHostExtensions(1, [root, dependencyA], [dependencyA.identifier]),
  };
  const foreign = {
    runningLocation: new m.RemoteRunningLocation(targetB, 'default', ['b1']),
    extensions: new m.ExtensionHostExtensions(1, [dependencyB], [dependencyB.identifier]),
  };
  const hosts = [source, target, foreign], calls = [];
  const service = { getExtensionHosts: () => hosts, getHostExtensions: host => {
    if (host === source) return [root];
    return host.extensions.allExtensions;
  }, whenInstalledExtensionsRegistered: async () => true };
  const registrations = [
    m.registerTargetCommand(service, target, 'probe.dependencyCommand', async (...args) => { calls.push({ host: 'A', args }); return 'A'; }),
    m.registerTargetCommand(service, foreign, 'probe.dependencyCommand', async (...args) => { calls.push({ host: 'B', args }); return 'B'; }),
  ];
  try {
    const selected = await m.captureCommandHost(service, 'probe.dependencyCommand', () => undefined,
      () => { throw new Error('唯一合法依赖目标不应弹出选择'); }, source);
    expect(selected).toBe(target);
    const result = await m.executeCommandForHost(m.CommandsRegistry.getCommand('probe.dependencyCommand'),
      { get: () => ({ activeEditor: undefined }) }, ['opaque', { value: 7 }], selected);
    expect(result).toBe('A'); expect(calls).toEqual([{ host: 'A', args: ['opaque', { value: 7 }] }]);
  } finally { registrations.forEach(registration => registration.dispose()); }
  receipt.checks.push('普通扩展命令只路由到发起宿主可见且同目标的依赖实例');
});

test('首次命令在宿主尚未绑定时从原 Registry 与 Tracker 选择目标', async () => {
  const a = extension(targetA), b = extension(targetB, '2.0.0');
  const registry = new m.ExtensionDescriptionRegistry(reader, [a, b], true), running = tracker(registry);
  running.setRemoteViews([targetA, targetB].map((installationTarget, index) => ({ installationTarget,
    viewId: 'default', workspaceIds: [index ? 'b1' : 'a1'], capabilities: fullCapabilities, nodeRuntime: { channel_state: 'available' } })));
  running.initializeRunningLocation([], [a, b]);
  let live = running.remoteRunningLocations.map(runningLocation => ({ runningLocation, extensions: null })), picks = 0;
  const hosts = [...live];
  const service = { getExtensionHosts: () => live, whenInstalledExtensionsRegistered: async () => true,
    getHostExtensions: host => live.includes(host) ? running.filterByRunningLocation(registry.getAllExtensionDescriptions(), host.runningLocation) : [] };
  try {
    const picked = await m.captureCommandHost(service, 'probe.shared', () => undefined, () => ({ pick: async values => {
      picks++; expect(values.map(value => value.host)).toEqual(hosts); return values[1];
    } }));
    expect(picked).toBe(hosts[1]); expect(picks).toBe(1);
    const resource = m.workspaceFileUri({ gatewayId: targetA.gatewayId, workspaceId: 'a1' }, 'root', 'root');
    expect(await m.captureCommandHost(service, 'probe.shared', () => resource, () => { throw new Error('已有资源目标不应弹选择'); })).toBe(hosts[0]);
    await expect(m.captureCommandHost(service, 'probe.shared', () => undefined, () => ({ pick: async values => {
      live = []; return values[0];
    } }))).rejects.toThrow('关闭');
  } finally { registry.dispose(); }
  receipt.checks.push('首次命令在绑定前使用原registry运行位置，明确选择／编辑器目标及等待中代际撤销');
});

test('原 CommandService 经真实 DI 构造不提前依赖 QuickInput 或 Editor，启动内置命令可执行', async () => {
  let pickerCreations = 0, editorCreations = 0;
  class QuickInput { constructor(commandService) { this.commands = commandService; pickerCreations++; } }
  class Editor { constructor(commandService) { this.commands = commandService; editorCreations++; } }
  m.ICommandService(QuickInput, undefined, 0); m.ICommandService(Editor, undefined, 0);
  const extensions = { getExtensionHosts: () => [], getHostExtensions: () => [], whenInstalledExtensionsRegistered: async () => true,
    activationEventIsDone: () => true, activateByEvent: async () => {} };
  const services = new m.ServiceCollection([m.ICommandService, new m.SyncDescriptor(m.CommandService)],
    [m.IQuickInputService, new m.SyncDescriptor(QuickInput)], [m.IEditorService, new m.SyncDescriptor(Editor)],
    [m.IExtensionService, extensions], [m.ILogService, new m.NullLogService()]);
  const instantiation = new m.InstantiationService(services, true);
  const registration = m.CommandsRegistry.registerCommand('probe.startup', () => 'started');
  try {
    const service = instantiation.invokeFunction(accessor => accessor.get(m.ICommandService));
    expect(pickerCreations).toBe(0); expect(editorCreations).toBe(0);
    expect(await service.executeCommand('probe.startup')).toBe('started');
    expect(pickerCreations).toBe(0); expect(editorCreations).toBe(0);
  } finally { registration.dispose(); instantiation.dispose(); }
  receipt.checks.push('原 InstantiationService 实际构造包含回指CommandService的UI服务，命令service不形成DI环；启动内置命令不加载无关UI');
});

test('typed URI 沿认证根往返，嵌套根最长匹配，未知文件与同路径多身份明确拒绝', () => {
  const binding = { gateway_id: targetA.gatewayId, backend_id: targetA.backendId, workspace_ids: ['a1'], initialization: { os: 'linux', logs_location_uri: 'file:///host/logs',
    environment: { app_root_uri: 'file:///host/app', global_storage_home_uri: 'file:///host/global', workspace_storage_home_uri: 'file:///host/workspace', profile_id: 'profile-target',
      remote_settings: { native_uri: 'file:///host/profile/settings.json', resource_uri: 'boxteam://gateway-a/gateway/gateway-a/resources/config/extension-host/backend-a/profiles/profile-target/settings' } },
    workspace: { folders: [{ workspace_id: 'a1', name: 'A1', resource_uri: 'boxteam://gateway-a/workspace/a1/files/root', native_uri: 'file:///work/a' }] } } };
  const transformer = new m.TargetUriTransformer(binding, () => [extension(targetA)]);
  const projected = m.URI.revive(transformer.transformIncoming(m.URI.file('/work/a/src/file.ts').toJSON()));
  expect(projected.scheme).toBe('boxteam-file');
  expect(m.URI.revive(transformer.transformIncoming(projected.toJSON())).toString()).toBe(projected.toString());
  const foreignGateway = projected.with({ authority: targetB.gatewayId });
  const foreignWorkspace = m.workspaceFileUri({ gatewayId: targetA.gatewayId, workspaceId: 'a2' }, 'root', 'root');
  expect(() => transformer.transformIncoming(foreignGateway.toJSON())).toThrow('已绑定工作区视图');
  expect(() => transformer.transformIncoming(foreignWorkspace.toJSON())).toThrow('已绑定工作区视图');
  expect(() => m.parseJsonAndRestoreBufferRefs(JSON.stringify({ resource: foreignGateway }), [], transformer)).toThrow('已绑定工作区视图');
  expect(m.parseJsonAndRestoreBufferRefs(JSON.stringify({ text: foreignGateway.toString() }), [], transformer)).toEqual({ text: foreignGateway.toString() });
  const custom = m.URI.parse('probe-custom:/resource').toJSON();
  expect(transformer.transformIncoming(custom)).toBe(custom);
  expect(m.URI.revive(transformer.transformOutgoing(projected.toJSON())).toString()).toBe(m.URI.file('/work/a/src/file.ts').toString());
  expect(() => transformer.transformIncoming(m.URI.file('/other/secret').toJSON())).toThrow('超出');
  binding.initialization.workspace.folders.push({ workspace_id: 'a2', name: 'nested', resource_uri: 'boxteam://gateway-a/workspace/a2/files/root', native_uri: 'file:///work/a/src' });
  const nested = new m.TargetUriTransformer(binding, () => []);
  expect(m.URI.revive(nested.transformIncoming(m.URI.file('/work/a/src/file.ts').toJSON())).path).toContain('/a2/');
  binding.initialization.workspace.folders.push({ ...binding.initialization.workspace.folders[1], workspace_id: 'a3' });
  expect(() => new m.TargetUriTransformer(binding, () => []).transformIncoming(m.URI.file('/work/a/src/file.ts').toJSON())).toThrow('多个业务工作区');
  receipt.checks.push('typed URI认证根往返、incoming业务文件跨Gateway/工作区拒绝、其他scheme保持、最长匹配、未知native路径与多身份歧义拒绝');
});

test('typed URI 使用目标 Windows 盘符、目录及 UNC 大小写语义，逻辑身份和 Linux 路径保持严格', () => {
  const binding = { gateway_id: targetA.gatewayId, backend_id: targetA.backendId, workspace_ids: ['DriveView', 'ShareView'], initialization: {
    os: 'windows', logs_location_uri: 'file:///C:/Host/Logs',
    environment: { app_root_uri: 'file:///C:/Host/App', global_storage_home_uri: 'file:///C:/Host/Global', workspace_storage_home_uri: 'file:///C:/Host/Workspace', profile_id: 'profile-target',
      remote_settings: { native_uri: 'file:///C:/Host/Profile/settings.json', resource_uri: 'boxteam://gateway-a/gateway/gateway-a/resources/config/extension-host/backend-a/profiles/profile-target/settings' } },
    workspace: { folders: [
      { workspace_id: 'DriveView', name: 'Drive', resource_uri: 'boxteam://workspace/DriveView/resources/files/root', native_uri: 'file:///C:/Work/Project' },
      { workspace_id: 'ShareView', name: 'Share', resource_uri: 'boxteam://workspace/ShareView/resources/files/root', native_uri: 'file://BuildServer/Share/Project' },
    ] } } };
  const transformer = new m.TargetUriTransformer(binding, () => []);
  for (const [index, value, expected] of [
    [0, 'file:///c:/work/project/Src/Case.ts', 'file:///C:/Work/Project/Src/Case.ts'],
    [1, 'file://buildserver/share/project/Src/Case.ts', 'file://BuildServer/Share/Project/Src/Case.ts'],
  ]) {
    const projected = m.URI.revive(transformer.transformIncoming(m.URI.parse(value).toJSON()));
    expect(projected.toString()).toBe(m.URI.joinPath(transformer.folders[index].uri, 'Src/Case.ts').toString());
    expect(m.URI.revive(transformer.transformOutgoing(projected.toJSON())).toString()).toBe(m.URI.parse(expected).toString());
    expect(() => transformer.transformOutgoing(projected.with({ authority: targetA.gatewayId.toUpperCase() }).toJSON())).toThrow('明确工作区视图');
    expect(() => transformer.transformIncoming(projected.with({ path: projected.path.replace('View', 'view') }).toJSON())).toThrow('已绑定工作区视图');
  }
  const home = m.URI.parse('file:///c:/host/app/runtime.js').toJSON();
  expect(transformer.transformIncoming(home)).toBe(home);
  for (const value of ['file:///D:/Work/Project/file.ts', 'file:///C:/Work/ProjectElse/file.ts',
    'file://OtherServer/Share/Project/file.ts', 'file://BuildServer/OtherShare/Project/file.ts', 'file:///C:/Work/Project/../../outside.ts']) {
    expect(() => transformer.transformIncoming(m.URI.parse(value).toJSON())).toThrow('超出');
  }
  binding.initialization.workspace.configuration = {
    native_uri: 'file:///C:/Host/View/workspace.code-workspace',
    resource_uri: 'boxteam://gateway-a/gateway/gateway-a/resources/config/extension-host/backend-a/views/view-a/workspace',
  };
  const configured = new m.TargetUriTransformer(binding, () => []);
  const configuration = m.URI.parse(binding.initialization.workspace.configuration.resource_uri);
  const stored = [{ path: '../../Work/Project', name: 'Custom Drive' }, { path: '\\\\buildserver\\share\\project', name: 'Custom Share' }];
  const folders = configured.workspaceFolders(stored, configuration);
  expect(folders.map(folder => folder.uri.toString())).toEqual(configured.folders.map(folder => folder.uri.toString()));
  expect(folders.map(folder => folder.name)).toEqual(['Custom Drive', 'Custom Share']);
  expect(folders[0].raw).toEqual(stored[0]);
  expect(() => configured.workspaceFolders([...stored].reverse(), configuration)).toThrow('重新挂接');
  expect(() => configured.workspaceFolders([{ path: 'D:\\Other' }, stored[1]], configuration)).toThrow('重新挂接');
  expect(configured.transformOutgoingURI(configuration).toString()).toBe(m.URI.parse(binding.initialization.workspace.configuration.native_uri).toString());
  expect(m.URI.revive(configured.transformIncoming(m.URI.parse(binding.initialization.workspace.configuration.native_uri))).toString()).toBe(configuration.toString());
  const linux = new m.TargetUriTransformer({ ...binding, initialization: { ...binding.initialization, os: 'linux',
    workspace: { folders: [{ ...binding.initialization.workspace.folders[0], native_uri: 'file:///Work/Project' }] } } }, () => []);
  const linuxFile = m.URI.parse('file:///Work/Project/Src/Case.ts');
  const projected = linux.transformIncoming(linuxFile.toJSON());
  expect(m.URI.revive(linux.transformOutgoing(projected)).toString()).toBe(linuxFile.toString());
  expect(() => linux.transformIncoming(m.URI.parse('file:///work/project/Src/Case.ts').toJSON())).toThrow('超出');
  receipt.checks.push('实际运行面' + process.platform + '：Windows盘符/目录及UNC大小写匹配并保留子路径，宿主目录同语义；跨驱动器/共享/越界拒绝，逻辑身份和Linux大小写严格');
  receipt.checks.push('原toWorkspaceFolders在目标Windows语义下解析相对盘符/UNC，保留raw及名称；视图根重排/替换拒绝，配置typed URI往返');
});

afterAll(async () => {
  try {
    const after = await fingerprint(), drift = sourcePaths.filter(path => fingerprints[path] !== after[path]);
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ ...receipt, fingerprints, fingerprintsAfter: after, drift }, null, 2));
    if (drift.length) throw new Error('验证期间源码改变：' + drift.join(', '));
  } finally { scanLifetime.dispose(); delete globalThis.__extensionInventoryBoundary; await run.close(); }
});
