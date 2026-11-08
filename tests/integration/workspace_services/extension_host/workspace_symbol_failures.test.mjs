import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/workspace_services/extension_host/workspace_symbol_failures.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const sourcePaths = [testFile, ...['scripts/sourceTransform.mjs', 'scripts/extensions/extensionWorkspaceSymbolsTransform.mjs',
  'src/search/symbolsQuickAccess.ts', 'src/extensions/workspaceSymbols.ts', 'src/extensions/targetUris.ts', 'src/extensions/commands.ts',
  'upstream/src/vs/workbench/contrib/search/common/search.ts', 'upstream/src/vs/workbench/contrib/search/browser/symbolsQuickAccess.ts',
  'upstream/src/vs/workbench/contrib/search/browser/search.contribution.ts', 'upstream/src/vs/platform/quickinput/browser/pickerQuickAccess.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts'].map(path => 'src/clients/workbench/' + path)];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path =>
  [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint(), transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const contribution = join(root, 'upstream/src/vs/workbench/contrib/search/browser/search.contribution.ts');
const parsed = ts.createSourceFile(contribution, await transform(contribution, await readFile(contribution, 'utf8')), ts.ScriptTarget.Latest, true);
const commands = parsed.statements.filter(statement => ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression)
  && ts.isPropertyAccessExpression(statement.expression.expression) && statement.expression.expression.getText(parsed) === 'CommandsRegistry.registerCommand'
  && ts.isStringLiteral(statement.expression.arguments[0]) && statement.expression.arguments[0].text === '_executeWorkspaceSymbolProvider');
if (commands.length !== 1) throw new Error('原工作区符号命令注册入口数量已变化：' + commands.length);
const registration = commands[0].expression.getText(parsed), bundle = join(run.artifactsDir, 'workspace-symbol-failures.fixture.mjs');
await writeFile(join(run.artifactsDir, 'original-command-registration.ts'), registration + ';\n');
await writeFile(join(run.artifactsDir, 'test-source.mjs'), await readFile(testFile, 'utf8'));
const built = await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
import { CommandsRegistry } from '${upstream}platform/commands/common/commands.js';
import { assertType } from '${upstream}base/common/types.js';
import { getWorkspaceSymbols } from '${upstream}workbench/contrib/search/common/search.js';
import { unpackCommandSource } from './src/clients/workbench/src/extensions/commands.js';
import { workspaceSymbolProvidersForHost } from './src/clients/workbench/src/extensions/workspaceSymbols.js';
export function registerOriginalWorkspaceSymbolCommand() { return ${registration}; }
export { CommandsRegistry, getWorkspaceSymbols };
export { executeCommandForHost } from './src/clients/workbench/src/extensions/commands.js';
export { workspaceSymbolProviders } from './src/clients/workbench/src/extensions/workspaceSymbols.js';
export { WorkspaceSymbolsQuickAccess } from './src/clients/workbench/src/search/symbolsQuickAccess.js';
export { WorkspaceSymbolProviderRegistry, WorkspaceSymbolItem } from '${upstream}workbench/contrib/search/common/search.js';
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { TargetUriTransformer, TargetWorkspaceResources } from './src/clients/workbench/src/extensions/targetUris.js';
export { IExtensionService } from '${upstream}workbench/services/extensions/common/extensions.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { setUnexpectedErrorHandler, NotSupportedError } from '${upstream}base/common/errors.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { URI } from '${upstream}base/common/uri.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' }, plugins: [buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile));
const checks = [], observations = [], browserEvents = [];
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>工作区符号失败组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('符号失败组件 HTTP 来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('符号失败组件 Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    const fingerprintsAfter = await fingerprint();
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, fingerprintsAfter, checks,
      boundary: '真实 Chromium 内运行原 getWorkspaceSymbols/Registry、原 SymbolsQuickAccess/PickerQuickAccess 与业务 # 子类、原 MainThreadLanguageFeatures/RPC/URI transport 及 AST 原公开命令 handler。认证 binding、Provider 回调、字节输送、Quick Input 控件和编辑/标签/配置/扩展清单服务为具名装具；不替代真实 Gateway/Node/SSH 或完整 Workbench，未验增量发布及分支重试。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
    expect(fingerprintsAfter).toEqual(fingerprints);
  }
});

async function inBrowser(name, body) {
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetimes = [], unexpected = [], opened = [];
      m.setUnexpectedErrorHandler(error => unexpected.push(error));
      const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 };
      const tick = () => new Promise(resolve => setTimeout(resolve, 0));
      function target(label, kind = 'node') {
        const workspace = { id: 'view-' + label, folders: [{ workspace_id: label, name: label,
          resource_uri: `boxteam://workspace/${label}/resources/files/root`, native_uri: `file:///targets/${label}/project` }] };
        const settings = { native_uri: 'file:///hosts/profile/settings.json', resource_uri: `boxteam://gateway-${label}/profile/default/settings` };
        const common = { gateway_id: 'gateway-' + label, backend_id: 'backend-' + label };
        const binding = kind === 'node' ? { ...common, initialization: { os: 'linux', logs_location_uri: 'file:///hosts/logs', workspace,
          environment: { app_root_uri: 'file:///hosts/app', global_storage_home_uri: 'file:///hosts/global', workspace_storage_home_uri: 'file:///hosts/storage',
            profile_id: 'default', remote_settings: settings } } } : { ...common, profile_id: 'default', context: { os: 'linux', workspace, remote_settings: settings } };
        const host = { binding, targetContext: { kind, connection: binding }, runningLocation: { kind: kind === 'node' ? 3 : 2 } };
        host.resources = kind === 'node' ? host.uriTransformer = new m.TargetUriTransformer(binding, () => []) : new m.TargetWorkspaceResources(host.targetContext);
        return host;
      }
      const owners = host => [{ gatewayId: host.binding.gateway_id, workspaceId: (host.binding.initialization?.workspace ?? host.binding.context.workspace).folders[0].workspace_id }];
      const symbol = (host, name = 'SameName') => ({ name, kind: 11, containerName: host.binding?.backend_id ?? 'window',
        location: { uri: host.resources ? m.URI.joinPath(host.resources.folders[0].uri, 'same.ts') : m.URI.parse('untitled:window-symbol'), range } });
      function register(host, provide = () => [symbol(host)]) {
        const calls = [], provider = { extensionHost: host, provideWorkspaceSymbols(query, token) { calls.push({ query, token }); return provide(query, token); } };
        lifetimes.push(() => registration.dispose());
        const registration = m.WorkspaceSymbolProviderRegistry.register(provider);
        return { host, provider, calls };
      }
      function quick(labelError) {
        const provider = new m.WorkspaceSymbolsQuickAccess({ getUriLabel(uri) { if (labelError) throw labelError; return uri.toString(); } },
          { open() { throw new Error('本组不应打开外部链接'); } }, { async openEditor(input, group) { opened.push({ input, group }); } },
          { getValue: () => ({ workbench: { editor: { enablePreview: false } } }) }, { getFocusedCodeEditor: () => undefined }, {});
        lifetimes.push(() => provider.dispose()); return provider;
      }
      function open(scope, token = m.CancellationToken.None, provider = quick()) {
        // 只替换 Quick Input 控件；过滤、busy、取消、无匹配及接受全部运行原 provide。
        const changed = new m.Emitter(), accepted = new m.Emitter(), completed = Promise.withResolvers(), writes = [];
        let items = [], busy = false, applied = false;
        const timeout = setTimeout(() => completed.reject(new Error('原 Picker 未完成本次查询')), 4000);
        const picker = { value: '#SameName', activeItems: [], selectedItems: [], keyMods: { alt: false, ctrlCmd: false, shift: false },
          onDidChangeValue: changed.event, onDidAccept: accepted.event, onDidTriggerItemButton: m.Event.None, onDidTriggerSeparatorButton: m.Event.None,
          hide() {}, get items() { return items; }, set items(value) { items = value; applied = true; writes.push(value); },
          get busy() { return busy; }, set busy(value) { busy = value; if (!value && applied) { clearTimeout(timeout); completed.resolve(); } } };
        const subscription = provider.provide(picker, token, scope);
        lifetimes.push(() => { clearTimeout(timeout); subscription.dispose(); changed.dispose(); accepted.dispose(); });
        return { picker, provider, writes, done: completed.promise, accept(item) { picker.selectedItems = [item]; accepted.fire({ inBackground: true }); } };
      }
      async function picks(provider, options, token = m.CancellationToken.None) {
        const store = new m.DisposableStore(); lifetimes.push(() => store.dispose()); return provider._getPicks('SameName', store, token, options);
      }
      async function connect(host) {
        // 认证描述、扩展回调和字节输送为具名装具；原 MainThread、RPC 和 URI transformer 不替换。
        const toMain = new m.Emitter(), toHost = new m.Emitter(), calls = [], releases = [], caches = new Set();
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        let sequence = 0;
        const client = { host, calls, releases, caches, onProvide: value => value };
        hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, {
          async $provideWorkspaceSymbols(handle, query, token) {
            calls.push({ handle, query, token }); const cacheId = ++sequence; caches.add(cacheId);
            const base = host.uriTransformer ? host.resources.folders[0].native : host.resources?.folders[0].uri;
            const value = { cacheId, symbols: [{ ...symbol(host), cacheId: [cacheId, 0], location: { uri: base ? m.URI.joinPath(base, 'same.ts') : m.URI.parse('untitled:window-symbol'), range } }] };
            return client.onProvide(value);
          },
          async $releaseWorkspaceSymbols(handle, cacheId) { releases.push({ handle, cacheId, existed: caches.delete(cacheId) }); },
        });
        const main = new m.MainThreadLanguageFeatures({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) }, undefined, undefined,
          new m.LanguageFeaturesService(), { asCanonicalUri: uri => uri }, undefined, undefined);
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, main);
        const remote = hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures), before = new Set(m.WorkspaceSymbolProviderRegistry.all());
        await remote.$registerNavigateTypeSupport(1, false);
        client.provider = m.WorkspaceSymbolProviderRegistry.all().find(provider => !before.has(provider));
        lifetimes.push(() => { main.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
        return client;
      }
      globalThis.failureProbe = { m, lifetimes, unexpected, opened, target, owners, symbol, register, quick, open, picks, connect, tick,
        summarize(items) { return items.map(item => ({ label: item.label, description: item.description, symbol: !!item.symbol,
          resource: item.resource?.toString(), actionable: typeof item.accept === 'function' || typeof item.trigger === 'function' })); },
        dispose() { for (const dispose of lifetimes.reverse()) dispose(); } };
    });
    const result = await page.evaluate(body); expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.failureProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('原聚合 catch 对同步 throw 与异步 reject 回传准确 Provider/错误，保留原排序去重及对象', async () => {
  const result = await inBrowser('原聚合错误回调与排序去重', async () => {
    const p = globalThis.failureProbe, aHost = p.target('A'), alpha = p.symbol(aHost, 'Alpha'), zulu = p.symbol(aHost, 'Zulu');
    const sync = new Error('sync original'), asyncError = new p.m.NotSupportedError('async original');
    const a = p.register(aHost, () => [zulu, alpha, alpha]), b = p.register(p.target('B', 'web'), () => { throw sync; });
    const c = p.register(p.target('C'), () => Promise.reject(asyncError)), failures = [];
    const items = await p.m.getWorkspaceSymbols('SameName', undefined, [a.provider, b.provider, c.provider], (provider, error) => failures.push({ provider, error }));
    return { names: items.map(item => item.symbol.name), objects: items[0].symbol === alpha && items[1].symbol === zulu,
      providers: items.every(item => item.provider === a.provider && item instanceof p.m.WorkspaceSymbolItem),
      failures: failures.map(item => ({ sync: item.error === sync, async: item.error === asyncError,
        owner: item.provider === b.provider ? 'B' : item.provider === c.provider ? 'C' : 'wrong' })).sort((x, y) => x.owner.localeCompare(y.owner)),
      calls: [a.calls.length, b.calls.length, c.calls.length], unexpected: p.unexpected.length };
  });
  expect(result.names).toEqual(['Alpha', 'Zulu']); expect([result.objects, result.providers]).toEqual([true, true]);
  expect(result.failures).toEqual([{ sync: true, async: false, owner: 'B' }, { sync: false, async: true, owner: 'C' }]);
  expect(result.calls).toEqual([1, 1, 1]); expect(result.unexpected).toBe(0);
  checks.push('原聚合catch回传原Provider和同一同步/异步错误；原对象/Provider/排序/去重保持且无全局错误旁路');
});

test('明确 A+B 的原 Picker 同时展示 A 命中与 B 失败，接受仍沿 A，未选 C/窗口零调用', async () => {
  const result = await inBrowser('原provide部分失败及接受归属', async () => {
    const p = globalThis.failureProbe, a = p.register(p.target('A')), original = new Error('B 原因含取消和 [命令](command:do-not-run)');
    const b = p.register(p.target('B', 'web'), () => Promise.reject(original)), c = p.register(p.target('C'));
    const window = p.register({ runningLocation: { kind: 2 } }), run = p.open({ searchOwners: [...p.owners(a.host), ...p.owners(b.host)] });
    await run.done; const success = run.picker.items.find(item => item.symbol); run.accept(success); await p.tick();
    return { items: p.summarize(run.picker.items), expected: p.symbol(a.host).location.uri.toString(),
      opens: p.opened.map(value => ({ uri: value.input.resource.toString(), pinned: value.input.options.pinned, preserveFocus: value.input.options.preserveFocus })),
      calls: [a.calls.length, b.calls.length, c.calls.length, window.calls.length], unexpected: p.unexpected.length, busy: run.picker.busy, placeholder: run.picker.placeholder };
  });
  expect(result.items).toHaveLength(2); expect(result.items[0].symbol).toBe(true); expect(result.items[0].resource).toBe(result.expected);
  expect(result.items[1].label).toContain('B 原因含取消和 [命令](command:do-not-run)');
  expect(result.items[1].description).toBe('gateway-B / backend-B'); expect(result.items[1].actionable).toBe(false);
  expect(result.opens).toEqual([{ uri: result.expected, pinned: true, preserveFocus: true }]); expect(result.calls).toEqual([1, 1, 0, 0]);
  expect(result.unexpected).toBe(0); expect(result.busy).toBe(false); expect(result.placeholder).toContain('A、B');
  checks.push('真实原provide保留成功候选/接受，B原错误和固定目标可见，错误无执行动作，未选目标零回调');
});

test('所有明确分支失败仍显示各原原因，不落入原无匹配空态', async () => {
  const result = await inBrowser('全分支失败与无匹配区分', async () => {
    const p = globalThis.failureProbe, a = p.register(p.target('A'), () => { throw new Error('A sync failure'); });
    const b = p.register(p.target('B', 'web'), () => Promise.reject(new Error('B async failure')));
    const run = p.open({ searchOwners: [...p.owners(a.host), ...p.owners(b.host)] }); await run.done;
    return { items: p.summarize(run.picker.items), calls: [a.calls.length, b.calls.length], unexpected: p.unexpected.length, busy: run.picker.busy };
  });
  expect(result.items).toHaveLength(2); expect(result.items.every(item => !item.symbol && !item.actionable)).toBe(true);
  expect(result.items.map(item => item.description).sort()).toEqual(['gateway-A / backend-A', 'gateway-B / backend-B']);
  expect(result.items.some(item => item.label.includes('A sync failure'))).toBe(true); expect(result.items.some(item => item.label.includes('B async failure'))).toBe(true);
  expect(result.calls).toEqual([1, 1]); expect(result.unexpected).toBe(0); expect(result.busy).toBe(false);
  checks.push('全部失败在原Picker中显示两原原因/目标，不能变成无匹配空态');
});

test('业务入口同步 scope 错误零查询，await 后的原候选转换拒绝也显示原错误', async () => {
  const result = await inBrowser('同步准入与异步候选错误', async () => {
    const p = globalThis.failureProbe, a = p.register(p.target('A'));
    const scoped = await p.picks(p.quick(), { searchOwnerError: 'frozen owner missing' }), before = a.calls.length;
    const transformed = await p.picks(p.quick(new Error('label service original failure')), { searchOwners: p.owners(a.host) });
    return { scoped: p.summarize(scoped), before, transformed: p.summarize(transformed), calls: a.calls.length, unexpected: p.unexpected.length };
  });
  expect(result.scoped).toHaveLength(1); expect(result.scoped[0].label).toContain('frozen owner missing'); expect(result.before).toBe(0);
  expect(result.transformed).toHaveLength(1); expect(result.transformed[0].label).toContain('label service original failure');
  expect(result.transformed[0].symbol).toBe(false); expect(result.calls).toBe(1); expect(result.unexpected).toBe(0);
  checks.push('同步scope拒绝零查询；真实原getSymbolPicks异步转换拒绝进入业务await/catch而非未处理rejection');
});

test('原 Picker 查询取消后不发布迟到成功或失败行，已取消 token 不再调用 Provider', async () => {
  const result = await inBrowser('取消与迟到结果', async () => {
    const p = globalThis.failureProbe, gate = Promise.withResolvers(), aEntered = Promise.withResolvers(), bEntered = Promise.withResolvers();
    const finishedA = Promise.withResolvers(), finishedB = Promise.withResolvers(), aHost = p.target('A');
    const a = p.register(aHost, async () => { aEntered.resolve(); await gate.promise; finishedA.resolve(); return [p.symbol(aHost)]; });
    const b = p.register(p.target('B'), async () => { bEntered.resolve(); await gate.promise; finishedB.resolve(); throw new Error('late B failure'); });
    const source = new p.m.CancellationTokenSource(); p.lifetimes.push(() => source.dispose());
    const scope = { searchOwners: [...p.owners(a.host), ...p.owners(b.host)] }, run = p.open(scope, source.token);
    await Promise.all([aEntered.promise, bEntered.promise]); source.cancel(); const before = run.writes.length; gate.resolve();
    await Promise.all([finishedA.promise, finishedB.promise]); await p.tick();
    const cancelled = await p.picks(p.quick(), scope, source.token);
    return { before, after: run.writes.length, items: p.summarize(run.picker.items), cancelled: p.summarize(cancelled),
      calls: [a.calls.length, b.calls.length], tokens: [a.calls[0].token.isCancellationRequested, b.calls[0].token.isCancellationRequested], unexpected: p.unexpected.length };
  });
  expect(result.before).toBe(0); expect(result.after).toBe(0); expect(result.items).toEqual([]); expect(result.cancelled).toEqual([]);
  expect(result.calls).toEqual([1, 1]); expect(result.tokens).toEqual([true, true]); expect(result.unexpected).toBe(0);
  checks.push('原Picker取消后零迟到成功/失败发布；原token到达所有Provider；已取消请求零新调用');
});

test('无 scope 聚合/原 Picker 与公开命令保留上游宽容，目标公开命令也不改变既有数组合同', async () => {
  const result = await inBrowser('原无scope与公开命令宽容语义', async () => {
    const p = globalThis.failureProbe, m = p.m, error = new Error('original tolerant failure');
    const a = p.register(p.target('A'), () => Promise.reject(error)), b = p.register(p.target('B', 'web'));
    const window = p.register({ runningLocation: { kind: 2 } });
    const aggregate = await m.getWorkspaceSymbols('SameName'), run = p.open(undefined); await run.done;
    const registration = m.registerOriginalWorkspaceSymbolCommand(); p.lifetimes.push(() => registration.dispose());
    const command = m.CommandsRegistry.getCommand('_executeWorkspaceSymbolProvider');
    const accessor = { get(id) { if (id !== m.IExtensionService) throw new Error('未知服务'); return { getExtensionHosts: () => [a.host, b.host, window.host] }; } };
    const unscopedCommand = await m.executeCommandForHost(command, accessor, ['SameName']);
    const targetCommand = await m.executeCommandForHost(command, accessor, ['SameName'], a.host);
    return { aggregate: aggregate.map(item => item.symbol.containerName).sort(), items: p.summarize(run.picker.items),
      unscopedCommand: unscopedCommand.map(item => item.containerName).sort(), targetCommand,
      logged: p.unexpected.length, originalErrors: p.unexpected.every(value => value === error) };
  });
  expect(result.aggregate).toEqual(['backend-B', 'window']); expect(result.items).toHaveLength(2); expect(result.items.every(item => item.symbol)).toBe(true);
  expect(result.unscopedCommand).toEqual(['backend-B', 'window']); expect(result.targetCommand).toEqual([]);
  expect(result.logged).toBe(4); expect(result.originalErrors).toBe(true);
  checks.push('未传回调的原聚合/无scopePicker/公开命令继续原unexpected handler与数组成功；目标公开命令宽容合同保持');
});

test('真实 MainThread/RPC 的跨目标结果拒绝进入 B 失败行，原 A 命中和 B cache 结算保持', async () => {
  const result = await inBrowser('原接收owner归属拒绝贯通业务Picker', async () => {
    const p = globalThis.failureProbe, a = await p.connect(p.target('A')), b = await p.connect(p.target('B', 'web'));
    const c = await p.connect(p.target('C')), window = await p.connect({ runningLocation: { kind: 2 } });
    b.onProvide = value => ({ ...value, symbols: value.symbols.map(symbol => ({ ...symbol, location: { ...symbol.location, uri: p.symbol(a.host).location.uri } })) });
    const run = p.open({ searchOwners: [...p.owners(a.host), ...p.owners(b.host)] }); await run.done; await p.tick();
    return { items: p.summarize(run.picker.items), expectedA: p.symbol(a.host).location.uri.toString(),
      calls: [a.calls.length, b.calls.length, c.calls.length, window.calls.length], releases: b.releases, caches: [...b.caches], unexpected: p.unexpected.length };
  });
  expect(result.items).toHaveLength(2); expect(result.items[0].resource).toBe(result.expectedA); expect(result.items[0].symbol).toBe(true);
  expect(result.items[1].label).toContain('不属于发起宿主的明确视图'); expect(result.items[1].description).toBe('gateway-B / backend-B');
  expect(result.items[1].symbol).toBe(false); expect(result.items[1].actionable).toBe(false); expect(result.calls).toEqual([1, 1, 0, 0]);
  expect(result.releases).toEqual([{ handle: 1, cacheId: 1, existed: true }]); expect(result.caches).toEqual([]); expect(result.unexpected).toBe(0);
  checks.push('原MainThread/RPC跨目标位置拒绝贯通B失败行；A typed命中保持、未选目标零调用、B cache准确释放');
});
