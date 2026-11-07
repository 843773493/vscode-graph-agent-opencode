import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_workspace_symbols.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'workspace-symbols.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/', transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const contribution = join(root, 'upstream/src/vs/workbench/contrib/search/browser/search.contribution.ts');
const parsed = ts.createSourceFile(contribution, await transform(contribution, await readFile(contribution, 'utf8')), ts.ScriptTarget.Latest, true);
const registrations = parsed.statements.filter(statement => ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression)
  && ts.isPropertyAccessExpression(statement.expression.expression) && statement.expression.expression.getText(parsed) === 'CommandsRegistry.registerCommand'
  && ts.isStringLiteral(statement.expression.arguments[0]) && statement.expression.arguments[0].text === '_executeWorkspaceSymbolProvider');
if (registrations.length !== 1) throw new Error('原工作区符号命令注册入口数量已变化：' + registrations.length);
const registration = registrations[0].expression.getText(parsed);
await writeFile(join(run.artifactsDir, 'original-command-registration.ts'), registration + ';\n');
const built = await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
import { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
import { assertType } from '${upstream}base/common/types.js';
import { getWorkspaceSymbols } from '${upstream}workbench/contrib/search/common/search.js';
import { unpackCommandSource } from './src/clients/workbench/src/extensions/commands.js';
import { workspaceSymbolProvidersForHost } from './src/clients/workbench/src/extensions/workspaceSymbols.js';
export function registerOriginalWorkspaceSymbolCommand() { return ${registration}; }
export { CommandsRegistry, getWorkspaceSymbols, unpackCommandSource, workspaceSymbolProvidersForHost };
export { workspaceSymbolProviders } from './src/clients/workbench/src/extensions/workspaceSymbols.js';
export { executeHostCommand, executeCommandForHost, captureCommandHost } from './src/clients/workbench/src/extensions/commands.js';
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { WorkspaceSymbolProviderRegistry, WorkspaceSymbolItem } from '${upstream}workbench/contrib/search/common/search.js';
export { CommandService } from '${upstream}workbench/services/commands/common/commandService.js';
export { IExtensionService } from '${upstream}workbench/services/extensions/common/extensions.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { isCancellationError } from '${upstream}base/common/errors.js';
export { Emitter } from '${upstream}base/common/event.js';
export { URI } from '${upstream}base/common/uri.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' }, plugins: [buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const sources = ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionWorkspaceSymbolsTransform.mjs', 'scripts/extensions/extensionOwnerTransform.mjs',
  'src/extensions/workspaceSymbols.ts', 'src/extensions/commands.ts', 'src/extensions/targetUris.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts', 'upstream/src/vs/workbench/contrib/search/common/search.ts',
  'upstream/src/vs/workbench/contrib/search/browser/search.contribution.ts'];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path, createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const checks = [], observations = [], browserEvents = [];
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>工作区符号组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('符号组件HTTP来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('符号组件Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks,
      boundary: '真实 Chromium 中的原 MainThreadLanguageFeatures、WorkspaceSymbolProviderRegistry/getWorkspaceSymbols、RPCProtocol/TargetUriTransformer、CommandService/DI 与 AST 提取的原内置命令。认证 binding、内存字节输送、扩展回调及扩展服务的宿主清单/激活等待是具名装具；不替代 Gateway、真实 Node、SSH 或完整符号界面验收。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
  }
});

async function inBrowser(name, body, argument) {
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetimes = [], service = new m.LanguageFeaturesService();
      const tick = () => new Promise(resolve => setTimeout(resolve, 0));
      const failure = promise => Promise.resolve(promise).then(() => 'unexpected-success', error => m.isCancellationError(error) ? 'cancelled' : error.message);
      const message = action => { try { action(); return 'unexpected-success'; } catch (error) { return error.message; } };
      function target(label, ids = label === 'A' ? ['a1', 'a2'] : ['b1']) {
        const binding = { gateway_id: 'gateway-symbols', backend_id: `backend-${label}`, initialization: { os: 'linux', logs_location_uri: 'file:///hosts/logs',
          environment: { app_root_uri: 'file:///hosts/app', global_storage_home_uri: 'file:///hosts/global', workspace_storage_home_uri: 'file:///hosts/storage',
            profile_id: 'default', remote_settings: { native_uri: 'file:///hosts/profile/settings.json', resource_uri: 'boxteam://gateway-symbols/profile/default/settings' } },
          workspace: { id: `view-${label}`, folders: ids.map(workspace_id => ({ workspace_id, name: workspace_id,
            resource_uri: `boxteam://workspace/${workspace_id}/resources/files/root`, native_uri: `file:///targets/${workspace_id}/project` })) } } };
        return { binding, targetContext: { kind: 'node', connection: binding }, uriTransformer: new m.TargetUriTransformer(binding, () => []), runningLocation: { kind: 3 } };
      }
      const owners = host => host.binding.initialization.workspace.folders.map(folder => ({ gatewayId: host.binding.gateway_id, workspaceId: folder.workspace_id }));
      async function connect(host, label = host.binding ? host.binding.backend_id : 'Web') {
        // 只替换扩展回调、认证描述和字节输送；原 RPC 处理 typed URI、取消与序列化。
        const toMain = new m.Emitter(), toHost = new m.Emitter(), queries = [], resolves = [], releases = [], caches = new Set();
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        let sequence = 0;
        const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 };
        const client = { host, queries, resolves, releases, caches, onProvide: async (_request, result) => result,
          onResolve: async request => ({ ...request.symbol, containerName: label + ' resolved' }) };
        const actor = {
          async $provideWorkspaceSymbols(handle, query, token) {
            const cacheId = ++sequence; caches.add(cacheId);
            const uris = host.uriTransformer?.folders.map(folder => m.URI.joinPath(folder.native, 'same.ts')) ?? [m.URI.parse('untitled:web-symbol.ts')];
            const result = { cacheId, symbols: uris.map((uri, index) => ({ name: 'SameName', kind: 11, containerName: label, cacheId: [cacheId, index], location: { uri, range } })) };
            const request = { handle, query, token, arity: arguments.length }; queries.push(request);
            return client.onProvide(request, result);
          },
          async $resolveWorkspaceSymbol(handle, value, token) {
            const symbol = { ...value, location: { ...value.location, uri: m.URI.revive(value.location.uri) } };
            const request = { handle, symbol, token }; resolves.push(request); return client.onResolve(request);
          },
          async $releaseWorkspaceSymbols(handle, id) { releases.push({ handle, id, existed: caches.delete(id) }); },
        };
        const main = new m.MainThreadLanguageFeatures({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) },
          undefined, undefined, service, { asCanonicalUri: uri => uri }, undefined, undefined);
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, main); hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, actor);
        const remote = hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures), before = new Set(m.WorkspaceSymbolProviderRegistry.all());
        await remote.$registerNavigateTypeSupport(1, true);
        const added = m.WorkspaceSymbolProviderRegistry.all().filter(provider => !before.has(provider));
        if (added.length !== 1) throw new Error('原 MainThread 应注册唯一工作区符号 Provider');
        Object.assign(client, { main, remote, provider: added[0] });
        lifetimes.push(() => { main.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
        return client;
      }
      const query = (scope, token = m.CancellationToken.None) => m.getWorkspaceSymbols('SameName', token, m.workspaceSymbolProviders(scope));
      const hostQuery = host => m.getWorkspaceSymbols('SameName', m.CancellationToken.None, m.workspaceSymbolProvidersForHost(host));
      const uris = items => items.map(item => item.symbol.location.uri.toString()).sort();
      const logicalUris = host => host.uriTransformer.folders.map(folder => m.URI.joinPath(folder.uri, 'same.ts').toString()).sort();
      globalThis.symbolProbe = { m, target, owners, connect, query, hostQuery, uris, logicalUris, tick, failure, message, lifetimes,
        dispose() { for (const dispose of lifetimes.reverse()) dispose(); } };
    });
    const result = await page.evaluate(body, argument); expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.symbolProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('完整宿主 view 在查询前选择，A1/A2/B1 同名结果保留各原 Provider，部分 view 与无 Provider 都零回调', async () => {
  const result = await inBrowser('完整 view、联合查询、部分交叉、无 Provider、无 Web 回退', async () => {
    const p = globalThis.symbolProbe, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const web = await p.connect({ runningLocation: { kind: 2 } }), aOwners = p.owners(a.host), bOwners = p.owners(b.host);
    const errors = [p.message(() => p.m.workspaceSymbolProviders([aOwners[0]])), p.message(() => p.m.workspaceSymbolProviders([aOwners[0], ...bOwners])),
      p.message(() => p.m.workspaceSymbolProviders([])), p.message(() => p.m.workspaceSymbolProviders([{ gatewayId: 'gateway-symbols', workspaceId: 'missing' }]))];
    const before = [a.queries.length, b.queries.length, web.queries.length], own = await p.query(aOwners);
    const ownCounts = [a.queries.length, b.queries.length, web.queries.length];
    const both = await p.query([...aOwners, ...bOwners, aOwners[0]]);
    return { errors, before, ownCounts, own: p.uris(own), expectedOwn: p.logicalUris(a.host), union: p.uris(both),
      expectedUnion: [...p.logicalUris(a.host), ...p.logicalUris(b.host)].sort(), counts: [a.queries.length, b.queries.length, web.queries.length],
      names: both.map(item => item.symbol.name), associations: both.every(item => item instanceof p.m.WorkspaceSymbolItem
        && item.provider === (item.symbol.containerName === 'backend-A' ? a.provider : b.provider)),
      typed: both.every(item => p.m.URI.isUri(item.symbol.location.uri)), sameHost: p.m.workspaceSymbolProvidersForHost(a.host)[0] === a.provider,
      clonedHost: p.m.workspaceSymbolProvidersForHost({ ...a.host }).length, webDefault: p.m.workspaceSymbolProvidersForHost(web.host) === undefined };
  });
  expect(result.errors[0]).toContain('完整宿主视图'); expect(result.errors[1]).toContain('完整宿主视图');
  expect(result.errors[2]).toContain('明确'); expect(result.errors[3]).toContain('没有已注册');
  expect(result.before).toEqual([0, 0, 0]); expect(result.ownCounts).toEqual([1, 0, 0]); expect(result.counts).toEqual([2, 1, 0]);
  expect(result.own).toEqual(result.expectedOwn); expect(result.union).toEqual(result.expectedUnion); expect(result.names).toEqual(['SameName', 'SameName', 'SameName']);
  expect([result.associations, result.typed, result.sameHost, result.webDefault]).toEqual([true, true, true, true]); expect(result.clonedHost).toBe(0);
  checks.push('完整 view 查询前准入、同名结果及 Provider 关联、部分/缺失 scope 零回调、无 Web 回退');
});

test('typed URI 往返及 resolve 沿原 Provider，替换查询和 unregister 精确释放原缓存', async () => {
  const result = await inBrowser('typed URI、原 symbol/provider 身份、同 Provider resolve 与 cache 释放', async () => {
    const p = globalThis.symbolProbe, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const items = await p.query(p.owners(a.host)), item = items[0], symbol = item.symbol, provider = item.provider;
    const retained = new Map([[symbol, 'symbol'], [provider, 'provider']]);
    const resolved = await provider.resolveWorkspaceSymbol(symbol, p.m.CancellationToken.None);
    const native = a.resolves[0].symbol.location.uri.toString(), cache = [...a.resolves[0].symbol.cacheId];
    await p.query(p.owners(a.host)); await p.tick(); const replaced = a.releases.map(value => value.id);
    await a.remote.$unregister(1); await p.tick();
    return { native, expectedNative: a.host.uriTransformer.transformOutgoingURI(symbol.location.uri).toString(), cache,
      logical: resolved.location.uri.toString(), original: symbol.location.uri.toString(), typed: p.m.URI.isUri(resolved.location.uri),
      identities: [retained.get(item.symbol), retained.get(item.provider)], originalContainer: symbol.containerName, resolvedContainer: resolved.containerName,
      replaced, releases: a.releases, caches: [...a.caches], bQueries: b.queries.length, registered: p.m.WorkspaceSymbolProviderRegistry.all().includes(provider) };
  });
  expect(result.native).toBe(result.expectedNative); expect(result.logical).toBe(result.original); expect(result.typed).toBe(true);
  expect(result.cache).toEqual([1, 0]); expect(result.identities).toEqual(['symbol', 'provider']);
  expect(result.originalContainer).toBe('backend-A'); expect(result.resolvedContainer).toBe('backend-A resolved'); expect(result.replaced).toEqual([1]);
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }, { handle: 1, id: 2, existed: true }]);
  expect(result.caches).toEqual([]); expect(result.bQueries).toBe(0); expect(result.registered).toBe(false);
  checks.push('原 symbol/provider 身份与 typed URI resolve 往返；每次替换和 unregister 释放准确 cache');
});

test('MainThread 整批拒绝视图外符号并释放 cache，resolve 在 RPC 前后核归属', async () => {
  const result = await inBrowser('MainThread provide/resolve 归属拒绝与 cache 结算', async () => {
    const p = globalThis.symbolProbe, a = await p.connect(p.target('A')), b = p.target('B');
    a.onProvide = async (_request, result) => ({ ...result, symbols: [...result.symbols, { ...result.symbols[0], location: { uri: p.m.URI.file('/hosts/app/outside.ts') } }] });
    // 该位置允许穿过真实 URI transport，但不属于 workspace view，必须由 MainThread 拒绝整批。
    const provide = await p.failure(a.provider.provideWorkspaceSymbols('bad batch', p.m.CancellationToken.None));
    a.onProvide = async (_request, result) => result;
    const [item] = await p.query(p.owners(a.host));
    const foreign = { ...item.symbol, location: { ...item.symbol.location, uri: p.m.URI.joinPath(b.uriTransformer.folders[0].uri, 'same.ts') } };
    const before = a.resolves.length, input = await p.failure(a.provider.resolveWorkspaceSymbol(foreign, p.m.CancellationToken.None));
    const preflightCalls = a.resolves.length - before;
    a.onResolve = async request => ({ ...request.symbol, location: { ...request.symbol.location, uri: p.m.URI.file('/hosts/app/outside.ts') } });
    const output = await p.failure(a.provider.resolveWorkspaceSymbol(item.symbol, p.m.CancellationToken.None));
    await a.remote.$unregister(1); await p.tick();
    return { provide, input, output, preflightCalls, releases: a.releases, caches: [...a.caches] };
  });
  for (const key of ['provide', 'input', 'output']) expect(result[key]).toContain('不属于发起宿主');
  expect(result.preflightCalls).toBe(0); expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }, { handle: 1, id: 2, existed: true }]);
  expect(result.caches).toEqual([]); checks.push('provide 整批归属拒绝及 cache 释放，resolve 输入零 RPC、返回再次准入');
});

for (const operation of ['cancel', 'unregister', 'dispose']) test(`${operation} 后 provide/resolve 迟到不发布，cache 回原代际且新宿主不串`, async () => {
  const result = await inBrowser(`${operation} 的迟到 provide/resolve、缓存和同 binding 新代际`, async operation => {
    const p = globalThis.symbolProbe, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const [initial] = await p.query(p.owners(a.host)), provideEntered = Promise.withResolvers(), resolveEntered = Promise.withResolvers();
    const provideGate = Promise.withResolvers(), resolveGate = Promise.withResolvers(), source = new p.m.CancellationTokenSource();
    a.onProvide = async (request, value) => { provideEntered.resolve({ request, value }); await provideGate.promise; return value; };
    a.onResolve = async request => { resolveEntered.resolve(request); await resolveGate.promise; return request.symbol; };
    const provided = p.failure(initial.provider.provideWorkspaceSymbols('late', source.token));
    const resolved = p.failure(initial.provider.resolveWorkspaceSymbol(initial.symbol, source.token));
    const [pendingProvide, pendingResolve] = await Promise.all([provideEntered.promise, resolveEntered.promise]);
    if (operation === 'cancel') source.cancel();
    else if (operation === 'unregister') await a.remote.$unregister(1);
    else a.main.dispose();
    await p.tick();
    const next = await p.connect(p.target('A'), 'new-generation'), nextItems = await p.hostQuery(next.host);
    provideGate.resolve(); resolveGate.resolve(); const outcomes = await Promise.all([provided, resolved]); await p.tick();
    const oldPresence = p.m.workspaceSymbolProvidersForHost(a.host).length;
    if (operation === 'cancel') await a.remote.$unregister(1);
    const before = a.queries.length, rejected = await p.failure(initial.provider.provideWorkspaceSymbols('after release', p.m.CancellationToken.None));
    const other = await p.hostQuery(b.host); await p.tick(); source.dispose();
    return { outcomes, rejected, oldPresence, tokens: [pendingProvide.request.token.isCancellationRequested, pendingResolve.token.isCancellationRequested],
      oldReleases: a.releases.map(value => ({ ...value })).sort((x, y) => x.id - y.id), oldCaches: [...a.caches], lateCalls: a.queries.length - before,
      newIds: nextItems.every(item => item.provider === next.provider && item.provider.extensionHost === next.host), newCount: nextItems.length,
      newReleases: [...next.releases], newCaches: [...next.caches], other: other.every(item => item.provider === b.provider), otherQueries: b.queries.length };
  }, operation);
  expect(result.outcomes).toEqual(['cancelled', 'cancelled']); expect(result.rejected).toBe('cancelled');
  expect(result.oldPresence).toBe(operation === 'cancel' ? 1 : 0); expect(result.tokens).toEqual(operation === 'cancel' ? [true, true] : [false, false]);
  expect(result.oldReleases).toEqual([{ handle: 1, id: 1, existed: true }, { handle: 1, id: 2, existed: true }]);
  expect(result.oldCaches).toEqual([]); expect(result.lateCalls).toBe(0); expect(result.newIds).toBe(true); expect(result.newCount).toBe(2);
  expect(result.newReleases).toEqual([]); expect(result.newCaches).toEqual([1]); expect(result.other).toBe(true); expect(result.otherQueries).toBe(1);
  checks.push(`${operation} 后原 Provider 拒绝迟到 provide/resolve；准确释放旧 cache，同 binding 新代际与 B 保持`);
});

test('AST 提取的原 builtin 命令经真实 CommandService 继承 Node source，Web/无 scope 保持原全窗口，activation 后撤代零回调', async () => {
  const result = await inBrowser('原 builtin 命令、私有 source、真实 CommandService/DI 和 activation 等待后代际准入', async () => {
    const p = globalThis.symbolProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const web = await p.connect({ runningLocation: { kind: 2 } }), original = m.registerOriginalWorkspaceSymbolCommand();
    let live = [a.host, b.host, web.host], activationDone = true, activate = async () => {}, picks = 0;
    // 宿主清单与激活等待为明确服务边界；分发、来源解包和原 handler 全由真实产品实现执行。
    const service = { getExtensionHosts: () => live, getHostExtensions: () => [], whenInstalledExtensionsRegistered: async () => true,
      activationEventIsDone: () => activationDone, activateByEvent: (...args) => activate(...args) };
    const di = new m.InstantiationService(new m.ServiceCollection([m.IExtensionService, service]), true);
    const commands = new m.CommandService(di, service, new m.NullLogService()), id = '_executeWorkspaceSymbolProvider';
    p.lifetimes.push(() => { commands.dispose(); di.dispose(); original.dispose(); });
    const aItems = await m.executeHostCommand(commands, a.host, id, ['from A']), aCounts = [a.queries.length, b.queries.length, web.queries.length];
    const bItems = await m.executeHostCommand(commands, b.host, id, ['from B']), bCounts = [a.queries.length, b.queries.length, web.queries.length];
    const webItems = await m.executeHostCommand(commands, web.host, id, ['from Web']);
    const globalItems = await commands.executeCommand(id, 'no scope');
    const packed = await m.executeHostCommand({ executeCommand: async (_id, ...args) => args }, a.host, id, ['private']);
    const unpacked = m.unpackCommandSource(packed);
    const captured = await m.captureCommandHost(service, id, () => { throw new Error('有 source 不应读取编辑器'); }, () => { picks++; throw new Error('不应选宿主'); }, unpacked.source);
    const direct = await di.invokeFunction(accessor => m.executeCommandForHost(m.CommandsRegistry.getCommand(id), accessor, ['direct'], captured));
    const missingHost = { ...a.host }; live = [...live, missingHost];
    const missing = await m.executeHostCommand(commands, missingHost, id, ['no own provider']);
    const entered = Promise.withResolvers(), release = Promise.withResolvers(); activationDone = false;
    activate = async () => { entered.resolve(); await release.promise; };
    const counts = [a.queries.length, b.queries.length, web.queries.length], stale = p.failure(m.executeHostCommand(commands, a.host, id, ['stale']));
    await entered.promise; live = live.filter(host => host !== a.host); release.resolve(); const closed = await stale;
    return { a: aItems.map(item => item.location.uri.toString()).sort(), expectedA: p.logicalUris(a.host), aCounts,
      b: bItems.map(item => item.location.uri.toString()).sort(), expectedB: p.logicalUris(b.host), bCounts,
      webCount: webItems.length, globalCount: globalItems.length, unpacked: unpacked.source === a.host && unpacked.args[0] === 'private', captured: captured === a.host,
      directCount: direct.length, missingCount: missing.length, picks, closed, counts, after: [a.queries.length, b.queries.length, web.queries.length],
      queries: [a, b, web].flatMap(client => client.queries.map(request => ({ query: request.query, arity: request.arity }))) };
  });
  expect(result.a).toEqual(result.expectedA); expect(result.b).toEqual(result.expectedB); expect(result.aCounts).toEqual([1, 0, 0]); expect(result.bCounts).toEqual([1, 1, 0]);
  expect(result.webCount).toBe(4); expect(result.globalCount).toBe(4); expect(result.unpacked).toBe(true); expect(result.captured).toBe(true);
  expect(result.directCount).toBe(2); expect(result.missingCount).toBe(0); expect(result.picks).toBe(0); expect(result.closed).toContain('关闭'); expect(result.after).toEqual(result.counts);
  expect(result.queries.every(request => typeof request.query === 'string' && request.arity === 3)).toBe(true);
  expect(result.queries.some(request => request.query === 'stale' || request.query === 'no own provider')).toBe(false);
  checks.push('原 builtin AST 注册及真实 CommandService source；Node 精确 host、Web/无 scope 原范围、activation 后撤代零回调');
});
