import { afterAll, beforeAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_native_management.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'management.fixture.mjs');
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { WebExtensionManagementService, captureTargetInstallOptions } from './src/clients/workbench/src/extensions/nativeManagement.js';
export { WebExtensionManagementService as OriginalManagementOwner } from '${upstream}workbench/services/extensionManagement/common/webExtensionManagementService.js';
export { WebExtensionsScannerService } from '${upstream}workbench/services/extensionManagement/browser/webExtensionsScannerService.js';
export { ExtensionManagementServerService } from '${upstream}workbench/services/extensionManagement/common/extensionManagementServerService.js';
export { ExtensionManagementService } from '${upstream}workbench/services/extensionManagement/common/extensionManagementService.js';
export { ExtensionType } from '${upstream}platform/extensions/common/extensions.js';
export { toExtensionDescription } from '${upstream}workbench/services/extensions/common/extensions.js';
export { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { extUri } from '${upstream}base/common/resources.js';
export { URI } from '${upstream}base/common/uri.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { managementGateway, onDidChangeExtensionManagementTarget } from './src/clients/workbench/src/managementTarget.js';
export { invalidateGatewayUserSession } from './src/clients/web/src/api/http.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [{ name: 'explicit-management-http-boundary', setup(builder) {
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({ contents: 'export const gateway = globalThis.managementTransport; export class GatewayRequestError extends Error {}', loader: 'ts' }));
  } }, buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });

let browser, server;
const checks = [];
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => new URL(request.url).pathname === '/fixture.mjs'
    ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
    : new Response('<!doctype html><title>原扩展管理 owner</title>', { headers: { 'Content-Type': 'text/html' } }) });
  run.addCleanup('管理组件来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'), headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('管理Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ checks,
    boundary: '真实Chromium、原管理服务边界/事件、市场消费者、目标选择命令、库存解析及descriptor序列化；GatewayHTTP、native库存和gallery回执为具名装具，真实安装/平台与资源读取另由Python Gateway测试证明，不代表完整市场窗口UI。' }, null, 2)); }
});

async function open() {
  const context = await browser.newContext(), page = await context.newPage();
  await page.goto(`http://127.0.0.1:${server.port}`);
  await page.evaluate(async () => {
    globalThis._VSCODE_FILE_ROOT = location.origin + '/';
    const state = { requests: [], domain: 'backend-a', domains: ['backend-a'], inventories: new Map(), handlers: new Map() };
    globalThis.managementTransport = { async request(endpoint, options = {}) {
      const body = options.body ? JSON.parse(options.body) : undefined; state.requests.push({ endpoint, body });
      if (state.handlers.has(endpoint)) return state.handlers.get(endpoint)(body, options);
      if (endpoint.endsWith('/management/gateways')) return { local_gateway_id: 'gateway-a', items: [{ gateway_id: 'gateway-a', name: '本地', connection_kind: 'local' }] };
      if (endpoint === '/api/gateway/extension-targets') return { protocol_version: 1, gateway_id: 'gateway-a', source: 'live',
        targets: state.domains.map(backend_id => ({ gateway_id: 'gateway-a', backend_id, capabilities: { installation_manage: true, host_manage: true } })) };
      if (endpoint.endsWith('/inventory')) return structuredClone(state.inventories.get(body.backend_id));
      throw new Error('未登记管理HTTP：' + endpoint);
    } };
    const m = await import('/fixture.mjs'), profileLocation = m.URI.parse('vscode-userdata:/default/extensions.json');
    const entry = (backend, version = '1.0.0', metadata = {}) => ({ extension_key: `probe.native@${version}#${backend}`, source: 'profile',
      is_valid: true, validations: [], metadata, readme_path: 'docs/guide.txt', changelog_path: 'notes/history.txt',
      description: { name: 'native', publisher: 'probe', version, engines: { vscode: '*' }, main: './entry.js', extensionKind: ['workspace'],
        identifier: { value: 'probe.native' }, extensionLocation: m.URI.file('/native/' + backend + '/' + version).toJSON(),
        targetPlatform: 'linux-x64', isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: !!metadata.preRelease } });
    const inventory = (backend, version, metadata = {}) => ({ protocol_version: 1, gateway_id: 'gateway-a', backend_id: backend, profile_scope: 'default',
      inventory: { profile_id: 'profile-' + backend, target_platform: 'linux-x64', extensions: [entry(backend, version, metadata)] } });
    for (const backend of ['backend-a', 'backend-b']) state.inventories.set(backend, inventory(backend, backend.endsWith('a') ? '1.0.0' : '2.0.0'));
    m.workspaceContext.publish({ gatewayId: 'gateway-a', workspaces: [], activeId: null, settings: null, loading: false, error: null });
    const profiles = { currentProfile: { id: 'browser-default', extensionsResource: profileLocation }, onDidChangeCurrentProfile: m.Event.None };
    const service = new m.WebExtensionManagementService({}, {}, new m.NullLogService(), {
      scanSystemExtensions: async () => [], scanCustomBuiltinExtensions: async () => [],
    }, profiles, { quality: 'stable' }, { isAllowed: () => true }, { defaultProfile: profiles.currentProfile }, { extUri: m.extUri });
    const select = async backend => {
      state.domains = ['backend-a', 'backend-b'];
      await m.CommandsRegistry.getCommand('boxteam.extensions.selectTarget').handler({ get: () => ({ async pick(items) {
        const values = await items; return values.find(item => item.target.backend_id === backend) ?? values[0];
      } }) });
    };
    const gallery = { type: 'gallery', identifier: { id: 'probe.native', uuid: 'gallery-uuid' }, version: '9.0.0',
      publisher: 'probe', name: 'native', allTargetPlatforms: ['linux-x64'], properties: {}, assets: {} };
    globalThis.managementProbe = { m, state, service, select, entry, inventory, gallery, profiles, profileLocation,
      initial: await service.getInstalled(m.ExtensionType.User) };
  });
  return { context, page };
}

test('唯一管理owner消费目标原生平台和current资源，旧scanner用户账本已物理下线', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const { m, service, initial, gallery, state } = globalThis.managementProbe, local = initial[0];
      const server = new m.ExtensionManagementServerService({ createInstance: type => { if (type !== m.WebExtensionManagementService) throw new Error('出现第二管理owner'); return service; } });
      const market = { extensionManagementServerService: server };
      const canInstall = await m.ExtensionManagementService.prototype.canInstallGalleryExtension.call(market, gallery);
      return { same: m.OriginalManagementOwner === m.WebExtensionManagementService, canInstall,
        target: local.installationTarget, platform: await service.getTargetPlatform(), location: local.location.toString(true),
        native: m.toExtensionDescription(local).extensionLocation.toString(true), readme: local.readmeUrl.toString(true), changelog: local.changelogUrl.toString(true),
        oldMethods: ['scanUserExtensions', 'addExtension', 'addExtensionFromGallery', 'removeExtension', 'updateMetadata', 'scanInstalledExtensions', 'writeInstalledExtensions'].filter(name => name in m.WebExtensionsScannerService.prototype),
        custom: typeof m.WebExtensionsScannerService.prototype.scanCustomBuiltinExtensions, ownerCount: [server.localExtensionManagementServer, server.remoteExtensionManagementServer, server.webExtensionManagementServer].filter(Boolean).length,
        inventoryReads: state.requests.filter(request => request.endpoint.endsWith('/inventory')).length };
    });
    expect(value.same).toBe(true); expect(value.canInstall).toBe(true); expect(value.platform).toBe('linux-x64');
    expect(value.target).toEqual({ kind: 'backend', gatewayId: 'gateway-a', backendId: 'backend-a' });
    expect(value.location).toContain('/current/gateway-a/backend-a/profile-backend-a/'); expect(value.native).toBe('file:///native/backend-a/1.0.0');
    expect(value.readme).toEndWith('/docs/guide.txt'); expect(value.changelog).toEndWith('/notes/history.txt');
    expect(value.oldMethods).toEqual([]); expect(value.custom).toBe('function'); expect(value.ownerCount).toBe(1); expect(value.inventoryReads).toBe(1);
    checks.push('唯一owner、目标native平台、main-only市场准入、current管理资源与native运行位置、删除旧scanner用户写链');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原管理启动读取等待Gateway初始化，失败保留原因且关闭后不开始库存请求', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const { m, service, state } = globalThis.managementProbe;
      const initial = m.workspaceContext.getSnapshot(), originalRefresh = m.workspaceContext.refresh;
      const result = {};
      try {
        let release;
        // 仅延迟现有初始化结果，库存解析和原管理入口仍运行真实实现。
        m.workspaceContext.refresh = () => new Promise(resolve => { release = () => { m.workspaceContext.publish(initial); resolve(); }; });
        m.workspaceContext.publish({ ...initial, gatewayId: null, loading: true });
        const count = state.requests.length, pending = service.getInstalled(m.ExtensionType.User);
        await Promise.resolve(); result.beforeReadyRequests = state.requests.length - count;
        release(); result.version = (await pending)[0].manifest.version;
        m.workspaceContext.publish({ ...initial, gatewayId: null, loading: true });
        const platform = service.getTargetPlatform(); await Promise.resolve(); release(); result.platform = await platform;
        m.workspaceContext.publish({ ...initial, gatewayId: null, loading: true });
        m.workspaceContext.refresh = async () => { m.workspaceContext.publish({ ...initial, gatewayId: null, loading: false, error: 'Gateway初始化实际失败' }); };
        const failedCount = state.requests.length;
        result.error = await service.getInstalled(m.ExtensionType.User).then(() => '错误地成功', error => error.message);
        result.failedRequests = state.requests.length - failedCount;
        m.workspaceContext.publish({ ...initial, gatewayId: null, loading: true });
        m.workspaceContext.refresh = () => new Promise(resolve => { release = () => { m.workspaceContext.publish(initial); resolve(); }; });
        const closedCount = state.requests.length, closed = service.getInstalled(m.ExtensionType.User);
        await Promise.resolve(); service.dispose(); release();
        result.closed = await closed.then(() => '错误地成功', error => error.message);
        result.closedRequests = state.requests.length - closedCount;
      } finally { m.workspaceContext.refresh = originalRefresh; }
      return result;
    });
    expect(value.beforeReadyRequests).toBe(0); expect(value.version).toBe('1.0.0'); expect(value.platform).toBe('linux-x64');
    expect(value.error).toBe('Gateway初始化实际失败'); expect(value.failedRequests).toBe(0);
    expect(value.closed).toBe('扩展管理服务已关闭'); expect(value.closedRequests).toBe(0);
    checks.push('原管理启动库存与平台等待已有Gateway初始化，真实初始化错误保留，关闭后零请求');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('旧身份共享库存读取立即撤销，新身份同目标不复用旧回执或被旧清理删掉', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const { m, service, state, inventory } = globalThis.managementProbe;
      const pending = [];
      let entered;
      const next = () => new Promise(resolve => { entered = resolve; });
      let requested = next();
      // HTTP 装具故意不理会 abort，核旧身份成功回执迟到时仍不能被新身份消费。
      state.handlers.set('/api/gateway/extension-installations/inventory', (body, options) => new Promise(resolve => {
        pending.push({ resolve, signal: options.signal }); entered();
      }));
      const first = service.getInstalled(m.ExtensionType.User).then(value => ({ version: value[0].manifest.version }), error => ({ error: error.name }));
      await requested;
      const joined = service.getInstalled(m.ExtensionType.User).then(value => ({ version: value[0].manifest.version }), error => ({ error: error.name }));
      await Promise.resolve(); await Promise.resolve();
      const oldShared = pending.length;
      m.invalidateGatewayUserSession(0);
      const oldResults = await Promise.all([first, joined]);
      requested = next(); let freshCompleted = false;
      const fresh = service.getInstalled(m.ExtensionType.User).then(value => { freshCompleted = true; return value; });
      await requested;
      const oldAborted = pending[0].signal.aborted, freshActive = !pending[1].signal.aborted;
      pending[0].resolve(inventory('backend-a', '1.0.0'));
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
      const oldDidNotCompleteFresh = !freshCompleted;
      const freshJoined = service.getInstalled(m.ExtensionType.User);
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
      const readsAfterOldSettled = pending.length;
      pending[1].resolve(inventory('backend-a', '3.0.0'));
      const versions = (await Promise.all([fresh, freshJoined])).map(value => value[0].manifest.version);
      return { oldShared, oldResults, oldAborted, freshActive, oldDidNotCompleteFresh, readsAfterOldSettled, versions };
    });
    expect(value.oldShared).toBe(1); expect(value.oldResults).toEqual([{ error: 'Canceled' }, { error: 'Canceled' }]);
    expect(value.oldAborted).toBe(true); expect(value.freshActive).toBe(true); expect(value.oldDidNotCompleteFresh).toBe(true);
    expect(value.readsAfterOldSettled).toBe(2); expect(value.versions).toEqual(['3.0.0', '3.0.0']);
    checks.push('真实身份失效事件取消并淘汰共享库存，旧HTTP迟到不污染新身份同目标结果或移除新共享请求');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('市场七选项沿descriptor保留显式false，目标选择变化不改投或污染当前列表事件', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { service, state } = p, events = [];
      service.onDidInstallExtensions(result => events.push(result));
      let release, entered;
      const started = new Promise(resolve => entered = resolve);
      state.handlers.set('/api/gateway/extension-installations/install-gallery', body => new Promise(resolve => { release = () => {
        const reply = p.inventory(body.backend_id, '8.0.0', { updated: true });
        state.inventories.set(body.backend_id, reply); resolve({ ...reply, installed: { id: 'probe.native', version: '8.0.0', profile_id: reply.inventory.profile_id, reused: false } });
      }; entered(); }));
      const pending = service.installFromGallery(p.gallery, { installPreReleaseVersion: true, installGivenVersion: false,
        donotIncludePackAndDependencies: true, isMachineScoped: false, isApplicationScoped: true, pinned: false, preRelease: true });
      await started; await p.select('backend-b'); release(); const installed = await pending;
      const [current] = await service.getInstalled(p.m.ExtensionType.User);
      return { sent: state.requests.find(request => request.endpoint.endsWith('/install-gallery')).body,
        installedVersion: installed.manifest.version, installedTarget: installed.installationTarget.backendId,
        currentVersion: current.manifest.version, currentTarget: current.installationTarget.backendId, eventCount: events.length };
    });
    expect(value.sent.backend_id).toBe('backend-a'); expect(value.sent.options).toEqual({ install_pre_release_version: true, install_given_version: false,
      donot_include_pack_and_dependencies: true, is_machine_scoped: false, is_application_scoped: true, pinned: false, pre_release: true });
    expect(value.installedVersion).toBe('8.0.0'); expect(value.installedTarget).toBe('backend-a'); expect(value.currentVersion).toBe('2.0.0');
    expect(value.currentTarget).toBe('backend-b'); expect(value.eventCount).toBe(0);
    checks.push('descriptor七bool线格式/false、原生兼容版本回执、在途目标冻结与事件归属');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原市场更新消费者沿已安装对象冻结目标，metadata不接受gallery身份为客户端事实', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, local = p.initial[0]; await p.select('backend-b');
      let updatedOptions;
      const owner = { getServer: () => ({ extensionManagementService: { installFromGallery: async (_gallery, options) => { updatedOptions = options; return local; } } }) };
      await p.m.ExtensionManagementService.prototype.updateFromGallery.call(owner, p.gallery, local, { pinned: false });
      let replyLocal;
      p.service.onDidUpdateExtensionMetadata(event => replyLocal = event.local);
      p.state.handlers.set('/api/gateway/extension-installations/metadata', body => p.inventory(body.backend_id, '1.0.0',
        { pinned: true, isMachineScoped: false, isApplicationScoped: true, preRelease: false, publisherDisplayName: 'Server publisher', id: 'server-uuid' }));
      const updated = await p.service.updateMetadata(local, { pinned: false, isMachineScoped: false, isApplicationScoped: true, preRelease: false,
        id: 'client-uuid', publisherDisplayName: 'Client publisher', publisherId: 'client-publisher', isPreReleaseVersion: false }, p.profileLocation);
      const sent = p.state.requests.find(request => request.endpoint.endsWith('/metadata')).body;
      return { updateTarget: updatedOptions.nativeTarget.backend_id, updateProfile: updatedOptions.nativeProfileId, sent,
        updated: { pinned: updated.pinned, publisher: updated.publisherDisplayName, uuid: updated.identifier.uuid }, currentEvent: !!replyLocal };
    });
    expect(value.updateTarget).toBe('backend-a'); expect(value.updateProfile).toBe('profile-backend-a');
    expect(value.sent.backend_id).toBe('backend-a'); expect(value.sent.profile_id).toBe('profile-backend-a'); expect(value.sent.refresh_from_gallery).toBe(true);
    expect(value.sent.metadata).toEqual({ is_machine_scoped: false, is_application_scoped: true, pinned: false, pre_release: false });
    expect(value.updated).toEqual({ pinned: true, publisher: 'Server publisher', uuid: 'server-uuid' }); expect(value.currentEvent).toBe(false);
    checks.push('原市场更新冻结local目标/profile、metadata descriptor及服务器完整对象、gallery事实原生重取');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('metadata失败触发原profile事件重取，精确卸载回执不匹配必须失败', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, local = p.initial[0], failures = [], reads = [];
      p.service.onDidChangeProfile(() => reads.push(p.service.getInstalled(p.m.ExtensionType.User)));
      p.state.handlers.set('/api/gateway/extension-installations/metadata', () => { throw new Error('native metadata失败'); });
      try { await p.service.updateMetadata(local, { pinned: false }, p.profileLocation); } catch (error) { failures.push(error.message); }
      await Promise.all(reads);
      p.state.handlers.set('/api/gateway/extension-installations/uninstall', body => ({ ...p.inventory(body.backend_id, '1.0.0'),
        uninstalled: { id: 'probe.native', version: 'wrong-version', profile_id: body.profile_id } }));
      try { await p.service.uninstall(local); } catch (error) { failures.push(error.message); }
      await Promise.all(reads);
      return { failures, reloads: reads.length, inventoryReads: p.state.requests.filter(request => request.endpoint.endsWith('/inventory')).length,
        uninstall: p.state.requests.find(request => request.endpoint.endsWith('/uninstall')).body };
    });
    expect(value.failures).toEqual(['native metadata失败', '目标卸载回执不匹配本次冻结的 profile 成员']);
    expect(value.reloads).toBe(2); expect(value.inventoryReads).toBe(3);
    expect(value.uninstall).toMatchObject({ backend_id: 'backend-a', profile_id: 'profile-backend-a', extension_id: 'probe.native', version: '1.0.0' });
    checks.push('失败沿原profile变化消费者重取、卸载精确version/profile回执不冒成功');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('不支持的profile和选项明确拒绝，畸形原生库存不进入管理列表', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, failures = [];
      for (const operation of [() => p.service.getInstalled(p.m.ExtensionType.User, p.m.URI.parse('vscode-userdata:/other/extensions.json')),
        () => p.service.updateMetadata(p.initial[0], { updated: true }, p.profileLocation),
        () => p.service.installFromGallery(p.gallery, { isWorkspaceScoped: true })]) {
        try { await operation(); } catch (error) { failures.push(error.message); }
      }
      const malformed = p.inventory('backend-a', '1.0.0'); delete malformed.inventory.target_platform;
      p.state.inventories.set('backend-a', malformed);
      try { await p.service.getInstalled(p.m.ExtensionType.User); } catch (error) { failures.push(error.message); }
      return { failures, installWrites: p.state.requests.filter(request => request.endpoint.endsWith('/install-gallery')).length };
    });
    expect(value.failures).toHaveLength(4); expect(value.failures[0]).toContain('默认 profile');
    expect(value.failures[1]).toContain('updated'); expect(value.failures[2]).toContain('非标准安装选项'); expect(value.failures[3]).toContain('平台不匹配');
    expect(value.installWrites).toBe(0); checks.push('非默认profile、未接线选项/metadata和畸形平台库存快失败');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});
