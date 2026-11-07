import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

// TODO: 合并进同一 Bun 进程前，先排清已复现的 HTTP 监听句柄失效；当前使用独立矩阵套件。
const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_language_features.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'language-features.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const built = await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { ExtHostLanguageFeatures } from '${upstream}workbench/api/common/extHostLanguageFeatures.js';
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { WorkspaceEdit, Range as ApiRange } from '${upstream}workbench/api/common/extHostTypes.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { rename, rawRename, prepareRename } from '${upstream}editor/contrib/rename/browser/rename.js';
export { LanguageFeaturesService } from '${upstream}editor/common/services/languageFeaturesService.js';
export { ILanguageFeaturesService } from '${upstream}editor/common/services/languageFeatures.js';
export { CallHierarchyProviderRegistry } from '${upstream}workbench/contrib/callHierarchy/common/callHierarchy.js';
export { TypeHierarchyProviderRegistry } from '${upstream}workbench/contrib/typeHierarchy/common/typeHierarchy.js';
export { getOccurrencesAcrossMultipleModels } from '${upstream}editor/contrib/wordHighlighter/browser/wordHighlighter.js';
export { InstantiationService } from '${upstream}platform/instantiation/common/instantiationService.js';
export { ServiceCollection } from '${upstream}platform/instantiation/common/serviceCollection.js';
export { IAiEditTelemetryService } from '${upstream}workbench/contrib/editTelemetry/browser/telemetry/aiEditTelemetry/aiEditTelemetryService.js';
export { ExtensionIdentifier } from '${upstream}platform/extensions/common/extensions.js';
export { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { CancellationError, isCancellationError } from '${upstream}base/common/errors.js';
export { Position } from '${upstream}editor/common/core/position.js';
export { Emitter } from '${upstream}base/common/event.js';
export { isWindows, isLinux } from '${upstream}base/common/platform.js';
export { URI } from '${upstream}base/common/uri.js';
export { TargetUriTransformer } from './src/clients/workbench/src/extensions/targetUris.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const sourcePaths = [
  'tests/integration/workspace_services/extension_host/extension_language_features.test.mjs',
  'src/clients/workbench/scripts/sourceTransform.mjs',
  'src/clients/workbench/scripts/extensions/extensionLanguageFeaturesTransform.mjs',
  'src/clients/workbench/src/extensions/languageFeatures.ts',
  'src/clients/workbench/src/extensions/targetUris.ts',
  'src/clients/workbench/upstream/src/vs/editor/common/languageFeatureRegistry.ts',
  'src/clients/workbench/upstream/src/vs/editor/common/languageSelector.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/common/extHostLanguageFeatures.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/common/extHostTypeConverters.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/common/extHostDocuments.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/common/extHostDocumentsAndEditors.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/common/extHostDocumentData.ts',
  'src/clients/workbench/upstream/src/vs/workbench/api/browser/mainThreadBulkEdits.ts',
  'src/clients/workbench/upstream/src/vs/editor/contrib/rename/browser/rename.ts',
  'src/clients/workbench/upstream/src/vs/editor/contrib/wordHighlighter/browser/wordHighlighter.ts',
];
const fingerprints = Object.fromEntries(await Promise.all(sourcePaths.map(async path => [path,
  createHash('sha256').update(await readFile(path)).digest('hex')])));
const checks = [], observations = [], browserEvents = [];
const linuxUserAgent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';
const windowsUserAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36';
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs'
      ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>语言 Provider 组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('语言组件HTTP来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('语言组件Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks,
      boundary: '真实 Chromium 中的原 MainThreadLanguageFeatures、LanguageFeaturesService/共享 registry、DI、wordHighlighter 多文档入口和 RPCProtocol/TargetUriTransformer。Rename版本及prepare合同另经原ExtHostLanguageFeatures/RenameAdapter、WorkspaceEdit.from、ExtHostDocuments/AndEditors/DocumentData与rename/rawRename/prepareRename；文档delta/change输入和扩展callback为具名边界，版本不由装具填入DTO。DTO整批准入及迟到回包场景的扩展actor刻意忽略取消；认证binding、内存字节传输、canonical URI接收端及ITextModel的URI/语言/同步状态和word fallback为具名装具。没有BulkEdit装具或磁盘写入，也未运行Gateway、真实Node、完整RenameController/UI点击、已返回edit后的UI/inline应用生命周期、Windows实机或Notebook cell/版本producer。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
  }
});

async function inBrowser(name, body, contextOptions = {}, argument) {
  const context = await browser.newContext(contextOptions), page = await context.newPage(), pageErrors = [];
  page.on('pageerror', error => { pageErrors.push(error.message); browserEvents.push({ name, kind: 'pageerror', message: error.message }); });
  page.on('console', message => { if (message.type() === 'error') browserEvents.push({ name, kind: 'console', message: message.text() }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetimes = [], tick = () => new Promise(resolve => setTimeout(resolve, 0));
      // 仅提供模型边界；评分、候选缓存、独占排序、注册和注销均运行原实现。
      const model = (uri, language = 'typescript', synchronized = true) => ({
        uri, getLanguageId: () => language, isTooLargeForSyncing: () => !synchronized, isForSimpleWidget: false,
      });
      function target(label, { os = 'linux', roots = [`file:///targets/${label}/root`, `file:///targets/${label}/other`] } = {}) {
        const binding = { gateway_id: `gateway-${label}`, backend_id: `backend-${label}`, view_id: `view-${label}`,
          initialization: { os, logs_location_uri: 'file:///hosts/logs',
            environment: { app_root_uri: 'file:///hosts/app', global_storage_home_uri: 'file:///hosts/global',
              workspace_storage_home_uri: 'file:///hosts/storage', profile_id: 'default',
              remote_settings: { native_uri: 'file:///hosts/profile/settings.json', resource_uri: `boxteam://gateway-${label}/profile/default/settings` } },
            workspace: { id: `view-${label}`, folders: roots.map((native_uri, index) => ({ name: `root-${index}`, workspace_id: `${label.toLowerCase()}-${index}`,
              resource_uri: `boxteam://workspace/${label.toLowerCase()}-${index}/resources/files/root`, native_uri })) } } };
        return { binding, targetContext: { kind: 'node', connection: binding }, uriTransformer: new m.TargetUriTransformer(binding, () => []), runningLocation: { kind: 3 } };
      }
      const document = (host, path = 'src/main.ts', index = 0, language = 'typescript', synchronized = true) =>
        model(m.URI.joinPath(host.uriTransformer.folders[index].uri, path), language, synchronized);
      const selector = fields => [{ $serialized: true, language: 'typescript', ...fields }];
      function owner(host, service, getProxy = () => ({ $supportsAutomaticNewSymbolNamesTriggerKind: async () => false })) {
        // 本片不执行遥测，也不装配语言配置服务；inline Provider 仍由真实 DI 创建并注册。
        const di = new m.InstantiationService(new m.ServiceCollection([m.ILanguageFeaturesService, service], [m.IAiEditTelemetryService, {}]), true);
        const canonical = [], main = new m.MainThreadLanguageFeatures({ extensionHost: host, getProxy }, undefined, undefined, service,
          { asCanonicalUri: uri => { canonical.push(uri.toString()); return uri; } }, di, undefined);
        const close = () => { main.dispose(); di.dispose(); };
        lifetimes.push(close);
        return { main, close, canonical };
      }
      function connect(host, service) {
        // 只替换字节输送和扩展代码；原 RPC 负责序列化、typed URI、取消与关闭结算。
        const toMain = new m.Emitter(), toHost = new m.Emitter(), requests = [], wire = [];
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => {
          wire.push({ direction: 'host', size: data.byteLength }); queueMicrotask(() => toHost.fire(data));
        }, drain: async () => {} }, null, host.uriTransformer);
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => {
          wire.push({ direction: 'main', size: data.byteLength }); queueMicrotask(() => toMain.fire(data));
        }, drain: async () => {} });
        const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 };
        let extHost, documents, documentOwner, registration;
        const client = { host, requests, wire, onHover: async () => ({ contents: [{ value: 'hover' }], range }),
          onRename: async request => ({ edits: [{ resource: request.resource, versionId: 7, textEdit: { range, text: request.newName } }] }),
          onPrepare: async () => ({ range, text: 'old' }) };
        const actor = {
          $provideHover(handle, resource, position, hoverContext, token) {
            const request = { kind: 'hover', handle, resource: m.URI.revive(resource), position, hoverContext, token };
            requests.push(request); return client.onHover(request);
          },
          async $provideDefinition(handle, resource, position, token) {
            requests.push({ kind: 'definition', handle, resource: m.URI.revive(resource), position, token });
            return [{ uri: m.URI.revive(resource), range, originSelectionRange: range, targetSelectionRange: range }];
          },
          async $provideMultiDocumentHighlights(handle, resource, position, others, token) {
            const resources = others.map(uri => m.URI.revive(uri));
            requests.push({ kind: 'multi', handle, resource: m.URI.revive(resource), resources, position, token });
            return resources.map(uri => ({ uri, highlights: [{ range, kind: 1 }] }));
          },
          $provideRenameEdits(handle, resource, position, newName, token) {
            const request = { kind: 'rename', handle, resource: m.URI.revive(resource), position, newName, token }; requests.push(request);
            return extHost ? extHost.$provideRenameEdits(handle, resource, position, newName, token) : client.onRename(request);
          },
          $resolveRenameLocation(handle, resource, position, token) {
            const request = { kind: 'prepare', handle, resource: m.URI.revive(resource), position, token }; requests.push(request);
            return extHost ? extHost.$resolveRenameLocation(handle, resource, position, token) : client.onPrepare(request);
          },
          async $supportsAutomaticNewSymbolNamesTriggerKind() { return false; },
        };
        const original = owner(host, service, mainRpc.getProxy.bind(mainRpc));
        mainRpc.set(m.MainContext.MainThreadLanguageFeatures, original.main);
        hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, actor);
        const close = async () => {
          original.close();
          if (registration) { registration.dispose(); await tick(); }
          if (documents) { documentOwner.acceptDocumentsAndEditorsDelta({ removedDocuments: documents.getAllDocumentData().map(data => data.document.uri) }); documents.dispose(); extHost.dispose(); }
          mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose();
        };
        lifetimes.push(close);
        return Object.assign(client, { main: original.main, canonical: original.canonical, remote: hostRpc.getProxy(m.MainContext.MainThreadLanguageFeatures), close,
          async registerRename(provider) {
            const context = { getProxy: hostRpc.getProxy.bind(hostRpc) }, log = new m.NullLogService();
            documentOwner = new m.ExtHostDocumentsAndEditors(context, log); documents = new m.ExtHostDocuments(context, documentOwner);
            extHost = new m.ExtHostLanguageFeatures(context, undefined, documents, undefined, undefined, log, undefined,
              { onExtensionError(_extension, error) { throw error; } });
            const before = new Set(service.renameProvider.allNoModel());
            registration = extHost.registerRenameProvider({ identifier: { value: 'probe.rename' }, name: 'rename', enabledApiProposals: [] }, ['typescript'], provider);
            await tick(); client.documents = documents; client.renameProvider = registered(service.renameProvider, before); return client.renameProvider;
          },
          addDocument(uri, version, text = 'old source') {
            documentOwner.acceptDocumentsAndEditorsDelta({ addedDocuments: [{ uri, versionId: version, lines: text.split('\n'), EOL: '\n', languageId: 'typescript', isDirty: false, encoding: 'utf8' }] });
          },
          changeDocument(uri, version, text) {
            const document = documents.getDocument(uri), lastLine = document.lineCount;
            documents.$acceptModelChanged(uri, { versionId: version, eol: '\n', isUndoing: false, isRedoing: false, isFlush: false, isEolChange: false, detailedReason: undefined,
              changes: [{ range: { startLineNumber: 1, startColumn: 1, endLineNumber: lastLine, endColumn: document.lineAt(lastLine - 1).text.length + 1 }, rangeOffset: 0, rangeLength: document.getText().length, text }] }, true);
          },
        });
      }
      const extension = new m.ExtensionIdentifier('probe.language'), legend = { tokenTypes: ['class'], tokenModifiers: [] };
      const service = new m.LanguageFeaturesService();
      const specs = [
        ['documentSymbolProvider', '$registerDocumentSymbolProvider', ['symbols']],
        ['codeLensProvider', '$registerCodeLensSupport', [undefined]],
        ['definitionProvider', '$registerDefinitionSupport', []],
        ['declarationProvider', '$registerDeclarationSupport', []],
        ['implementationProvider', '$registerImplementationSupport', []],
        ['typeDefinitionProvider', '$registerTypeDefinitionSupport', []],
        ['hoverProvider', '$registerHoverProvider', []],
        ['evaluatableExpressionProvider', '$registerEvaluatableExpressionProvider', []],
        ['inlineValuesProvider', '$registerInlineValuesProvider', [undefined]],
        ['documentHighlightProvider', '$registerDocumentHighlightProvider', []],
        ['multiDocumentHighlightProvider', '$registerMultiDocumentHighlightProvider', []],
        ['linkedEditingRangeProvider', '$registerLinkedEditingRangeProvider', []],
        ['referenceProvider', '$registerReferenceSupport', []],
        ['codeActionProvider', '$registerCodeActionSupport', [{}, 'actions', 'probe.language', false]],
        ['documentPasteEditProvider', '$registerPasteEditProvider', [{}]],
        ['documentFormattingEditProvider', '$registerDocumentFormattingSupport', [extension, 'format']],
        ['documentRangeFormattingEditProvider', '$registerRangeFormattingSupport', [extension, 'range', false]],
        ['onTypeFormattingEditProvider', '$registerOnTypeFormattingSupport', [[';'], extension]],
        ['renameProvider', '$registerRenameSupport', [false]],
        ['newSymbolNamesProvider', '$registerNewSymbolNamesProvider', []],
        ['documentSemanticTokensProvider', '$registerDocumentSemanticTokensProvider', [legend, undefined]],
        ['documentRangeSemanticTokensProvider', '$registerDocumentRangeSemanticTokensProvider', [legend, undefined]],
        ['completionProvider', '$registerCompletionsProvider', [[], false, extension]],
        ['inlineCompletionsProvider', '$registerInlineCompletionsSupport', [false, 'probe.language', '1.0.0', undefined, [], undefined, undefined, [], false, false, undefined, false, false, undefined, false]],
        ['signatureHelpProvider', '$registerSignatureHelpProvider', [{}]],
        ['inlayHintsProvider', '$registerInlayHintsProvider', [false, undefined, 'hints']],
        ['linkProvider', '$registerDocumentLinkProvider', [false]],
        ['colorProvider', '$registerDocumentColorProvider', []],
        ['foldingRangeProvider', '$registerFoldingRangeProvider', [extension, undefined]],
        ['selectionRangeProvider', '$registerSelectionRangeProvider', []],
        ['callHierarchy', '$registerCallHierarchyProvider', []],
        ['typeHierarchy', '$registerTypeHierarchyProvider', []],
        ['documentDropEditProvider', '$registerDocumentOnDropEditProvider', [{}]],
      ].map(([name, method, args]) => ({ name, method, args, registry: name === 'callHierarchy' ? m.CallHierarchyProviderRegistry
        : name === 'typeHierarchy' ? m.TypeHierarchyProviderRegistry : service[name] }));
      function registered(registry, before) {
        const added = registry.allNoModel().filter(provider => !before.has(provider));
        if (added.length !== 1) throw new Error(`原注册入口应新增一个 Provider，实际 ${added.length}`);
        return added[0];
      }
      function register(main, handle, spec, filters) {
        const before = new Set(spec.registry.allNoModel());
        main[spec.method](handle, filters, ...spec.args);
        return registered(spec.registry, before);
      }
      async function rpcRegister(client, handle, method, filters, registry, ...args) {
        const before = new Set(registry.allNoModel());
        await client.remote[method](handle, filters, ...args);
        return registered(registry, before);
      }
      globalThis.languageProbe = { m, model, target, document, selector, owner, connect, service, specs, register, rpcRegister, tick,
        async dispose() { for (const close of lifetimes.reverse()) await close(); } };
    });
    const result = await page.evaluate(body, argument);
    expect(pageErrors).toEqual([]);
    observations.push({ name, result });
    return result;
  } finally {
    try { await page.evaluate(() => globalThis.languageProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('原 MainThread 的全部文档 selector 注册进入共享 registry，A/B 同语言同名文档及多根仅选择自身', async () => {
  const result = await inBrowser('33 类真实 MainThread 注册、共享 registry、多根归属、注销及候选缓存', () => {
    const p = globalThis.languageProbe, a = p.target('A'), b = p.target('B');
    const first = p.owner(a, p.service), second = p.owner(b, p.service);
    const a1 = p.document(a), a2 = p.document(a, 'src/main.ts', 1), b1 = p.document(b);
    const outside = p.model(p.m.URI.parse('untitled:main.ts'));
    return p.specs.map((spec, index) => {
      const handle = index + 1, ownA = p.register(first.main, handle, spec, p.selector({}));
      const ownB = p.register(second.main, handle, spec, p.selector({}));
      const identity = new Map([[ownA, 'A'], [ownB, 'B']]);
      const read = model => spec.registry.ordered(model).map(provider => identity.get(provider) ?? 'unexpected');
      const selected = [read(a1), read(b1), read(a2), read(b1), read(a1), read(outside)];
      const all = spec.registry.all(a1).map(provider => identity.get(provider));
      const groups = spec.registry.orderedGroups(b1).map(values => values.map(provider => identity.get(provider)));
      const secondary = [spec.registry.score(ownA, a2), spec.registry.score(ownA, b1), spec.registry.score(ownB, a1)];
      first.main.$unregister(handle);
      const after = [read(a1), read(b1), spec.registry.score(ownA, a1)];
      second.main.$unregister(handle);
      return { name: spec.name, selected, all, groups, secondary, after, remaining: spec.registry.allNoModel().length };
    });
  });
  expect(result).toHaveLength(33);
  for (const entry of result) expect(entry).toEqual({ name: entry.name,
    selected: [['A'], ['B'], ['A'], ['B'], ['A'], []], all: ['A'], groups: [['B']], secondary: [10, 0, 0],
    after: [[], ['B'], 0], remaining: 0 });
  checks.push('33 类原 MainThread 注册、共享 registry、多根归属、注销及候选缓存');
});

test('selector 经真实 RPC 保留 file scheme、绝对 glob、typed baseUri 与原生 base 的 RelativePattern 语义', async () => {
  const result = await inBrowser('原 RPC typed RelativePattern、file scheme、绝对 glob 及正反例', async () => {
    const p = globalThis.languageProbe, host = p.target('A'), foreign = p.target('B'), client = p.connect(host, p.service);
    const registry = p.service.hoverProvider;
    const docs = [p.document(host), p.document(host, 'src/main.ts', 1), p.document(foreign), p.document(host, 'src/main.js'),
      p.document(host, 'src/main.ts', 0, 'javascript'), p.model(p.m.URI.file('/targets/A/root/src/main.ts'))];
    const root = host.uriTransformer.folders[0].native;
    const cases = [
      ['file', p.selector({ scheme: 'file' }), [true, true, false, true, false, false]],
      ['absolute', p.selector({ scheme: 'file', pattern: root.fsPath + '/src/**/*.ts' }), [true, false, false, false, false, false]],
      ['typed-relative', p.selector({ scheme: 'file', pattern: { base: root.fsPath, baseUri: root, pattern: 'src/**/*.ts' } }), [true, false, false, false, false, false]],
      ['native-relative', p.selector({ pattern: { base: root.fsPath, pattern: 'src/**/*.ts' } }), [true, false, false, false, false, false]],
      ['wrong-scheme', p.selector({ scheme: 'untitled' }), [false, false, false, false, false, false]],
      ['wrong-base', p.selector({ pattern: { base: root.fsPath + '/other', pattern: '**/*.ts' } }), [false, false, false, false, false, false]],
      ['array', [...p.selector({ language: 'javascript', scheme: 'file', pattern: root.fsPath + '/src/*.js' }),
        ...p.selector({ scheme: 'file', pattern: root.fsPath + '/src/*.ts' })], [true, false, false, false, false, false]],
    ];
    const results = [];
    for (const [name, filters, expected] of cases) {
      const before = JSON.stringify(filters), provider = await p.rpcRegister(client, 1, '$registerHoverProvider', filters, registry);
      const selected = docs.map(model => registry.ordered(model).includes(provider));
      await client.remote.$unregister(1);
      results.push({ name, selected, expected, unmodified: JSON.stringify(filters) === before, remaining: registry.allNoModel().length });
    }
    return results;
  });
  for (const entry of result) {
    expect(entry.selected).toEqual(entry.expected);
    expect(entry.unmodified).toBe(true); expect(entry.remaining).toBe(0);
  }
  checks.push('原 RPC typed RelativePattern、file scheme、绝对 glob 及正反例');
});

test('foreign exclusive 不压制 own/Web，交替候选和注册注销后仍复用各原 Entry 的评分', async () => {
  const result = await inBrowser('exclusive 在归属评分后生效；交替 A/B、递归、注册注销与 Web 共存', () => {
    const p = globalThis.languageProbe, a = p.target('A'), b = p.target('B'), registry = p.service.hoverProvider;
    const first = p.owner(a, p.service), second = p.owner(b, p.service), web = p.owner({ runningLocation: { kind: 2 } }, p.service);
    const spec = p.specs.find(value => value.name === 'hoverProvider'), aDoc = p.document(a), bDoc = p.document(b);
    const ownA = p.register(first.main, 1, spec, p.selector({})), ownWeb = p.register(web.main, 1, spec, p.selector({}));
    const exclusiveB = p.register(second.main, 1, spec, p.selector({ exclusive: true }));
    const ids = new Map([[ownA, 'A'], [ownWeb, 'Web'], [exclusiveB, 'B-exclusive']]);
    const read = (model, recursive = false) => registry.ordered(model, recursive).map(provider => ids.get(provider) ?? 'unexpected');
    const initial = [read(aDoc), read(bDoc), read(aDoc), read(bDoc, true), read(bDoc), read(aDoc)];
    const moreA = p.register(first.main, 2, spec, p.selector({})); ids.set(moreA, 'A2');
    const added = [read(aDoc), read(bDoc)];
    first.main.$unregister(2);
    const removed = [read(aDoc), read(bDoc)];
    second.main.$unregister(1);
    const noB = [read(bDoc), read(aDoc)];
    const nextB = p.register(second.main, 1, spec, p.selector({})); ids.set(nextB, 'B-next');
    const replacement = [read(aDoc), read(bDoc), read(aDoc), read(bDoc)];
    first.close();
    return { initial, added, removed, noB, replacement, afterClose: [read(aDoc), read(bDoc)],
      languages: [...registry.registeredLanguageIds], exclusiveSecondary: registry.score(exclusiveB, bDoc) };
  });
  expect(result).toEqual({ initial: [['Web', 'A'], ['B-exclusive'], ['Web', 'A'], ['Web'], ['B-exclusive'], ['Web', 'A']],
    added: [['A2', 'Web', 'A'], ['B-exclusive']], removed: [['Web', 'A'], ['B-exclusive']], noB: [['Web'], ['Web', 'A']],
    replacement: [['Web', 'A'], ['B-next', 'Web'], ['Web', 'A'], ['B-next', 'Web']], afterClose: [['Web'], ['B-next', 'Web']],
    languages: ['typescript'], exclusiveSecondary: 0 });
  checks.push('exclusive 在归属评分后生效；交替 A/B、递归、注册注销与 Web 共存');
});

test('Web MainThread 保留原 selector 匹配、同步限制和未修改的 RelativePattern', async () => {
  const result = await inBrowser('Web selector 原匹配与直接原 registry 注册一致，原输入不被目标投影改写', () => {
    const p = globalThis.languageProbe, host = p.target('A'), registry = p.service.hoverProvider;
    const web = p.owner({ runningLocation: { kind: 2 } }, p.service), spec = p.specs.find(value => value.name === 'hoverProvider');
    const root = host.uriTransformer.folders[0].uri, native = host.uriTransformer.folders[0].native;
    const docs = [p.document(host), p.model(p.m.URI.joinPath(native, 'src/main.ts')), p.model(p.m.URI.parse('untitled:main.ts')),
      // 同一 URI 的真实模型不能同时具有相反的同步状态；各负例使用独立模型身份。
      p.document(host, 'src/javascript.ts', 0, 'javascript'), p.document(host, 'src/large.ts', 0, 'typescript', false)];
    const cases = [
      [p.selector({}), [true, true, true, false, false]],
      [p.selector({ scheme: 'file' }), [false, true, false, false, false]],
      [p.selector({ scheme: 'boxteam-file', pattern: '**/src/*.ts' }), [true, false, false, false, false]],
      [p.selector({ pattern: { base: root.fsPath, baseUri: root, pattern: 'src/*.ts' } }), [true, false, false, false, false]],
    ];
    return cases.map(([filters, expected]) => {
      const before = JSON.stringify(filters), direct = {}, registration = registry.register(filters, direct);
      try {
        const provider = p.register(web.main, 1, spec, filters);
        const selected = docs.map(model => registry.ordered(model).includes(provider));
        const original = docs.map(model => registry.ordered(model).includes(direct));
        web.main.$unregister(1);
        return { selected, original, expected, unmodified: JSON.stringify(filters) === before };
      } finally { registration.dispose(); }
    });
  });
  for (const entry of result) {
    expect(entry.selected).toEqual(entry.expected); expect(entry.original).toEqual(entry.expected); expect(entry.unmodified).toBe(true);
  }
  checks.push('Web selector 原匹配与直接原 registry 注册一致，原输入不被目标投影改写');
});

test('Windows 目标在 Linux Chromium 中按目标大小写和原生路径评分，POSIX 保持大小写敏感', async () => {
  const result = await inBrowser('Windows 目标评分逻辑与 POSIX 对照；非 Windows 实机验收', async () => {
    const p = globalThis.languageProbe, win = p.target('WIN', { os: 'windows', roots: ['file:///C:/Project', 'file://Server/Share/Project'] });
    const linux = p.target('LINUX', { roots: ['file:///targets/Project'] }), registry = p.service.hoverProvider;
    const windows = p.connect(win, p.service), posix = p.connect(linux, p.service), results = [];
    const winDoc = p.document(win, 'Src/Main.TS'), uncDoc = p.document(win, 'Src/Main.TS', 1), linuxDoc = p.document(linux, 'Src/Main.TS');
    const cases = [
      ['absolute', windows, p.selector({ scheme: 'file', pattern: 'C:\\PROJECT\\SRC\\**\\*.ts' }), winDoc, true],
      ['drive-exact', windows, p.selector({ scheme: 'file', pattern: 'c:/PROJECT/SRC/MAIN.ts' }), winDoc, true],
      ['typed-relative', windows, p.selector({ scheme: 'file', pattern: { base: 'C:\\PROJECT', baseUri: p.m.URI.parse('file:///c:/PROJECT'), pattern: 'src/**/*.ts' } }), winDoc, true],
      ['native-relative', windows, p.selector({ pattern: { base: 'C:\\PROJECT', pattern: 'src/**/*.ts' } }), winDoc, true],
      ['unc-absolute', windows, p.selector({ scheme: 'file', pattern: '//SERVER/SHARE/PROJECT/SRC/**/*.ts' }), uncDoc, true],
      ['unc-typed-relative', windows, p.selector({ scheme: 'file', pattern: { base: '//SERVER/SHARE/PROJECT', baseUri: p.m.URI.parse('file://SERVER/SHARE/PROJECT'), pattern: 'src/**/*.ts' } }), uncDoc, true],
      ['unc-native-relative', windows, p.selector({ pattern: { base: '//SERVER/SHARE/PROJECT', pattern: 'src/**/*.ts' } }), uncDoc, true],
      ['posix-case', posix, p.selector({ scheme: 'file', pattern: '/targets/project/src/**/*.ts' }), linuxDoc, false],
      ['posix-exact', posix, p.selector({ scheme: 'file', pattern: '/targets/Project/Src/**/*.TS' }), linuxDoc, true],
    ];
    for (const [name, client, filters, doc, expected] of cases) {
      const provider = await p.rpcRegister(client, 1, '$registerHoverProvider', filters, registry);
      results.push({ name, matched: registry.ordered(doc).includes(provider), foreign: registry.ordered(client === windows ? linuxDoc : winDoc).includes(provider), expected });
      await client.remote.$unregister(1);
    }
    return { renderer: { windows: p.m.isWindows, linux: p.m.isLinux }, cases: results };
  }, { userAgent: linuxUserAgent });
  expect(result.renderer).toEqual({ windows: false, linux: true });
  for (const entry of result.cases) { expect(entry.matched).toBe(entry.expected); expect(entry.foreign).toBe(false); }
  checks.push('Linux renderer 上 Windows 目标的大小写、盘符、UNC 和 POSIX 对照；非 Windows 实机');
});

test('模拟 Windows renderer 时 POSIX 目标仍按自身路径和大小写匹配 exact 与 nested RelativePattern', async () => {
  const result = await inBrowser('Windows userAgent renderer 的 POSIX 目标 exact/nested/RelativePattern 与大小写边界', async () => {
    const p = globalThis.languageProbe, host = p.target('POSIX', { roots: ['file:///srv/Project'] }), client = p.connect(host, p.service);
    const registry = p.service.hoverProvider, doc = p.document(host, 'src/nested/main.ts');
    const sibling = p.document(host, 'src-other/main.ts'), differentlyCased = p.document(host, 'src/NESTED/main.ts');
    const cases = [
      ['exact', p.selector({ scheme: 'file', pattern: '/srv/Project/src/nested/main.ts' }), [true, false, false]],
      ['nested-glob', p.selector({ pattern: '/srv/Project/src/**/*.ts' }), [true, false, true]],
      ['typed-relative', p.selector({ pattern: { base: '/srv/Project/src', baseUri: p.m.URI.parse('file:///srv/Project/src'), pattern: 'nested/**/*.ts' } }), [true, false, false]],
      ['native-relative', p.selector({ pattern: { base: '/srv/Project/src', pattern: 'nested/**/*.ts' } }), [true, false, false]],
      ['nested-base', p.selector({ pattern: { base: '/srv/Project/src/nested', baseUri: p.m.URI.parse('file:///srv/Project/src/nested'), pattern: '*.ts' } }), [true, false, false]],
      ['wrong-absolute-case', p.selector({ pattern: '/srv/project/src/**/*.ts' }), [false, false, false]],
      ['wrong-base-case', p.selector({ pattern: { base: '/srv/project/src', pattern: '**/*.ts' } }), [false, false, false]],
      ['wrong-relative-case', p.selector({ pattern: { base: '/srv/Project/src', pattern: 'NESTED/**/*.ts' } }), [false, false, true]],
    ];
    const results = [];
    for (const [name, filters, expected] of cases) {
      const provider = await p.rpcRegister(client, 1, '$registerHoverProvider', filters, registry);
      results.push({ name, selected: [doc, sibling, differentlyCased].map(model => registry.ordered(model).includes(provider)), expected });
      await client.remote.$unregister(1);
    }
    return { renderer: { windows: p.m.isWindows, linux: p.m.isLinux }, cases: results };
  }, { userAgent: windowsUserAgent });
  expect(result.renderer).toEqual({ windows: true, linux: false });
  for (const entry of result.cases) expect(entry.selected).toEqual(entry.expected);
  checks.push('Windows userAgent renderer 上 POSIX exact/nested/RelativePattern 与大小写；非 Windows 实机');
});

test('原 MainThread 与 wordHighlighter 多文档入口在 RPC 前过滤 secondary 文档，Web 保持全窗口匹配', async () => {
  const result = await inBrowser('真实 wordHighlighter secondary 评分复用注册项，Node 只发送 A2，Web 发送 A2/B1', async () => {
    const p = globalThis.languageProbe, a = p.target('A'), b = p.target('B'), registry = p.service.multiDocumentHighlightProvider;
    const client = p.connect(a, p.service), a1 = p.document(a), a2 = p.document(a, 'src/main.ts', 1), b1 = p.document(b);
    const large = p.document(a, 'src/large.ts', 0, 'typescript', false), position = new p.m.Position(1, 1);
    const provider = await p.rpcRegister(client, 1, '$registerMultiDocumentHighlightProvider', p.selector({}), registry);
    const scoreBefore = [registry.score(provider, a2), registry.score(provider, b1), registry.score(provider, large)];
    const node = await p.m.getOccurrencesAcrossMultipleModels(registry, a1, position, p.m.CancellationToken.None, [a2, b1, large]);
    const request = client.requests.find(value => value.kind === 'multi');
    await client.remote.$unregister(1);
    const afterUnregister = registry.score(provider, a2);
    const web = p.connect({ runningLocation: { kind: 2 } }, p.service);
    await p.rpcRegister(web, 1, '$registerMultiDocumentHighlightProvider', p.selector({}), registry);
    const all = await p.m.getOccurrencesAcrossMultipleModels(registry, a1, position, p.m.CancellationToken.None, [a2, b1, large]);
    const webRequest = web.requests.find(value => value.kind === 'multi');
    return { scoreBefore, afterUnregister, node: [...node.keys()].map(uri => uri.toString()),
      nodePrimary: request.resource.toString(), nodeOthers: request.resources.map(uri => uri.toString()),
      web: [...all.keys()].map(uri => uri.toString()), webPrimary: webRequest.resource.toString(), webOthers: webRequest.resources.map(uri => uri.toString()),
      expected: { a1: a1.uri.toString(), a2: a2.uri.toString(), b1: b1.uri.toString(),
        nativeA1: a.uriTransformer.transformOutgoingURI(a1.uri).toString(), nativeA2: a.uriTransformer.transformOutgoingURI(a2.uri).toString() } };
  });
  expect(result.scoreBefore).toEqual([10, 0, 0]); expect(result.afterUnregister).toBe(0);
  expect(result.node).toEqual([result.expected.a2]); expect(result.nodePrimary).toBe(result.expected.nativeA1);
  expect(result.nodeOthers).toEqual([result.expected.nativeA2]);
  expect(result.web).toEqual([result.expected.a2, result.expected.b1]); expect(result.webPrimary).toBe(result.expected.a1);
  expect(result.webOthers).toEqual([result.expected.a2, result.expected.b1]);
  checks.push('真实 wordHighlighter secondary 评分复用注册项，Node 只发送 A2，Web 发送 A2/B1');
});

test('原 MainThread 注册到共享 registry 后经真实 RPC 回调、URI 往返、取消及关闭，迟到响应不能影响另一宿主', async () => {
  const result = await inBrowser('真实 MainThread/registry/RPC 回调、typed URI、取消和关闭后的迟到响应', async () => {
    const p = globalThis.languageProbe, a = p.target('A'), b = p.target('B'), first = p.connect(a, p.service), second = p.connect(b, p.service);
    const registry = p.service.hoverProvider, definition = p.service.definitionProvider, aDoc = p.document(a), bDoc = p.document(b);
    const filters = p.selector({ scheme: 'file', pattern: '**/*.ts' }), position = new p.m.Position(1, 1);
    const aProvider = await p.rpcRegister(first, 1, '$registerHoverProvider', filters, registry);
    const bProvider = await p.rpcRegister(second, 1, '$registerHoverProvider', filters, registry);
    await p.rpcRegister(first, 2, '$registerDefinitionSupport', filters, definition);
    const aHover = await registry.ordered(aDoc)[0].provideHover(aDoc, position, p.m.CancellationToken.None);
    const bHover = await registry.ordered(bDoc)[0].provideHover(bDoc, position, p.m.CancellationToken.None);
    const locations = await definition.ordered(aDoc)[0].provideDefinition(aDoc, position, p.m.CancellationToken.None);
    const nativeRequests = [first.requests[0].resource.toString(), second.requests[0].resource.toString()];
    const started = Promise.withResolvers(), cancelled = Promise.withResolvers(), source = new p.m.CancellationTokenSource();
    first.onHover = request => new Promise((_resolve, reject) => {
      started.resolve(); request.token.onCancellationRequested(() => { cancelled.resolve(request.token.isCancellationRequested); reject(new p.m.CancellationError()); });
    });
    const pending = aProvider.provideHover(aDoc, position, source.token).then(() => 'unexpected-success', error => p.m.isCancellationError(error) ? 'cancelled' : error.message);
    await started.promise; source.cancel();
    const cancellation = { observed: await cancelled.promise, result: await pending };
    const beforePreCancelled = first.requests.length;
    const preCancelled = await aProvider.provideHover(aDoc, position, p.m.CancellationToken.Cancelled)
      .then(() => 'unexpected-success', error => p.m.isCancellationError(error) ? 'cancelled' : error.message);
    const preCancelledRequests = first.requests.length - beforePreCancelled;
    const entered = Promise.withResolvers(), release = Promise.withResolvers();
    first.onHover = async () => { entered.resolve(); return await release.promise; };
    const late = aProvider.provideHover(aDoc, position, p.m.CancellationToken.None)
      .then(() => 'unexpected-success', error => p.m.isCancellationError(error) ? 'cancelled' : error.message);
    await entered.promise; first.close();
    const closedResult = await late, firstCount = first.requests.length;
    release.resolve({ contents: [{ value: 'late result' }] });
    await new Promise(resolve => setTimeout(resolve, 0));
    const afterClose = { aProviders: registry.ordered(aDoc).length, bIdentity: registry.ordered(bDoc)[0] === bProvider,
      removedDefinition: definition.ordered(aDoc).length, lateCalls: first.requests.length - firstCount,
      bCallback: (await registry.ordered(bDoc)[0].provideHover(bDoc, position, p.m.CancellationToken.None)).contents[0].value };
    source.dispose();
    return { aHover: aHover.contents[0].value, bHover: bHover.contents[0].value, nativeRequests,
      expectedNative: [a.uriTransformer.transformOutgoingURI(aDoc.uri).toString(), b.uriTransformer.transformOutgoingURI(bDoc.uri).toString()],
      revivedDefinition: p.m.URI.isUri(locations[0].uri), definitionUri: locations[0].uri.toString(), expectedDefinition: aDoc.uri.toString(),
      cancellation, preCancelled, preCancelledRequests, closedResult, afterClose };
  });
  expect(result.aHover).toBe('hover'); expect(result.bHover).toBe('hover');
  expect(result.nativeRequests).toEqual(result.expectedNative);
  expect(result.revivedDefinition).toBe(true); expect(result.definitionUri).toBe(result.expectedDefinition);
  expect(result.cancellation).toEqual({ observed: true, result: 'cancelled' });
  expect(result.preCancelled).toBe('cancelled'); expect(result.preCancelledRequests).toBe(0); expect(result.closedResult).toBe('cancelled');
  expect(result.afterClose).toEqual({ aProviders: 0, bIdentity: true, removedDefinition: 0, lateCalls: 0, bCallback: 'hover' });
  checks.push('真实 MainThread/registry/RPC 回调、typed URI、取消和关闭后的迟到响应');
});

test('原 RenameAdapter 调用前冻结双根版本，等待及返回后变版不重盖，同原生 URI 的 A/B 与新查询独立', async () => {
  const result = await inBrowser('Rename原版本producer、原rawRename与双宿主隔离', async () => {
    const p = globalThis.languageProbe, m = p.m, registry = p.service.renameProvider, entered = Promise.withResolvers(), gate = Promise.withResolvers();
    const host = p.target('A'), peer = p.target('B', { roots: [host.binding.initialization.workspace.folders[0].native_uri] });
    const a = p.connect(host, p.service), b = p.connect(peer, p.service), callback = [], position = new m.Position(1, 2);
    const native = host.uriTransformer.folders.map(folder => m.URI.joinPath(folder.native, 'src/main.ts'));
    const peerNative = m.URI.joinPath(peer.uriTransformer.folders[0].native, 'src/main.ts');
    const editFor = (uris, text) => { const edit = new m.WorkspaceEdit(); for (const uri of uris) edit.replace(uri, new m.ApiRange(0, 0, 0, 3), text); return edit; };
    await a.registerRename({ async provideRenameEdits(document, pos, newName) {
      callback.push({ uri: document.uri.toString(), version: document.version, position: { line: pos.line, character: pos.character }, newName,
        versions: native.map(uri => a.documents.getDocument(uri).version) });
      const edit = editFor(native, newName); entered.resolve(); await gate.promise; return edit;
    } });
    await b.registerRename({ provideRenameEdits(_document, _position, newName) { return editFor([peerNative], newName); } });
    a.addDocument(native[0], 11); a.addDocument(native[1], 21); b.addDocument(peerNative, 91);
    const doc = p.document(host), pending = m.rawRename(registry, doc, position, 'renamed');
    await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入原RenameAdapter callback等待'); })]);
    a.changeDocument(native[1], 22, 'changed-during-rename'); const during = native.map(uri => a.documents.getDocument(uri).version);
    const peerResult = await m.rawRename(registry, p.document(peer), position, 'peer'); gate.resolve();
    const edit = await pending, frozen = edit.edits.map(value => value.versionId);
    a.changeDocument(native[1], 23, 'changed-after-return'); const fresh = await m.rawRename(registry, doc, position, 'fresh');
    return { callback, during, frozen, after: edit.edits.map(value => value.versionId), current: native.map(uri => a.documents.getDocument(uri).version),
      fresh: fresh.edits.map(value => value.versionId), peerVersions: peerResult.edits.map(value => value.versionId), sameNative: native[0].toString() === peerNative.toString(),
      typed: edit.edits.every(value => m.URI.isUri(value.resource)), resources: edit.edits.map(value => value.resource.toString()),
      expected: host.uriTransformer.folders.map(folder => m.URI.joinPath(folder.uri, 'src/main.ts').toString()),
      requests: a.requests.map(value => ({ kind: value.kind, resource: value.resource.toString(), newName: value.newName })), native: native[0].toString() };
  });
  expect(result.callback[0]).toEqual({ uri: result.native, version: 11, position: { line: 0, character: 1 }, newName: 'renamed', versions: [11, 21] });
  expect(result.during).toEqual([11, 22]); expect(result.frozen).toEqual([11, 21]); expect(result.after).toEqual([11, 21]); expect(result.current).toEqual([11, 23]);
  expect(result.fresh).toEqual([11, 23]); expect(result.peerVersions).toEqual([91]); expect(result.sameNative).toBe(true); expect(result.typed).toBe(true);
  expect(result.resources).toEqual(result.expected); expect(result.requests).toEqual(['renamed', 'fresh'].map(newName => ({ kind: 'rename', resource: result.native, newName })));
  checks.push('原rawRename/registry/RPC/RenameAdapter冻结调用前双根版本，同原生URI的另一宿主及新查询独立；返回后不重盖，不执行BulkEdit');
});

test('Rename 原转换器保留 create 的版本抑制，调用后才同步的资源不认证新版本', async () => {
  const result = await inBrowser('Rename原producer的create与未同步资源版本边界', async () => {
    const p = globalThis.languageProbe, m = p.m, host = p.target('A'), client = p.connect(host, p.service), entered = Promise.withResolvers(), gate = Promise.withResolvers();
    const own = host.uriTransformer.folders.map(folder => m.URI.joinPath(folder.native, 'src/main.ts')), unknown = m.URI.joinPath(host.uriTransformer.folders[0].native, 'unknown.ts');
    await client.registerRename({ async provideRenameEdits() {
      const edit = new m.WorkspaceEdit(); edit.replace(own[0], new m.ApiRange(0, 0, 0, 3), 'known'); edit.createFile(own[1]);
      edit.replace(own[1], new m.ApiRange(0, 0, 0, 3), 'created'); edit.replace(unknown, new m.ApiRange(0, 0, 0, 3), 'unknown');
      entered.resolve(); await gate.promise; return edit;
    } });
    client.addDocument(own[0], 5); client.addDocument(own[1], 6);
    const pending = m.rawRename(p.service.renameProvider, p.document(host), new m.Position(1, 1), 'mixed');
    await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入Rename版本边界callback'); })]);
    client.addDocument(unknown, 80); gate.resolve(); const result = await pending;
    return { edits: result.edits.map(edit => ({ type: edit.textEdit ? 'text' : 'file', version: edit.versionId ?? null, resource: (edit.resource ?? edit.newResource).toString() })),
      expected: [own[0], own[1], own[1], unknown].map(uri => m.URI.revive(host.uriTransformer.transformIncoming(uri)).toString()), unknownVersion: client.documents.getDocument(unknown).version };
  });
  expect(result.edits.map(value => value.type)).toEqual(['text', 'file', 'text', 'text']); expect(result.edits.map(value => value.version)).toEqual([5, null, null, null]);
  expect(result.edits.map(value => value.resource)).toEqual(result.expected); expect(result.unknownVersion).toBe(80);
  checks.push('原Rename WorkspaceEdit.from继续抑制同批create+text版本，调用时未同步资源不读返回时的新版本；不应用编辑');
});

test('Rename 合法首项后的 foreign resource／oldResource／newResource 与 app 位置整批拒绝，原 canonical revive 零调用', async () => {
  const result = await inBrowser('Rename结果整批准入、非typed后项与原RPC', async () => {
    const p = globalThis.languageProbe, m = p.m, host = p.target('A'), client = p.connect(host, p.service), position = new m.Position(1, 1), doc = p.document(host);
    const provider = await p.rpcRegister(client, 1, '$registerRenameSupport', p.selector({}), p.service.renameProvider, true);
    const own = m.URI.joinPath(host.uriTransformer.folders[0].native, 'src/main.ts'), foreign = p.document(p.target('B')).uri;
    // 不带 $mid 的 UriComponents 可穿过原 transport，必须由 MainThread 按字段核整批归属。
    const raw = { scheme: foreign.scheme, authority: foreign.authority, path: foreign.path, query: foreign.query, fragment: foreign.fragment };
    const text = resource => ({ resource, textEdit: { range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, text: 'renamed' } });
    const cases = [text(raw), { newResource: raw }, { oldResource: raw, newResource: own }, { oldResource: own, newResource: raw }, { oldResource: raw },
      { ...text(own), oldResource: raw, newResource: own }, text(m.URI.file('/hosts/app/help.ts'))], rejected = [];
    for (const invalid of cases) {
      client.onRename = async () => ({ edits: [text(own), invalid] }); const before = client.canonical.length, wire = client.wire.length;
      const state = await m.rawRename(p.service.renameProvider, doc, position, 'bad').then(() => ({ published: true }), error => ({ published: false, message: error.message }));
      rejected.push({ ...state, canonicalCalls: client.canonical.length - before, transported: client.wire.length > wire });
    }
    client.onRename = async () => ({ edits: [text(own)] }); const valid = await provider.provideRenameEdits(doc, position, 'valid', m.CancellationToken.None);
    return { rejected, count: client.requests.length, rawTyped: '$mid' in raw, valid: valid.edits[0].resource.toString(), typed: m.URI.isUri(valid.edits[0].resource), expected: doc.uri.toString() };
  });
  expect(result.rejected).toHaveLength(7);
  for (const entry of result.rejected) { expect(entry.published).toBe(false); expect(entry.message).toContain('扩展编辑超出发起宿主工作区视图'); expect(entry.canonicalCalls).toBe(0); expect(entry.transported).toBe(true); }
  expect(result.count).toBe(8); expect(result.rawTyped).toBe(false); expect(result.typed).toBe(true); expect(result.valid).toBe(result.expected);
  checks.push('原Rename结果按全部资源端点整批准入，foreign非typed后项及可过transport的app位置均在原canonical revive前拒绝；随后合法结果恢复');
});

test('Rename provide／prepare 入口已取消时零 RPC，原注册和后续查询保持可用', async () => {
  const result = await inBrowser('Rename预取消的provide与prepare边界', async () => {
    const p = globalThis.languageProbe, m = p.m, host = p.target('A'), client = p.connect(host, p.service), doc = p.document(host), position = new m.Position(1, 1);
    const provider = await p.rpcRegister(client, 1, '$registerRenameSupport', p.selector({}), p.service.renameProvider, true);
    const outcome = promise => promise.then(() => 'published', error => m.isCancellationError(error) ? 'cancelled' : error.message);
    const cancelled = [await outcome(provider.provideRenameEdits(doc, position, 'new', m.CancellationToken.Cancelled)),
      await outcome(provider.resolveRenameLocation(doc, position, m.CancellationToken.Cancelled))], calls = client.requests.length;
    const next = await m.rawRename(p.service.renameProvider, doc, position, 'new');
    return { cancelled, calls, sameProvider: p.service.renameProvider.ordered(doc)[0] === provider, next: next.edits[0].textEdit.text, total: client.requests.length };
  });
  expect(result).toEqual({ cancelled: ['cancelled', 'cancelled'], calls: 0, sameProvider: true, next: 'new', total: 1 });
  checks.push('原Rename provide/prepare预取消在RPC前拒绝，不改注册或后续合法查询');
});

for (const stage of ['rename', 'prepare']) for (const invalidation of ['cancel', 'unregister', 'dispose']) {
  test(`Rename ${stage} 等待中 ${invalidation} 不发布迟到结果，保留 RPC 的旧引用拒绝且新代际／B 不受影响`, async () => {
    const result = await inBrowser(`Rename ${stage}/${invalidation} 的原owner结果准入`, async ({ stage, invalidation }) => {
      const p = globalThis.languageProbe, m = p.m, host = p.target('A'), peer = p.target('B'), client = p.connect(host, p.service), other = p.connect(peer, p.service);
      const registry = p.service.renameProvider, doc = p.document(host), peerDoc = p.document(peer), position = new m.Position(1, 1), source = new m.CancellationTokenSource();
      const provider = await p.rpcRegister(client, 1, '$registerRenameSupport', p.selector({}), registry, true);
      const peerProvider = await p.rpcRegister(other, 1, '$registerRenameSupport', p.selector({}), registry, true);
      const invoke = (provider, model, token) => stage === 'rename' ? provider.provideRenameEdits(model, position, 'new', token) : provider.resolveRenameLocation(model, position, token);
      const summarize = value => stage === 'rename' ? value.edits[0].textEdit.text : value.text;
      const outcome = promise => promise.then(() => 'published', error => m.isCancellationError(error) ? 'cancelled' : error.message);
      const entered = Promise.withResolvers(), gate = Promise.withResolvers(), normalRename = client.onRename, normalPrepare = client.onPrepare;
      // 这个扩展actor刻意忽略取消；保留原RPC以区分MainThread结果门与关闭transport产生的拒绝。
      if (stage === 'rename') client.onRename = async request => { entered.resolve(); await gate.promise; return { edits: [{ resource: request.resource, textEdit: { range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, text: 'late' } }] }; };
      else client.onPrepare = async () => { entered.resolve(); await gate.promise; return { range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 }, text: 'late' }; };
      const pending = outcome(invoke(provider, doc, source.token)); await Promise.race([entered.promise, pending.then(() => { throw new Error('未进入Rename等待回包'); })]);
      if (invalidation === 'cancel') source.cancel(); else if (invalidation === 'unregister') await client.remote.$unregister(1); else client.main.dispose();
      await p.tick(); const tokenCancelled = client.requests[0].token.isCancellationRequested, afterInvalidation = registry.ordered(doc).includes(provider);
      client.onRename = normalRename; client.onPrepare = normalPrepare;
      const peerValue = summarize(await invoke(peerProvider, peerDoc, m.CancellationToken.None));
      let fresh = client, freshProvider = provider, freshValue;
      if (invalidation !== 'cancel') {
        if (invalidation === 'dispose') fresh = p.connect(host, p.service);
        freshProvider = await p.rpcRegister(fresh, 1, '$registerRenameSupport', p.selector({}), registry, true);
        freshValue = summarize(await invoke(freshProvider, doc, m.CancellationToken.None));
      }
      gate.resolve(); const late = await pending;
      if (invalidation === 'cancel') freshValue = summarize(await invoke(provider, doc, m.CancellationToken.None));
      const beforeStale = client.requests.length, blockedToken = invalidation === 'cancel' ? m.CancellationToken.Cancelled : m.CancellationToken.None;
      const stale = [await outcome(provider.provideRenameEdits(doc, position, 'stale', blockedToken)), await outcome(provider.resolveRenameLocation(doc, position, blockedToken))];
      source.dispose(); return { late, tokenCancelled, afterInvalidation, peerValue, freshValue, stale, staleRpc: client.requests.length - beforeStale,
        freshIdentity: registry.ordered(doc)[0] === freshProvider, newGeneration: freshProvider !== provider, peerIdentity: registry.ordered(peerDoc)[0] === peerProvider };
    }, {}, { stage, invalidation });
    expect(result).toEqual({ late: 'cancelled', tokenCancelled: true, afterInvalidation: invalidation === 'cancel', peerValue: stage === 'rename' ? 'new' : 'old',
      freshValue: stage === 'rename' ? 'new' : 'old', stale: ['cancelled', 'cancelled'], staleRpc: 0,
      freshIdentity: true, newGeneration: invalidation !== 'cancel', peerIdentity: true });
    checks.push(`原Rename ${stage}等待中${invalidation}后拒绝迟到结果，保留RPC、原token取消、旧引用零派发及新代际/B身份不变；不覆盖返回edit后的应用`);
  });
}

test('原 RenameAdapter 的 prepare Range／placeholder、缺省 fallback、rejectReason 和 undefined 保持原消费者合同', async () => {
  const result = await inBrowser('Rename真实prepare与原rename消费者拒绝合同', async () => {
    const p = globalThis.languageProbe, m = p.m, host = p.target('A'), client = p.connect(host, p.service), registry = p.service.renameProvider;
    const native = m.URI.joinPath(host.uriTransformer.folders[0].native, 'src/main.ts'), doc = p.document(host), position = new m.Position(1, 2);
    doc.getWordAtPosition = () => ({ word: 'fallback', startColumn: 1, endColumn: 4 }); let prepareMode = 'range', editMode = 'value', provides = 0;
    const provider = await client.registerRename({ prepareRename() {
      if (prepareMode === 'error') throw new Error('prepare blocked'); if (prepareMode === 'undefined') return undefined;
      const range = new m.ApiRange(0, 0, 0, 3); return prepareMode === 'placeholder' ? { range, placeholder: 'alias' } : range;
    }, provideRenameEdits() {
      provides++; if (editMode === 'error') throw new Error('rename blocked'); if (editMode === 'string') throw 'literal refused'; if (editMode === 'undefined') return undefined;
      const edit = new m.WorkspaceEdit(); edit.replace(native, new m.ApiRange(0, 0, 0, 3), 'renamed'); return edit;
    } });
    client.addDocument(native, 17, 'old source');
    const range = await m.prepareRename(registry, doc, position); prepareMode = 'placeholder'; const placeholder = await m.prepareRename(registry, doc, position);
    prepareMode = 'undefined'; const absent = await provider.resolveRenameLocation(doc, position, m.CancellationToken.None), fallback = await m.prepareRename(registry, doc, position);
    prepareMode = 'error'; const beforeReject = provides, rejectedPrepare = await m.rename(registry, doc, position, 'new'), skippedProvide = provides === beforeReject;
    prepareMode = 'range'; const success = await m.rename(registry, doc, position, 'new');
    editMode = 'error'; const directReject = await provider.provideRenameEdits(doc, position, 'new', m.CancellationToken.None), rejectedRename = await m.rawRename(registry, doc, position, 'new');
    editMode = 'string'; const stringReject = await m.rawRename(registry, doc, position, 'new'); editMode = 'undefined';
    const absentEdit = await provider.provideRenameEdits(doc, position, 'new', m.CancellationToken.None);
    return { range, placeholder, absent: absent === undefined, fallback, rejectedPrepare, skippedProvide,
      success: success.edits.map(value => ({ version: value.versionId, resource: value.resource.toString(), text: value.textEdit.text })), expected: doc.uri.toString(),
      directReject: { reason: directReject.rejectReason, hasEdits: directReject.edits !== undefined }, rejectedRename, stringReject, absentEdit: absentEdit === undefined };
  });
  const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 };
  expect(result.range).toEqual({ range, text: 'old' }); expect(result.placeholder).toEqual({ range, text: 'alias' }); expect(result.absent).toBe(true);
  expect(result.fallback).toEqual({ range, text: 'fallback' }); expect(result.rejectedPrepare).toEqual({ edits: [], rejectReason: 'prepare blocked' }); expect(result.skippedProvide).toBe(true);
  expect(result.success).toEqual([{ version: 17, resource: result.expected, text: 'renamed' }]); expect(result.directReject).toEqual({ reason: 'rename blocked', hasEdits: false });
  expect(result.rejectedRename).toEqual({ edits: [], rejectReason: 'rename blocked' }); expect(result.stringReject).toEqual({ edits: [], rejectReason: 'literal refused' }); expect(result.absentEdit).toBe(true);
  checks.push('原RenameAdapter保留Range原正文/placeholder/undefined，原prepareRename fallback及rename拒绝时跳过provide；Error/string rejectReason和无edit不被整批guard改写');
});

test('Web 原 Rename producer 保留 file／untitled 和 prepare，未实现 prepare 时仍使用原 word fallback', async () => {
  const result = await inBrowser('Web Rename原范围、真实producer与可选prepare', async () => {
    const p = globalThis.languageProbe, m = p.m, registry = p.service.renameProvider, host = { runningLocation: { kind: 2 } }, client = p.connect(host, p.service);
    const uris = [m.URI.file('/outside-web/source.ts'), m.URI.parse('untitled:rename.ts')], position = new m.Position(1, 1), doc = p.model(uris[0]);
    const makeEdit = () => { const edit = new m.WorkspaceEdit(); for (const uri of uris) edit.replace(uri, new m.ApiRange(0, 0, 0, 3), 'web'); return edit; };
    await client.registerRename({ prepareRename: () => ({ range: new m.ApiRange(0, 0, 0, 3), placeholder: 'web-source' }), provideRenameEdits: makeEdit });
    client.addDocument(uris[0], 4); client.addDocument(uris[1], 9); const prepared = await m.prepareRename(registry, doc, position), edits = await m.rename(registry, doc, position, 'web');
    client.main.dispose(); const next = p.connect(host, p.service); await next.registerRename({ provideRenameEdits: makeEdit });
    next.addDocument(uris[0], 5); doc.getWordAtPosition = () => ({ word: 'web-word', startColumn: 1, endColumn: 4 });
    const fallback = await m.prepareRename(registry, doc, position);
    return { prepared, resources: edits.edits.map(value => value.resource.toString()), expected: uris.map(uri => uri.toString()), versions: edits.edits.map(value => value.versionId),
      noPrepare: next.renameProvider.resolveRenameLocation === undefined, fallback };
  });
  const range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 };
  expect(result.prepared).toEqual({ range, text: 'web-source' }); expect(result.resources).toEqual(result.expected); expect(result.versions).toEqual([4, 9]);
  expect(result.noPrepare).toBe(true); expect(result.fallback).toEqual({ range, text: 'web-word' });
  checks.push('无binding Web经原RenameAdapter/rename消费者保留file/untitled范围与prepare；不增加Node归属限制，缺少prepare继续原word fallback');
});
