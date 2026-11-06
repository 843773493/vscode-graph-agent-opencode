import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/workspace_services/extension_host/extension_installation_actions.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const bundle = join(run.artifactsDir, 'installation-actions.fixture.mjs');
const sourcePaths = [testFile, ...['scripts/sourceTransform.mjs', 'scripts/extensions/extensionBrowserTransform.mjs',
  'scripts/extensions/extensionOwnerTransform.mjs', 'scripts/extensions/extensionRunningTransform.mjs',
  'src/extensions/installation.ts', 'src/agentApi.ts', 'src/workspaceContext.ts', 'src/workspaceFiles.ts',
  'upstream/src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts',
  'upstream/src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'upstream/src/vs/workbench/services/extensions/common/extensionRunningLocationTracker.ts',
  'upstream/src/vs/platform/actions/common/actions.ts', 'upstream/src/vs/platform/commands/common/commands.ts'].map(path => 'src/clients/workbench/' + path),
  'src/clients/web/src/api/http.ts'];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint(), observations = [], browserEvents = [];
async function originalSource(relative) {
  const path = join(root, 'upstream/src/vs', relative);
  return ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
}
function selectNodes(source, predicate) {
  const nodes = [];
  const visit = node => { if (predicate(node)) nodes.push(node.getText(source)); ts.forEachChild(node, visit); };
  visit(source); return nodes;
}
const contribution = await originalSource('workbench/contrib/extensions/browser/extensions.contribution.ts');
const commandNames = ['SELECT_INSTALL_VSIX_EXTENSION_COMMAND_ID', 'INSTALL_EXTENSION_FROM_VSIX_COMMAND_ID'];
const calls = commandNames.map(id => {
  const matches = selectNodes(contribution, node => ts.isCallExpression(node) && node.expression.getText(contribution) === 'this.registerExtensionAction'
    && node.arguments[0]?.properties?.some(field => ts.isPropertyAssignment(field) && field.name.getText(contribution) === 'id' && field.initializer.getText(contribution) === id));
  if (matches.length !== 1) throw new Error('原 VSIX action 数量已改变：' + id);
  return matches[0];
});
function originalMethod(source, owner, name) {
  const matches = selectNodes(source, node => ts.isMethodDeclaration(node) && node.name.getText(source) === name && node.parent.name?.text === owner);
  if (matches.length !== 1) throw new Error('原方法数量已改变：' + owner + '.' + name);
  return matches[0];
}
const register = originalMethod(contribution, 'ExtensionsContributions', 'registerExtensionAction');
const abstractService = await originalSource('workbench/services/extensions/common/abstractExtensionService.ts');
const viewMethods = ['_setExtensionWorkspaceViews', 'getExtensionWorkspaceViews'].map(name => originalMethod(abstractService, 'AbstractExtensionService', name));
const fixture = `
import { DisposableStore } from '${upstream}base/common/lifecycle.js';
import { localize, localize2 } from '${upstream}nls.js';
import { Action2, MenuId, MenuRegistry, registerAction2 } from '${upstream}platform/actions/common/actions.js';
import { ContextKeyExpr } from '${upstream}platform/contextkey/common/contextkey.js';
import { ExtensionsLocalizedLabel } from '${upstream}platform/extensionManagement/common/extensionManagement.js';
import { ResourceContextKey } from '${upstream}workbench/common/contextkeys.js';
import { SELECT_INSTALL_VSIX_EXTENSION_COMMAND_ID, INSTALL_EXTENSION_FROM_VSIX_COMMAND_ID, VIEWLET_ID } from '${upstream}workbench/contrib/extensions/common/extensions.js';
import { installTargetVsix } from './src/clients/workbench/src/extensions/installation.js';
class OriginalInstallActions {
  readonly registrations = new DisposableStore();
  constructor() { ${calls.map(call => `this.registrations.add(${call});`).join('\n')} }
  ${register}
}
export function registerOriginalInstallActions() { return new OriginalInstallActions().registrations; }
export class OriginalViewOwner {
  constructor(private readonly _runningLocations) { }
  ${viewMethods.join('\n')}
}
export { DisposableStore, MenuId, MenuRegistry, ResourceContextKey, VIEWLET_ID };
export { URI } from '${upstream}base/common/uri.js';
export { Emitter } from '${upstream}base/common/event.js';
export { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
export { IFileDialogService } from '${upstream}platform/dialogs/common/dialogs.js';
export { INotificationService } from '${upstream}platform/notification/common/notification.js';
export { IProgressService } from '${upstream}platform/progress/common/progress.js';
export { IQuickInputService } from '${upstream}platform/quickinput/common/quickInput.js';
export { IWorkspaceContextService } from '${upstream}platform/workspace/common/workspace.js';
export { IExtensionService } from '${upstream}workbench/services/extensions/common/extensions.js';
export { IHostService } from '${upstream}workbench/services/host/browser/host.js';
export { ExtensionDescriptionRegistry } from '${upstream}workbench/services/extensions/common/extensionDescriptionRegistry.js';
export { ExtensionRunningLocationTracker } from '${upstream}workbench/services/extensions/common/extensionRunningLocationTracker.js';
export { workspaceFileUri, workspaceFileSource } from './src/clients/workbench/src/workspaceFiles.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { invalidateGatewayUserSession } from './src/clients/web/src/api/http.js';
`;
await writeFile(join(run.artifactsDir, 'original-installation-actions.ts'), fixture);
const built = await build({ stdin: { contents: fixture, loader: 'ts', resolveDir: resolve('.') }, outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const installPath = '/api/gateway/extension-installations/install';
const backends = { 'gateway-a': '11111111-1111-4111-8111-111111111111', 'gateway-b': '22222222-2222-4222-8222-222222222222' };
let browser, server, current;
function envelope(data, requestId, status = 200) {
  return Response.json({ code: 0, data, message: '', request_id: requestId }, { status, headers: { 'X-Request-ID': requestId } });
}
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === '/fixture.mjs') return new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } });
    if (!path.startsWith('/api/')) return new Response('<!doctype html><title>目标 VSIX 安装命令</title>', { headers: { 'Content-Type': 'text/html' } });
    const state = current, requestId = 'vsix-' + (state.requests.length + 1);
    const body = request.method === 'POST' ? await request.json() : undefined;
    const item = { path, method: request.method, body, token: request.headers.get('X-Local-Token'), requestId };
    state.requests.push(item);
    if (path === '/api/gateway/auth/local-credential') return envelope({ token: 'test-local-credential' }, requestId);
    if (path === '/api/gateway/users/current') return envelope({ user_id: 'test-user' }, requestId);
    if (path !== installPath) return Response.json({ detail: '未授权的额外请求：' + path }, { status: 500 });
    const index = state.requests.filter(entry => entry.path === installPath).length;
    state.installSeen.resolve(item);
    if (state.policy.holdInstall) await state.installGate.promise;
    if (state.policy.failAt === index) return Response.json({ detail: { code: 'target-install-rejected', message: '原安装后的权限或回执检查失败' } }, { status: state.policy.httpStatus ?? 409, headers: { 'X-Request-ID': requestId } });
    if (state.policy.userRequired) return Response.json({ detail: { code: 'user_session_required' } }, { status: 401, headers: { 'X-Request-ID': requestId } });
    if (state.policy.brokenBody) return new Response('{"code":0,"data":', { headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId } });
    const profile = 'profile-' + body.gateway_id, version = body.gateway_id === 'gateway-a' ? '8.7.6' : '2.4.8';
    const result = { protocol_version: 1, gateway_id: body.gateway_id, backend_id: body.backend_id,
      workspace_ids: body.workspace_ids, profile_scope: 'default', inventory: { profile_id: profile, extensions: [] },
      installed: { id: 'probe.from-receipt', version, profile_id: profile } };
    if (state.policy.badReceipt === 'target') result.backend_id = backends['gateway-b'];
    if (state.policy.badReceipt === 'view') result.workspace_ids = [...body.workspace_ids].reverse();
    if (state.policy.badReceipt === 'profile') result.installed.profile_id = 'another-profile';
    if (state.policy.badReceipt === 'version') result.installed.version = '';
    return envelope(result, requestId);
  } });
  run.addCleanup('安装API HTTP边界', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('安装命令Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    const after = await fingerprint(), drift = sourcePaths.filter(path => fingerprints[path] !== after[path]);
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, fingerprintsAfter: after, drift,
      boundary: '真实Chromium内运行AST提取的两条原VSIX action注册、原registerExtensionAction、原CommandsRegistry/MenuRegistry/ContextKey表达式、新安装owner、原workspaceFileUri/Source、workspaceContext、agentApi及共享HTTP/凭据/用户失效屏障。安装视图由真实Tracker和AST提取的原AbstractExtensionService setter/getter投影；registration等待、挂接根、QuickInput/原文件对话框服务、进度/通知/reload为具名UI边界。HTTP服务只提供具名Gateway安装回执和故障，不调用真实Gateway/Backend/Node或磁盘安装器；不证明完整主窗口渲染、用户权限策略、真实安装/profile或Windows。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
    if (drift.length) throw new Error('验证期间源码改变：' + drift.join(', '));
  }
});

async function inBrowser(name, config, body, policy = {}) {
  const state = { name, policy, requests: [], installSeen: Promise.withResolvers(), installGate: Promise.withResolvers() };
  current = state;
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}/case/${encodeURIComponent(name)}`);
    await page.evaluate(async config => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetime = new m.DisposableStore(), changed = new m.Emitter();
      lifetime.add(m.registerOriginalInstallActions()); lifetime.add(changed);
      const owners = { a1: { gatewayId: 'gateway-a', workspaceId: 'workspace-a1' }, a2: { gatewayId: 'gateway-a', workspaceId: 'workspace-a2' },
        b: { gatewayId: 'gateway-b', workspaceId: 'workspace-b' }, unknown: { gatewayId: 'gateway-a', workspaceId: 'unattached' } };
      const rootPath = key => 'boxteam://workspace/' + owners[key].workspaceId + '/resources/files/root';
      const resource = key => key === 'native' ? m.URI.file('/browser-local/probe.vsix') : key === 'invalid' ? null
        : key === 'root' ? m.workspaceFileUri(owners.a1, rootPath('a1'), rootPath('a1'))
          : m.workspaceFileUri(owners[key], rootPath(key) + '/packages/filename-1.0.0.vsix', rootPath(key));
      let folders = ['a1', 'a2', 'b'].map((key, index) => ({ uri: m.workspaceFileUri(owners[key], rootPath(key), rootPath(key)), name: key.toUpperCase(), index }));
      if (config.noRoots) folders = [];
      if (config.missingPeerRoot) folders = folders.filter(folder => m.workspaceFileSource(folder.uri).workspaceId !== 'workspace-a2');
      const registry = lifetime.add(new m.ExtensionDescriptionRegistry({ readActivationEvents: () => [] }, [], true));
      const tracker = new m.ExtensionRunningLocationTracker(registry, {}, {}, {}, {}, {}), owner = new m.OriginalViewOwner(tracker);
      const views = [ { installationTarget: { kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' }, viewId: 'default', workspaceIds: ['workspace-a2', 'workspace-a1'] },
        { installationTarget: { kind: 'backend', gatewayId: 'gateway-b', backendId: '22222222-2222-4222-8222-222222222222' }, viewId: 'default', workspaceIds: ['workspace-b'] } ];
      owner._setExtensionWorkspaceViews(config.noView ? views.slice(1) : config.noViewB ? views.slice(0, 1) : views);
      m.workspaceContext.publish({ gatewayId: 'control-gateway', workspaces: [], activeId: 'active-a1', settings: null, loading: false, error: null });
      const picks = [], dialogs = [], prompts = [], reports = [], gate = Promise.withResolvers(), registration = Promise.withResolvers();
      const p = { m, owner, tracker, picks, dialogs, prompts, reports, reloads: 0, started: false, completed: false,
        registrationCalls: 0, viewReads: 0, gate, registration,
        focus() { m.workspaceContext.publish({ ...m.workspaceContext.getSnapshot(), activeId: 'active-b' }); },
        invalidate(kind) {
          if (kind === 'identity') m.invalidateGatewayUserSession(0);
          else if (kind === 'gateway') m.workspaceContext.publish({ ...m.workspaceContext.getSnapshot(), gatewayId: 'another-control-gateway' });
          else { const removed = folders.find(folder => m.workspaceFileSource(folder.uri).workspaceId === owners[kind].workspaceId);
            folders = folders.filter(folder => folder !== removed); changed.fire({ added: [], removed: [removed], changed: [] }); }
        },
        replaceView() { owner._setExtensionWorkspaceViews([{ ...views[0], installationTarget: { ...views[0].installationTarget, backendId: '33333333-3333-4333-8333-333333333333' } }, views[1]]); },
        mutateResources(keys) { p.inputResources.splice(0, p.inputResources.length, ...keys.map(resource)); },
        releaseDialog(keys = ['a1']) { gate.resolve(keys.map(resource)); },
        result() { return { error: p.error, completed: p.completed, picks, dialogs, reports, reloads: p.reloads,
          registrationCalls: p.registrationCalls, viewReads: p.viewReads, rootListeners: changed.hasListeners(),
          prompts: prompts.map(prompt => ({ severity: prompt.severity, message: prompt.message, choices: prompt.choices.map(choice => choice.label) })) }; },
        async start(command, keys = ['a1'], singular = false) {
          p.started = true;
          const id = command === 'select' ? 'workbench.extensions.action.installVSIX' : 'workbench.extensions.command.installFromVSIX';
          const resources = keys.map(resource);
          p.inputResources = resources;
          p.outcome = Promise.resolve(m.CommandsRegistry.getCommand(id).handler(accessor, ...(command === 'select' ? [] : [singular ? resources[0] : resources])))
            .then(() => { p.error = null; }, error => { p.error = { name: error.name, message: error.message,
              cause: error.cause && { name: error.cause.name, message: error.cause.message, status: error.cause.status,
                detail: error.cause.detail, path: error.cause.path, requestId: error.cause.requestId } }; }).finally(() => { p.completed = true; });
          await p.outcome; return p.result();
        },
        async finish() { await p.outcome; return p.result(); },
        dispose() { lifetime.dispose(); },
      };
      const services = new Map([
        [m.IWorkspaceContextService, { getCompleteWorkspace: async () => ({ folders }), getWorkspace: () => ({ folders }), onDidChangeWorkspaceFolders: changed.event }],
        [m.IExtensionService, { whenInstalledExtensionsRegistered: async () => { p.registrationCalls++; if (config.holdRegistration) await registration.promise; },
          getExtensionWorkspaceViews: () => { p.viewReads++; return owner.getExtensionWorkspaceViews(); } }],
        [m.IQuickInputService, { pick: async (items, options, token) => { picks.push({ items: items.map(item => ({ label: item.label, description: item.description,
          detail: item.detail, root: item.root.toString(), workspaceIds: item.target.workspace_ids })), options }); p.pickToken = token;
          return config.cancelPick ? undefined : items[config.pickIndex ?? 0]; } }],
        [m.IFileDialogService, { showOpenDialog: async options => { dialogs.push({ ...options, defaultUri: options.defaultUri.toString() });
          if (config.focusDialog) { p.focus(); p.replaceView(); }
          return config.holdDialog ? gate.promise : config.cancelDialog ? undefined : (config.dialogKeys ?? ['a1']).map(resource); } }],
        [m.IProgressService, { withProgress: async (options, task) => { reports.push({ options }); return task({ report: value => {
          reports.push(value);
          if (config.invalidateAtSecondReport && reports.filter(report => report.message).length === 2) p.invalidate(config.invalidateAtSecondReport);
        } }); } }],
        [m.INotificationService, { prompt: (severity, message, choices) => { prompts.push({ severity, message, choices }); } }],
        [m.IHostService, { reload: async () => { p.reloads++; } }],
      ]);
      const accessor = { get: id => { if (!services.has(id)) throw new Error('安装命令不应读取其它服务：' + id); return services.get(id); } };
      globalThis.vsix = p;
    }, config);
    await body(page, state);
    expect(errors).toEqual([]);
    expect(state.requests.filter(item => ![installPath, '/api/gateway/auth/local-credential', '/api/gateway/users/current'].includes(item.path))).toEqual([]);
  } finally {
    state.installGate.resolve();
    observations.push({ name, requests: state.requests, outcome: await page.evaluate(() => globalThis.vsix?.result()).catch(() => undefined), pageErrors: errors });
    await page.evaluate(() => globalThis.vsix?.dispose()); await context.close();
  }
}
const invoke = (page, command = 'direct', keys = ['a1'], singular = false) => page.evaluate(input => globalThis.vsix.start(...input), [command, keys, singular]);
const installs = state => state.requests.filter(item => item.path === installPath);
const requestFor = (key, gateway = 'gateway-a') => ({ protocol_version: 1, gateway_id: gateway, backend_id: backends[gateway],
  workspace_ids: gateway === 'gateway-a' ? ['workspace-a2', 'workspace-a1'] : ['workspace-b'],
  source_uri: 'boxteam://workspace/' + key + '/resources/files/root/packages/filename-1.0.0.vsix' });

test('原两条命令和菜单在纯Web窗口可达，Explorer只接受工作区VSIX', async () => inBrowser('menus', {}, async page => {
  const result = await page.evaluate(() => {
    const { m, owner } = globalThis.vsix;
    const enabled = (menu, id, values) => m.MenuRegistry.getMenuItems(menu).filter(item => item.command?.id === id)
      .map(item => !item.when || item.when.evaluate({ getValue: key => values[key] }));
    const web = { hasLocalServer: false, hasRemoteServer: false, hasWebServer: true, viewContainer: m.VIEWLET_ID,
      resourceScheme: 'boxteam-file', resourceExtname: '.vsix' };
    return { palette: enabled(m.MenuId.CommandPalette, 'workbench.extensions.action.installVSIX', web),
      title: enabled(m.MenuId.ViewContainerTitle, 'workbench.extensions.action.installVSIX', web),
      explorer: enabled(m.MenuId.ExplorerContext, 'workbench.extensions.command.installFromVSIX', web),
      native: enabled(m.MenuId.ExplorerContext, 'workbench.extensions.command.installFromVSIX', { ...web, resourceScheme: 'file' }),
      text: enabled(m.MenuId.ExplorerContext, 'workbench.extensions.command.installFromVSIX', { ...web, resourceExtname: '.txt' }),
      views: owner.getExtensionWorkspaceViews().map(view => ({ installationTarget: view.installationTarget, viewId: view.viewId, workspaceIds: view.workspaceIds })) };
  });
  expect(result.palette).toEqual([true]); expect(result.title).toEqual([true]); expect(result.explorer).toEqual([true]);
  expect(result.native).toEqual([false]); expect(result.text).toEqual([false]);
  expect(result.views.map(view => [view.installationTarget.backendId, view.viewId, view.workspaceIds])).toEqual([
    [backends['gateway-a'], 'default', ['workspace-a2', 'workspace-a1']], [backends['gateway-b'], 'default', ['workspace-b']]]);
}));

for (const cancel of ['cancelPick', 'cancelDialog']) test(`${cancel}取消原服务零安装请求`, async () => inBrowser(cancel, { [cancel]: true }, async (page, state) => {
  const result = await invoke(page, 'select');
  expect(result.error).toBeNull(); expect(installs(state)).toEqual([]); expect(state.requests).toEqual([]);
  expect(result.dialogs).toHaveLength(cancel === 'cancelPick' ? 0 : 1); expect(result.rootListeners).toBe(false); expect(result.prompts).toEqual([]);
}));

test('原selector固定完整有序view，活动标签和后续view变化不改投，使用原回执版本及reload', async () => inBrowser('selected-freeze', { focusDialog: true }, async (page, state) => {
  const result = await invoke(page, 'select');
  expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
  expect(installs(state)[0].token).toBe('test-local-credential');
  expect(result.dialogs[0]).toMatchObject({ availableFileSystems: ['boxteam-file'], canSelectFiles: true, canSelectFolders: false, canSelectMany: true,
    filters: [{ name: 'VSIX Extensions', extensions: ['vsix'] }] });
  expect(result.dialogs[0].defaultUri).toBe(result.picks[0].items[0].root);
  expect(result.picks[0].items[0].detail).toContain(backends['gateway-a']);
  expect(result.picks[0].items[0].workspaceIds).toEqual(['workspace-a2', 'workspace-a1']);
  expect(result.prompts).toHaveLength(1); expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[0].message).toContain('当前运行版本尚未切换'); expect(result.prompts[0].choices).toEqual(['重新加载窗口']);
  expect(result.reloads).toBe(0); await page.evaluate(() => globalThis.vsix.prompts[0].choices[0].run());
  expect(await page.evaluate(() => globalThis.vsix.reloads)).toBe(1); expect(installs(state)).toHaveLength(1);
}));

test('原直接URI入口保留单URI合同，跨目标列表按原顺序安装并汇总一次通知', async () => inBrowser('direct-targets', {}, async (page, state) => {
  let result = await invoke(page, 'direct', ['a1'], true);
  expect(result.error).toBeNull(); expect(installs(state)[0].body).toEqual(requestFor('workspace-a1'));
  result = await invoke(page, 'direct', ['b', 'a2']);
  expect(result.error).toBeNull(); expect(result.picks).toEqual([]); expect(result.dialogs).toEqual([]);
  expect(installs(state).slice(1).map(item => item.body)).toEqual([requestFor('workspace-b', 'gateway-b'), requestFor('workspace-a2')]);
  expect(result.prompts).toHaveLength(2); expect(result.prompts[1].message).toContain('probe.from-receipt@2.4.8');
  expect(result.prompts[1].message).toContain('probe.from-receipt@8.7.6');
}));

for (const invalid of ['native', 'unknown', 'root', 'invalid']) test(`直接URI批次后项${invalid}非法时整批零安装`, async () => inBrowser('invalid-' + invalid, {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', invalid]);
  expect(result.error?.message).toBeTruthy(); expect(state.requests).toEqual([]); expect(result.prompts).toEqual([]);
}));

test('目标文件对话框返回另一已挂接根仍拒绝，零安装且不改投', async () => inBrowser('dialog-other-root', { dialogKeys: ['b'] }, async (page, state) => {
  const result = await invoke(page, 'select');
  expect(result.error?.message).toContain('本次选择'); expect(state.requests).toEqual([]); expect(result.prompts).toEqual([]);
}));

for (const config of [{ noView: true }, { noRoots: true }]) test(`${Object.keys(config)[0]}没有明确安装view时零发现零安装`, async () => inBrowser(Object.keys(config)[0], config, async (page, state) => {
  const result = await invoke(page, config.noRoots ? 'select' : 'direct');
  expect(result.error?.message).toBeTruthy(); expect(state.requests).toEqual([]); expect(result.prompts).toEqual([]);
}));

test('批次后项目标view缺失时前项零安装，完整view的其它根缺失也拒绝', async () => {
  await inBrowser('later-view-missing', { noViewB: true }, async (page, state) => {
    const result = await invoke(page, 'direct', ['a1', 'b']);
    expect(result.error?.message).toContain('安装视图'); expect(state.requests).toEqual([]);
  });
  await inBrowser('view-peer-root-missing', { missingPeerRoot: true }, async (page, state) => {
    const result = await invoke(page);
    expect(result.error?.message).toContain('工作区集合已改变'); expect(state.requests).toEqual([]);
  });
});

for (const invalidation of ['a2', 'identity', 'gateway']) test(`文件选择等待中${invalidation}失效，迟到资源零安装`, async () => inBrowser('dialog-invalidated-' + invalidation, { holdDialog: true }, async (page, state) => {
  await page.evaluate(() => { void globalThis.vsix.start('select'); });
  await page.waitForFunction(() => globalThis.vsix.dialogs.length === 1);
  await page.evaluate(kind => { globalThis.vsix.invalidate(kind); globalThis.vsix.releaseDialog(); }, invalidation);
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error?.message).toContain('已改变'); expect(state.requests).toEqual([]); expect(result.prompts).toEqual([]);
  expect(result.rootListeners).toBe(false);
}));

test('等待原extension注册完成前不读取view或写入，之后仍沿同一原命令安装', async () => inBrowser('registration', { holdRegistration: true }, async (page, state) => {
  await page.evaluate(() => { void globalThis.vsix.start('direct'); });
  await page.waitForFunction(() => globalThis.vsix.registrationCalls === 1);
  expect(await page.evaluate(() => globalThis.vsix.viewReads)).toBe(0); expect(state.requests).toEqual([]);
  await page.evaluate(() => globalThis.vsix.registration.resolve());
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error).toBeNull(); expect(result.viewReads).toBe(1); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
}));

test('首次await前冻结调用方URI数组，注册等待中的外部改写不改投或追加', async () => inBrowser('input-array-freeze', { holdRegistration: true }, async (page, state) => {
  await page.evaluate(() => { void globalThis.vsix.start('direct'); });
  await page.waitForFunction(() => globalThis.vsix.registrationCalls === 1);
  await page.evaluate(() => { globalThis.vsix.mutateResources(['b', 'a2']); globalThis.vsix.registration.resolve(); });
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
}));

test('已发安装等待中身份失效，迟到回执不提示成功也不重放', async () => inBrowser('receipt-invalidated', {}, async (page, state) => {
  await page.evaluate(() => { void globalThis.vsix.start('direct'); }); await state.installSeen.promise;
  await page.evaluate(() => globalThis.vsix.invalidate('identity')); state.installGate.resolve();
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error?.message).toContain('未取得成功安装回执'); expect(result.error?.cause?.message).toContain('已改变');
  expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
}, { holdInstall: true }));

for (const httpStatus of [403, 409]) test(`HTTP ${httpStatus}保留原cause/status/detail/request_id，失败批次不重放或继续后项`, async () => inBrowser('http-error-' + httpStatus, {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', 'b']);
  expect(result.error?.cause).toMatchObject({ name: 'HttpRequestError', status: httpStatus, path: installPath,
    detail: { code: 'target-install-rejected', message: '原安装后的权限或回执检查失败' }, requestId: installs(state)[0].requestId });
  expect(result.error?.message).toContain(result.error.cause.message); expect(result.error?.message).toContain('核实后再决定重试');
  expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
}, { failAt: 1, httpStatus }));

test('后项失败保留已确认成功回执，一次通知和原错误并存，第三项不安装', async () => inBrowser('partial-error', {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', 'b', 'a2']);
  expect(result.error?.cause?.status).toBe(409); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1'), requestFor('workspace-b', 'gateway-b')]);
  expect(result.prompts).toHaveLength(1); expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[0].message).not.toContain('2.4.8'); expect(result.reloads).toBe(0);
}, { failAt: 2 }));

for (const invalidation of ['a2', 'identity', 'gateway']) test(`首项已确认后${invalidation}中止后项，撤根保留回执且身份失效不泄露旧结果`, async () => inBrowser('partial-invalidated-' + invalidation,
  { invalidateAtSecondReport: invalidation }, async (page, state) => {
    const result = await invoke(page, 'direct', ['a1', 'b', 'a2']);
    expect(result.error?.cause?.message).toContain('已改变'); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
    expect(result.prompts).toHaveLength(invalidation === 'a2' ? 1 : 0);
    if (invalidation === 'a2') expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  }));

for (const badReceipt of ['target', 'view', 'profile', 'version']) test(`安装${badReceipt}回执不匹配只提示未确认，不重放或伪造成功`, async () => inBrowser('bad-receipt-' + badReceipt, {}, async (page, state) => {
  const result = await invoke(page);
  expect(result.error?.message).toContain('回执不匹配'); expect(result.error?.message).toContain('查询原目标库存');
  expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
}, { badReceipt }));

test('原HTTP正文截断保留诊断和cause，仅一次安装请求', async () => inBrowser('broken-body', {}, async (page, state) => {
  const result = await invoke(page);
  expect(result.error?.message).toContain('未取得成功安装回执'); expect(result.error?.message).toContain('核实后再决定重试');
  expect(result.error?.cause?.message).toContain(installPath); expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
}, { brokenBody: true }));

test('共享HTTP的user_session_required恢复触发原身份失效，不重发安装POST', async () => inBrowser('user-session-required', {}, async (page, state) => {
  const result = await invoke(page);
  expect(result.error?.message).toContain('未取得成功安装回执'); expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
  expect(state.requests.filter(item => item.path === '/api/gateway/users/current')).toHaveLength(1);
}, { userRequired: true }));
