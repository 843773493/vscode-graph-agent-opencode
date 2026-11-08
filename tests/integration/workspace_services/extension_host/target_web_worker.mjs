import assert from 'node:assert/strict';
import { copyFile, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';

const spec = JSON.parse(await readFile(process.argv[2], 'utf8'));
const root = resolve('src/clients/workbench'), output = join(spec.artifacts, 'browser');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: spec.commit, version: spec.version }, []);
const options = { bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' };
await build({ ...options, entryPoints: [join(root, 'upstream/src/vs/workbench/api/worker/extensionHostWorkerMain.ts')], outfile: join(output, 'worker.mjs') });
await copyFile(join(root, 'upstream/src/vs/workbench/services/extensions/worker/webWorkerExtensionHostIframe.html'), join(output, 'iframe.html'));
await writeFile(join(output, 'index.html'), '<!doctype html><title>真实目标 Web Worker</title>');
await build({ ...options, stdin: { resolveDir: resolve('.'), contents: `
export { TargetWebExtensionHost } from './src/clients/workbench/src/extensions/webHost.js';
export { TargetWebHostConnection } from './src/clients/workbench/src/extensions/webConnection.js';
export { scanTargetExtensions } from './src/clients/workbench/src/extensions/inventory.js';
export { targetConfigurationRequest } from './src/clients/workbench/src/extensions/targetContext.js';
export { workspaceFileUri } from './src/clients/workbench/src/workspaceFiles.js';
export { gateway } from './src/clients/workbench/src/agentApi.js';
export { MainThreadConfiguration } from '${upstream}workbench/api/browser/mainThreadConfiguration.js';
export { MainThreadWorkspace } from '${upstream}workbench/api/browser/mainThreadWorkspace.js';
export { MainThreadFileSystem } from '${upstream}workbench/api/browser/mainThreadFileSystem.js';
export { MainThreadStorage } from '${upstream}workbench/api/browser/mainThreadStorage.js';
export { ExtensionStorageService } from '${upstream}platform/extensionManagement/common/extensionStorage.js';
export { ExtensionHostExtensions, ExtensionHostStartup } from '${upstream}workbench/services/extensions/common/extensions.js';
export { LocalWebWorkerRunningLocation } from '${upstream}workbench/services/extensions/common/extensionRunningLocation.js';
export { ExtHostContext, MainContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { IEnvironmentService } from '${upstream}platform/environment/common/environment.js';
export { IConfigurationService } from '${upstream}platform/configuration/common/configuration.js';
export { Extensions, ConfigurationScope } from '${upstream}platform/configuration/common/configurationRegistry.js';
export { Registry } from '${upstream}platform/registry/common/platform.js';
export { IWorkspaceContextService } from '${upstream}platform/workspace/common/workspace.js';
export { IWorkspaceTrustManagementService } from '${upstream}platform/workspace/common/workspaceTrust.js';
export { IFileService } from '${upstream}platform/files/common/files.js';
export { IUriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentity.js';
export { IPolicyService, NullPolicyService } from '${upstream}platform/policy/common/policy.js';
export { ILogService, NullLogService } from '${upstream}platform/log/common/log.js';
export { IUserDataProfilesService } from '${upstream}platform/userDataProfile/common/userDataProfile.js';
export { IUserDataProfileService } from '${upstream}workbench/services/userDataProfile/common/userDataProfile.js';
export { IBrowserWorkbenchEnvironmentService } from '${upstream}workbench/services/environment/browser/environmentService.js';
export { IRemoteAgentService } from '${upstream}workbench/services/remote/common/remoteAgentService.js';
export { IEditorGroupsService } from '${upstream}workbench/services/editor/common/editorGroupsService.js';
export { IPathService } from '${upstream}workbench/services/path/common/pathService.js';
export { InMemoryStorageService } from '${upstream}platform/storage/common/storage.js';
export { URI } from '${upstream}base/common/uri.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event } from '${upstream}base/common/event.js';
` }, outfile: join(output, 'main.mjs') });

const browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
  headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const context = await browser.newContext();
const [cookieName, ...cookieValue] = spec.cookie.split('=');
await context.addCookies([{ name: cookieName, value: cookieValue.join('='), url: spec.url, httpOnly: true, sameSite: 'Lax' }]);
const page = await context.newPage(), consoleMessages = [], requests = [];
page.on('console', message => consoleMessages.push({ type: message.type(), text: message.text() }));
page.on('pageerror', error => consoleMessages.push({ type: 'pageerror', text: error.stack }));
page.on('request', request => requests.push({ method: request.method(), url: request.url() }));
try {
  await page.goto(spec.url + '/worker-fixture/index.html');
  const result = await page.evaluate(async spec => {
    globalThis._VSCODE_FILE_ROOT = location.origin + '/worker-fixture/';
    const m = await import('/worker-fixture/main.mjs'), lifetime = new m.DisposableStore();
    globalThis.workerProbe = { lifetime, hosts: [] };
    const logs = new m.NullLogService(), files = lifetime.add(new m.FileService(logs));
    for (const scheme of ['vscode-userdata', 'boxteam-file']) files.registerProvider(scheme, lifetime.add(new m.InMemoryFileSystemProvider()));
    const identity = lifetime.add(new m.UriIdentityService(files)), storage = lifetime.add(new m.InMemoryStorageService());
    const profile = { id: 'browser-default', isDefault: true, settingsResource: m.URI.parse('vscode-userdata:/profile/settings.json'),
      tasksResource: m.URI.parse('vscode-userdata:/profile/tasks.json'), mcpResource: m.URI.parse('vscode-userdata:/profile/mcp.json'),
      globalStorageHome: m.URI.parse('vscode-userdata:/global') };
    await files.createFolder(m.URI.parse('vscode-userdata:/profile'));
    const environment = { cacheHome: m.URI.parse('vscode-userdata:/cache'), extHostLogsPath: m.URI.parse('vscode-userdata:/logs'),
      workspaceStorageHome: m.URI.parse('vscode-userdata:/workspace'), options: {}, isBuilt: false, debugRenderer: false };
    const trust = { isWorkspaceTrusted: () => true, onDidChangeTrust: m.Event.None, onDidChangeTrustedFolders: m.Event.None };
    const services = new m.ServiceCollection([m.IFileService, files], [m.IUriIdentityService, identity], [m.ILogService, logs],
      [m.IBrowserWorkbenchEnvironmentService, environment], [m.IEnvironmentService, environment],
      [m.IUserDataProfileService, { currentProfile: profile, onDidChangeCurrentProfile: m.Event.None }],
      [m.IUserDataProfilesService, { defaultProfile: profile }], [m.IPolicyService, new m.NullPolicyService()],
      [m.IRemoteAgentService, { getEnvironment() { throw new Error('目标 Web 不得使用窗口 RemoteAgent'); } }],
      [m.IWorkspaceTrustManagementService, trust], [m.IEditorGroupsService, {}], [m.IPathService, {}]);
    const parent = lifetime.add(new m.InstantiationService(services, true));
    m.Registry.as(m.Extensions.Configuration).registerConfiguration({ id: 'probe', properties: {
      'probe.remote': { type: 'string', default: 'unset', scope: m.ConfigurationScope.MACHINE },
    } });
    const folderUris = spec.targets.map(target => m.workspaceFileUri({ gatewayId: target.gatewayId, workspaceId: target.workspaceId }, target.root, target.root));
    const snapshot = await m.scanTargetExtensions({ getCompleteWorkspace: async () => ({ folders: folderUris.map(uri => ({ uri })) }) },
      extension => extension.extensionKind ?? ['web'], lifetime, spec.targets[0].gatewayId, logs);
    if (snapshot.views.length !== 2 || snapshot.extensions.some(extension => extension.main)) throw new Error('目标库存没有得到双 Web-only 实例');
    const errors = [], calls = [], notifications = { error: error => errors.push(String(error)) }, labels = { getWorkspaceLabel: workspace => workspace.name ?? workspace.id };
    const product = { version: spec.version, commit: spec.commit, nameLong: 'Worker probe', urlProtocol: 'boxteam', quality: 'stable' };
    const storageOwner = lifetime.add(new m.ExtensionStorageService(storage, product, logs));
    const make = async (view, label, descriptions) => {
      const store = lifetime.add(new m.DisposableStore()), connection = view.webHost.connection;
      const targetContext = { kind: 'web', connection }, request = m.targetConfigurationRequest(targetContext);
      await m.gateway.request('/api/gateway/extension-configurations/write', { method: 'POST', body: JSON.stringify({ ...request,
        resource_uri: connection.context.remote_settings.resource_uri,
        content_base64: btoa(JSON.stringify({ 'probe.remote': label + '-remote' })),
        options: { create: true, overwrite: true, unlock: false, atomic: false, append: false } }) });
      const location = new m.LocalWebWorkerRunningLocation(0, view.installationTarget, view.viewId, view.workspaceIds);
      const extensions = new m.ExtensionHostExtensions(1, descriptions, descriptions.map(extension => extension.identifier));
      const host = store.add(new m.TargetWebExtensionHost(location, m.ExtensionHostStartup.EagerManualStart, { getInitData: async () => ({ extensions }) },
        view.webHost, parent, notifications, { sessionId: label, machineId: 'probe', sqmId: 'probe', firstSessionDate: '2026-11-07' },
        { getWorkspace: () => ({ id: 'window', folders: [] }), getWorkbenchState: () => 1 }, labels, logs,
        { getRegisteredLoggers: () => [] }, environment, { defaultProfile: profile }, product, { mainContainer: document.body }, storage,
        { getWorkerUrl: descriptor => locationOrigin(descriptor.label === 'webWorkerExtensionHostIframe' ? 'iframe.html' : 'worker.mjs') },
        { defaultLogLevels: { extensions: [] } }));
      function locationOrigin(file) { return globalThis.location.origin + '/worker-fixture/' + file; }
      const rpc = store.add(new m.RPCProtocol(await host.start())), child = host.customerInstantiationService;
      const extContext = { extensionHost: host, getProxy: id => rpc.getProxy(id), set: (id, actor) => rpc.set(id, actor) };
      const register = (actor, methods) => rpc.set(m.MainContext[actor], methods);
      const notify = (actor, names) => register(actor, Object.fromEntries(names.map(name => [name, (...args) => calls.push({ label, actor, name })])));
      notify('MainThreadLogger', ['$registerLogger', '$deregisterLogger', '$flush', '$log', '$createLogger', '$setVisibility']);
      notify('MainThreadTelemetry', ['$publicLog2', '$publicLog']);
      notify('MainThreadConsole', ['$logExtensionHostMessage']);
      notify('MainThreadTerminalService', ['$registerProcessSupport']);
      notify('MainThreadSearch', ['$registerTextSearchProvider']);
      notify('MainThreadTask', ['$registerSupportedExecutions', '$registerTaskSystem']);
      notify('MainThreadDebugService', ['$registerDebugTypes']);
      notify('MainThreadCommands', ['$registerCommand', '$unregisterCommand']);
      register('MainThreadWindow', { $getInitialState: () => ({ isFocused: true, isActive: true }) });
      register('MainThreadLanguageModelTools', { $getTools: () => [] });
      register('MainThreadSecretState', Object.fromEntries(['$getPassword', '$setPassword', '$deletePassword', '$getKeys'].map(name => [name, () => { calls.push({ label, actor: 'MainThreadSecretState', name }); throw new Error('秘密不应进入主线程'); }])));
      register('MainThreadErrors', { $onUnexpectedError: error => errors.push(error) });
      register('MainThreadExtensionService', {
        $onWillActivateExtension() {}, $onDidActivateExtension() {}, $setPerformanceMarks() {},
        $onExtensionActivationError: (...args) => errors.push(args), $onExtensionRuntimeError: (...args) => errors.push(args),
        $asBrowserUri: uri => uri,
      });
      register('MainThreadConfiguration', store.add(child.createInstance(m.MainThreadConfiguration, extContext)));
      register('MainThreadFileSystem', store.add(new m.MainThreadFileSystem(extContext, files)));
      register('MainThreadStorage', store.add(new m.MainThreadStorage(extContext, storageOwner, storage, child, logs)));
      const workspace = child.invokeFunction(accessor => accessor.get(m.IWorkspaceContextService));
      register('MainThreadWorkspace', store.add(new m.MainThreadWorkspace(extContext, {}, workspace, {}, {}, {}, {}, notifications,
        {}, child, labels, environment, files, trust, {}, {})));
      const extension = descriptions[0].identifier, service = rpc.getProxy(m.ExtHostContext.ExtHostExtensionService);
      await service.$startExtensionHost({ versionId: 1, toAdd: [], toRemove: [], addActivationEvents: {}, myToAdd: [], myToRemove: [] });
      if (!await service.$activate(extension, { startup: false, extensionId: extension, activationEvent: 'api' })) throw new Error('原 Worker 激活没有成功');
      const inspect = (...args) => rpc.getProxy(m.ExtHostContext.ExtHostCommands).$executeContributedCommand('boxteam.browser.inspect', ...args);
      const value = { host, store, inspect, connection }; globalThis.workerProbe.hosts.push(value); return value;
    };
    const [viewA, viewB] = spec.targets.map(target => snapshot.views.find(view => view.installationTarget.backendId === target.backendId));
    const descriptions = view => snapshot.extensions.filter(extension => extension.installationTarget.backendId === view.installationTarget.backendId);
    const a = await make(viewA, 'A', descriptions(viewA)), b = await make(viewB, 'B', descriptions(viewB));
    const firstA = await a.inspect('A-value'), firstB = await b.inspect();
    const afterA = await a.inspect(), afterB = await b.inspect('B-value');
    const emptyAdmission = lifetime.add(new m.TargetWebHostConnection({ ...viewA.webHost.request, view_id: 'empty-view', workspace_ids: [] }));
    await emptyAdmission.connect();
    const emptyView = { ...viewA, viewId: 'empty-view', workspaceIds: [], webHost: emptyAdmission };
    const empty = await make(emptyView, 'Empty', descriptions(viewA).map(extension => ({ ...extension,
      extensionLocation: emptyAdmission.packageUri(extension.extensionKey, extension.identifier.value) })));
    const emptyResult = await empty.inspect(); empty.store.dispose();
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = firstA.style;
    const loaded = new Promise((resolve, reject) => { link.onload = resolve; link.onerror = reject; }); document.head.append(link); await loaded;
    const asset = document.body.appendChild(document.createElement('div')); asset.className = 'worker-asset';
    const style = getComputedStyle(asset).backgroundImage;
    const upgrade = await m.gateway.request('/api/gateway/extension-installations/install', { method: 'POST', body: JSON.stringify({ protocol_version: 1,
      gateway_id: spec.targets[0].gatewayId, backend_id: spec.targets[0].backendId,
      workspace_ids: [spec.upgrade.workspace_id], source_uri: spec.upgrade.source_uri }) });
    const upgradedOldA = await a.inspect();
    const uninstall = await m.gateway.request('/api/gateway/extension-installations/uninstall', { method: 'POST', body: JSON.stringify({ protocol_version: 1,
      gateway_id: spec.targets[0].gatewayId, backend_id: spec.targets[0].backendId, profile_id: upgrade.installed.profile_id,
      extension_id: upgrade.installed.id, version: upgrade.installed.version }) });
    const uninstalledOldA = await a.inspect();
    let newAdmission;
    const retired = new m.TargetWebHostConnection({ ...viewA.webHost.request, view_id: 'after-uninstall' });
    try { await retired.connect(); } catch (error) { newAdmission = error.message; } finally { retired.dispose(); }
    a.store.dispose();
    const stillB = await b.inspect();
    let closedA;
    try { a.host.targetContext.connection; viewA.webHost.connection; } catch (error) { closedA = error.message; }
    const bindings = [a.connection, b.connection].map(value => ({ gateway_id: value.gateway_id, backend_id: value.backend_id,
      profile_id: value.profile_id, view_id: value.view_id, generation: value.generation, workspace_ids: value.workspace_ids }));
    return { firstA, firstB, afterA, afterB, emptyResult, upgradedOldA, uninstalledOldA, stillB, style, newAdmission, closedA,
      upgraded: upgrade.installed.version, uninstalled: uninstall.uninstalled.version, errors, secretCalls: calls.filter(call => call.actor === 'MainThreadSecretState'),
      bindings, typedRoots: folderUris.map(uri => uri.toString(true)), iframeCount: document.querySelectorAll('iframe.web-worker-ext-host-iframe').length };
  }, spec);
  await writeFile(join(spec.artifacts, 'worker-result.json'), JSON.stringify(result, null, 2));
  assert.equal(result.firstA.version, '1.0.0'); assert.equal(result.firstB.version, '2.0.0');
  assert.equal(result.firstA.chunk, '1.0.0'); assert.equal(result.firstB.chunk, '2.0.0');
  assert.deepEqual(result.firstA.folders, [result.typedRoots[0]]); assert.deepEqual(result.firstB.folders, [result.typedRoots[1]]);
  assert.equal(result.firstA.setting, 'A-remote'); assert.equal(result.firstB.setting, 'B-remote');
  assert.deepEqual(result.firstA.before, { global: null, workspace: null }); assert.deepEqual(result.firstB.before, { global: null, workspace: null });
  assert.deepEqual(result.afterA.before, { global: 'A-value', workspace: 'A-value' });
  assert.deepEqual(result.stillB.before, { global: 'B-value', workspace: 'B-value' });
  assert.deepEqual(result.emptyResult.folders, []); assert.equal(result.emptyResult.setting, 'Empty-remote');
  assert.deepEqual(result.emptyResult.before, { global: 'A-value', workspace: null });
  assert.equal(result.emptyResult.globalStorageUri, result.firstA.globalStorageUri); assert.notEqual(result.emptyResult.storageUri, result.firstA.storageUri);
  for (const value of [result.firstA, result.firstB]) {
    assert.match(value.extensionUri, /\/api\/gateway\/extension-packages\/content\//);
    assert.match(value.storageUri, /^vscode-userdata:/); assert.match(value.globalStorageUri, /^vscode-userdata:/);
    assert.equal(value.errors.length, 5); assert.equal(value.errors.filter(error => error.includes('SecretStorage')).length, 4);
    assert.equal(value.errors.filter(error => error.includes('setKeysForSync')).length, 1);
    assert.equal(value.extensionUri.includes('token'), false);
  }
  assert.notEqual(result.firstA.globalStorageUri, result.firstB.globalStorageUri);
  assert.notEqual(result.firstA.storageUri, result.firstB.storageUri); assert.notEqual(result.firstA.logUri, result.firstB.logUri);
  assert.equal(result.upgraded, '3.0.0'); assert.equal(result.uninstalled, '3.0.0');
  assert.equal(result.upgradedOldA.marker, '1.0.0'); assert.equal(result.uninstalledOldA.marker, '1.0.0');
  assert.match(result.newAdmission, /关闭|失效|不存在|拒绝/); assert.match(result.closedA, /关闭/);
  assert.equal(result.iframeCount, 1); assert.deepEqual(result.secretCalls, []); assert.deepEqual(result.errors, []);
  await page.waitForFunction(() => performance.getEntriesByType('resource').some(entry => entry.name.endsWith('/dist/images/icon.svg')));
  assert.match(result.style, /\/dist\/images\/icon.svg/);
  await writeFile(join(spec.artifacts, 'worker-receipt.json'), JSON.stringify({ result, requests, consoleMessages,
    boundary: '真实Gateway认证、WS准入、原Node管理库存、冻结GET、目标Python配置、原iframe/Worker/握手/RPC/MainThreadWorkspace/Configuration/FileSystem/Storage及公开扩展API；窗口UI、搜索/编辑服务、日志通知和浏览器文件存储为具名装具，不证明完整Workbench或磁盘持久性。' }, null, 2));
} finally {
  try { await page.evaluate(() => globalThis.workerProbe?.lifetime.dispose()); }
  finally {
    await writeFile(join(spec.artifacts, 'worker-browser-messages.json'), JSON.stringify({ consoleMessages, requests }, null, 2));
    await context.close(); await browser.close();
  }
}
