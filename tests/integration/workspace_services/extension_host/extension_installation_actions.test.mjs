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
  'scripts/fileDialogTransform.mjs', 'upstream/src/vs/platform/files/common/fileService.ts',
  'upstream/src/vs/workbench/services/dialogs/browser/simpleFileDialog.ts',
  'scripts/extensions/extensionOwnerTransform.mjs', 'scripts/extensions/extensionRunningTransform.mjs', 'scripts/extensions/extensionInstanceTransform.mjs',
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
export { SimpleFileDialog } from '${upstream}workbench/services/dialogs/browser/simpleFileDialog.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { FileOperationError, FileOperationResult, toFileOperationResult, toFileSystemProviderErrorCode } from '${upstream}platform/files/common/files.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { OperatingSystem } from '${upstream}base/common/platform.js';
export { installBackendFiles, workspaceFileUri, workspaceFileSource } from './src/clients/workbench/src/workspaceFiles.js';
export { workspaceContext } from './src/clients/workbench/src/workspaceContext.js';
export { invalidateGatewayUserSession } from './src/clients/web/src/api/http.js';
export { selectExtensionManagementTargetCommand } from './src/clients/workbench/src/managementTarget.js';
`;
await writeFile(join(run.artifactsDir, 'original-installation-actions.ts'), fixture);
const built = await build({ stdin: { contents: fixture, loader: 'ts', resolveDir: resolve('.') }, outfile: bundle,
  bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const installPath = '/api/gateway/extension-installations/install';
const backends = { 'gateway-a': '11111111-1111-4111-8111-111111111111', 'gateway-b': '22222222-2222-4222-8222-222222222222' };
const workspaceOwners = {
  'workspace-a1': { gatewayId: 'gateway-a', name: 'A1' },
  'workspace-a2': { gatewayId: 'gateway-a', name: 'A2' },
  'workspace-b': { gatewayId: 'gateway-b', name: 'B' },
};
let browser, server, current;
function envelope(data, requestId, status = 200) {
  return Response.json({ code: 0, data, message: '', request_id: requestId }, { status, headers: { 'X-Request-ID': requestId } });
}
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, async fetch(request) {
    const url = new URL(request.url), path = url.pathname;
    if (path === '/fixture.mjs') return new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } });
    if (!path.startsWith('/api/')) return new Response('<!doctype html><title>目标 VSIX 安装命令</title>', { headers: { 'Content-Type': 'text/html' } });
    const state = current, requestId = 'vsix-' + (state.requests.length + 1);
    const body = request.method === 'POST' ? await request.json() : undefined;
    const item = { path, query: Object.fromEntries(url.searchParams), method: request.method, body, token: request.headers.get('X-Local-Token'), requestId,
      workspaceId: request.headers.get('X-BoxTeam-Workspace-Id'), gatewayId: request.headers.get('X-BoxTeam-Target-Gateway-Id') };
    state.requests.push(item);
    if (path === '/api/gateway/auth/local-credential') return envelope({ token: 'test-local-credential' }, requestId);
    if (path === '/api/gateway/users/current') return envelope({ user_id: 'test-user' }, requestId);
    if (path === '/api/gateway/management/gateways') return envelope({ local_gateway_id: 'gateway-a', items: [
      { gateway_id: 'gateway-a', name: '本地', connection_kind: 'local' },
      { gateway_id: 'gateway-b', name: '远端 B', connection_kind: 'remote_gateway', gateway_connection_id: 'connection-b' },
    ] }, requestId);
    if (path === '/api/gateway/extension-targets' && request.method === 'POST') {
      const gatewayId = body.gateway_id;
      const targetGate = state.targetResolveGate;
      if (targetGate) { targetGate.seen.resolve(item); await targetGate.release.promise; state.targetResolveGate = undefined; }
      if (state.policy.discoveryFailure) return Response.json({ detail: { code: 'target-discovery-rejected', message: '安装域发现失败' }, request_id: requestId }, { status: state.policy.discoveryFailure, headers: { 'X-Request-ID': requestId } });
      const capability = { installation_manage: !(gatewayId === 'gateway-a' && state.policy.hostOnlyA || gatewayId === 'gateway-b' && state.policy.hostOnlyB),
        host_manage: !(gatewayId === 'gateway-a' && state.policy.installOnlyA) };
      const targetIds = state.policy.multipleDomains && gatewayId === state.policy.multipleDomainsGateway
        ? [backends[gatewayId], '33333333-3333-4333-8333-333333333333'] : [backends[gatewayId]];
      const targets = state.policy.noTargets ? [] : targetIds.filter(Boolean).map(backend_id => ({ protocol_version: 1,
        gateway_id: gatewayId, backend_id, source: 'live', capabilities: capability }));
      return envelope({ protocol_version: 1, gateway_id: gatewayId, source: 'live', targets }, requestId);
    }
    if (path === '/api/gateway/workspaces' && request.method === 'GET') return envelope({ items: Object.entries(workspaceOwners)
      .filter(([, owner]) => owner.gatewayId === 'gateway-a' || owner.gatewayId === 'gateway-b')
      .map(([workspace_id, owner]) => ({ workspace_id, gateway_id: owner.gatewayId, name: owner.name, status: 'connected',
        backend_url: 'http://127.0.0.1:8010', connection_kind: 'local', runtime_action: 'none', checked_at: '2026-11-07T00:00:00Z',
        affected_workspace_ids: [], remote_gateways: [] })), active_workspace: null }, requestId);
    if (path === '/api/gateway/extension-targets/resolve' && request.method === 'POST') {
      const requestedWorkspace = body.workspace_ids?.[0], owner = workspaceOwners[requestedWorkspace];
      if (!owner || owner.gatewayId !== body.gateway_id) return Response.json({ detail: '来源工作区归属错误' }, { status: 409, headers: { 'X-Request-ID': requestId } });
      const backendId = body.backend_id ?? backends[owner.gatewayId];
      state.targetResolveCount = (state.targetResolveCount ?? 0) + 1;
      if (state.policy.resolveFailure && (!state.policy.resolveFailAt || state.targetResolveCount === state.policy.resolveFailAt)) {
        return Response.json({ detail: { code: 'target-resolve-rejected', message: '目标解析失败' }, request_id: requestId }, { status: state.policy.resolveFailure, headers: { 'X-Request-ID': requestId } });
      }
      return envelope({ protocol_version: 1, gateway_id: owner.gatewayId, workspace_ids: body.workspace_ids,
        backend_id: backendId, source: 'live', capabilities: { installation_manage: !(owner.gatewayId === 'gateway-a' && state.policy.hostOnlyA
          || owner.gatewayId === 'gateway-b' && state.policy.hostOnlyB),
          host_manage: true } }, requestId);
    }
    if (path === '/api/v1/workspace/files/roots' && request.method === 'GET') {
      const workspaceId = item.workspaceId;
      if (!workspaceOwners[workspaceId]) return Response.json({ detail: 'unknown workspace' }, { status: 404, headers: { 'X-Request-ID': requestId } });
      return envelope({ workspace_uri: `boxteam://workspace/${workspaceId}/resources/files/root`,
        filesystem_uri: `boxteam://workspace/${workspaceId}/resources/files/root`, filesystem_label: 'Workspace' }, requestId);
    }
    const fail = status => Response.json({ detail: { code: 'directory-read-failed', message: '原目录读取失败：' + requestId } },
      { status, headers: { 'X-Request-ID': requestId } });
    const emptySegment = () => Response.json({ detail: { owner: 'resource_uri_grammar', code: 'empty_segment', message: 'URI 不得有空 segment' }, request_id: requestId },
      { status: 400, headers: { 'X-Request-ID': requestId } });
    if (state.policy.realDialog && path.endsWith('/resources/navigation')) {
      if (body.uri.endsWith('/')) return emptySegment();
      const name = body.uri.split('/').at(-1), gate = state.statGates.get(name);
      if (gate) { gate.seen.resolve(item); await gate.release.promise; }
      if (state.policy.statFailure) return fail(state.policy.statFailure);
      if (state.policy.statFaults?.[name]) return fail(state.policy.statFaults[name]);
      if (name === 'missing-dir' || name === 'new.vsix' && !state.policy.existsNewFile) return fail(404);
      return envelope({ node: { uri: body.uri, resource_shape: name.endsWith('.vsix') ? 'file' : 'directory',
        stat: { changed_at_ms: 1, modified_at_ms: 2, size: 3 } } }, requestId);
    }
    if (state.policy.realDialog && path === '/api/v1/workspace/files') {
      const directory = url.searchParams.get('path');
      if (directory.endsWith('/')) return emptySegment();
      const name = directory.split('/').at(-1), fault = state.policy.directoryFaults?.[name];
      const gate = state.directoryGates.get(name);
      if (gate) { gate.seen.resolve(item); await gate.release.promise; }
      if (typeof fault === 'number') return fail(fault);
      if (fault === 'offline') return fail(503);
      const names = state.policy.empty || name === 'empty' ? [] : name === 'root'
        ? [['packages', 'directory'], ['slow', 'directory'], ['fast', 'directory'], ['empty', 'directory'], ['.hidden.vsix', 'file'], ['root.vsix', 'file']]
        : [['filename-1.0.0.vsix', 'file']];
      return envelope({ path: directory, root_path: directory.split('/resources/files/')[0] + '/resources/files/root',
        directory_key: { workspace_id: item.workspaceId, scope: 'workspace', root: directory.split('/resources/files/')[0] + '/resources/files/root', path: directory },
        directory_revision: 1, items: names.map(([name, kind]) => ({ name, kind, path: directory + '/' + encodeURIComponent(name) })),
        truncated: fault === 'truncated', limit: 500 }, requestId);
    }
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
      installed: { id: 'probe.from-receipt', version, profile_id: profile, reused: state.policy.reused ?? false } };
    if (state.policy.badReceipt === 'target') result.backend_id = backends['gateway-b'];
    if (state.policy.badReceipt === 'view') result.workspace_ids = ['workspace-a2'];
    if (state.policy.badReceipt === 'profile') result.installed.profile_id = 'another-profile';
    if (state.policy.badReceipt === 'version') result.installed.version = '';
    if (state.policy.badReceipt === 'reused') delete result.installed.reused;
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
      boundary: '真实 Chromium 中运行 AST 提取的两条原 VSIX action 注册、原 registerExtensionAction、原 CommandsRegistry/MenuRegistry/ContextKey 表达式、新安装 owner、原 workspaceFileUri/Source、workspaceContext、agentApi 及共享 HTTP/凭据/用户失效屏障。HTTP fixture 覆盖安装域发现、来源工作区列表、目标解析、工作区文件根和安装回执；目录组运行实际转换后的原 FileService、产品 WorkspaceFiles 和 SimpleFileDialog。QuickInput、路径/语言/上下文/存储服务为具名 UI 边界；文件对话框、进度/通知/reload 为具名边界。HTTP 服务不调用真实 Gateway/Backend/Node 或磁盘安装器；不证明完整主窗口渲染、真实权限/磁盘错误、安装/profile 或 Windows。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
    if (drift.length) throw new Error('验证期间源码改变：' + drift.join(', '));
  }
});

async function inBrowser(name, config, body, policy = {}) {
  const state = { name, policy: { ...config, ...policy, realDialog: config.realDialog }, requests: [], installSeen: Promise.withResolvers(), installGate: Promise.withResolvers(),
    directoryGates: new Map(), statGates: new Map() };
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
      const views = [ { installationTarget: { kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' }, viewId: 'default', workspaceIds: ['workspace-a2', 'workspace-a1'],
        capabilities: { installation_manage: !config.hostOnlyA, host_manage: !config.installOnlyA } },
        { installationTarget: { kind: 'backend', gatewayId: 'gateway-b', backendId: '22222222-2222-4222-8222-222222222222' }, viewId: 'default', workspaceIds: ['workspace-b'],
          capabilities: { installation_manage: !config.hostOnlyB, host_manage: true } } ];
      owner._setExtensionWorkspaceViews(config.noView ? views.slice(1) : config.noViewB ? views.slice(0, 1) : views);
      m.workspaceContext.publish({ gatewayId: 'gateway-a', workspaces: [], activeId: 'active-a1', settings: null, loading: false, error: null });
      const picks = [], dialogs = [], prompts = [], reports = [], gate = Promise.withResolvers(), registration = Promise.withResolvers();
      const filePicks = [], contextValues = new Map(), storageChanged = lifetime.add(new m.Emitter());
      let hiddenFiles = true;
      const files = config.realDialog ? lifetime.add(new m.FileService(new m.NullLogService())) : undefined;
      if (files) m.installBackendFiles(files, lifetime.add(new m.UriIdentityService(files)));
      const createFilePick = () => {
        const events = Object.fromEntries(['TriggerButton', 'Custom', 'Accept', 'ChangeActive', 'ChangeValue', 'Hide'].map(name => [name, new m.Emitter()]));
        const picker = { events, visible: false, disposed: false, disposeCalls: 0, lateWrites: 0, writes: [], _value: '', _busy: false, _items: [],
          activeItems: [], selectedItems: [], valueSelection: [0, 0],
          get value() { return this._value; }, set value(value) { if (this.disposed) this.lateWrites++; const changed = this._value !== value; this._value = value; if (changed) events.ChangeValue.fire(value); },
          get busy() { return this._busy; }, set busy(value) { if (this.disposed) this.lateWrites++; this._busy = value; this.writes.push({ busy: value }); },
          get items() { return this._items; }, set items(value) { if (this.disposed) this.lateWrites++; this._items = value; this.writes.push({ items: value.map(item => item.label) }); },
          show() { for (const other of filePicks) if (other !== this) other.hide(); this.visible = true; },
          hide() { if (this.visible) { this.visible = false; events.Hide.fire(); } }, inputHasFocus() { return false; },
          dispose() { this.disposeCalls++; this.hide(); this.disposed = true; for (const event of Object.values(events)) event.dispose(); },
          accept(label) { if (label) { const item = this.items.find(item => item.label === label); if (!item) throw new Error('原对话框没有目录项：' + label);
            this.activeItems = [item]; this.selectedItems = this.canSelectMany && !item.isFolder ? [item] : []; }
            events.Accept.fire(); },
        };
        for (const [name, event] of Object.entries(events)) picker['onDid' + name] = event.event;
        picker.onDidChangeValue = listener => events.ChangeValue.event(value => { picker.inputPromise = listener(value); });
        filePicks.push(picker); return picker;
      };
      const originalDialog = options => {
        const dialog = new m.SimpleFileDialog(files, { createQuickPick: createFilePick }, { getSeparator: () => '/' },
          { getWorkspace: () => ({ folders }) }, { info: message => prompts.push({ severity: 1, message, choices: [] }) },
          { preferredHome: async () => folders[0].uri }, { getModel: () => null }, { guessLanguageIdByFilepathOrFirstLine: () => null },
          {}, { getEnvironment: async () => null },
          { defaultUriScheme: 'boxteam-file', getOperatingSystem: async () => m.OperatingSystem.Linux, getPath: async () => ({ sep: '/' }), userHome: async () => folders[0].uri },
          { lookupKeybinding: () => undefined }, { createKey: (key, value) => { contextValues.set(key, value); return { set: value => contextValues.set(key, value) }; } },
          { isScreenReaderOptimized: () => false }, { getBoolean: () => hiddenFiles, onDidChangeValue: () => storageChanged.event,
            store: (_key, value) => { hiddenFiles = value; storageChanged.fire(); } });
        lifetime.add(dialog);
        const result = options.save ? dialog.showSaveDialog(options) : dialog.showOpenDialog(options);
        result.catch(() => {});
        return { dialog, result };
      };
      const p = { m, owner, tracker, views, picks, dialogs, prompts, reports, reloads: 0, started: false, completed: false,
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
        filePicks, files, originalDialog, root: folders[0]?.uri,
        openDialog(options = {}) {
          p.completed = false; p.error = undefined;
          const original = originalDialog({ defaultUri: folders[0]?.uri, availableFileSystems: ['boxteam-file'], canSelectFiles: true, ...options });
          p.activeDialog = original.dialog;
          p.outcome = original.result.then(value => { p.selection = Array.isArray(value) ? value.map(uri => uri.toString()) : value?.toString(); p.error = null; },
            error => { p.error = { name: error.name, message: error.message, cause: error.cause && { message: error.cause.message, status: error.cause.status } }; })
            .finally(() => { p.completed = true; });
          return p.outcome;
        },
        fileState() { const picker = filePicks.at(-1); return picker && { value: picker.value, busy: picker.busy, items: picker.items.map(item => item.label),
          visible: picker.visible, disposed: picker.disposed, disposeCalls: picker.disposeCalls, lateWrites: picker.lateWrites,
          currentFolder: p.activeDialog?.currentFolder?.toString(),
          pendingUpdate: !!p.activeDialog?.updatingPromise, context: contextValues.get('remoteFileDialogVisible') }; },
        result() { return { error: p.error, completed: p.completed, selection: p.selection, picks, dialogs, reports, reloads: p.reloads, fileState: p.fileState(),
          storageListeners: storageChanged.hasListeners(),
          registrationCalls: p.registrationCalls, viewReads: p.viewReads, rootListeners: changed.hasListeners(),
          prompts: prompts.map(prompt => ({ severity: prompt.severity, message: prompt.message, choices: prompt.choices.map(choice => choice.label) })) }; },
        async start(command, keys = ['a1'], singular = false) {
          p.started = true; p.completed = false; p.error = undefined;
          const id = command === 'select' ? 'workbench.extensions.action.installVSIX' : 'workbench.extensions.command.installFromVSIX';
          const resources = keys.map(resource);
          p.inputResources = resources;
          const outcome = m.CommandsRegistry.getCommand(id).handler(accessor, ...(command === 'select' ? [] : [singular ? resources[0] : resources]));
          accessorValid = false;
          p.outcome = Promise.resolve(outcome)
            .then(() => { p.error = null; }, error => { p.error = { name: error.name, message: error.message, status: error.status, requestId: error.requestId,
              cause: error.cause && { name: error.cause.name, message: error.cause.message, status: error.cause.status,
                detail: error.cause.detail, path: error.cause.path, requestId: error.cause.requestId,
                cause: error.cause.cause && { message: error.cause.cause.message, status: error.cause.cause.status, requestId: error.cause.cause.requestId } } }; }).finally(() => { p.completed = true; accessorValid = true; });
          await p.outcome; return p.result();
        },
        async finish() { await p.outcome; return p.result(); },
        async chooseExtensionTarget(gatewayId = 'gateway-a', domainIndex = 0) {
          p.targetGatewayId = gatewayId;
          p.targetDomainIndex = domainIndex;
          const outcome = m.CommandsRegistry.getCommand(m.selectExtensionManagementTargetCommand).handler(accessor);
          accessorValid = false;
          try {
            await outcome; p.error = null;
            return p.result();
          } catch (error) { p.error = { name: error.name, message: error.message }; return p.result(); }
          finally { accessorValid = true; }
        },
        dispose() { lifetime.dispose(); },
      };
      const services = new Map([
        [m.IWorkspaceContextService, { getCompleteWorkspace: async () => ({ folders }), getWorkspace: () => ({ folders }), onDidChangeWorkspaceFolders: changed.event }],
        [m.IExtensionService, { whenInstalledExtensionsRegistered: async () => { p.registrationCalls++; if (config.holdRegistration) await registration.promise; },
          getExtensionWorkspaceViews: () => { p.viewReads++; return owner.getExtensionWorkspaceViews(); } }],
        [m.IQuickInputService, { pick: async (items, options, token) => { const pickIndex = options.title === '扩展管理目标'
          ? items.findIndex(item => item.target.gateway_id === (p.targetGatewayId ?? config.targetGatewayId ?? 'gateway-a'))
          : options.title?.startsWith('选择扩展安装域') ? (p.targetDomainIndex ?? config.targetDomainIndex ?? 0)
            : options.title?.startsWith('选择 VSIX 来源工作区') && config.sourceWorkspaceId
              ? items.findIndex(item => item.workspace.workspace_id === config.sourceWorkspaceId) : config.pickIndex ?? 0;
          p.targetGatewayId = undefined; p.targetDomainIndex = undefined;
          picks.push({ items: items.map(item => ({ label: item.label, description: item.description,
          detail: item.detail, root: item.workspace ? item.workspace.workspace_id : item.target?.backend_id,
          workspaceIds: item.target?.workspace_ids, capabilities: item.target?.capabilities,
          workspaceId: item.workspace?.workspace_id })), options }); p.pickToken = token;
          return (config.cancelPick && options.title !== '扩展管理目标' || config.cancelDomainPick && options.title?.startsWith('选择扩展安装域'))
            ? undefined : items[pickIndex]; } }],
        [m.IFileDialogService, { showOpenDialog: async options => { dialogs.push({ ...options, defaultUri: options.defaultUri.toString() });
          if (config.focusDialog) { p.focus(); p.replaceView(); }
          if (config.realDialog) { const original = originalDialog(options); p.activeDialog = original.dialog; return original.result; }
          return config.holdDialog ? gate.promise : config.cancelDialog ? undefined : (config.dialogKeys ?? ['a1']).map(resource); } }],
        [m.IProgressService, { withProgress: async (options, task) => { reports.push({ options }); return task({ report: value => {
          reports.push(value);
          if (config.invalidateAtSecondReport && reports.filter(report => report.message).length === 2) p.invalidate(config.invalidateAtSecondReport);
        } }); } }],
        [m.INotificationService, { prompt: (severity, message, choices) => { prompts.push({ severity, message, choices }); } }],
        [m.IHostService, { reload: async () => { p.reloads++; } }],
      ]);
      let accessorValid = true;
      const accessor = { get: id => { if (!accessorValid) throw new Error('ServicesAccessor 在命令同步调用后已失效');
        if (!services.has(id)) throw new Error('安装命令不应读取其它服务：' + id); return services.get(id); } };
      globalThis.vsix = p;
    }, config);
    await body(page, state);
    expect(errors).toEqual([]);
    const managementPaths = ['/api/gateway/management/gateways', '/api/gateway/extension-targets', '/api/gateway/workspaces', '/api/gateway/extension-targets/resolve', '/api/v1/workspace/files/roots'];
    expect(state.requests.filter(item => ![installPath, '/api/gateway/auth/local-credential', '/api/gateway/users/current', ...managementPaths].includes(item.path)
      && !(config.realDialog && (item.path === '/api/v1/workspace/files' || item.path.endsWith('/resources/navigation'))))).toEqual([]);
    if (config.realDialog) expect(state.requests.filter(item => item.path === '/api/v1/workspace/files' || item.path.endsWith('/resources/navigation'))
      .filter(item => (item.body?.uri ?? item.query.path).endsWith('/'))).toEqual([]);
  } finally {
    state.installGate.resolve();
    for (const gate of state.directoryGates.values()) gate.release.resolve();
    for (const gate of state.statGates.values()) gate.release.resolve();
    state.targetResolveGate?.release.resolve();
    observations.push({ name, requests: state.requests, outcome: await page.evaluate(() => globalThis.vsix?.result()).catch(() => undefined), pageErrors: errors });
    await page.evaluate(() => globalThis.vsix?.dispose()); await context.close();
  }
}
const invoke = (page, command = 'direct', keys = ['a1'], singular = false) => page.evaluate(input => globalThis.vsix.start(...input), [command, keys, singular]);
const installs = state => state.requests.filter(item => item.path === installPath);
const beginPicker = page => page.evaluate(() => { void globalThis.vsix.start('select'); });
const pickerReady = page => page.waitForFunction(() => { const p = globalThis.vsix, state = p.fileState(); return state && !state.busy && !state.disposed && !p.completed; });
const pickerCompleted = page => page.waitForFunction(() => globalThis.vsix.completed);
const listRequests = state => state.requests.filter(item => item.path === '/api/v1/workspace/files');
const hold = () => ({ seen: Promise.withResolvers(), release: Promise.withResolvers() });
const requestFor = (key, gateway = 'gateway-a') => {
  const workspaceId = key.startsWith('workspace-') ? key : ({ a1: 'workspace-a1', a2: 'workspace-a2', b: 'workspace-b' })[key];
  return { protocol_version: 1, gateway_id: gateway, ...(gateway === 'gateway-b' ? { gateway_connection_id: 'connection-b' } : {}),
    backend_id: backends[gateway], workspace_ids: [workspaceId],
    source_uri: 'boxteam://workspace/' + workspaceId + '/resources/files/root/packages/filename-1.0.0.vsix' };
};

for (const fault of [403, 404, 503, 'truncated']) {
  test(`真实原文件对话框目录${fault}沿原action失败，不返回空目录或安装`, async () => inBrowser('original-directory-' + fault,
    { realDialog: true }, async (page, state) => {
      const result = await invoke(page, 'select');
      expect(result.error.message).toContain(fault === 'truncated' ? '分页游标' : '原目录读取失败：');
      if (typeof fault === 'number') expect((result.error.cause ?? result.error).status).toBe(fault);
      expect(result.completed).toBe(true); expect(installs(state)).toEqual([]);
      expect(listRequests(state)).toHaveLength(1);
      expect(listRequests(state)[0]).toMatchObject({ gatewayId: 'gateway-a', workspaceId: 'workspace-a1', query: { scope: 'workspace' } });
      expect(result.fileState).toMatchObject({ busy: false, visible: false, disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
    }, { directoryFaults: { root: fault } }));
}

test('原FileService区分stat缺失候选和readdir404，两者保留准确原cause', async () => inBrowser('original-resolve-phases',
  { realDialog: true }, async (page, state) => {
    const resolve = page => page.evaluate(async () => {
      const p = globalThis.vsix, m = p.m;
      try { await p.files.resolve(p.root); throw new Error('应拒绝失败目录'); }
      catch (error) { return { lookup: error instanceof m.FileOperationError, code: m.toFileSystemProviderErrorCode(error),
        operation: m.toFileOperationResult(error), message: error.message, cause: { status: error.cause?.status, requestId: error.cause?.requestId } }; }
    });
    const directory = await resolve(page);
    expect(directory).toMatchObject({ lookup: false, code: 'EntryNotFound', operation: 1, message: expect.stringContaining('原目录读取失败：'),
      cause: { status: 404, requestId: listRequests(state)[0].requestId } });
    const missing = await page.evaluate(async () => {
      const p = globalThis.vsix, m = p.m;
      try { await p.files.resolve(m.URI.joinPath(p.root, 'new.vsix')); throw new Error('应拒绝缺失候选'); }
      catch (error) { return { lookup: error instanceof m.FileOperationError, operation: m.toFileOperationResult(error), message: error.message,
        cause: { code: m.toFileSystemProviderErrorCode(error.cause), status: error.cause?.cause?.status, requestId: error.cause?.cause?.requestId } }; }
    });
    expect(missing).toMatchObject({ lookup: true, operation: 1, message: expect.stringContaining('原目录读取失败：'),
      cause: { code: 'EntryNotFound', status: 404, requestId: state.requests.at(-1).requestId } });
    expect(installs(state)).toEqual([]);
  }, { directoryFaults: { root: 404 } }));

for (const recursive of [true, false]) {
  test(`原FileService${recursive ? '递归目录' : '子项metadata'}拒绝不得丢项伪装成功`, async () => inBrowser('original-resolve-child-' + recursive,
    { realDialog: true }, async (page, state) => {
      const result = await page.evaluate(async recursive => {
        const p = globalThis.vsix;
        try { await p.files.resolve(p.root, recursive ? { resolveTo: [p.m.URI.joinPath(p.root, 'packages')] } : { resolveMetadata: true }); return { success: true }; }
        catch (error) { return { message: error.message, code: p.m.toFileSystemProviderErrorCode(error), status: error.cause?.status }; }
      }, recursive);
      expect(result).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), code: 'NoPermissions', status: 403 });
      expect(installs(state)).toEqual([]);
    }, recursive ? { directoryFaults: { packages: 403 } } : { statFaults: { 'root.vsix': 403 } }));
}

test('真实stat权限错误在创建picker前沿action保留cause，不能探测其它目录', async () => inBrowser('original-stat-denied',
  { realDialog: true }, async (page, state) => {
    const result = await invoke(page, 'select');
    expect(result.error).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), cause: { status: 403 } });
    expect(result.fileState).toBeUndefined(); expect(listRequests(state)).toEqual([]); expect(installs(state)).toEqual([]);
    expect(result.storageListeners).toBe(false);
    expect(state.requests.filter(item => item.path.endsWith('/resources/navigation'))).toHaveLength(1);
  }, { statFailure: 403 }));

test('folder-only祖先检查的原权限拒绝不能误报不存在', async () => inBrowser('original-folder-ancestor-denied',
  { realDialog: true }, async (page, state) => {
    await page.evaluate(() => { void globalThis.vsix.openDialog({ canSelectFiles: false, canSelectFolders: true }); }); await pickerReady(page);
    state.policy.statFaults = { root: 403 };
    const result = await page.evaluate(async () => {
      const p = globalThis.vsix;
      try { return { value: await p.activeDialog.validate(p.m.URI.joinPath(p.root, 'missing-dir', 'new.vsix')) }; }
      catch (error) { return { message: error.message, status: error.cause?.status, requestId: error.cause?.requestId }; }
    });
    expect(result).toMatchObject({ message: expect.stringContaining('原目录读取失败：'), status: 403, requestId: state.requests.at(-1).requestId });
    expect(state.requests.at(-1).body.uri).toBe('boxteam://workspace/workspace-a1/resources/files/root');
    expect(installs(state)).toEqual([]);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
  }));

test('合法空目录保持成功等待，用户取消一次结算并清理原picker', async () => inBrowser('original-empty',
  { realDialog: true }, async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    expect(await page.evaluate(() => globalThis.vsix.fileState())).toMatchObject({ items: [], busy: false, disposed: false, context: true });
    expect(listRequests(state)).toHaveLength(1);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.vsix.result());
    expect(result.error).toBeNull(); expect(installs(state)).toEqual([]);
    expect(result.fileState).toMatchObject({ disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
  }, { empty: true }));

test('原dialog目录尾分隔符只留在输入框，真实HTTP stat与list均无尾空段', async () => inBrowser('original-directory-separator',
  { realDialog: true }, async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    await page.evaluate(async () => { const p = globalThis.vsix, picker = p.filePicks.at(-1); picker.value = p.root.path + '/packages/'; await picker.inputPromise; });
    const current = await page.evaluate(() => globalThis.vsix.fileState());
    expect(current.value).toEndWith('/packages/'); expect(current.currentFolder).toEndWith('/packages/');
    expect(current).toMatchObject({ busy: false, visible: true, disposed: false });
    expect(listRequests(state).map(item => item.query.path)).toEqual([
      'boxteam://workspace/workspace-a1/resources/files/root', 'boxteam://workspace/workspace-a1/resources/files/root/packages',
    ]);
    expect(state.requests.filter(item => item.path.endsWith('/resources/navigation') && item.body.uri.endsWith('/packages')).map(item => item.body.uri))
      .toEqual(['boxteam://workspace/workspace-a1/resources/files/root/packages', 'boxteam://workspace/workspace-a1/resources/files/root/packages']);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
    expect((await page.evaluate(() => globalThis.vsix.result())).error).toBeNull(); expect(installs(state)).toEqual([]);
  }));

test('目录拒绝先终结，再解除busy；排队接受不能使用旧列表', async () => inBrowser('original-queued-accept',
  { realDialog: true }, async (page, state) => {
    const gate = hold(); state.directoryGates.set('root', gate);
    await beginPicker(page); await gate.seen.promise;
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept());
    expect(await page.evaluate(() => globalThis.vsix.completed)).toBe(false); expect(installs(state)).toEqual([]);
    gate.release.resolve(); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.vsix.result());
    expect(result.error.message).toContain('原目录读取失败：'); expect(result.fileState.busy).toBe(false); expect(installs(state)).toEqual([]);
  }, { directoryFaults: { root: 403 } }));

for (const entry of ['accept', 'input', 'hidden']) {
  test(`原${entry}入口的目录拒绝接回同一对话框Promise`, async () => inBrowser('original-entry-' + entry,
    { realDialog: true }, async (page, state) => {
      await beginPicker(page); await pickerReady(page);
      state.policy.directoryFaults = { root: 403, packages: 403 };
      await page.evaluate(entry => {
        const p = globalThis.vsix, picker = p.filePicks.at(-1);
        if (entry === 'accept') picker.accept('packages');
        else if (entry === 'hidden') picker.events.TriggerButton.fire({});
        else picker.value = p.root.path + '/packages/';
      }, entry);
      await pickerCompleted(page);
      const result = await page.evaluate(() => globalThis.vsix.result());
      expect(result.error.message).toContain('原目录读取失败：'); expect(installs(state)).toEqual([]);
      expect(listRequests(state)).toHaveLength(2);
      expect(result.fileState).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, pendingUpdate: false, context: false, lateWrites: 0 });
    }));
}

test('原对话框失败后明确重开恢复，仍只向冻结的A安装一次', async () => inBrowser('original-retry',
  { realDialog: true }, async (page, state) => {
    expect((await invoke(page, 'select')).error.message).toContain('原目录读取失败：');
    expect(installs(state)).toEqual([]); state.policy.directoryFaults = {};
    await beginPicker(page); await pickerReady(page);
    await page.evaluate(() => { const p = globalThis.vsix; p.focus(); p.replaceView(); p.filePicks.at(-1).accept('packages'); });
    await page.waitForFunction(() => globalThis.vsix.fileState()?.items.includes('filename-1.0.0.vsix') && !globalThis.vsix.fileState().busy);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept('filename-1.0.0.vsix')); await pickerCompleted(page);
    const result = await page.evaluate(() => globalThis.vsix.result());
    expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
    expect(result.fileState).toMatchObject({ disposed: true, disposeCalls: 1, pendingUpdate: false, context: false });
  }, { directoryFaults: { root: 403 } }));

for (const oldFault of [undefined, 403]) {
  test(`旧目录请求取消后迟到${oldFault ?? '成功'}不清除新busy或覆盖items`, async () => inBrowser('original-stale-' + oldFault,
    { realDialog: true }, async (page, state) => {
      await beginPicker(page); await pickerReady(page);
      const slow = hold(), fast = hold(); state.directoryGates.set('slow', slow); state.directoryGates.set('fast', fast);
      state.policy.directoryFaults = { slow: oldFault };
      await page.evaluate(() => { const p = globalThis.vsix; p.filePicks.at(-1).value = p.root.path + '/slow/'; }); await slow.seen.promise;
      await page.evaluate(() => { const p = globalThis.vsix; p.filePicks.at(-1).value = p.root.path + '/fast/'; }); await fast.seen.promise;
      const late = page.waitForResponse(response => new URL(response.url()).searchParams.get('path')?.replace(/\/$/, '').endsWith('/slow'));
      slow.release.resolve(); await (await late).finished();
      const waiting = await page.evaluate(() => ({ state: globalThis.vsix.fileState(), completed: globalThis.vsix.completed }));
      expect(waiting.completed).toBe(false); expect(waiting.state).toMatchObject({ busy: true, pendingUpdate: true, disposed: false });
      expect(waiting.state.value).toEndWith('/fast/'); expect(waiting.state.items).toContain('packages');
      fast.release.resolve(); await pickerReady(page);
      expect(await page.evaluate(() => globalThis.vsix.fileState())).toMatchObject({ items: ['..', 'filename-1.0.0.vsix'], busy: false, pendingUpdate: false });
      await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
      expect((await page.evaluate(() => globalThis.vsix.result())).error).toBeNull(); expect(installs(state)).toEqual([]);
    }));
}

for (const parentProbe of [false, true]) for (const lateFault of [undefined, 403]) {
test(`旧输入${parentProbe ? '父目录' : '目录'}stat迟到${lateFault ?? '成功'}不能重启旧读取或报错`, async () => inBrowser('original-stale-stat-' + parentProbe + '-' + lateFault,
  { realDialog: true }, async (page, state) => {
    await beginPicker(page); await pickerReady(page);
    const gate = hold(); state.statGates.set('slow', gate); state.policy.statFaults = { slow: lateFault };
    await page.evaluate(parentProbe => { const p = globalThis.vsix, picker = p.filePicks.at(-1);
      picker.value = p.root.path + (parentProbe ? '/slow/new.vsix' : '/slow/'); p.slowInput = picker.inputPromise; }, parentProbe);
    await gate.seen.promise;
    await page.evaluate(async () => { const p = globalThis.vsix, picker = p.filePicks.at(-1); picker.value = p.root.path + '/fast/'; await picker.inputPromise; });
    expect((await page.evaluate(() => globalThis.vsix.fileState())).currentFolder).toEndWith('/fast/');
    gate.release.resolve(); await page.evaluate(() => globalThis.vsix.slowInput);
    const current = await page.evaluate(() => globalThis.vsix.fileState());
    expect(current.currentFolder).toEndWith('/fast/'); expect(current.value).toEndWith('/fast/');
    expect(await page.evaluate(() => globalThis.vsix.completed)).toBe(false);
    expect(listRequests(state).map(item => item.query.path.replace(/\/$/, '').split('/').at(-1))).toEqual(['root', 'fast']);
    expect(installs(state)).toEqual([]);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
  }));
}

test('加载期间取消立即结算，迟到失败不更新已dispose控件', async () => inBrowser('original-cancel-pending',
  { realDialog: true }, async (page, state) => {
    const gate = hold(); state.directoryGates.set('root', gate);
    await beginPicker(page); await gate.seen.promise;
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
    const before = await page.evaluate(() => globalThis.vsix.fileState());
    const late = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/workspace/files');
    gate.release.resolve(); await (await late).finished();
    expect(await page.evaluate(() => globalThis.vsix.fileState())).toEqual(before);
    expect(before).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, pendingUpdate: false, lateWrites: 0, context: false });
    expect((await page.evaluate(() => globalThis.vsix.result())).error).toBeNull(); expect(installs(state)).toEqual([]);
  }, { directoryFaults: { root: 403 } }));

for (const missingParent of [false, true]) {
  test(`保存新文件${missingParent ? '及缺失父目录确认' : '候选'}保留原返回URI合同`, async () => inBrowser('original-save-' + missingParent,
    { realDialog: true }, async (page, state) => {
      await page.evaluate(missingParent => { const p = globalThis.vsix; void p.openDialog({ save: true,
        defaultUri: p.m.URI.joinPath(p.root, ...(missingParent ? ['missing-dir', 'new.vsix'] : ['new.vsix'])) }); }, missingParent);
      await pickerReady(page); await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept());
      if (missingParent) {
        await page.waitForFunction(() => globalThis.vsix.filePicks.length === 2 && globalThis.vsix.filePicks.at(-1).visible);
        expect(await page.evaluate(() => globalThis.vsix.completed)).toBe(false);
        await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept());
      }
      await pickerCompleted(page); const result = await page.evaluate(() => globalThis.vsix.result());
      expect(result.error).toBeNull(); expect(result.selection).toEndWith(missingParent ? '/missing-dir/new.vsix' : '/new.vsix');
      expect(installs(state)).toEqual([]);
      expect(await page.evaluate(() => globalThis.vsix.filePicks.map(picker => ({ disposed: picker.disposed, lateWrites: picker.lateWrites })))).toEqual(
        Array.from({ length: missingParent ? 2 : 1 }, () => ({ disposed: true, lateWrites: 0 })));
    }));
}

test('关闭后接受验证的迟到stat不会创建确认prompt或安装', async () => inBrowser('original-cancel-validating',
  { realDialog: true }, async (page, state) => {
    await page.evaluate(() => { const p = globalThis.vsix; void p.openDialog({ save: true, defaultUri: p.m.URI.joinPath(p.root, 'new.vsix') }); });
    await pickerReady(page); state.policy.existsNewFile = true;
    const gate = hold(); state.statGates.set('new.vsix', gate);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept()); await gate.seen.promise;
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
    const late = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/resources/navigation') && response.request().postDataJSON().uri.endsWith('/new.vsix'));
    gate.release.resolve(); await (await late).finished();
    const result = await page.evaluate(() => ({ result: globalThis.vsix.result(), picks: globalThis.vsix.filePicks.length }));
    expect(result.picks).toBe(1); expect(result.result.error).toBeNull(); expect(result.result.selection).toBeUndefined();
    expect(result.result.fileState).toMatchObject({ disposed: true, pendingUpdate: false, lateWrites: 0 }); expect(installs(state)).toEqual([]);
  }));

test('确认prompt期间dispose结清原validate和picker，不能复活原控件', async () => inBrowser('original-dispose-confirmation',
  { realDialog: true }, async (page, state) => {
    await page.evaluate(() => { const p = globalThis.vsix; void p.openDialog({ save: true, defaultUri: p.m.URI.joinPath(p.root, 'new.vsix') }); });
    await pickerReady(page); state.policy.existsNewFile = true;
    await page.evaluate(() => { const p = globalThis.vsix; p.validation = p.activeDialog.validate(p.m.URI.joinPath(p.root, 'new.vsix'))
      .then(value => { p.validationValue = value; p.validationCompleted = true; }); });
    await page.waitForFunction(() => globalThis.vsix.filePicks.length === 2 && globalThis.vsix.filePicks.at(-1).visible);
    await page.evaluate(() => globalThis.vsix.activeDialog.dispose()); await pickerCompleted(page);
    await page.waitForFunction(() => globalThis.vsix.validationCompleted);
    expect(await page.evaluate(() => ({ value: globalThis.vsix.validationValue, selection: globalThis.vsix.selection,
      picks: globalThis.vsix.filePicks.map(picker => ({ disposed: picker.disposed, visible: picker.visible, lateWrites: picker.lateWrites })) })))
      .toEqual({ value: false, selection: undefined, picks: [{ disposed: true, visible: false, lateWrites: 0 }, { disposed: true, visible: false, lateWrites: 0 }] });
    expect(installs(state)).toEqual([]);
  }));

for (const denied of [false, true]) {
test(`非工作区scheme${denied ? '拒绝仍按原宽容合同' : '正常空目录'}沿原picker取消`, async () => inBrowser('original-native-contract-' + denied,
  { realDialog: true }, async (page, state) => {
    const result = await page.evaluate(async denied => {
      const p = globalThis.vsix, m = p.m;
      const provider = denied ? new class extends m.InMemoryFileSystemProvider { async readdir() { throw new Error('原native目录失败'); } }()
        : new m.InMemoryFileSystemProvider();
      const registration = p.files.registerProvider('file', provider);
      const stat = await p.files.resolve(m.URI.file('/'));
      p.nativeRegistration = registration; p.nativeProvider = provider;
      void p.openDialog({ defaultUri: m.URI.file('/'), availableFileSystems: ['file'] });
      return { isDirectory: stat.isDirectory, children: stat.children };
    }, denied);
    expect(result).toEqual({ isDirectory: true, children: [] }); await pickerReady(page);
    expect((await page.evaluate(() => globalThis.vsix.fileState())).items).toEqual([]);
    await page.evaluate(() => globalThis.vsix.filePicks.at(-1).hide()); await pickerCompleted(page);
    const canceled = await page.evaluate(() => { const p = globalThis.vsix, result = p.result(); p.nativeRegistration.dispose(); p.nativeProvider.dispose(); return result; });
    expect(canceled.error).toBeNull(); expect(canceled.selection).toBeUndefined();
    expect(canceled.fileState).toMatchObject({ busy: false, disposed: true, lateWrites: 0, context: false });
    expect(installs(state)).toEqual([]); expect(listRequests(state)).toEqual([]);
  }));
}

test('非工作区scheme原picker成功选择仍返回原file URI', async () => inBrowser('original-native-success',
  { realDialog: true }, async (page, state) => {
    await page.evaluate(async () => {
      const p = globalThis.vsix, m = p.m, provider = new m.InMemoryFileSystemProvider();
      const registration = p.files.registerProvider('file', provider);
      await provider.writeFile(m.URI.file('/native.vsix'), new Uint8Array([1]), { create: true, overwrite: false });
      p.nativeRegistration = registration; p.nativeProvider = provider;
      void p.openDialog({ defaultUri: m.URI.file('/'), availableFileSystems: ['file'] });
    });
    await pickerReady(page); await page.evaluate(() => globalThis.vsix.filePicks.at(-1).accept('native.vsix')); await pickerCompleted(page);
    const result = await page.evaluate(() => { const p = globalThis.vsix, result = p.result(); p.nativeRegistration.dispose(); p.nativeProvider.dispose(); return result; });
    expect(result.error).toBeNull(); expect(result.selection).toEqual(['file:///native.vsix']);
    expect(result.fileState).toMatchObject({ busy: false, disposed: true, disposeCalls: 1, lateWrites: 0, context: false });
    expect(installs(state)).toEqual([]); expect(listRequests(state)).toEqual([]);
  }));

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

test('原宿主view投影保持独立，安装目标权限只由Gateway实时解析', async () => inBrowser('installation-only', { installOnlyA: true, hostOnlyB: true }, async (page, state) => {
  const projection = await page.evaluate(() => {
    const { owner, tracker, views } = globalThis.vsix, projected = owner.getExtensionWorkspaceViews();
    return { sameViews: projected.map((view, index) => view === views[index]), sameCapabilities: projected.map((view, index) => view.capabilities === views[index].capabilities),
      capabilities: projected.map(view => view.capabilities), workspaceIds: projected.map(view => view.workspaceIds),
      hosts: tracker.remoteRunningLocations.map(location => location.installationTarget.backendId) };
  });
  expect(projection).toEqual({ sameViews: [true, true], sameCapabilities: [true, true],
    capabilities: [{ installation_manage: true, host_manage: false }, { installation_manage: false, host_manage: true }],
    workspaceIds: [['workspace-a2', 'workspace-a1'], ['workspace-b']], hosts: [backends['gateway-b']] });
  const result = await invoke(page, 'direct', ['a1']);
  expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
  expect(result.picks).toEqual([]); expect(result.prompts).toHaveLength(1);
  expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
}));

test('标题栏选择 B 后连续安装沿用其 Backend，无逐次目标选择且焦点变化不改投', async () => inBrowser('selected-freeze', { focusDialog: true, targetGatewayId: 'gateway-b', dialogKeys: ['b'] }, async (page, state) => {
  await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-b'));
  const targetPickCount = await page.evaluate(() => globalThis.vsix.picks.filter(pick => pick.options.title === '扩展管理目标').length);
  const first = await invoke(page, 'select', ['b']);
  const second = await invoke(page, 'direct', ['b']);
  const result = second;
  expect(first.error).toBeNull(); expect(result.error).toBeNull();
  expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-b', 'gateway-b'), requestFor('workspace-b', 'gateway-b')]);
  expect(await page.evaluate(() => globalThis.vsix.picks.filter(pick => pick.options.title === '扩展管理目标').length)).toBe(targetPickCount);
  expect(result.dialogs).toHaveLength(1);
  expect(state.requests.filter(item => item.path === '/api/gateway/extension-targets/resolve').map(item => item.body)).toEqual([
    { protocol_version: 1, gateway_id: 'gateway-b', workspace_ids: ['workspace-b'], gateway_connection_id: 'connection-b', backend_id: backends['gateway-b'] },
    { protocol_version: 1, gateway_id: 'gateway-b', workspace_ids: ['workspace-b'], gateway_connection_id: 'connection-b', backend_id: backends['gateway-b'] },
  ]);
  expect(result.prompts.at(-1).message).toContain('gateway-b');
}));

test('文件对话框等待期间切换管理目标不改写已冻结的安装', async () => inBrowser('selected-freeze', { focusDialog: true, holdDialog: true, targetGatewayId: 'gateway-a' }, async (page, state) => {
  await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-a'));
  await page.evaluate(() => { void globalThis.vsix.start('select'); });
  await page.waitForFunction(() => globalThis.vsix.dialogs.length === 1);
  await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-b'));
  await page.evaluate(() => globalThis.vsix.releaseDialog());
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
  expect(installs(state)[0].token).toBe('test-local-credential');
  expect(result.dialogs).toHaveLength(1);
  expect(result.dialogs[0]).toMatchObject({ availableFileSystems: ['boxteam-file'], canSelectFiles: true, canSelectFolders: false, canSelectMany: true,
    filters: [{ name: 'VSIX Extensions', extensions: ['vsix'] }] });
  expect(result.dialogs[0].defaultUri).toContain('workspace-a1');
  expect(result.picks.at(-1).items[0].workspaceId).toBe('workspace-a1');
  expect(state.requests.filter(item => item.path === '/api/gateway/extension-targets/resolve').map(item => item.body)).toEqual([
    { protocol_version: 1, gateway_id: 'gateway-a', workspace_ids: ['workspace-a1'], backend_id: backends['gateway-a'] },
  ]);
  expect(result.prompts).toHaveLength(1); expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[0].message).toContain('当前运行版本尚未切换'); expect(result.prompts[0].choices).toEqual(['重新加载窗口']);
  expect(result.reloads).toBe(0); await page.evaluate(() => globalThis.vsix.prompts[0].choices[0].run());
  expect(await page.evaluate(() => globalThis.vsix.reloads)).toBe(1); expect(installs(state)).toHaveLength(1);
}));

test('多安装域未从标题栏选择时安装明确拒绝，选择后才沿用所选 Backend', async () => inBrowser('multiple-install-domains',
  { multipleDomains: true, multipleDomainsGateway: 'gateway-a', targetGatewayId: 'gateway-a', targetDomainIndex: 1 }, async (page, state) => {
    let result = await invoke(page, 'direct', ['a1']);
    expect(result.error?.message).toContain('从扩展视图标题工具栏选择安装域'); expect(installs(state)).toEqual([]);
    await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-a', 1));
    const titlePicks = await page.evaluate(() => globalThis.vsix.picks.map(pick => pick.options.title));
    expect(titlePicks).toEqual(['扩展管理目标', '选择扩展安装域 · 本地']);
    expect(state.requests.filter(item => item.path === '/api/gateway/extension-targets').map(item => item.body)).toEqual([
      { protocol_version: 1, gateway_id: 'gateway-a' },
      { protocol_version: 1, gateway_id: 'gateway-a' },
    ]);
    result = await invoke(page, 'direct', ['a2']);
    expect(result.error).toBeNull(); expect(installs(state).map(item => item.body)).toEqual([{
      protocol_version: 1, gateway_id: 'gateway-a', backend_id: '33333333-3333-4333-8333-333333333333',
      workspace_ids: ['workspace-a2'], source_uri: 'boxteam://workspace/workspace-a2/resources/files/root/packages/filename-1.0.0.vsix',
    }]);
  }));

test('标题栏取消安装域选择保留原管理目标且不安装', async () => inBrowser('cancel-domain-selection',
  { multipleDomains: true, multipleDomainsGateway: 'gateway-a', cancelDomainPick: true }, async (page, state) => {
    await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-a'));
    expect(await page.evaluate(() => globalThis.vsix.picks.map(pick => pick.options.title))).toEqual(['扩展管理目标', '选择扩展安装域 · 本地']);
    const result = await invoke(page, 'direct', ['a1']);
    expect(result.error?.message).toContain('有多个安装域'); expect(installs(state)).toEqual([]);
  }));

test('直接URI来源不隐式切换目标；同Gateway多来源工作区按原顺序安装', async () => inBrowser('direct-targets', {}, async (page, state) => {
  let result = await invoke(page, 'direct', ['a1'], true);
  expect(result.error).toBeNull(); expect(installs(state)[0].body).toEqual(requestFor('workspace-a1'));
  result = await invoke(page, 'direct', ['a2']);
  expect(result.error).toBeNull(); expect(result.picks).toEqual([]); expect(result.dialogs).toEqual([]);
  expect(installs(state).slice(1).map(item => item.body)).toEqual([requestFor('workspace-a2')]);
  expect(result.prompts).toHaveLength(2); expect(result.prompts[1].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[1].message).not.toContain('probe.from-receipt@2.4.8');
}));

test('跨Gateway直接来源在整个批次零安装拒绝', async () => inBrowser('cross-gateway-source', {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', 'b']);
  expect(result.error?.message).toContain('当前不支持跨 Gateway 来源');
  expect(installs(state)).toEqual([]); expect(result.prompts).toEqual([]);
}));

test('所选安装域缺少安装权限时来源解析拒绝且不安装', async () => inBrowser('installation-permission-denied',
  { hostOnlyB: true, targetGatewayId: 'gateway-b' }, async (page, state) => {
    await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-b'));
    const result = await invoke(page, 'direct', ['b']);
    expect(result.error?.message).toContain('未授权扩展安装管理'); expect(installs(state)).toEqual([]);
    expect(state.requests.filter(item => item.path === '/api/gateway/extension-targets').map(item => item.body)).toEqual([
      { protocol_version: 1, gateway_id: 'gateway-b', gateway_connection_id: 'connection-b' },
    ]);
  }));

test('取消来源工作区选择不打开文件选择器且不安装', async () => inBrowser('cancel-source-workspace', { cancelPick: true }, async (page, state) => {
  const result = await invoke(page, 'select');
  expect(result.error).toBeNull(); expect(result.dialogs).toEqual([]); expect(installs(state)).toEqual([]);
  expect(state.requests.map(item => item.path)).toEqual([
    '/api/gateway/auth/local-credential', '/api/gateway/extension-targets', '/api/gateway/workspaces',
  ]);
  expect(result.picks.map(pick => pick.options.title)).toEqual(['选择 VSIX 来源工作区 · 本地']);
}));

test('复用回执明确说明原正文未覆盖且仍需重新加载，不再次请求安装', async () => inBrowser('reuse-version', {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1']);
  expect(result.error).toBeNull(); expect(installs(state)).toHaveLength(1);
  expect(result.prompts).toHaveLength(1);
  expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[0].message).toContain('复用已有版本，VSIX 正文未覆盖');
  expect(result.prompts[0].message).toContain('当前运行版本尚未切换');
  expect(result.prompts[0].message).not.toContain('已安装新制品');
  expect(result.prompts[0].choices).toEqual(['重新加载窗口']);
}, { reused: true }));

for (const invalid of ['native', 'unknown', 'root', 'invalid']) test(`直接URI批次后项${invalid}非法时整批零安装`, async () => inBrowser('invalid-' + invalid, {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', invalid]);
  expect(result.error?.message).toBeTruthy(); expect(installs(state)).toEqual([]); expect(result.prompts).toEqual([]);
  if (invalid === 'unknown') expect(state.requests.some(item => item.path === '/api/gateway/extension-targets/resolve')).toBe(true);
  else expect(state.requests).toEqual([]);
}));

test('目标文件对话框返回另一工作区根仍拒绝，零安装且不改投', async () => inBrowser('dialog-other-root', { dialogKeys: ['a2'] }, async (page, state) => {
  const result = await invoke(page, 'select');
  expect(result.error?.message).toContain('本次选择'); expect(installs(state)).toEqual([]); expect(result.prompts).toEqual([]);
}));

test('Gateway没有安装域时不查询来源工作区、不打开文件选择器或安装', async () => inBrowser('no-targets', { noTargets: true }, async (page, state) => {
  const result = await page.evaluate(() => globalThis.vsix.chooseExtensionTarget('gateway-a'));
  expect(result.error?.message).toContain('没有可用的扩展安装域'); expect(result.dialogs).toEqual([]);
  expect(state.requests.map(item => item.path)).toEqual([
    '/api/gateway/auth/local-credential', '/api/gateway/management/gateways', '/api/gateway/extension-targets',
  ]);
  expect(state.requests.at(-1).body).toEqual({ protocol_version: 1, gateway_id: 'gateway-a' });
}));

test('直接批次后项目标解析失败时前项零安装', async () => inBrowser('later-target-resolve-missing', { resolveFailure: 409, resolveFailAt: 2 }, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', 'a2']);
  expect(result.error?.message).toBeTruthy(); expect(installs(state)).toEqual([]);
}));

for (const invalidation of ['identity', 'gateway']) test(`文件选择等待中${invalidation}失效，迟到资源零安装`, async () => inBrowser('dialog-invalidated-' + invalidation, { holdDialog: true }, async (page, state) => {
  await page.evaluate(() => { void globalThis.vsix.start('select'); });
  await page.waitForFunction(() => globalThis.vsix.dialogs.length === 1);
  await page.evaluate(kind => { globalThis.vsix.invalidate(kind); globalThis.vsix.releaseDialog(); }, invalidation);
  const result = await page.evaluate(() => globalThis.vsix.finish());
  expect(result.error?.message).toContain('已改变'); expect(installs(state)).toEqual([]); expect(result.prompts).toEqual([]);
  expect(result.rootListeners).toBe(false);
}));

test('首次await前冻结调用方URI数组，目标解析等待中的外部改写不改投或追加', async () => inBrowser('input-array-freeze', {}, async (page, state) => {
  const targetGate = hold(); state.targetResolveGate = targetGate;
  await page.evaluate(() => { void globalThis.vsix.start('direct'); });
  await targetGate.seen.promise;
  await page.evaluate(() => globalThis.vsix.mutateResources(['a2'])); targetGate.release.resolve();
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
  const result = await invoke(page, 'direct', ['a1', 'a2']);
  expect(result.error?.cause).toMatchObject({ name: 'HttpRequestError', status: httpStatus, path: installPath,
    detail: { code: 'target-install-rejected', message: '原安装后的权限或回执检查失败' }, requestId: installs(state)[0].requestId });
  expect(result.error?.message).toContain(result.error.cause.message); expect(result.error?.message).toContain('核实后再决定重试');
  expect(installs(state)).toHaveLength(1); expect(result.prompts).toEqual([]);
}, { failAt: 1, httpStatus }));

test('后项失败保留已确认成功回执，一次通知和原错误并存，第三项不安装', async () => inBrowser('partial-error', {}, async (page, state) => {
  const result = await invoke(page, 'direct', ['a1', 'a2', 'a1']);
    expect(result.error?.cause?.status).toBe(409); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1'), requestFor('workspace-a2')]);
  expect(result.prompts).toHaveLength(1); expect(result.prompts[0].message).toContain('probe.from-receipt@8.7.6');
  expect(result.prompts[0].message).not.toContain('2.4.8'); expect(result.reloads).toBe(0);
}, { failAt: 2 }));

for (const invalidation of ['identity', 'gateway']) test(`首项已确认后${invalidation}中止后项，不泄露旧结果`, async () => inBrowser('partial-invalidated-' + invalidation,
  { invalidateAtSecondReport: invalidation }, async (page, state) => {
    const result = await invoke(page, 'direct', ['a1', 'a2', 'a1']);
    expect(result.error?.cause?.message).toContain('已改变'); expect(installs(state).map(item => item.body)).toEqual([requestFor('workspace-a1')]);
    expect(result.prompts).toEqual([]);
  }));

for (const badReceipt of ['target', 'view', 'profile', 'version', 'reused']) test(`安装${badReceipt}回执不匹配只提示未确认，不重放或伪造成功`, async () => inBrowser('bad-receipt-' + badReceipt, {}, async (page, state) => {
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
