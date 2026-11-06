import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_code_actions.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'code-actions.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const built = await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { ILanguageFeaturesService } from '${upstream}editor/common/services/languageFeatures.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { isCancellationError } from '${upstream}base/common/errors.js';
export { Emitter } from '${upstream}base/common/event.js';
export { URI } from '${upstream}base/common/uri.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const sources = ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionLanguageFeaturesTransform.mjs',
  'scripts/extensions/extensionWorkspaceSymbolsTransform.mjs', 'src/extensions/languageFeatures.ts', 'src/extensions/targetUris.ts',
  'upstream/src/vs/editor/common/languageFeatureRegistry.ts', 'upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadBulkEdits.ts', 'upstream/src/vs/workbench/services/extensions/common/rpcProtocol.ts'];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path, createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const checks = [], observations = [], browserEvents = [];
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>Code action 组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('Code action组件HTTP来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Code action组件Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks,
      boundary: '真实 Chromium 中的原 MainThreadLanguageFeatures、LanguageFeaturesService/共享 registry、DI、RPCProtocol/TargetUriTransformer 与 WorkspaceEdit revive。认证 binding、内存字节输送、扩展 provide/resolve 回调及 cache 分配/释放接收端、模型 URI/语言/同步状态和 canonical URI 服务是具名装具；注册、注销、主线程生命周期、结果准入、action 修改与 release 派发均执行转换后的原 MainThread。未运行 Gateway、真实 Node、ExtHost CodeActionAdapter、实际编辑应用、命令执行、Notebook 或 Windows；不扩展 list 已 dispose 后 resolve 的合同。',
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
      const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 }, actionContext = { only: 'quickfix', trigger: 1 };
      const tick = () => new Promise(resolve => setTimeout(resolve, 0));
      const failure = promise => Promise.resolve(promise).then(() => 'unexpected-success', error => m.isCancellationError(error) ? 'cancelled' : error.message);
      const textEdit = resource => ({ resource, versionId: 7, textEdit: { range, text: 'replacement' } });
      const literalCommand = label => ({ id: 'probe.codeAction', title: label, $ident: 'opaque-command-id', arguments: [
        'boxteam-file://unrelated/body.ts', { text: 'file:///outside/command-body.ts', edit: { edits: [{ resource: 'literal URI text' }] } },
      ] });
      function target(label) {
        const roots = label === 'A' ? ['a1', 'a2'] : ['b1'];
        const binding = { gateway_id: 'gateway-actions', backend_id: 'backend-' + label, initialization: { os: 'linux', logs_location_uri: 'file:///hosts/logs',
          environment: { app_root_uri: 'file:///hosts/app', global_storage_home_uri: 'file:///hosts/global', workspace_storage_home_uri: 'file:///hosts/storage',
            profile_id: 'default', remote_settings: { native_uri: `file:///hosts/${label}/profile/settings.json`, resource_uri: `boxteam://gateway-actions/config/${label}/settings` } },
          workspace: { id: 'view-' + label, folders: roots.map(workspace_id => ({ workspace_id, name: workspace_id,
            resource_uri: `boxteam://workspace/${workspace_id}/resources/files/root`, native_uri: `file:///targets/${workspace_id}/project` })) } } };
        return { binding, uriTransformer: new m.TargetUriTransformer(binding, () => []), runningLocation: { kind: 3 } };
      }
      const nativeResources = host => host.uriTransformer?.folders.map(folder => m.URI.joinPath(folder.native, 'same.ts'))
        ?? [m.URI.file('/outside-web/actions.ts'), m.URI.parse('untitled:actions.ts')];
      const logicalResources = host => nativeResources(host).map(uri => host.uriTransformer ? m.URI.revive(host.uriTransformer.transformIncoming(uri)) : uri);
      const document = (host, index = 0) => ({ uri: logicalResources(host)[index], getLanguageId: () => 'typescript',
        isTooLargeForSyncing: () => false, isForSimpleWidget: false });
      const ownEdit = host => ({ edits: nativeResources(host).map(textEdit) });
      function invalidEdits(host, foreignHost) {
        const own = nativeResources(host)[0], foreign = logicalResources(foreignHost)[0];
        // UriComponents 不要求 $mid；未带 typed 标记的原 DTO 仍须由 MainThread 整批准入。
        const raw = { scheme: foreign.scheme, authority: foreign.authority, path: foreign.path, query: foreign.query, fragment: foreign.fragment };
        return [textEdit(raw), { newResource: raw }, { oldResource: raw, newResource: own }, { oldResource: own, newResource: raw },
          { oldResource: raw }, { ...textEdit(own), oldResource: raw, newResource: own }, textEdit(m.URI.file('/hosts/app/outside.ts'))];
      }
      async function connect(host, label = host.binding?.backend_id ?? 'Web') {
        const toMain = new m.Emitter(), toHost = new m.Emitter(), provides = [], resolves = [], releases = [], caches = new Set();
        // 仅替换字节输送与扩展回调；原 RPC 处理 typed URI、取消和序列化。
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        let sequence = 0;
        const client = { host, label, provides, resolves, releases, caches, onProvide: async (_request, value) => value, onResolve: async (_request, value) => value };
        const actor = {
          async $provideCodeActions(handle, resource, rangeOrSelection, context, token) {
            const cacheId = ++sequence, request = { handle, resource: m.URI.revive(resource), range: rangeOrSelection, context, token, arity: arguments.length };
            provides.push(request);
            const result = await client.onProvide(request, { cacheId, actions: [
              { cacheId: [cacheId, 0], title: label + ' editable', kind: 'quickfix', isPreferred: true, edit: ownEdit(host), command: literalCommand('before') },
              { cacheId: [cacheId, 1], title: label + ' command only', kind: 'quickfix', command: literalCommand('command only') },
              { cacheId: [cacheId, 2], title: label + ' disabled', disabled: '不可应用' },
            ] });
            if (result && typeof result.cacheId === 'number') caches.add(result.cacheId);
            return result;
          },
          async $resolveCodeAction(handle, cacheId, token) {
            const request = { handle, cacheId, token, arity: arguments.length }; resolves.push(request);
            return client.onResolve(request, { edit: ownEdit(host), command: literalCommand('resolved') });
          },
          async $releaseCodeActions(handle, id) { releases.push({ handle, id, existed: caches.delete(id) }); },
        };
        const di = new m.InstantiationService(new m.ServiceCollection([m.ILanguageFeaturesService, service]), true);
        const main = new m.MainThreadLanguageFeatures({ extensionHost: host, getProxy: mainRpc.getProxy.bind(mainRpc) }, undefined, undefined,
          service, { asCanonicalUri: uri => uri }, di, undefined);
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, main); hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, actor);
        const remote = hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures), before = new Set(service.codeActionProvider.allNoModel());
        const metadata = { providedKinds: ['quickfix', 'refactor'], documentation: [{ kind: 'quickfix', command: literalCommand('documentation') }] };
        await remote.$registerCodeActionSupport(1, [{ $serialized: true, language: 'typescript' }], metadata, label, 'probe.code-actions', true);
        const added = service.codeActionProvider.allNoModel().filter(provider => !before.has(provider));
        if (added.length !== 1) throw new Error('原 code action 注册入口应生成唯一 Provider');
        Object.assign(client, { main, remote, provider: added[0], metadata });
        lifetimes.push(() => { main.dispose(); di.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
        return client;
      }
      const provide = (client, token = m.CancellationToken.None, index = 0) => client.provider.provideCodeActions(document(client.host, index), range, actionContext, token);
      const editUris = action => action.edit?.edits.map(edit => edit.resource.toString());
      const disposeList = async list => { list.dispose(); list.dispose(); await tick(); };
      globalThis.actionProbe = { m, target, connect, service, document, provide, textEdit, ownEdit, invalidEdits, nativeResources, logicalResources,
        literalCommand, range, actionContext, editUris, disposeList, tick, failure, lifetimes, dispose() { for (const dispose of lifetimes.reverse()) dispose(); } };
    });
    const result = await page.evaluate(body, argument); expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.actionProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('原注册表选择双根宿主，provide/resolve 恢复完整 edit 并保留原 action 身份和 metadata', async () => {
  const result = await inBrowser('双根编辑、真实RPC、原action身份与缓存释放', async () => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const selections = [0, 1].map(index => p.service.codeActionProvider.ordered(p.document(a.host, index)).map(provider => provider === a.provider ? 'A' : 'other'));
    const peerSelection = p.service.codeActionProvider.ordered(p.document(b.host)).map(provider => provider === b.provider ? 'B' : 'other');
    const list = await p.provide(a, m.CancellationToken.None, 1), action = list.actions[0], initialUris = p.editUris(action), previousEdit = action.edit;
    const resolved = await a.provider.resolveCodeAction(action, m.CancellationToken.None);
    const result = { selections, peerSelection, titles: list.actions.map(value => value.title), initialUris, resolvedUris: p.editUris(resolved),
      expectedUris: p.logicalResources(a.host).map(uri => uri.toString()), typed: resolved.edit.edits.every(edit => m.URI.isUri(edit.resource)),
      versions: resolved.edit.edits.map(edit => edit.versionId), identity: resolved === action, replacedEdit: resolved.edit !== previousEdit,
      command: resolved.command, expectedCommand: p.literalCommand('resolved'), metadata: { kinds: a.provider.providedCodeActionKinds,
        documentation: a.provider.documentation, displayName: a.provider.displayName, extensionId: a.provider.extensionId }, expectedMetadata: a.metadata,
      request: { resource: a.provides[0].resource.toString(), range: a.provides[0].range, context: a.provides[0].context, arity: a.provides[0].arity },
      expectedNative: p.nativeResources(a.host)[1].toString(), expectedRange: p.range, expectedContext: p.actionContext,
      resolveRequest: { handle: a.resolves[0].handle, cacheId: a.resolves[0].cacheId, arity: a.resolves[0].arity }, peerCalls: b.provides.length };
    await p.disposeList(list); return { ...result, releases: a.releases, caches: [...a.caches] };
  });
  expect(result.selections).toEqual([['A'], ['A']]); expect(result.peerSelection).toEqual(['B']); expect(result.peerCalls).toBe(0);
  expect(result.titles).toEqual(['backend-A editable', 'backend-A command only', 'backend-A disabled']); expect(result.initialUris).toEqual(result.expectedUris); expect(result.resolvedUris).toEqual(result.expectedUris);
  expect([result.typed, result.identity, result.replacedEdit]).toEqual([true, true, true]); expect(result.versions).toEqual([7, 7]); expect(result.command).toEqual(result.expectedCommand);
  expect(result.metadata).toEqual({ kinds: result.expectedMetadata.providedKinds, documentation: result.expectedMetadata.documentation, displayName: 'backend-A', extensionId: 'probe.code-actions' });
  expect(result.request).toEqual({ resource: result.expectedNative, range: result.expectedRange, context: result.expectedContext, arity: 5 });
  expect(result.resolveRequest).toEqual({ handle: 1, cacheId: [1, 0], arity: 3 }); expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('原registry双根选择、RPC native/typed URI往返、edit与metadata完整、resolve原action身份、双dispose精确单次release');
});

test('后一 action 的 WorkspaceEdit 包含 foreign 后项时整批不发布，app 位置经过真实 transport 后仍拒绝', async () => {
  const result = await inBrowser('provide整批全部端点归属与准确cache释放', async () => {
    const p = globalThis.actionProbe, a = await p.connect(p.target('A')), invalid = p.invalidEdits(a.host, p.target('B')), outcomes = [];
    for (const item of invalid) {
      a.onProvide = async (_request, list) => ({ ...list, actions: [list.actions[0], { ...list.actions[1],
        edit: { edits: [p.textEdit(p.nativeResources(a.host)[0]), item] } }] });
      outcomes.push(await p.failure(p.provide(a)));
    }
    await p.tick(); return { outcomes, releases: a.releases, caches: [...a.caches], calls: a.provides.length, resolves: a.resolves.length };
  });
  expect(result.outcomes).toHaveLength(7); for (const outcome of result.outcomes) expect(outcome).toContain('超出发起宿主工作区视图');
  expect(result.outcomes.at(-1)).toContain('file:///hosts/app/outside.ts'); expect(result.calls).toBe(7); expect(result.resolves).toBe(0);
  expect(result.releases).toEqual(Array.from({ length: 7 }, (_, index) => ({ handle: 1, id: index + 1, existed: true }))); expect(result.caches).toEqual([]);
  checks.push('provide后项foreign resource/oldResource/newResource及typed app整批拒绝，合法前action不发布，每个原cache只release一次');
});

test('resolve 的 foreign 后项拒绝时不修改原 edit 或 command，原 list 仍持有 cache', async () => {
  const result = await inBrowser('resolve整批准入和原action不变', async () => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), list = await p.provide(a), action = list.actions[0];
    const edit = action.edit, command = action.command, original = JSON.stringify(action), outcomes = [], unchanged = [];
    for (const item of p.invalidEdits(a.host, p.target('B'))) {
      a.onResolve = async () => ({ edit: { edits: [p.textEdit(p.nativeResources(a.host)[0]), item] }, command: p.literalCommand('must-not-replace') });
      outcomes.push(await p.failure(a.provider.resolveCodeAction(action, m.CancellationToken.None)));
      unchanged.push(action.edit === edit && action.command === command && JSON.stringify(action) === original);
    }
    await p.tick(); const beforeDispose = { releases: [...a.releases], caches: [...a.caches] }; await p.disposeList(list);
    return { outcomes, unchanged, beforeDispose, releases: a.releases, caches: [...a.caches], ids: a.resolves.map(request => request.cacheId) };
  });
  expect(result.outcomes).toHaveLength(7); for (const outcome of result.outcomes) expect(outcome).toContain('超出发起宿主工作区视图');
  expect(result.unchanged).toEqual(Array(7).fill(true)); expect(result.beforeDispose).toEqual({ releases: [], caches: [1] }); expect(result.ids).toEqual(Array(7).fill([1, 0]));
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('resolve整批归属先于action edit/command赋值，失败保持原对象/正文，cache由原list双dispose单次结算');
});

test('无 edit、原命令正文、空 resolve 与无 list 保留原行为', async () => {
  const result = await inBrowser('无edit action和命令正文原样', async () => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), list = await p.provide(a), commandOnly = list.actions[1], disabled = list.actions[2];
    const command = commandOnly.command, before = JSON.stringify(commandOnly); a.onResolve = async () => ({});
    const resolved = await a.provider.resolveCodeAction(commandOnly, m.CancellationToken.None);
    const resolvedDisabled = await a.provider.resolveCodeAction(disabled, m.CancellationToken.None);
    a.onProvide = async () => undefined; const missing = await p.provide(a);
    const result = { identity: resolved === commandOnly && resolvedDisabled === disabled, unchanged: commandOnly.command === command && JSON.stringify(commandOnly) === before,
      command: commandOnly.command, expected: p.literalCommand('command only'), absentEdits: [commandOnly.edit === undefined, disabled.edit === undefined],
      disabled: disabled.disabled, missing: missing === undefined, resolveIds: a.resolves.map(request => request.cacheId), cacheBeforeDispose: [...a.caches] };
    await p.disposeList(list); return { ...result, releases: a.releases, caches: [...a.caches] };
  });
  expect([result.identity, result.unchanged, result.missing]).toEqual([true, true, true]); expect(result.command).toEqual(result.expected); expect(result.absentEdits).toEqual([true, true]);
  expect(result.disabled).toBe('不可应用'); expect(result.resolveIds).toEqual([[1, 1], [1, 2]]); expect(result.cacheBeforeDispose).toEqual([1]);
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('无edit/disabled action、命令字符串正文和空resolve原样；undefined list不造cache/release');
});

test('Web 无 binding 保留原 file 与 untitled 编辑范围和 resolve 行为', async () => {
  const result = await inBrowser('Web原code action编辑范围', async () => {
    const p = globalThis.actionProbe, m = p.m, web = await p.connect({ runningLocation: { kind: 2 } }), list = await p.provide(web), action = list.actions[0];
    const before = p.editUris(action), resolved = await web.provider.resolveCodeAction(action, m.CancellationToken.None);
    const result = { before, after: p.editUris(resolved), expected: p.logicalResources(web.host).map(uri => uri.toString()),
      identity: resolved === action, typed: resolved.edit.edits.every(edit => m.URI.isUri(edit.resource)), command: resolved.command,
      expectedCommand: p.literalCommand('resolved'), selected: p.service.codeActionProvider.ordered(p.document(web.host)).includes(web.provider) };
    await p.disposeList(list); return { ...result, releases: web.releases, caches: [...web.caches] };
  });
  expect(result.before).toEqual(result.expected); expect(result.after).toEqual(result.expected); expect([result.identity, result.typed, result.selected]).toEqual([true, true, true]);
  expect(result.command).toEqual(result.expectedCommand); expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('Web无binding保持原file/untitled provide/resolve编辑范围、action身份和cache收尾');
});

test('入口已取消时 provide/resolve 零 RPC，已发布 action 和 cache 不变', async () => {
  const result = await inBrowser('入口取消零RPC', async () => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), source = new m.CancellationTokenSource(); source.cancel();
    const provide = await p.failure(p.provide(a, source.token)), before = a.provides.length, list = await p.provide(a), action = list.actions[0], snapshot = JSON.stringify(action);
    const resolve = await p.failure(a.provider.resolveCodeAction(action, source.token)); await p.tick();
    const state = { provide, resolve, before, calls: [a.provides.length, a.resolves.length], unchanged: JSON.stringify(action) === snapshot,
      beforeDispose: { releases: [...a.releases], caches: [...a.caches] } }; await p.disposeList(list); source.dispose();
    return { ...state, releases: a.releases, caches: [...a.caches] };
  });
  expect(result.provide).toBe('cancelled'); expect(result.resolve).toBe('cancelled'); expect(result.before).toBe(0); expect(result.calls).toEqual([1, 0]); expect(result.unchanged).toBe(true);
  expect(result.beforeDispose).toEqual({ releases: [], caches: [1] }); expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('入口已取消provide/resolve均零RPC，原action与已发布list cache保持');
});

for (const operation of ['cancel', 'unregister', 'dispose']) test(`${operation} 后迟到 provide/resolve 不发布或修改 action，cache 归原 list 且不重复释放`, async () => {
  const result = await inBrowser(`${operation}的迟到结果、linked token、原cache与新代际`, async operation => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B'));
    const initial = await p.provide(a), action = initial.actions[0], savedEdit = action.edit, savedCommand = action.command, snapshot = JSON.stringify(action);
    const provideEntered = Promise.withResolvers(), resolveEntered = Promise.withResolvers(), provideGate = Promise.withResolvers(), resolveGate = Promise.withResolvers();
    const source = new m.CancellationTokenSource();
    a.onProvide = async (request, value) => { provideEntered.resolve(request); await provideGate.promise; return value; };
    a.onResolve = async (request, value) => { resolveEntered.resolve(request); await resolveGate.promise; return { ...value, command: p.literalCommand('late-command') }; };
    const provided = p.failure(p.provide(a, source.token)), resolved = p.failure(a.provider.resolveCodeAction(action, source.token));
    const [provideRequest, resolveRequest] = await Promise.all([
      Promise.race([provideEntered.promise, provided.then(value => { throw new Error('provide在回调gate前结束：' + value); })]),
      Promise.race([resolveEntered.promise, resolved.then(value => { throw new Error('resolve在回调gate前结束：' + value); })]),
    ]);
    if (operation === 'cancel') source.cancel(); else if (operation === 'unregister') await a.remote.$unregister(1); else a.main.dispose();
    await p.tick(); const tokens = [provideRequest.token.isCancellationRequested, resolveRequest.token.isCancellationRequested];
    const present = p.service.codeActionProvider.allNoModel().includes(a.provider), beforeLate = { releases: [...a.releases], caches: [...a.caches] };
    const next = await p.connect(p.target('A'), 'new-generation'), nextList = await p.provide(next), peerList = await p.provide(b);
    provideGate.resolve(); resolveGate.resolve(); const outcomes = await Promise.all([provided, resolved]); await p.tick();
    const beforeListDispose = { releases: [...a.releases], caches: [...a.caches] }, counts = [a.provides.length, a.resolves.length];
    const rejected = await Promise.all([p.failure(p.provide(a, source.token)), p.failure(a.provider.resolveCodeAction(action, source.token))]);
    const unchanged = action.edit === savedEdit && action.command === savedCommand && JSON.stringify(action) === snapshot;
    if (operation === 'cancel') await a.remote.$unregister(1);
    await p.disposeList(initial); const isolated = { nextCaches: [...next.caches], nextReleases: [...next.releases], peerCaches: [...b.caches], peerReleases: [...b.releases] };
    await p.disposeList(nextList); await p.disposeList(peerList); source.dispose();
    return { tokens, present, beforeLate, outcomes, beforeListDispose, rejected, unchanged, counts, afterCounts: [a.provides.length, a.resolves.length],
      oldReleases: a.releases, oldCaches: [...a.caches], isolated, nextReleases: next.releases, peerReleases: b.releases,
      nextTitle: nextList.actions[0].title, peerTitle: peerList.actions[0].title };
  }, operation);
  expect(result.tokens).toEqual([true, true]); expect(result.present).toBe(operation === 'cancel'); expect(result.beforeLate).toEqual({ releases: [], caches: [1] });
  expect(result.outcomes).toEqual(['cancelled', 'cancelled']); expect(result.beforeListDispose).toEqual({ releases: [{ handle: 1, id: 2, existed: true }], caches: [1] });
  expect(result.rejected).toEqual(['cancelled', 'cancelled']); expect(result.unchanged).toBe(true); expect(result.counts).toEqual([2, 1]); expect(result.afterCounts).toEqual(result.counts);
  expect(result.oldReleases).toEqual([{ handle: 1, id: 2, existed: true }, { handle: 1, id: 1, existed: true }]); expect(result.oldCaches).toEqual([]);
  expect(result.isolated).toEqual({ nextCaches: [1], nextReleases: [], peerCaches: [1], peerReleases: [] }); expect(result.nextTitle).toBe('new-generation editable'); expect(result.peerTitle).toBe('backend-B editable');
  expect(result.nextReleases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.peerReleases).toEqual([{ handle: 1, id: 1, existed: true }]);
  checks.push(`${operation}取消原linked token，迟到provide仅释放新cache、resolve不改action，原list双dispose单次收尾，新代际/peer独立`);
});
