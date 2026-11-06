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
export { ExtHostLanguageFeatures } from '${upstream}workbench/api/common/extHostLanguageFeatures.js';
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { CommandsConverter } from '${upstream}workbench/api/common/extHostCommands.js';
export { CodeAction, CodeActionKind, WorkspaceEdit, Range as ApiRange } from '${upstream}workbench/api/common/extHostTypes.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { ILanguageFeaturesService } from '${upstream}editor/common/services/languageFeatures.js';
export { CodeActionItem } from '${upstream}editor/contrib/codeAction/common/types.js';
export { applyCodeAction, ApplyCodeActionReason } from '${upstream}editor/contrib/codeAction/browser/codeAction.js';
export { CodeActionController } from '${upstream}editor/contrib/codeAction/browser/codeActionController.js';
export { IBulkEditService } from '${upstream}editor/browser/services/bulkEditService.js';
export { ICommandService } from '${upstream}platform/commands/common/commands.js';
export { ITelemetryService } from '${upstream}platform/telemetry/common/telemetry.js';
export { INotificationService } from '${upstream}platform/notification/common/notification.js';
export { IAccessibilitySignalService } from '${upstream}platform/accessibilitySignal/browser/accessibilitySignalService.js';
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
  'upstream/src/vs/editor/common/languages.ts', 'upstream/src/vs/editor/contrib/codeAction/common/types.ts',
  'upstream/src/vs/editor/contrib/codeAction/browser/codeAction.ts',
  'upstream/src/vs/editor/contrib/codeAction/browser/codeActionController.ts',
  'upstream/src/vs/workbench/api/common/extHostLanguageFeatures.ts', 'upstream/src/vs/workbench/api/common/extHostTypeConverters.ts',
  'upstream/src/vs/workbench/api/common/extHostDocuments.ts', 'upstream/src/vs/workbench/api/common/extHostDocumentsAndEditors.ts',
  'upstream/src/vs/workbench/api/common/extHostDocumentData.ts',
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
      boundary: '真实 Chromium 中的原 MainThreadLanguageFeatures、LanguageFeaturesService/共享 registry、DI、RPCProtocol/TargetUriTransformer、WorkspaceEdit revive、CodeActionItem 与 applyCodeAction。版本producer另经原ExtHostLanguageFeatures/CodeActionAdapter、WorkspaceEdit.from、CommandsConverter及ExtHostDocuments/AndEditors/DocumentData；输入delta/change、扩展callback、诊断空集与命令注册接收端是具名边界，版本不由装具写入DTO。Controller错误场景借用原applyCodeAction/_trigger方法，editor、progress、model.trigger与通知接收端为具名UI装具，不构造Controller模型或菜单。其余DTO准入场景的扩展callback/cache接收端、认证binding、内存字节输送、模型URI/语言/同步状态和canonical URI服务是具名装具；BulkEdit、命令、遥测、通知与无障碍服务为边界装具，预览只模拟BulkEdit内部取消检查，内存副作用不是真实磁盘编辑。未运行Gateway、真实Node、真实BulkEditService、命令处理器、UI点击、Notebook版本producer或Windows；不扩展list已dispose后resolve的合同。',
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
      async function connect(host, label = host.binding?.backend_id ?? 'Web', producer) {
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
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, main);
        const remote = hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures), before = new Set(service.codeActionProvider.allNoModel());
        const metadata = { providedKinds: ['quickfix', 'refactor'], documentation: [{ kind: 'quickfix', command: literalCommand('documentation') }] };
        let registration, extHost, documents, documentOwner;
        if (producer) {
          const context = { getProxy: hostRpc.getProxy.bind(hostRpc) }, log = new m.NullLogService();
          documentOwner = new m.ExtHostDocumentsAndEditors(context, log); documents = new m.ExtHostDocuments(context, documentOwner);
          const converter = new m.CommandsConverter({ registerCommand: () => ({ dispose() {} }) }, () => undefined, log);
          extHost = new m.ExtHostLanguageFeatures(context, undefined, documents, { converter }, { getDiagnostics: () => [] }, log,
            { report() { throw new Error('版本producer场景不应使用弃用API'); } }, { onExtensionError(_extension, error) { throw error; } });
          // 只观察原 ExtHost actor 的调用和 cache 生命周期；准入、版本生产与缓存算法全部走原入口。
          hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, {
            async $provideCodeActions(handle, resource, selection, context, token) {
              provides.push({ handle, resource: m.URI.revive(resource), token });
              const result = await extHost.$provideCodeActions(handle, resource, selection, context, token);
              if (result) caches.add(result.cacheId); return result;
            },
            async $resolveCodeAction(handle, cacheId, token) { resolves.push({ handle, cacheId, token }); return extHost.$resolveCodeAction(handle, cacheId, token); },
            $releaseCodeActions(handle, id) { releases.push({ handle, id, existed: caches.delete(id) }); extHost.$releaseCodeActions(handle, id); },
          });
          registration = extHost.registerCodeActionProvider({ identifier: { value: 'probe.code-actions' }, name: label, displayName: label, enabledApiProposals: [] },
            ['typescript'], producer, { providedCodeActionKinds: [m.CodeActionKind.QuickFix] });
          Object.assign(client, { documents, documentOwner,
            addDocument(uri, version, text = 'old-source') {
              documentOwner.acceptDocumentsAndEditorsDelta({ addedDocuments: [{ uri, versionId: version, lines: text.split('\n'), EOL: '\n', languageId: 'typescript', isDirty: false, encoding: 'utf8' }] });
            },
            changeDocument(uri, version, text) {
              const document = documents.getDocument(uri), lastLine = document.lineCount, oldText = document.getText();
              documents.$acceptModelChanged(uri, { versionId: version, eol: '\n', isUndoing: false, isRedoing: false, isFlush: false, isEolChange: false, detailedReason: undefined,
                changes: [{ range: { startLineNumber: 1, startColumn: 1, endLineNumber: lastLine, endColumn: document.lineAt(lastLine - 1).text.length + 1 }, rangeOffset: 0, rangeLength: oldText.length, text }] }, true);
            },
          });
          await tick();
        } else {
          hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, actor);
          await remote.$registerCodeActionSupport(1, [{ $serialized: true, language: 'typescript' }], metadata, label, 'probe.code-actions', true);
        }
        const added = service.codeActionProvider.allNoModel().filter(provider => !before.has(provider));
        if (added.length !== 1) throw new Error('原 code action 注册入口应生成唯一 Provider');
        Object.assign(client, { main, remote, provider: added[0], metadata });
        lifetimes.push(async () => {
          if (registration) { registration.dispose(); await tick(); }
          if (documents) { documentOwner.acceptDocumentsAndEditorsDelta({ removedDocuments: documents.getAllDocumentData().map(data => data.document.uri) }); documents.dispose(); extHost.dispose(); }
          main.dispose(); di.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose();
        });
        return client;
      }
      const provide = (client, token = m.CancellationToken.None, index = 0) => client.provider.provideCodeActions(document(client.host, index), range, actionContext, token);
      const editUris = action => action.edit?.edits.map(edit => edit.resource.toString());
      const disposeList = async list => { list.dispose(); list.dispose(); await tick(); };
      function applyBoundary() {
        // 原 apply 通过真实 DI 取服务；边界仅记录派发和内存效果，不复制应用流程或执行磁盘／命令处理器。
        const state = { bulk: [], commands: [], effects: [], order: [], notifications: [], telemetry: [], signals: [] };
        const commit = call => {
          state.effects.push(call.edit.edits.map(edit => ({ resource: edit.resource.toString(), versionId: edit.versionId, text: edit.textEdit.text })));
          state.order.push('effect');
        };
        state.onBulk = async call => { commit(call); return { isApplied: true, ariaSummary: '内存编辑边界' }; };
        const di = new m.InstantiationService(new m.ServiceCollection(
          [m.IBulkEditService, { apply: async (edit, options) => {
            const call = { edit, options }; state.bulk.push(call); state.order.push('bulk'); return state.onBulk(call);
          } }],
          [m.ICommandService, { executeCommand: async (id, ...args) => { state.commands.push({ id, args }); state.order.push('command'); } }],
          [m.ITelemetryService, { publicLog2: (event, data) => state.telemetry.push({ event, data }) }],
          [m.INotificationService, { error: error => state.notifications.push(String(error)) }],
          [m.IAccessibilitySignalService, { playSignal: signal => { state.signals.push(signal); } }],
        ), true);
        lifetimes.push(() => di.dispose());
        return Object.assign(state, { commit, instantiationService: di,
          apply: (item, token = m.CancellationToken.None, options) => di.invokeFunction(m.applyCodeAction, item, m.ApplyCodeActionReason.FromCodeActions, options, token),
          snapshot: () => ({ bulk: state.bulk.map(({ edit, options }) => ({ resources: editUris({ edit }), versions: edit.edits.map(value => value.versionId),
            tokenPresent: options.token !== undefined, tokenCancelled: options.token?.isCancellationRequested === true, preview: options.showPreview === true,
            label: options.label, code: options.code, respectAutoSaveConfig: options.respectAutoSaveConfig })),
          commands: [...state.commands], effects: [...state.effects], order: [...state.order], notifications: [...state.notifications], telemetry: [...state.telemetry] }),
        });
      }
      globalThis.actionProbe = { m, target, connect, service, document, provide, textEdit, ownEdit, invalidEdits, nativeResources, logicalResources,
        literalCommand, range, actionContext, editUris, disposeList, applyBoundary, tick, failure, lifetimes, async dispose() { for (const dispose of lifetimes.reverse()) await dispose(); } };
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

test('原 apply 先派发直接 edit 再执行 command，command-only 经过原 resolve 后保留命令正文', async () => {
  const result = await inBrowser('原CodeActionItem与apply的正常应用', async () => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), list = await p.provide(a), boundary = p.applyBoundary();
    await boundary.apply(new m.CodeActionItem(list.actions[0], a.provider)); const directResolves = a.resolves.length;
    a.onResolve = async () => ({}); await boundary.apply(new m.CodeActionItem(list.actions[1], a.provider));
    const command = p.literalCommand('正文'), state = { ...boundary.snapshot(), directResolves, resolveIds: a.resolves.map(value => value.cacheId),
      resources: p.logicalResources(a.host).map(uri => uri.toString()), command: { id: command.id, args: command.arguments } };
    await p.disposeList(list); return { ...state, releases: a.releases, caches: [...a.caches] };
  });
  expect(result.directResolves).toBe(0); expect(result.resolveIds).toEqual([[1, 1]]);
  expect(result.bulk).toEqual([{ resources: result.resources, versions: [7, 7], tokenPresent: true, tokenCancelled: false, preview: false,
    label: 'backend-A editable', code: 'undoredo.codeAction', respectAutoSaveConfig: true }]);
  expect(result.effects).toEqual([result.resources.map(resource => ({ resource, versionId: 7, text: 'replacement' }))]);
  expect(result.commands).toEqual([result.command, result.command]); expect(result.order).toEqual(['bulk', 'effect', 'command', 'command']); expect(result.notifications).toEqual([]);
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('原CodeActionItem/apply的直接edit零resolve、edit先于command、command-only原resolve与完整命令参数；BulkEdit/命令为边界记录');
});

for (const operation of ['cancel', 'unregister', 'dispose']) test(`已发布 action 在 ${operation} 后进入原 apply 时零 BulkEdit／命令派发，新代际和 B 仍可应用`, async () => {
  const result = await inBrowser(`${operation}后应用已发布action`, async operation => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), b = await p.connect(p.target('B')), list = await p.provide(a);
    const source = new m.CancellationTokenSource(), original = JSON.stringify(list.actions), boundary = p.applyBoundary();
    if (operation === 'cancel') source.cancel(); else if (operation === 'unregister') await a.remote.$unregister(1); else a.main.dispose();
    await boundary.apply(new m.CodeActionItem(list.actions[0], a.provider), source.token);
    await boundary.apply(new m.CodeActionItem(list.actions[1], a.provider), source.token);
    const stopped = { ...boundary.snapshot(), resolves: a.resolves.length, unchanged: JSON.stringify(list.actions) === original,
      providerCancelled: a.provider.lifecycleToken.isCancellationRequested, registered: p.service.codeActionProvider.allNoModel().includes(a.provider),
      releases: [...a.releases], caches: [...a.caches] };
    const next = await p.connect(p.target('A'), 'new-generation'), nextList = await p.provide(next), peerList = await p.provide(b), active = p.applyBoundary();
    await active.apply(new m.CodeActionItem(nextList.actions[0], next.provider)); await active.apply(new m.CodeActionItem(peerList.actions[0], b.provider));
    await p.disposeList(list); const isolated = { next: [...next.caches], peer: [...b.caches], nextReleases: [...next.releases], peerReleases: [...b.releases] };
    await p.disposeList(nextList); await p.disposeList(peerList); source.dispose();
    return { stopped, active: active.snapshot(), isolated, oldReleases: a.releases, oldCaches: [...a.caches], nextReleases: next.releases, peerReleases: b.releases };
  }, operation);
  expect(result.stopped.bulk).toEqual([]); expect(result.stopped.effects).toEqual([]); expect(result.stopped.commands).toEqual([]); expect(result.stopped.resolves).toBe(0);
  expect(result.stopped.telemetry).toEqual([]); expect(result.stopped.notifications).toEqual([]); expect(result.stopped.unchanged).toBe(true);
  expect(result.stopped.providerCancelled).toBe(operation !== 'cancel'); expect(result.stopped.registered).toBe(operation === 'cancel');
  expect(result.stopped.releases).toEqual([]); expect(result.stopped.caches).toEqual([1]);
  expect(result.active.bulk).toHaveLength(2); expect(result.active.effects).toHaveLength(2); expect(result.active.commands).toHaveLength(2); expect(result.active.notifications).toEqual([]);
  expect(result.active.bulk.map(value => value.tokenCancelled)).toEqual([false, false]); expect(result.active.bulk.map(value => value.label)).toEqual(['new-generation editable', 'backend-B editable']);
  expect(result.isolated).toEqual({ next: [1], peer: [1], nextReleases: [], peerReleases: [] });
  expect(result.oldReleases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.oldCaches).toEqual([]);
  expect(result.nextReleases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.peerReleases).toEqual([{ handle: 1, id: 1, existed: true }]);
  checks.push(`${operation}后原apply拒绝已发布直接edit/command-only且零resolve，原cache由list释放，同binding新registration与B仍成功`);
});

for (const operation of ['cancel', 'unregister', 'dispose']) for (const phase of ['resolve', 'preview', 'applied']) test(`${phase} 等待中 ${operation} 取消原 apply，已完成效果保留而 command 不执行`, async () => {
  const result = await inBrowser(`${phase}等待中的${operation}`, async ({ operation, phase }) => {
    const p = globalThis.actionProbe, m = p.m, a = await p.connect(p.target('A')), list = await p.provide(a), boundary = p.applyBoundary();
    const source = new m.CancellationTokenSource(), entered = Promise.withResolvers(), gate = Promise.withResolvers(), action = list.actions[phase === 'resolve' ? 1 : 0];
    const snapshot = () => JSON.stringify({ ...action, edit: action.edit && { ...action.edit,
      edits: action.edit.edits.map(edit => ({ ...edit, resource: edit.resource.toString() })) } });
    const original = snapshot(), savedEdit = action.edit, savedCommand = action.command;
    if (phase === 'resolve') {
      a.onResolve = async (request, value) => { entered.resolve(request); await gate.promise; return value; };
    } else {
      boundary.onBulk = async call => {
        // 仅预览边界模拟 BulkEdit 内部的取消检查；applied 边界已产生不可回滚效果，但故意延迟成功回执。
        if (phase === 'applied') boundary.commit(call);
        entered.resolve(call); await gate.promise;
        if (phase === 'preview' && !call.options.token?.isCancellationRequested) boundary.commit(call);
        return { isApplied: phase === 'applied' || !call.options.token?.isCancellationRequested, ariaSummary: '等待边界' };
      };
    }
    const applying = boundary.apply(new m.CodeActionItem(action, a.provider), source.token, { preview: phase === 'preview' });
    const request = await Promise.race([entered.promise, applying.then(() => { throw new Error('原apply在等待边界前结束'); })]);
    const token = phase === 'resolve' ? request.token : request.options.token, before = { present: token !== undefined, cancelled: token?.isCancellationRequested === true, effects: boundary.effects.length };
    let cancellationEvents = 0; const subscription = token?.onCancellationRequested(() => cancellationEvents++);
    if (operation === 'cancel') source.cancel(); else if (operation === 'unregister') await a.remote.$unregister(1); else a.main.dispose();
    await p.tick(); const after = { cancelled: token?.isCancellationRequested === true, cancellationEvents, effects: boundary.effects.length };
    gate.resolve(); await applying; subscription?.dispose();
    const state = { ...boundary.snapshot(), before, after, resolveIds: a.resolves.map(value => value.cacheId),
      unchanged: action.edit === savedEdit && action.command === savedCommand && snapshot() === original,
      beforeListDispose: { releases: [...a.releases], caches: [...a.caches] } };
    await p.disposeList(list); source.dispose(); return { ...state, releases: a.releases, caches: [...a.caches] };
  }, { operation, phase });
  expect(result.before).toEqual({ present: true, cancelled: false, effects: phase === 'applied' ? 1 : 0 });
  expect(result.after).toEqual({ cancelled: true, cancellationEvents: 1, effects: phase === 'applied' ? 1 : 0 });
  expect(result.bulk).toHaveLength(phase === 'resolve' ? 0 : 1); expect(result.effects).toHaveLength(phase === 'applied' ? 1 : 0);
  if (phase !== 'resolve') { expect(result.bulk[0].tokenCancelled).toBe(true); expect(result.bulk[0].preview).toBe(phase === 'preview'); }
  expect(result.commands).toEqual([]); expect(result.notifications).toEqual([]); expect(result.unchanged).toBe(true);
  expect(result.resolveIds).toEqual(phase === 'resolve' ? [[1, 1]] : []); expect(result.beforeListDispose).toEqual({ releases: [], caches: [1] });
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push(`${phase}/${operation}通过原apply合并token并取消；resolve不修改action，预览边界无效果，已生效边界保留效果但零command，list单次release`);
});

test('Web 和无 provider 的内置直接 action 仍经原 apply 成功，Web 生命周期不增加 Node 限制', async () => {
  const result = await inBrowser('Web与内置直接action保留原应用行为', async () => {
    const p = globalThis.actionProbe, m = p.m, web = await p.connect({ runningLocation: { kind: 2 } }), list = await p.provide(web), boundary = p.applyBoundary();
    web.onResolve = async () => ({});
    await boundary.apply(new m.CodeActionItem(list.actions[0], web.provider)); await boundary.apply(new m.CodeActionItem(list.actions[1], web.provider));
    const noLifetime = web.provider.lifecycleToken === undefined; web.main.dispose();
    await boundary.apply(new m.CodeActionItem(list.actions[0], web.provider));
    const builtin = { title: 'builtin direct', edit: { edits: [p.textEdit(m.URI.file('/outside/builtin.ts'))] }, command: p.literalCommand('builtin') };
    await boundary.apply(new m.CodeActionItem(builtin, undefined));
    const state = { ...boundary.snapshot(), noLifetime, resources: p.logicalResources(web.host).map(uri => uri.toString()), resolveIds: web.resolves.map(value => value.cacheId) };
    await p.disposeList(list); return { ...state, releases: web.releases, caches: [...web.caches] };
  });
  expect(result.noLifetime).toBe(true); expect(result.bulk.map(value => value.resources)).toEqual([result.resources, result.resources, ['file:///outside/builtin.ts']]);
  expect(result.bulk.map(value => [value.tokenPresent, value.tokenCancelled])).toEqual([[true, false], [true, false], [true, false]]);
  expect(result.effects).toHaveLength(3); expect(result.commands).toHaveLength(4); expect(result.resolveIds).toEqual([[1, 1]]); expect(result.notifications).toEqual([]);
  expect(result.releases).toEqual([{ handle: 1, id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('Web的原file/untitled直接应用与command-only、关闭后已发布直接action及无provider内置直接action成功，不增加Node归属限制');
});

test('原 CodeActionAdapter 在 provide 调用前冻结双根版本，等待及发布后的变更不重盖，A/B 同名原生 URI 独立', async () => {
  const result = await inBrowser('provide原版本producer与双宿主隔离', async () => {
    const p = globalThis.actionProbe, m = p.m, entered = Promise.withResolvers(), gate = Promise.withResolvers(); let a, callbackDocument, callbackVersions;
    const actionFor = (client, title) => {
      const action = new m.CodeAction(title, m.CodeActionKind.QuickFix), edit = new m.WorkspaceEdit();
      for (const uri of p.nativeResources(client.host)) edit.replace(uri, new m.ApiRange(0, 0, 0, 3), 'replacement');
      action.edit = edit; return action;
    };
    a = await p.connect(p.target('A'), 'producer-A', { async provideCodeActions(document) {
      callbackDocument = { uri: document.uri.toString(), version: document.version }; callbackVersions = p.nativeResources(a.host).map(uri => a.documents.getDocument(uri).version);
      const action = actionFor(a, 'provide A'); entered.resolve(); await gate.promise; return [action];
    } });
    const own = p.nativeResources(a.host); a.addDocument(own[0], 11); a.addDocument(own[1], 21);
    const peerHost = p.target('B'); peerHost.binding.initialization.workspace.folders[0].native_uri = a.host.binding.initialization.workspace.folders[0].native_uri;
    peerHost.uriTransformer = new m.TargetUriTransformer(peerHost.binding, () => []);
    let b; b = await p.connect(peerHost, 'producer-B', { provideCodeActions() { return [actionFor(b, 'provide B')]; } });
    b.addDocument(p.nativeResources(b.host)[0], 91);
    const pending = p.provide(a); await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入原provide callback等待'); })]);
    a.changeDocument(own[1], 22, 'changed-during-provide'); const duringVersions = own.map(uri => a.documents.getDocument(uri).version), peerList = await p.provide(b); gate.resolve();
    const list = await pending, action = list.actions[0], frozen = action.edit.edits.map(edit => edit.versionId);
    a.changeDocument(own[1], 23, 'changed-after-publication'); const boundary = p.applyBoundary(); await boundary.apply(new m.CodeActionItem(action, a.provider));
    const state = { callbackDocument, callbackVersions, duringVersions, native: own[0].toString(), peerNative: p.nativeResources(b.host)[0].toString(), frozen,
      after: action.edit.edits.map(edit => edit.versionId), current: own.map(uri => a.documents.getDocument(uri).version),
      peerVersions: peerList.actions[0].edit.edits.map(edit => edit.versionId), resources: p.editUris(action), expected: p.logicalResources(a.host).map(uri => uri.toString()),
      bulkVersions: boundary.snapshot().bulk[0].versions, resolves: a.resolves.length };
    await p.disposeList(list); await p.disposeList(peerList);
    return { ...state, releases: a.releases.map(({ id, existed }) => ({ id, existed })), peerReleases: b.releases.map(({ id, existed }) => ({ id, existed })), caches: [...a.caches, ...b.caches] };
  });
  expect(result.callbackDocument).toEqual({ uri: result.native, version: 11 }); expect(result.callbackVersions).toEqual([11, 21]); expect(result.duringVersions).toEqual([11, 22]);
  expect(result.peerNative).toBe(result.native); expect(result.frozen).toEqual([11, 21]); expect(result.after).toEqual([11, 21]);
  expect(result.current).toEqual([11, 23]); expect(result.peerVersions).toEqual([91]); expect(result.resources).toEqual(result.expected); expect(result.bulkVersions).toEqual([11, 21]); expect(result.resolves).toBe(0);
  expect(result.releases).toEqual([{ id: 1, existed: true }]); expect(result.peerReleases).toEqual([{ id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('原ExtHost注册/CodeActionAdapter/WorkspaceEdit.from在provide调用前冻结双根版本，真实文档change与另一宿主同URI不重盖，原RPC/apply接收相同版本且cache单次释放');
});

test('无 edit action 经原 UI apply 触发 resolve，版本按 resolve 自身调用前冻结并保持原 action 身份', async () => {
  const result = await inBrowser('resolve原版本producer及独立冻结时点', async () => {
    const p = globalThis.actionProbe, m = p.m, entered = Promise.withResolvers(), gate = Promise.withResolvers(); let a, original, sameCandidate, callbackVersions;
    a = await p.connect(p.target('A'), 'producer-resolve', {
      provideCodeActions() { original = new m.CodeAction('lazy versioned', m.CodeActionKind.QuickFix); return [original]; },
      async resolveCodeAction(candidate) {
        sameCandidate = candidate === original; callbackVersions = p.nativeResources(a.host).map(uri => a.documents.getDocument(uri).version); const edit = new m.WorkspaceEdit();
        for (const uri of p.nativeResources(a.host)) edit.replace(uri, new m.ApiRange(0, 0, 0, 3), 'resolved');
        candidate.edit = edit; candidate.command = { title: 'resolved command', command: 'probe.producer-resolved' }; entered.resolve(); await gate.promise; return candidate;
      },
    });
    const own = p.nativeResources(a.host); a.addDocument(own[0], 31); a.addDocument(own[1], 41);
    const list = await p.provide(a), action = list.actions[0], absentBefore = action.edit === undefined;
    a.changeDocument(own[0], 32, 'changed-before-resolve'); const item = new m.CodeActionItem(action, a.provider), boundary = p.applyBoundary();
    const applying = boundary.apply(item); await Promise.race([entered.promise, applying.then(() => { throw new Error('未经过原CodeActionItem的resolve等待'); })]);
    a.changeDocument(own[1], 42, 'changed-during-resolve'); const duringVersions = own.map(uri => a.documents.getDocument(uri).version); gate.resolve(); await applying;
    const frozen = action.edit.edits.map(edit => edit.versionId); a.changeDocument(own[1], 43, 'changed-after-resolve');
    const state = { absentBefore, sameCandidate, identity: item.action === action, callbackVersions, duringVersions, frozen, after: action.edit.edits.map(edit => edit.versionId),
      current: own.map(uri => a.documents.getDocument(uri).version), bulkVersions: boundary.snapshot().bulk[0].versions, commands: boundary.commands,
      resolveIds: a.resolves.map(request => request.cacheId), resources: p.editUris(action), expected: p.logicalResources(a.host).map(uri => uri.toString()) };
    await p.disposeList(list); return { ...state, releases: a.releases.map(({ id, existed }) => ({ id, existed })), caches: [...a.caches] };
  });
  expect([result.absentBefore, result.sameCandidate, result.identity]).toEqual([true, true, true]); expect(result.callbackVersions).toEqual([32, 41]); expect(result.duringVersions).toEqual([32, 42]);
  expect(result.frozen).toEqual([32, 41]); expect(result.after).toEqual([32, 41]);
  expect(result.current).toEqual([32, 43]); expect(result.bulkVersions).toEqual([32, 41]); expect(result.commands).toEqual([{ id: 'probe.producer-resolved', args: [] }]); expect(result.resolveIds).toEqual([[1, 0]]);
  expect(result.resources).toEqual(result.expected); expect(result.releases).toEqual([{ id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('原apply/CodeActionItem对无edit action resolve，独立读取该调用前版本而非provide旧版本或返回时新版本，原action/命令与cache生命周期保留');
});

test('原 producer 保留 command-only、同批 create 的版本抑制及调用时未同步资源的 undefined 版本', async () => {
  const result = await inBrowser('producer无版本资源与原create规则', async () => {
    const p = globalThis.actionProbe, m = p.m, entered = Promise.withResolvers(), gate = Promise.withResolvers(); let a, unknown;
    a = await p.connect(p.target('A'), 'producer-boundaries', { async provideCodeActions() {
      const own = p.nativeResources(a.host), edit = new m.WorkspaceEdit(), direct = new m.CodeAction('mixed versions', m.CodeActionKind.QuickFix);
      edit.replace(own[0], new m.ApiRange(0, 0, 0, 3), 'known'); edit.createFile(own[1]); edit.replace(own[1], new m.ApiRange(0, 0, 0, 3), 'created');
      edit.replace(unknown, new m.ApiRange(0, 0, 0, 3), 'unknown'); direct.edit = edit;
      const commandOnly = new m.CodeAction('command only', m.CodeActionKind.QuickFix); commandOnly.command = { title: 'unchanged command', command: 'probe.producer-command' };
      entered.resolve(); await gate.promise; return [direct, commandOnly];
    } });
    const own = p.nativeResources(a.host); unknown = m.URI.joinPath(a.host.uriTransformer.folders[0].native, 'unknown.ts'); a.addDocument(own[0], 5); a.addDocument(own[1], 6);
    const pending = p.provide(a); await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入producer边界callback'); })]);
    a.addDocument(unknown, 80, 'synchronized-after-call'); gate.resolve(); const list = await pending;
    const state = { edits: list.actions[0].edit.edits.map(edit => ({ type: edit.textEdit ? 'text' : 'file', resource: (edit.resource ?? edit.newResource).toString(), version: edit.versionId ?? null })),
      expectedResources: [own[0], own[1], own[1], unknown].map(uri => m.URI.revive(a.host.uriTransformer.transformIncoming(uri)).toString()),
      unknownCurrent: a.documents.getDocument(unknown).version, command: list.actions[1].command, commandHasEdit: list.actions[1].edit !== undefined,
      ids: list.actions.map(action => action.cacheId), count: list.actions.length };
    await p.disposeList(list); return { ...state, releases: a.releases.map(({ id, existed }) => ({ id, existed })), caches: [...a.caches] };
  });
  expect(result.count).toBe(2); expect(result.edits.map(edit => edit.type)).toEqual(['text', 'file', 'text', 'text']); expect(result.edits.map(edit => edit.resource)).toEqual(result.expectedResources);
  expect(result.edits.map(edit => edit.version)).toEqual([5, null, null, null]); expect(result.unknownCurrent).toBe(80); expect(result.commandHasEdit).toBe(false);
  expect(result.command).toMatchObject({ id: 'probe.producer-command', title: 'unchanged command' }); expect(result.ids).toEqual([[1, 0], [1, 1]]);
  expect(result.releases).toEqual([{ id: 1, existed: true }]); expect(result.caches).toEqual([]);
  checks.push('真实producer不造command-only编辑，WorkspaceEdit.from保持create+text版本抑制，调用后才同步资源仍无版本，整list cache准确释放');
});

test('原 Controller 将 BulkEdit 拒绝交给同一错误通知，等待结束后重新触发并结束进度，core 仍拒绝且不执行 command', async () => {
  const result = await inBrowser('Controller原应用错误呈现与finally边界', async () => {
    const p = globalThis.actionProbe, m = p.m, boundary = p.applyBoundary();
    const cause = new Error('revision 21 != 22'), conflict = new Error('same.ts has changed in the meantime', { cause });
    const item = new m.CodeActionItem({ title: 'version conflict', edit: { edits: [p.textEdit(m.URI.file('/ui/same.ts'))] },
      command: { id: 'probe.must-not-run', title: '后继命令' } }, undefined);
    boundary.onBulk = async () => { throw conflict; };
    const coreRejection = await boundary.apply(item).then(() => ({ sameError: false }), error => ({ sameError: error === conflict, sameCause: error.cause === cause }));
    const entered = Promise.withResolvers(), gate = Promise.withResolvers(), order = [], progress = [], notifications = [], triggers = [], editor = {};
    boundary.onBulk = async () => { order.push('bulk'); entered.resolve(); await gate.promise; throw conflict; };
    // 只借用原 Controller 的应用和触发方法；模型、进度与通知是明确的 UI 接收端，不复制控制流程。
    const controller = { _editor: editor, _instantiationService: boundary.instantiationService,
      _progressService: { show(infinite, delay) { progress.push({ infinite, delay }); order.push('show'); return { done() { order.push('done'); } }; } },
      _notificationService: { error(error) { notifications.push(error); order.push('notification'); } },
      _model: { trigger(trigger) { triggers.push(trigger); order.push('trigger'); } }, _trigger: m.CodeActionController.prototype._trigger };
    const applying = m.CodeActionController.prototype.applyCodeAction.call(controller, item, true, true, m.ApplyCodeActionReason.FromCodeActions);
    await Promise.race([entered.promise, applying.then(() => { throw new Error('原 Controller 未等待 BulkEdit'); })]);
    const waiting = { order: [...order], notifications: notifications.length, triggers: triggers.length, commands: [...boundary.commands] };
    gate.resolve(); await applying;
    return { coreRejection, waiting, order, progress, triggers, notificationCount: notifications.length,
      sameError: notifications[0] === conflict, sameCause: notifications[0]?.cause === cause, message: notifications[0]?.message,
      editorPassed: boundary.bulk[1].options.editor === editor, preview: boundary.bulk[1].options.showPreview,
      commands: boundary.commands, effects: boundary.effects, coreNotifications: boundary.notifications, bulkCount: boundary.bulk.length };
  });
  expect(result.coreRejection).toEqual({ sameError: true, sameCause: true });
  expect(result.waiting).toEqual({ order: ['show', 'bulk'], notifications: 0, triggers: 0, commands: [] });
  expect(result.order).toEqual(['show', 'bulk', 'notification', 'trigger', 'done']); expect(result.progress).toEqual([{ infinite: true, delay: 500 }]);
  expect(result.triggers).toEqual([{ type: 2, triggerAction: 'quick fix action', filter: {} }]);
  expect(result.notificationCount).toBe(1); expect([result.sameError, result.sameCause, result.editorPassed, result.preview]).toEqual([true, true, true, true]);
  expect(result.message).toBe('same.ts has changed in the meantime'); expect(result.bulkCount).toBe(2);
  expect(result.commands).toEqual([]); expect(result.effects).toEqual([]); expect(result.coreNotifications).toEqual([]);
  checks.push('原Controller/DI/core apply实际等待BulkEdit拒绝，原Error及cause只交UI通知一次，retrigger和progress.done按序完成，core继续拒绝且零command/副作用');
});
