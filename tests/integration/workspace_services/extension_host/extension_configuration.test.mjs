import { afterAll, beforeAll, expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_configuration.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'configuration.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { targetConfigurationServices } from './src/clients/workbench/src/extensions/configuration.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
export { MainThreadConfiguration } from '${upstream}workbench/api/browser/mainThreadConfiguration.js';
export { MainThreadFileSystem } from '${upstream}workbench/api/browser/mainThreadFileSystem.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { IConfigurationService, ConfigurationTarget } from '${upstream}platform/configuration/common/configuration.js';
export { Extensions, ConfigurationScope } from '${upstream}platform/configuration/common/configurationRegistry.js';
export { Registry } from '${upstream}platform/registry/common/platform.js';
export { IWorkspaceContextService } from '${upstream}platform/workspace/common/workspace.js';
export { IWorkspaceTrustManagementService } from '${upstream}platform/workspace/common/workspaceTrust.js';
export { IFileService, toFileSystemProviderErrorCode } from '${upstream}platform/files/common/files.js';
export { IUriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentity.js';
export { IPolicyService, NullPolicyService } from '${upstream}platform/policy/common/policy.js';
export { ILogService, NullLogService } from '${upstream}platform/log/common/log.js';
export { IUserDataProfilesService } from '${upstream}platform/userDataProfile/common/userDataProfile.js';
export { IUserDataProfileService } from '${upstream}workbench/services/userDataProfile/common/userDataProfile.js';
export { IBrowserWorkbenchEnvironmentService } from '${upstream}workbench/services/environment/browser/environmentService.js';
export { IRemoteAgentService } from '${upstream}workbench/services/remote/common/remoteAgentService.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer, encodeBase64, decodeBase64 } from '${upstream}base/common/buffer.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event } from '${upstream}base/common/event.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [{ name: 'explicit-configuration-transport-boundary', setup(builder) {
    builder.onLoad({ filter: /\/extensions\/configurationFiles\.ts$/ }, async args => {
      let contents = await readFile(args.path, 'utf8');
      contents = contents.replace("import { gateway, GatewayRequestError } from '../agentApi.js';", 'const { gateway, GatewayRequestError } = globalThis.configurationTransport;');
      contents = contents.replace("import { requestGatewayResponse } from '../../../web/src/api/http.js';", 'const { requestGatewayResponse } = globalThis.configurationTransport;');
      return { contents, loader: 'ts', resolveDir: dirname(args.path) };
    });
  } }, buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });

let browser, server;
const checks = [];
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>目标配置组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('配置组件来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('配置Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({
    boundary: '真实Chromium中的原WorkspaceService、FileService、配置parser、child DI及MainThreadConfiguration；认证binding、配置HTTP/SSE与原生磁盘由具名内存装具替代，不代表Gateway、Node、真实编辑器保存或Windows原生验收。', checks,
  }, null, 2)); }
});

async function open() {
  const context = await browser.newContext(), page = await context.newPage();
  await page.goto(`http://127.0.0.1:${server.port}`);
  await page.evaluate(async () => {
    globalThis._VSCODE_FILE_ROOT = location.origin + '/';
    // 仅传输替身：两文件由真实内存provider持有，原配置服务没有额外正文或保存基线。
    const transport = globalThis.configurationTransport = { requests: [], mapping: new Map(), subscribers: [],
      GatewayRequestError: class extends Error { constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; } } };
    transport.gateway = { async request(endpoint, options) {
      options.signal.throwIfAborted();
      const body = JSON.parse(options.body); transport.requests.push({ endpoint, body });
      if (transport.failure && endpoint.endsWith(transport.failureOperation ?? '/write')) throw transport.failure;
      const native = transport.mapping.get(body.resource_uri);
      if (!native) throw new transport.GatewayRequestError(403, '未登记配置资源');
      try {
        const stat = () => transport.disk.stat(native).then(value => ({ ...value, resource_uri: body.resource_uri }));
        if (endpoint.endsWith('/stat')) return await stat();
        if (endpoint.endsWith('/read')) return { resource_uri: body.resource_uri, content_base64: transport.m.encodeBase64(transport.m.VSBuffer.wrap(await transport.disk.readFile(native))) };
        if (endpoint.endsWith('/write')) {
          await transport.disk.writeFile(native, transport.m.decodeBase64(body.content_base64).buffer, { ...body.options, atomic: false });
          return await stat();
        }
        throw new Error('未知配置操作');
      } catch (error) { if (error.code === 'EntryNotFound') throw new transport.GatewayRequestError(404, error.message); throw error; }
    } };
    transport.requestGatewayResponse = async (_port, _endpoint, options) => {
      let subscription;
      const stream = new ReadableStream({ start(controller) {
        const send = (event, value) => controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(value)}\n\n`));
        send('ready', { changes: [], overflow: true });
        subscription = { body: JSON.parse(options.body), send }; transport.subscribers.push(subscription);
        options.signal.addEventListener('abort', () => { transport.subscribers.splice(transport.subscribers.indexOf(subscription), 1); controller.close(); }, { once: true });
      }, cancel() { const index = transport.subscribers.indexOf(subscription); if (index >= 0) transport.subscribers.splice(index, 1); } });
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } });
    };
    const m = transport.m = await import('/fixture.mjs'), logs = new m.NullLogService(), files = new m.FileService(logs);
    const disk = transport.disk = new m.InMemoryFileSystemProvider();
    const ordinary = new m.InMemoryFileSystemProvider(), profile = new m.InMemoryFileSystemProvider();
    files.registerProvider('boxteam-file', ordinary); files.registerProvider('vscode-userdata', profile);
    const identity = new m.UriIdentityService(files), lifetime = new m.DisposableStore();
    lifetime.add(files); lifetime.add(identity); lifetime.add(disk); lifetime.add(ordinary); lifetime.add(profile);
    const put = async (provider, resource, value) => {
      await files.createFolder(m.URI.parse('vscode-userdata:/profile'));
      const pieces = resource.path.split('/').slice(1, -1); let parent = resource.with({ path: '/' });
      for (const piece of pieces) { parent = m.URI.joinPath(parent, piece); try { await provider.mkdir(parent); } catch (error) { if (error.code !== 'EntryExists') throw error; } }
      await provider.writeFile(resource, new TextEncoder().encode(JSON.stringify(value)), { create: true, overwrite: true, unlock: false, atomic: false });
    };
    const settings = m.URI.parse('vscode-userdata:/profile/settings.json');
    await put(profile, settings, { 'probe.value': 'local', 'probe.machine': 'local-machine' });
    const currentProfile = { id: 'web-profile', isDefault: true, settingsResource: settings,
      tasksResource: m.URI.parse('vscode-userdata:/profile/tasks.json'), mcpResource: m.URI.parse('vscode-userdata:/profile/mcp.json') };
    const services = new m.ServiceCollection([m.IFileService, files], [m.IUriIdentityService, identity], [m.ILogService, logs],
      [m.IBrowserWorkbenchEnvironmentService, { cacheHome: m.URI.parse('vscode-userdata:/cache'), options: {}, isBuilt: false }],
      [m.IUserDataProfileService, { currentProfile, onDidChangeCurrentProfile: m.Event.None }],
      [m.IUserDataProfilesService, { defaultProfile: currentProfile }], [m.IPolicyService, new m.NullPolicyService()],
      [m.IRemoteAgentService, { getEnvironment() { throw new Error('不应取得窗口RemoteAgent环境'); } }],
      [m.IWorkspaceTrustManagementService, { isWorkspaceTrusted: () => true, onDidChangeTrust: m.Event.None }]);
    const parent = lifetime.add(new m.InstantiationService(services, true));
    m.Registry.as(m.Extensions.Configuration).registerConfiguration({ id: 'probe', properties: {
      'probe.value': { type: 'string', default: 'default', scope: m.ConfigurationScope.LANGUAGE_OVERRIDABLE },
      'probe.machine': { type: 'string', default: 'machine-default', scope: m.ConfigurationScope.MACHINE },
    } });
    const hosts = new Map();
    const make = async (label, roots = [label + '1'], sharedProfile) => {
      const gatewayId = sharedProfile?.binding.gateway_id ?? 'gateway-' + label.toLowerCase(), backendId = sharedProfile?.binding.backend_id ?? 'backend-' + label;
      const config = { native_uri: `file:///hosts/${label}/view/workspace.code-workspace`, resource_uri: `boxteam://${gatewayId}/gateway/${gatewayId}/resources/config/extension-host/${backendId}/views/view-${label}/workspace` };
      const remote = sharedProfile?.remote ?? { native_uri: `file:///hosts/${label}/profile/settings.json`, resource_uri: `boxteam://${gatewayId}/gateway/${gatewayId}/resources/config/extension-host/${backendId}/profiles/default/settings` };
      const binding = { gateway_id: gatewayId, backend_id: backendId, view_id: 'default', generation: 'generation-' + label, client_id: 'client',
        workspace_ids: roots, initialization: { os: 'linux', logs_location_uri: `file:///hosts/${label}/logs`,
          environment: { profile_id: 'default', remote_settings: remote, app_root_uri: `file:///hosts/${label}/app`, global_storage_home_uri: `file:///hosts/${label}/global`, workspace_storage_home_uri: `file:///hosts/${label}/storage` },
          workspace: { id: 'view-' + label, name: label, configuration: roots.length > 1 ? config : undefined,
            folders: roots.map(workspace_id => ({ workspace_id, name: workspace_id, native_uri: `file:///work/${label}/${workspace_id}`, resource_uri: `boxteam://workspace/${workspace_id}/resources/files/root` })) } } };
      const transformer = new m.TargetUriTransformer(binding, () => []), store = lifetime.add(new m.DisposableStore());
      for (const resource of [remote, ...(roots.length > 1 ? [config] : [])]) {
        transport.mapping.set(resource.resource_uri, m.URI.parse(resource.native_uri));
        transport.mapping.set(m.URI.joinPath(m.URI.parse(resource.resource_uri), '..').toString(), m.URI.joinPath(m.URI.parse(resource.native_uri), '..'));
      }
      if (!sharedProfile) await put(disk, m.URI.parse(remote.native_uri), { 'probe.machine': label + '-remote' });
      if (roots.length > 1) await put(disk, m.URI.parse(config.native_uri), { folders: binding.initialization.workspace.folders.map(folder => ({ uri: folder.native_uri, name: folder.name })), settings: { 'probe.value': label + '-view' } });
      for (const folder of transformer.folders) await put(ordinary, m.URI.joinPath(folder.uri, '.vscode', 'settings.json'), {
        'probe.value': folder.name + '-folder', '[typescript]': { 'probe.value': folder.name + '-language' },
      });
      let child;
      try { child = await m.targetConfigurationServices(parent, binding, transformer, store); }
      catch (error) { store.dispose(); throw error; }
      const owner = child.invokeFunction(accessor => accessor.get(m.IConfigurationService)), snapshots = [];
      const main = store.add(child.createInstance(m.MainThreadConfiguration, { extensionHost: { binding }, getProxy: () => ({
        $initializeConfiguration: data => snapshots.push(data), $acceptConfigurationChanged: data => snapshots.push(data),
      }) }));
      const host = { binding, transformer, child, owner, main, store, snapshots, config, remote };
      hosts.set(label, host); return host;
    };
    globalThis.probe = { m, files, disk, profile, ordinary, put, parent, lifetime, hosts, make, transport };
  });
  return { context, page };
}

test('原目标配置初始化、scope合并和typed多根仅消费本视图，单根不造workspace文件', async () => {
  const { context, page } = await open();
  try {
    const result = await page.evaluate(async () => {
      const p = globalThis.probe, a = await p.make('A', ['A1', 'A2']), b = await p.make('B');
      const inspect = a.owner.inspect('probe.value', { resource: a.transformer.folders[1].uri, overrideIdentifier: 'typescript' });
      return { value: a.owner.getValue('probe.value'), second: a.owner.getValue('probe.value', { resource: a.transformer.folders[1].uri }),
        language: a.owner.getValue('probe.value', { resource: a.transformer.folders[1].uri, overrideIdentifier: 'typescript' }),
        machine: a.owner.getValue('probe.machine'), b: b.owner.getValue('probe.value'), bMachine: b.owner.getValue('probe.machine'),
        inspect: { userLocalValue: inspect.userLocalValue, workspaceValue: inspect.workspaceValue, workspaceFolderValue: inspect.workspaceFolderValue },
        aFolders: a.snapshots[0].folders.map(([uri]) => uri.toString()), bFolders: b.snapshots[0].folders.map(([uri]) => uri.toString()),
        ownerIsContext: a.child.invokeFunction(accessor => accessor.get(p.m.IWorkspaceContextService)) === a.owner,
        bConfig: b.owner.getWorkspace().configuration, viewNative: a.transformer.transformOutgoingURI(a.owner.getWorkspace().configuration).toString(),
      };
    });
    expect(result.value).toBe('A-view'); expect(result.second).toBe('A2-folder'); expect(result.language).toBe('A2-language');
    expect(result.machine).toBe('A-remote'); expect(result.b).toBe('B1-folder'); expect(result.bMachine).toBe('B-remote');
    expect(result.inspect).toEqual({ userLocalValue: 'local', workspaceValue: 'A-view', workspaceFolderValue: 'A2-language' });
    expect(result.aFolders).toHaveLength(2); expect(result.aFolders.every(value => value.startsWith('boxteam-file://gateway-a/'))).toBe(true);
    expect(result.bFolders).toHaveLength(1); expect(result.bFolders[0]).toStartWith('boxteam-file://gateway-b/');
    expect(result.ownerIsContext).toBe(true); expect(result.bConfig).toBeNull(); expect(result.viewNative).toBe('file:///hosts/A/view/workspace.code-workspace');
    checks.push('原child DI、profile/remote/view/folder/language合并、inspect、初始MainThread数据及单根/多根资源身份');
  } finally { await page.evaluate(() => globalThis.probe.lifetime.dispose()); await context.close(); }
});

test('配置变化只通知所属视图，watch不建立保存基线，关闭后拒绝写入并释放订阅', async () => {
  const { context, page } = await open();
  try {
    await page.evaluate(async () => {
      const p = globalThis.probe, a = await p.make('A'), b = await p.make('B');
      p.beforeB = b.snapshots.length;
      await p.files.writeFile(p.m.URI.parse(a.remote.resource_uri), p.m.VSBuffer.fromString(JSON.stringify({ 'probe.machine': 'A-updated' })));
      for (const subscription of p.transport.subscribers) if (subscription.body.backend_id === a.binding.backend_id) subscription.send('changes', { changes: [{ resource_uri: a.remote.resource_uri, kind: 'edit' }], overflow: false });
    });
    await page.waitForFunction(() => globalThis.probe.hosts.get('A').owner.getValue('probe.machine') === 'A-updated');
    const result = await page.evaluate(async () => {
      const p = globalThis.probe, a = p.hosts.get('A'), b = p.hosts.get('B'), failures = [];
      try { await a.main.$updateConfigurationOption(p.m.ConfigurationTarget.MEMORY, 'probe.value', 'bad', { resource: b.transformer.folders[0].uri }); } catch (error) { failures.push(error.message); }
      const writes = p.transport.requests.filter(request => request.endpoint.endsWith('/write'));
      a.main.dispose();
      try { await a.main.$updateConfigurationOption(p.m.ConfigurationTarget.MEMORY, 'probe.value', 'closed'); } catch (error) { failures.push(error.message); }
      a.store.dispose();
      try { await p.files.readFile(p.m.URI.parse(a.remote.resource_uri)); } catch (error) { failures.push(error.message); }
      return { bUnchanged: b.snapshots.length === p.beforeB, failures, writes, active: p.transport.subscribers.map(value => value.body.backend_id) };
    });
    expect(result.bUnchanged).toBe(true); expect(result.failures).toHaveLength(3);
    expect(result.failures[0]).toContain('不属于'); expect(result.failures[1]).toContain('已经关闭'); expect(result.failures[2]).toContain('未绑定');
    expect(result.writes).toHaveLength(1); expect(result.writes[0].body).not.toHaveProperty('expected_revision');
    expect(result.writes[0].body.options).toEqual({ create: true, overwrite: true, unlock: false, atomic: false, append: false });
    expect(result.active).toEqual(['backend-B']);
    checks.push('真实FileService写flags、SSE变化解析/重载、目标隔离、无新增hash、MainThread非法归属/关闭拒绝及watch回收');
  } finally { await page.evaluate(() => globalThis.probe.lifetime.dispose()); await context.close(); }
});

test('原文件RPC核本host配置归属，共享profile关闭一代后仍可读写并监听', async () => {
  const { context, page } = await open();
  try {
    const result = await page.evaluate(async () => {
      const p = globalThis.probe, a = await p.make('A'), b = await p.make('B'), c = await p.make('C', ['C1'], a);
      const main = a.store.add(a.child.createInstance(p.m.MainThreadFileSystem, {
        extensionHost: { binding: a.binding, uriTransformer: a.transformer },
        getProxy: () => ({ $acceptProviderInfos() {} }),
      }));
      const own = p.m.URI.parse(a.remote.resource_uri), foreign = p.m.URI.parse(b.remote.resource_uri);
      const before = p.transport.requests.length, blocked = [];
      for (const call of [() => main.$stat(foreign), () => main.$readFile(foreign),
        () => main.$writeFile(foreign, p.m.VSBuffer.fromString('{}')), () => main.$mkdir(foreign),
        () => main.$delete(foreign, { recursive: false, useTrash: false }), () => main.$readdir(foreign),
        () => main.$rename(own, foreign, { overwrite: true }), () => main.$copy(foreign, own, { overwrite: true })]) {
        try { await call(); } catch (error) { blocked.push(error.message); }
      }
      const foreignRequests = p.transport.requests.length - before;
      const parent = p.m.URI.joinPath(own, '..');
      const parentStat = await main.$stat(parent);
      await main.$mkdir(parent);
      const errors = [];
      for (const detail of ['配置所属宿主代际已撤销', { owner: 'extension_configuration', code: 'EntryExists', message: '文件已存在' }]) {
        p.transport.failure = new p.transport.GatewayRequestError(409, '冲突', detail);
        try { await p.files.getProvider('boxteam').writeFile(own, new TextEncoder().encode('{}'), { create: true, overwrite: true }); }
        catch (error) { errors.push(p.m.toFileSystemProviderErrorCode(error)); }
      }
      p.transport.failure = undefined;
      a.store.dispose();
      await p.files.writeFile(own, p.m.VSBuffer.fromString(JSON.stringify({ 'probe.machine': 'shared-profile-updated' })));
      for (const subscription of p.transport.subscribers) if (subscription.body.generation === c.binding.generation) {
        subscription.send('changes', { changes: [{ resource_uri: own.toString(), kind: 'edit' }], overflow: false });
      }
      return { blocked, foreignRequests, parentType: parentStat.type, errors,
        active: p.transport.subscribers.map(value => value.body.generation),
        remainingRead: (await p.files.readFile(own)).value.toString() };
    });
    expect(result.blocked).toHaveLength(8); expect(result.blocked.every(message => message.includes('不属于'))).toBe(true);
    expect(result.foreignRequests).toBe(0); expect(result.parentType).toBe(2);
    expect(result.errors).toEqual(['Unavailable', 'EntryExists']);
    expect(result.active).toEqual(['generation-B', 'generation-C']);
    expect(result.remainingRead).toContain('shared-profile-updated');
    await page.waitForFunction(() => globalThis.probe.hosts.get('C').owner.getValue('probe.machine') === 'shared-profile-updated');
    checks.push('原MainThreadFileSystem八种跨host调用零传输、精确父stat/mkdir、409错误归属、共享profile剩余代际读写和watch存续');
  } finally { await page.evaluate(() => globalThis.probe.lifetime.dispose()); await context.close(); }
});

test('目标配置权限或代际失效不能初始化为空配置，监听越界明确报告', async () => {
  const { context, page } = await open();
  try {
    const result = await page.evaluate(async () => {
      const p = globalThis.probe, failures = [];
      for (const [label, status] of [['Denied', 403], ['Revoked', 409]]) {
        p.transport.failureOperation = '/read';
        p.transport.failure = new p.transport.GatewayRequestError(status, label, label);
        try { await p.make(label); } catch (error) { failures.push(error.message); }
      }
      p.transport.failure = undefined;
      const a = await p.make('A'), b = await p.make('B');
      p.watchErrors = [];
      p.lifetime.add(p.files.onDidWatchError(error => p.watchErrors.push(error.message)));
      for (const subscription of p.transport.subscribers) if (subscription.body.generation === a.binding.generation) {
        subscription.send('changes', { changes: [{ resource_uri: b.remote.resource_uri, kind: 'edit' }], overflow: false });
      }
      return { failures, aValue: a.owner.getValue('probe.machine'), bValue: b.owner.getValue('probe.machine') };
    });
    expect(result.failures).toHaveLength(2);
    expect(result.failures[0]).toContain('Denied'); expect(result.failures[1]).toContain('Revoked');
    expect(result.aValue).toBe('A-remote'); expect(result.bValue).toBe('B-remote');
    await page.waitForFunction(() => globalThis.probe.watchErrors.some(error => error.includes('未绑定资源')));
    checks.push('目标拒绝读取/已撤销代际初始化失败、监听跨资源事件沿原watch error公开且不改变配置');
  } finally { await page.evaluate(() => globalThis.probe.lifetime.dispose()); await context.close(); }
});
