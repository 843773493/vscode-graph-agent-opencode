import { afterAll, beforeAll, expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_native_management.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'management.fixture.mjs');
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const workbenchPath = join(root, 'upstream/src/vs/workbench/contrib/extensions/browser/extensionsWorkbenchService.ts');
const workbenchSource = ts.createSourceFile(workbenchPath, await transform(workbenchPath, await readFile(workbenchPath, 'utf8')), ts.ScriptTarget.Latest, true);
const workbenchClass = workbenchSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtensionsWorkbenchService');
const workbenchParameters = workbenchClass.members.find(ts.isConstructorDeclaration).parameters.map(parameter => parameter.name.getText(workbenchSource));
const navigationPath = join(root, 'src/react/useNavigationRuntime.ts');
const navigationSource = ts.createSourceFile(navigationPath, await readFile(navigationPath, 'utf8'), ts.ScriptTarget.Latest, true);
const navigation = navigationSource.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'useNavigationRuntime');
// 运行导航的原发布 effect；目录、会话等其它 hook 不属于这个竞态的边界。
const publication = navigation.body.statements.filter(node => ts.isExpressionStatement(node) && ts.isCallExpression(node.expression)
  && node.expression.expression.getText(navigationSource) === 'useEffect' && node.getText(navigationSource).includes('workspaceContext.publish'));
if (publication.length !== 1) throw new Error('导航快照发布 effect 的原接线已改变');
await build({ stdin: { resolveDir: resolve('.'), contents: `
import { useEffect, createElement } from './src/clients/web/node_modules/react/index.js';
import { createRoot } from './src/clients/web/node_modules/react-dom/client.js';
import { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export function navigationPublicationRoot() {
  function Publication({ state, rendered }) {
    ${publication[0].getText(navigationSource)}
    useEffect(rendered, [state]);
    return null;
  }
  const root = createRoot(document.createElement('div'));
  return { render: state => new Promise(rendered => root.render(createElement(Publication, { state, rendered }))), unmount: () => root.unmount() };
}
export { WebExtensionManagementService, captureTargetInstallOptions } from './src/clients/workbench/src/extensions/nativeManagement.js';
export { WebExtensionManagementService as OriginalManagementOwner } from '${upstream}workbench/services/extensionManagement/common/webExtensionManagementService.js';
export { WebExtensionsScannerService } from '${upstream}workbench/services/extensionManagement/browser/webExtensionsScannerService.js';
export { ExtensionManagementServerService } from '${upstream}workbench/services/extensionManagement/common/extensionManagementServerService.js';
export { ExtensionManagementService } from '${upstream}workbench/services/extensionManagement/common/extensionManagementService.js';
export { Extension, ExtensionsWorkbenchService } from '${upstream}workbench/contrib/extensions/browser/extensionsWorkbenchService.js';
export { Extensions as OriginalExtensions } from '${upstream}workbench/contrib/extensions/browser/extensionsWorkbenchService.js';
export { InstallAnotherVersionAction, ExtensionStatusAction, ExtensionRuntimeStateAction } from '${upstream}workbench/contrib/extensions/browser/extensionsActions.js';
export { ExtensionsInput } from '${upstream}workbench/contrib/extensions/common/extensionsInput.js';
export { ExtensionState } from '${upstream}workbench/contrib/extensions/common/extensions.js';
export { EnablementState } from '${upstream}workbench/services/extensionManagement/common/extensionManagement.js';
export { ExtensionManifestPropertiesService } from '${upstream}workbench/services/extensions/common/extensionManifestPropertiesService.js';
export { ExtensionRunningLocationTracker } from '${upstream}workbench/services/extensions/common/extensionRunningLocationTracker.js';
export { renderMarkdown } from '${upstream}base/browser/markdownRenderer.js';
export { ExtensionDescriptionRegistry } from '${upstream}workbench/services/extensions/common/extensionDescriptionRegistry.js';
export { default as Severity } from '${upstream}base/common/severity.js';
export { CancellationToken } from '${upstream}base/common/cancellation.js';
export { ExtensionType, ExtensionIdentifier } from '${upstream}platform/extensions/common/extensions.js';
export { toExtensionDescription } from '${upstream}workbench/services/extensions/common/extensions.js';
export { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { extUri } from '${upstream}base/common/resources.js';
export { URI } from '${upstream}base/common/uri.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { managementGateway, onDidChangeExtensionManagementTarget } from './src/clients/workbench/src/managementTarget.js';
export { invalidateGatewayUserSession } from './src/clients/web/src/api/http.js';
export const workbenchParameters = ${JSON.stringify(workbenchParameters)};
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  loader: { '.css': 'empty', '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [{ name: 'explicit-management-http-boundary', setup(builder) {
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({ contents: 'export const gateway = globalThis.managementTransport; export class GatewayRequestError extends Error {}', loader: 'ts' }));
    // 仅给原内部类增加测试导出；算法和所有消费者仍经过唯一生产源码转换。
    builder.onLoad({ filter: /\/contrib\/extensions\/browser\/extensionsWorkbenchService\.ts$/ }, async args => ({
      contents: (await transform(args.path, await readFile(args.path, 'utf8'))).replace('class Extensions extends Disposable', 'export class Extensions extends Disposable'),
      loader: 'ts', resolveDir: dirname(args.path),
    }));
  } }, buildSourcePlugin(root, transform)],
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
    boundary: '真实Chromium、原管理服务边界/事件、市场消费者、目标选择命令、库存解析、descriptor序列化及原状态Action/runtimeState/Tracker；GatewayHTTP、native库存、gallery回执、运行清单和UI依赖为具名装具，真实安装/平台与资源读取另由Python Gateway测试证明，不代表完整市场窗口UI。' }, null, 2)); }
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
    const createWorkbench = () => {
      const observed = { opened: [], synchronized: [], locals: [], galleryFetches: [], galleryGate: undefined, getExtensionsGate: undefined };
      const galleryApi = { isEnabled: () => true,
        query: async () => {
          if (observed.galleryGate) { observed.galleryGate.entered.resolve(); await observed.galleryGate.release.promise; }
          return { firstPage: [gallery], total: 2, pageSize: 1, getPage: async () => [gallery] };
        },
        getExtensions: async (infos, options) => {
          observed.galleryFetches.push({ infos, options });
          if (observed.getExtensionsGate) { observed.getExtensionsGate.entered.resolve(); await observed.getExtensionsGate.release.promise; }
          return [gallery];
        },
      };
      const product = { version: '1.145.0', commit: 'probe', defaultChatAgent: { extensionId: 'probe.chat' } };
      const instantiation = { createInstance: (Type, ...args) => Type === m.Extension
        ? new Type(...args, galleryApi, {}, new m.NullLogService(), {}, product) : new Type(...args) };
      const server = { id: 'web', extensionManagementService: service };
      const management = { preferPreReleases: false, getExtensionsControlManifest: async () => ({ malicious: [], deprecated: {} }),
        getTargetPlatform: options => m.ExtensionManagementService.prototype.getTargetPlatform.call({
          extensionManagementServerService: { webExtensionManagementServer: server },
        }, options),
        installFromGallery: (value, options) => service.installFromGallery(value, options),
        updateFromGallery: (value, local, options) => m.ExtensionManagementService.prototype.updateFromGallery.call({
          getServer: () => ({ extensionManagementService: service }),
        }, value, local, options),
        uninstallExtensions: extensions => service.uninstallExtensions(extensions),
      };
      const dependencies = { galleryService: galleryApi, extensionManagementService: management, instantiationService: instantiation,
        configurationService: { getValue: () => false }, productService: product,
        editorService: { openEditor: async input => { observed.opened.push(input); } },
        extensionGalleryManifestService: { getExtensionGalleryManifest: async () => ({}) },
        contextKeyService: { createKey: () => ({ set() {}, reset() {} }) }, urlService: { registerHandler() {} },
        updateService: { state: { type: 'idle' } },
        userDataProfilesService: { profiles: [profiles.currentProfile] },
      };
      // 原构造器和管理目标订阅真实执行；后台启动、渲染、进度UI属于完整页面验收边界。
      class ViewWorkbench extends m.ExtensionsWorkbenchService { async initialize() {} }
      const workbench = new ViewWorkbench(...m.workbenchParameters.map(name => dependencies[name] ?? {}));
      service._register(workbench);
      Object.assign(workbench, {
        extensionManagementServerService: { webExtensionManagementServer: server, getExtensionManagementServer: () => server },
        webExtensions: { canInstall: (...args) => m.OriginalExtensions.prototype.canInstall.call({ server }, ...args),
          getExtensionState: extension => m.OriginalExtensions.prototype.getExtensionState.call({
            installed: observed.locals, installing: [], uninstalling: [],
          }, extension) },
        syncInstalledExtensionsWithGallery: async (...args) => { observed.synchronized.push(args); },
        shouldAutoUpdateExtension: () => true, getRuntimeState: () => undefined,
        withProgress: async (_options, task) => task(),
      });
      Object.defineProperties(workbench, { local: { get: () => observed.locals }, installed: { get: () => observed.locals } });
      return { workbench, observed, makeLocal: local => instantiation.createInstance(m.Extension,
        extension => workbench.getExtensionState(extension), () => undefined, { extensionManagementService: service }, local, gallery, undefined, undefined) };
    };
    const createRuntimeStatus = (views, local) => {
      const { workbench } = createWorkbench(), logs = new m.NullLogService(), statusState = { running: [], statuses: {}, canAdd: false, canAddCalls: 0, actions: [] };
      const configuration = { inspect: () => ({}), getValue: () => undefined, onDidChangeConfiguration: m.Event.None };
      const properties = new m.ExtensionManifestPropertiesService({}, configuration, { isWorkspaceTrustEnabled: () => false }, logs);
      const registry = new m.ExtensionDescriptionRegistry({ readActivationEvents: extension => extension.activationEvents ?? [] }, [], true);
      const tracker = new m.ExtensionRunningLocationTracker(registry, {}, {}, configuration, logs, properties);
      tracker.setRemoteViews(views);
      // 运行清单、动态加入能力和 UI 依赖为装具；状态判断仍运行原 Action、原方法及真实 Tracker/manifest owner。
      const extensionService = { get extensions() { return statusState.running; }, onDidChangeExtensions: m.Event.None,
        getExtensionsStatus: () => statusState.statuses,
        getExtensionWorkspaceViews: () => tracker.workspaceViews,
        canAddExtension: () => { statusState.canAddCalls++; return statusState.canAdd; }, canRemoveExtension: () => false };
      const server = workbench.extensionManagementServerService.webExtensionManagementServer;
      const extension = new m.Extension(() => m.ExtensionState.Installed, value => workbench.getRuntimeState(value),
        server, local, undefined, undefined, undefined, {}, {}, logs, {}, { version: '1.145.0', quality: 'stable' });
      const enablement = { isEnabled: () => [m.EnablementState.EnabledGlobally, m.EnablementState.EnabledWorkspace,
        m.EnablementState.EnabledByEnvironment].includes(extension.enablementState) };
      const allowed = { isAllowed: () => ({ value: 'fixture policy denied' }), onDidChangeAllowedExtensionsConfigValue: m.Event.None };
      Object.assign(workbench, { extensionService, extensionManifestPropertiesService: properties, extensionEnablementService: enablement,
        getRuntimeState: m.ExtensionsWorkbenchService.prototype.getRuntimeState, canSetLanguage: () => false,
        shouldRequireConsentToUpdate: async () => undefined, isAutoUpdateDelayed: () => false,
        updateRunningExtensions: async () => { statusState.actions.push('restart'); } });
      const action = new m.ExtensionStatusAction(workbench.extensionManagementServerService,
        { onDidChangeFormatters: m.Event.None }, {}, { isWorkspaceTrustEnabled: () => false }, { isWorkspaceTrusted: () => true },
        workbench, extensionService, properties, { getWorkspace: () => ({ folders: [] }) }, {}, allowed, enablement,
        { onDidChangeAccessData: m.Event.None, getAccessData: () => undefined }, { getExtensionGalleryManifest: async () => ({}) }, configuration);
      const runtimeAction = new m.ExtensionRuntimeStateAction({ reload: () => { statusState.actions.push('reload'); } },
        workbench, {}, extensionService, {}, { publicLog2() {} });
      action.extension = extension; runtimeAction.extension = extension;
      return { extension, state: statusState, runtimeAction, runtimeStatus: () => workbench.getExtensionRuntimeStatus(extension), async read() {
        await action.recomputeStatus(); runtimeAction.update();
        return { messages: action.status.map(status => {
          const rendered = m.renderMarkdown(status.message);
          try { return rendered.element.textContent.replaceAll('\u00a0', ' '); } finally { rendered.dispose(); }
        }), warning: action.class.includes('extension-status-warning'),
          runtime: extension.runtimeState ?? null, restartEnabled: runtimeAction.enabled, enablementState: extension.enablementState };
      }, dispose() { action.dispose(); runtimeAction.dispose(); workbench.dispose(); properties.dispose(); registry.dispose(); } };
    };
    globalThis.managementProbe = { m, state, service, select, entry, inventory, gallery, profiles, profileLocation, createWorkbench, createRuntimeStatus,
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

test('导航晚挂载不撤销已初始化Gateway的库存，真实身份变化仍撤销旧请求', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const { m, service, state, inventory } = globalThis.managementProbe;
      const navigation = m.navigationPublicationRoot(), pending = [];
      const initial = { browserViewScope: undefined, gatewayWorkspaces: [], activeGatewayWorkspaceId: null,
        uiSettings: {}, uiSettingsLoaded: false, isBootstrapping: true, workspaceSwitching: false, gatewayError: null };
      let entered;
      const requested = new Promise(resolve => entered = resolve);
      state.handlers.set('/api/gateway/extension-installations/inventory', (_body, options) => new Promise(resolve => {
        pending.push({ resolve, signal: options.signal }); entered();
      }));
      const first = service.getInstalled(m.ExtensionType.User).then(value => ({ version: value[0].manifest.version }), error => ({ error: error.message }));
      try {
        await requested;
        await navigation.render(initial);
        const afterMount = { gatewayId: m.workspaceContext.getSnapshot().gatewayId, aborted: pending[0].signal.aborted };
        const ready = { ...initial, browserViewScope: 'gateway-a', uiSettingsLoaded: true, isBootstrapping: false };
        await navigation.render(ready);
        pending[0].resolve(inventory('backend-a', '4.0.0'));
        const firstResult = await first;
        let nextEntered;
        const nextRequested = new Promise(resolve => nextEntered = resolve);
        entered = nextEntered;
        const second = service.getInstalled(m.ExtensionType.User).then(() => '错误地成功', error => error.name);
        await nextRequested;
        await navigation.render({ ...ready, browserViewScope: 'gateway-b' });
        const changed = { aborted: pending[1].signal.aborted, result: await second };
        pending[1].resolve(inventory('backend-a', '5.0.0'));
        return { afterMount, firstResult, changed };
      } finally { navigation.unmount(); for (const item of pending) item.resolve(inventory('backend-a', '1.0.0')); }
    });
    expect(value.afterMount).toEqual({ gatewayId: 'gateway-a', aborted: false });
    expect(value.firstResult).toEqual({ version: '4.0.0' });
    expect(value.changed).toEqual({ aborted: true, result: 'Canceled' });
    checks.push('原导航React发布effect晚挂载及初始化完成不抹去已核Gateway身份，真实身份变化仍取消原管理在途请求');
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

test('原市场详情按目标分开标签，旧B详情和确认中的安装切回A仍只写B', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed } = p.createWorkbench();
      await p.select('backend-b');
      const [b] = (await workbench.queryGallery({}, p.m.CancellationToken.None)).firstPage;
      await workbench.open(b);
      await p.select('backend-a');
      const [a] = await workbench.getExtensions([{ id: p.gallery.identifier.id }], p.m.CancellationToken.None);
      await workbench.open(a);
      const [inputB, inputA] = observed.opened;
      p.state.handlers.set('/api/gateway/extension-installations/install-gallery', body => {
        const reply = p.inventory(body.backend_id, p.gallery.version);
        p.state.inventories.set(body.backend_id, structuredClone(reply));
        return { ...reply, installed: { id: p.gallery.identifier.id, version: p.gallery.version, profile_id: reply.inventory.profile_id, reused: false } };
      });
      const versionAction = new p.m.InstallAnotherVersionAction(b, true, workbench, workbench.extensionManagementService,
        {}, {}, {}, {}, { isAllowed: () => true, onDidChangeAllowedExtensionsConfigValue: p.m.Event.None });
      p.service._register(versionAction);
      const versionActionBeforeInstall = versionAction.enabled;
      const installed = await workbench.install(b);
      versionAction.update();
      const versionActionAfterInstall = versionAction.enabled;
      const prompt = { entered: Promise.withResolvers(), release: Promise.withResolvers() };
      workbench.dialogService = { prompt: async () => { prompt.entered.resolve(); return prompt.release.promise; } };
      await p.select('backend-b');
      const pending = workbench.install(b, { justification: '确认后沿原详情安装', isMachineScoped: false });
      await Promise.race([prompt.entered.promise, pending.then(() => { throw new Error('原安装在确认前结束'); })]);
      await p.select('backend-a'); prompt.release.resolve({ result: true });
      await pending;
      return { targetA: a.managementTarget, targetB: b.managementTarget,
        matches: inputB.matches(inputA), sameMatches: inputB.matches(new p.m.ExtensionsInput(b)),
        resources: [inputA.resource.toString(), inputB.resource.toString()], descriptions: [inputA.getDescription(), inputB.getDescription()],
        installedTarget: installed.local.installationTarget.backendId, oldDetailTarget: b.local.installationTarget.backendId,
        marketServer: b.server === workbench.extensionManagementServerService.webExtensionManagementServer,
        installedServer: installed.server === b.server, versionActionBeforeInstall, versionActionAfterInstall,
        writes: p.state.requests.filter(request => request.endpoint.endsWith('/install-gallery')).map(request => request.body.backend_id) };
    });
    expect(value.targetA).toMatchObject({ gateway_id: 'gateway-a', backend_id: 'backend-a' });
    expect(value.targetB).toMatchObject({ gateway_id: 'gateway-a', backend_id: 'backend-b' });
    expect(value.matches).toBe(false); expect(value.sameMatches).toBe(true);
    expect(value.resources[0]).not.toBe(value.resources[1]);
    expect(value.descriptions[0]).toContain('backend-a'); expect(value.descriptions[1]).toContain('backend-b');
    expect(value.installedTarget).toBe('backend-b'); expect(value.oldDetailTarget).toBe('backend-b');
    expect(value.marketServer).toBe(true); expect(value.installedServer).toBe(true);
    expect(value.versionActionBeforeInstall).toBe(false); expect(value.versionActionAfterInstall).toBe(true);
    expect(value.writes).toEqual(['backend-b', 'backend-b']);
    checks.push('原queryGallery/getExtensions、目标详情标签身份、旧B详情Install及确认等待均只写B');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原gallery首屏和ID查询迟到被拒绝，不把A结果复用成B已安装对象', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed, makeLocal } = p.createWorkbench(), results = [];
      for (const mode of ['query', 'ids']) {
        await p.select('backend-a');
        const gate = { entered: Promise.withResolvers(), release: Promise.withResolvers() };
        observed[mode === 'query' ? 'galleryGate' : 'getExtensionsGate'] = gate;
        const pending = (mode === 'query' ? workbench.queryGallery({}, p.m.CancellationToken.None)
          : workbench.getExtensions([{ id: p.gallery.identifier.id }], p.m.CancellationToken.None)).then(
            () => ({ status: 'fulfilled' }), error => ({ status: 'rejected', message: error.message }));
        await Promise.race([gate.entered.promise, pending.then(result => { throw new Error('原查询在Gallery前结束：' + JSON.stringify(result)); })]);
        await p.select('backend-b');
        const [localB] = await p.service.getInstalled(p.m.ExtensionType.User);
        observed.locals = [makeLocal(localB)]; gate.release.resolve(); results.push(await pending);
        observed.galleryGate = undefined; observed.getExtensionsGate = undefined;
      }
      return { results, synchronized: observed.synchronized.length, target: observed.locals[0].managementTarget };
    });
    expect(value.results.map(result => result.status)).toEqual(['rejected', 'rejected']);
    for (const result of value.results) expect(result.message).toMatch(/Canceled|目标.*改变|目标.*变更|目标.*切换/);
    expect(value.synchronized).toBe(0); expect(value.target).toMatchObject({ backend_id: 'backend-b' });
    checks.push('原首屏与公开ID查询在目标切换后拒绝迟到，零跨目标同步或对象复用');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原gallery分页切目标后拒绝旧页，旧详情目标不受新同ID库存对象影响', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed, makeLocal } = p.createWorkbench();
      await p.select('backend-a');
      const pager = await workbench.queryGallery({}, p.m.CancellationToken.None), old = pager.firstPage[0];
      await p.select('backend-b');
      const [localB] = await p.service.getInstalled(p.m.ExtensionType.User); observed.locals = [makeLocal(localB)];
      const result = await pager.getPage(1, p.m.CancellationToken.None).then(() => 'fulfilled', error => error.message);
      return { result, oldTarget: old.managementTarget, currentTarget: observed.locals[0].managementTarget,
        synchronized: observed.synchronized.length };
    });
    expect(value.result).toMatch(/Canceled|目标.*改变|目标.*变更|目标.*切换/);
    expect(value.oldTarget).toMatchObject({ backend_id: 'backend-a' }); expect(value.currentTarget).toMatchObject({ backend_id: 'backend-b' });
    expect(value.synchronized).toBe(1);
    checks.push('原gallery分页带目标代际，A详情保留目标而B同ID库存独立');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('Linux列表保留的Windows详情按原目标判断准入、查询指定版本及执行版本选择', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed } = p.createWorkbench();
      const windows = p.state.inventories.get('backend-b');
      windows.inventory.target_platform = 'win32-x64'; windows.inventory.extensions[0].description.targetPlatform = 'win32-x64';
      p.gallery.allTargetPlatforms = ['win32-x64']; p.gallery.properties.targetPlatform = 'win32-x64'; p.gallery.isSigned = true;
      await p.select('backend-b');
      const [b] = (await workbench.queryGallery({}, p.m.CancellationToken.None)).firstPage;
      await p.select('backend-a');
      const [a] = (await workbench.queryGallery({}, p.m.CancellationToken.None)).firstPage;
      const canB = await workbench.canInstall(b), canA = await workbench.canInstall(a);
      p.state.handlers.set('/api/gateway/extension-installations/install-gallery', body => {
        const reply = p.inventory(body.backend_id, body.version);
        reply.inventory.target_platform = body.backend_id === 'backend-b' ? 'win32-x64' : 'linux-x64';
        reply.inventory.extensions[0].description.targetPlatform = reply.inventory.target_platform;
        p.state.inventories.set(body.backend_id, structuredClone(reply));
        return { ...reply, installed: { id: p.gallery.identifier.id, version: body.version, profile_id: reply.inventory.profile_id, reused: false } };
      });
      const installed = await workbench.install(b, { version: '8.0.0' });
      const picker = { entered: Promise.withResolvers(), release: Promise.withResolvers() }, platforms = [];
      const action = { enabled: true, extension: b, extensionsWorkbenchService: workbench,
        extensionManagementService: workbench.extensionManagementService,
        extensionGalleryService: { getAllCompatibleVersions: async (_id, _pre, platform) => {
          platforms.push(platform); return [{ version: '7.0.0', date: '2026-11-08T00:00:00Z', isPreReleaseVersion: false }];
        } },
        quickInputService: { pick: async picks => { picker.entered.resolve(picks); await picker.release.promise; return picks[0]; } },
      };
      const pending = p.m.InstallAnotherVersionAction.prototype.run.call(action);
      await Promise.race([picker.entered.promise, pending.then(() => { throw new Error('原版本动作在选择器前结束'); })]);
      // 模拟同一原动作被新列表对象重新绑定；进行中的选择必须保留发起B详情。
      action.extension = a; picker.release.resolve(); await pending;
      return { canB, canA: canA === true ? true : canA.value, selectedPlatform: platforms,
        queryPlatforms: observed.galleryFetches.filter(item => item.infos[0].version).map(item => item.options?.targetPlatform),
        queries: observed.galleryFetches.filter(item => item.infos[0].version).map(item => item.infos[0].version),
        installed: installed.local.installationTarget.backendId,
        writes: p.state.requests.filter(request => request.endpoint.endsWith('/install-gallery')).map(request => request.body.backend_id) };
    });
    expect(value.canB).toBe(true); expect(value.canA).not.toBe(true); expect(value.canA).toContain('Linux');
    expect(value.selectedPlatform).toEqual(['win32-x64']); expect(value.queryPlatforms).toEqual(['win32-x64', 'win32-x64']);
    expect(value.queries).toEqual(['8.0.0', '7.0.0']); expect(value.installed).toBe('backend-b');
    expect(value.writes).toEqual(['backend-b', 'backend-b']);
    checks.push('真实原生库存A/Linux与B/Windows，旧B详情准入／指定版本／原版本选择动作均使用Windows且只写B');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原B详情卸载只清B对象，当前A同ID库存和卸载后详情归属保持', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed, makeLocal } = p.createWorkbench();
      await p.select('backend-b');
      const [localB] = await p.service.getInstalled(p.m.ExtensionType.User), b = makeLocal(localB);
      await p.select('backend-a');
      const a = makeLocal(p.initial[0]); observed.locals = [a];
      p.state.handlers.set('/api/gateway/extension-installations/uninstall', body => {
        const reply = p.inventory(body.backend_id, body.version); reply.inventory.extensions = [];
        p.state.inventories.set(body.backend_id, reply);
        return { ...reply, uninstalled: { id: p.gallery.identifier.id, version: body.version, profile_id: reply.inventory.profile_id } };
      });
      const before = { a: a.state, b: b.state };
      await workbench.uninstall(b);
      return { before, after: { a: a.state, b: b.state }, aVersion: a.local.manifest.version, bHasLocal: !!b.local,
        targetB: b.managementTarget.backend_id, installed: p.m.ExtensionState.Installed, uninstalled: p.m.ExtensionState.Uninstalled,
        writes: p.state.requests.filter(request => request.endpoint.endsWith('/uninstall')).map(request => request.body) };
    });
    expect(value.before).toEqual({ a: value.installed, b: value.installed });
    expect(value.after).toEqual({ a: value.installed, b: value.uninstalled });
    expect(value.aVersion).toBe('1.0.0'); expect(value.bHasLocal).toBe(false); expect(value.targetB).toBe('backend-b');
    expect(value.writes).toHaveLength(1); expect(value.writes[0]).toMatchObject({ backend_id: 'backend-b', profile_id: 'profile-backend-b', version: '2.0.0' });
    checks.push('原卸载／state消费者精确B、A同ID保持，清local后原详情目标仍B');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原窗口builtin安装版本冻结B并返回新目标对象，异步切A不改写原builtin', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const p = globalThis.managementProbe, { workbench, observed, makeLocal } = p.createWorkbench();
      const local = { ...p.initial[0], type: p.m.ExtensionType.System, isBuiltin: true,
        location: p.m.URI.parse('builtin:/probe.native'), installationTarget: { kind: 'web', profileId: 'browser-default' },
        installationProfileId: undefined, extensionKey: undefined };
      const builtin = makeLocal(local), gate = { entered: Promise.withResolvers(), release: Promise.withResolvers() };
      observed.locals = [builtin]; observed.getExtensionsGate = gate;
      p.state.handlers.set('/api/gateway/extension-installations/install-gallery', body => {
        const reply = p.inventory(body.backend_id, body.version);
        p.state.inventories.set(body.backend_id, structuredClone(reply));
        return { ...reply, installed: { id: p.gallery.identifier.id, version: body.version, profile_id: reply.inventory.profile_id, reused: false } };
      });
      await p.select('backend-b');
      const pending = workbench.install(builtin, { version: '8.0.0' });
      await Promise.race([gate.entered.promise, pending.then(() => { throw new Error('原builtin安装在版本查询前结束'); })]);
      await p.select('backend-a'); gate.release.resolve();
      const installed = await pending;
      return { sameObject: installed === builtin, installedTarget: installed.managementTarget,
        installedLocalTarget: installed.local.installationTarget,
        originalTarget: builtin.local.installationTarget, originalVersion: builtin.local.manifest.version,
        originalManagementTarget: builtin.managementTarget ?? null, originalLocation: builtin.local.location.toString(),
        writes: p.state.requests.filter(request => request.endpoint.endsWith('/install-gallery')).map(request => request.body.backend_id) };
    });
    expect(value.sameObject).toBe(false); expect(value.installedTarget).toMatchObject({ backend_id: 'backend-b' });
    expect(value.installedLocalTarget).toEqual({ kind: 'backend', gatewayId: 'gateway-a', backendId: 'backend-b' });
    expect(value.originalTarget).toEqual({ kind: 'web', profileId: 'browser-default' });
    expect(value.originalVersion).toBe('1.0.0'); expect(value.originalManagementTarget).toBeNull();
    expect(value.originalLocation).toBe('builtin:/probe.native'); expect(value.writes).toEqual(['backend-b']);
    checks.push('原窗口builtin指定版本转唯一原生B安装，查询等待切A仍B；新回执对象不改原builtin实例或Web归属');
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

test('原状态Action从精确Tracker视图说明Node不可运行，原runtimeState不再伪报重启', async () => {
  const { context, page } = await open();
  try {
    const values = await page.evaluate(async () => {
      const { m, initial, createRuntimeStatus } = globalThis.managementProbe, target = initial[0].installationTarget;
      const view = { installationTarget: target, viewId: 'default', workspaceIds: ['workspace-a'],
        capabilities: { installation_manage: true, host_manage: true } };
      const hybrid = { ...initial[0], manifest: { ...initial[0].manifest, browser: './browser.js', extensionKind: ['workspace', 'web'] } };
      const cases = [
        { name: 'empty', views: [{ ...view, workspaceIds: [] }] },
        { name: 'unavailable', views: [{ ...view, nodeRuntime: { channel_state: 'unavailable', reason: 'fixture unavailable' } }] },
        { name: 'draining', views: [{ ...view, nodeRuntime: { channel_state: 'draining', reason: 'fixture draining' } }] },
        { name: 'missing', views: [view] }, { name: 'unbound', views: [] },
        { name: 'permission', views: [{ ...view, capabilities: { installation_manage: true, host_manage: false } }] },
        { name: 'other-target', views: [{ ...view, installationTarget: { ...target, backendId: 'backend-b' }, nodeRuntime: { channel_state: 'available' } }] },
        { name: 'other-running', views: [view, { ...view, installationTarget: { ...target, backendId: 'backend-b' }, nodeRuntime: { channel_state: 'available' } }],
          running: { ...m.toExtensionDescription(initial[0]), version: '9.0.0', installationTarget: { ...target, backendId: 'backend-b' } } },
        { name: 'hybrid-unbound', local: hybrid, views: [] },
        { name: 'hybrid-permission', local: hybrid, views: [{ ...view, capabilities: { installation_manage: true, host_manage: false } }] },
        { name: 'unscoped-running', views: [view], running: { ...m.toExtensionDescription(initial[0]), installationTarget: undefined } },
      ];
      const results = [];
      for (const item of cases) {
        const probe = createRuntimeStatus(item.views, item.local ?? initial[0]);
        try {
          if (item.running) probe.state.running.push(item.running);
          const status = await probe.read(); await probe.runtimeAction.run();
          results.push({ name: item.name, ...status, canAddCalls: probe.state.canAddCalls, actions: probe.state.actions,
            enablementUnchanged: probe.extension.enablementState === m.EnablementState.EnabledGlobally });
        } finally { probe.dispose(); }
      }
      return results;
    });
    const reasons = ['需要挂接', 'fixture unavailable', 'fixture draining', 'runtime', '没有该安装目标的运行视图', '没有目标扩展宿主管理权限', '没有该安装目标的运行视图', 'runtime',
      '没有该安装目标的运行视图', '没有目标扩展宿主管理权限', 'runtime'];
    for (const [index, value] of values.entries()) {
      expect(value.messages).toHaveLength(1); expect(value.messages[0]).toContain('当前不可运行'); expect(value.messages[0]).toContain(reasons[index]);
      expect(value.warning).toBe(true); expect(value.runtime).toBeNull(); expect(value.restartEnabled).toBe(false);
      expect(value.canAddCalls).toBe(0); expect(value.actions).toEqual([]); expect(value.enablementUnchanged).toBe(true);
    }
    checks.push('原Action精确目标无根/未挂接/无权限/Node unavailable与draining明确警告，原runtimeState和运行Action零虚假重启且enablement不变');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('原状态入口保留合法Node、browser回退、声明式与窗口builtin的新安装重启行为', async () => {
  const { context, page } = await open();
  try {
    const values = await page.evaluate(async () => {
      const { initial, createRuntimeStatus } = globalThis.managementProbe, local = initial[0];
      const view = { installationTarget: local.installationTarget, viewId: 'default', workspaceIds: ['workspace-a'],
        capabilities: { installation_manage: true, host_manage: true }, nodeRuntime: { channel_state: 'available' } };
      const cases = [
        { name: 'node', local, views: [view] },
        { name: 'hybrid-web', local: { ...local, manifest: { ...local.manifest, browser: './browser.js' } }, views: [{ ...view, nodeRuntime: undefined }] },
        { name: 'browser', local: { ...local, manifest: { ...local.manifest, main: undefined, browser: './browser.js', extensionKind: undefined } }, views: [{ ...view, nodeRuntime: undefined }] },
        { name: 'declarative', local: { ...local, manifest: { ...local.manifest, main: undefined, extensionKind: undefined } }, views: [view] },
        { name: 'builtin', local: { ...local, isBuiltin: true, installationTarget: { kind: 'web', profileId: 'browser-default' } }, views: [] },
      ];
      const results = [];
      for (const item of cases) {
        const probe = createRuntimeStatus(item.views, item.local);
        try { results.push({ name: item.name, ...await probe.read() }); } finally { probe.dispose(); }
      }
      return results;
    });
    for (const value of values) {
      expect(value.messages).toEqual([]); expect(value.warning).toBe(false);
      expect(value.runtime).toMatchObject({ action: 'restartExtensions' }); expect(value.restartEnabled).toBe(true);
    }
    checks.push('合法Node、同包browser回退、browser-only、无脚本与原Web builtin仍交原新安装重启分支');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});

test('Node不可用诊断保留原invalid、用户禁用、策略及更新状态优先级', async () => {
  const { context, page } = await open();
  try {
    const value = await page.evaluate(async () => {
      const { m, initial, createRuntimeStatus } = globalThis.managementProbe, local = initial[0];
      const view = { installationTarget: local.installationTarget, viewId: 'default', workspaceIds: [],
        capabilities: { installation_manage: true, host_manage: true } };
      const cases = [
        { name: 'invalid', local: { ...local, isValid: false, validations: [[m.Severity.Error, 'fixture invalid manifest']] }, enablement: m.EnablementState.DisabledByInvalidExtension },
        { name: 'global', enablement: m.EnablementState.DisabledGlobally }, { name: 'workspace', enablement: m.EnablementState.DisabledWorkspace },
        { name: 'environment', enablement: m.EnablementState.DisabledByEnvironment }, { name: 'policy', enablement: m.EnablementState.DisabledByAllowlist },
      ];
      const priorities = [];
      for (const item of cases) {
        const probe = createRuntimeStatus([view], item.local ?? local);
        try { probe.extension.enablementState = item.enablement; priorities.push({ name: item.name, ...await probe.read() }); }
        finally { probe.dispose(); }
      }
      const update = createRuntimeStatus([view], local);
      try {
        update.state.running.push({ ...m.toExtensionDescription(local), version: '0.9.0' });
        const pendingUpdate = await update.read();
        update.extension.enablementState = m.EnablementState.DisabledGlobally;
        const pendingDisable = await update.read();
        const builtin = createRuntimeStatus([], { ...local, isBuiltin: true, installationTarget: { kind: 'web', profileId: 'browser-default' } });
        try {
          builtin.state.running.push(m.toExtensionDescription(local));
          builtin.extension.enablementState = m.EnablementState.DisabledGlobally;
          const otherTargetRunning = await builtin.read();
          const windowStatus = { id: new m.ExtensionIdentifier(local.identifier.id), installationTarget: { profileId: 'browser-default', kind: 'web' }, activationTimes: { fixture: true } };
          builtin.state.statuses = { backend: { id: windowStatus.id, installationTarget: local.installationTarget }, window: windowStatus };
          return { priorities, pendingUpdate, pendingDisable, otherTargetRunning,
            runtimeIdentity: builtin.runtimeStatus() === windowStatus };
        } finally { builtin.dispose(); }
      } finally { update.dispose(); }
    });
    const messages = ['fixture invalid manifest', 'disabled globally by the user', 'disabled for this workspace by the user', 'disabled by the environment', 'fixture policy denied'];
    for (const [index, status] of value.priorities.entries()) {
      expect(status.messages).toHaveLength(1); expect(status.messages[0]).toContain(messages[index]); expect(status.runtime).toBeNull();
    }
    expect(value.pendingUpdate.messages).toEqual([]);
    expect(value.pendingUpdate.runtime).toMatchObject({ action: 'restartExtensions' }); expect(value.pendingUpdate.runtime.reason).toContain('enable the updated extension');
    expect(value.pendingDisable.runtime).toMatchObject({ action: 'restartExtensions' }); expect(value.pendingDisable.runtime.reason).toContain('disable this extension');
    expect(value.otherTargetRunning.messages).toEqual(['This extension is disabled globally by the user.']);
    expect(value.otherTargetRunning.runtime).toBeNull(); expect(value.runtimeIdentity).toBe(true);
    checks.push('原invalid、全局/工作区禁用、环境与策略消息保持，旧运行版本更新和禁用仍有真实原重启动作');
  } finally { await page.evaluate(() => globalThis.managementProbe.service.dispose()); await context.close(); }
});
