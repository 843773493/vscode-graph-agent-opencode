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
const sources = ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionLanguageFeaturesTransform.mjs',
  'upstream/src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts', 'upstream/src/vs/workbench/api/common/extHostLanguageFeatures.ts',
  'upstream/src/vs/workbench/api/common/extHostTypeConverters.ts', 'upstream/src/vs/workbench/api/common/extHostDocuments.ts',
  'upstream/src/vs/workbench/api/common/extHostDocumentsAndEditors.ts', 'upstream/src/vs/workbench/api/common/extHostDocumentData.ts',
  'upstream/src/vs/workbench/services/extensions/common/rpcProtocol.ts'];
const fingerprints = Object.fromEntries(await Promise.all(sources.map(async path => [path,
  createHash('sha256').update(await readFile(join(root, path))).digest('hex')])));
const built = await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadLanguageFeatures } from '${upstream}workbench/api/browser/mainThreadLanguageFeatures.js';
export { ExtHostLanguageFeatures } from '${upstream}workbench/api/common/extHostLanguageFeatures.js';
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { CommandsConverter } from '${upstream}workbench/api/common/extHostCommands.js';
export { CodeAction, CodeActionKind, WorkspaceEdit, Range } from '${upstream}workbench/api/common/extHostTypes.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
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
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' },
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile, null, 2));
const checks = [], observations = [], browserEvents = [];
let browser, server;

beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>Code Action 通用行为验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('Code Action fixture 来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Code Action Chromium', () => browser.close());
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints, checks, observations,
    boundary: '真实 Chromium 中通过原 MainThreadLanguageFeatures、LanguageFeaturesService、RPCProtocol、ExtHostLanguageFeatures/CodeActionAdapter、ExtHostDocuments 与 WorkspaceEdit.from 验证 CodeAction 版本快照、注册取消和 cache 释放；不覆盖 Gateway、旧 Node Extension Host、目标宿主隔离、真实磁盘编辑或 UI 菜单。' }, null, 2));
  await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
  await run.close();
});

async function inBrowser(name, body) {
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), service = new m.LanguageFeaturesService();
      const extension = { identifier: { value: 'probe.code-actions' }, name: 'code-actions', enabledApiProposals: [] };
      const uri = m.URI.file('/workspace/code-actions.ts'), range = { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4 };
      const actionContext = { only: 'quickfix', trigger: 1 }, tick = () => new Promise(resolve => setTimeout(resolve, 0));
      const mainToHost = new m.Emitter(), hostToMain = new m.Emitter(), requests = { provide: [], resolve: [], release: [] }, caches = new Set();
      const mainRpc = new m.RPCProtocol({ onMessage: mainToHost.event, send: data => queueMicrotask(() => hostToMain.fire(data)), drain: async () => {} });
      const hostRpc = new m.RPCProtocol({ onMessage: hostToMain.event, send: data => queueMicrotask(() => mainToHost.fire(data)), drain: async () => {} });
      const instantiation = new m.InstantiationService(new m.ServiceCollection([m.ILanguageFeaturesService, service]), true);
      const main = new m.MainThreadLanguageFeatures({ getProxy: mainRpc.getProxy.bind(mainRpc) },
        undefined, undefined, service, { asCanonicalUri: value => value }, instantiation, undefined);
      mainRpc.set(m.MainContext.MainThreadLanguageFeatures, main);
      const log = new m.NullLogService(), documentOwner = new m.ExtHostDocumentsAndEditors({ getProxy: hostRpc.getProxy.bind(hostRpc) }, log);
      const documents = new m.ExtHostDocuments({ getProxy: hostRpc.getProxy.bind(hostRpc) }, documentOwner);
      const converter = new m.CommandsConverter({ registerCommand: () => ({ dispose() {} }) }, () => undefined, log);
      const features = new m.ExtHostLanguageFeatures({ getProxy: hostRpc.getProxy.bind(hostRpc) }, undefined, documents,
        { notebookDocuments: [] }, { converter }, { getDiagnostics: () => [] }, log,
        { report() { throw new Error('不应调用已弃用的 CodeAction API'); } }, { onExtensionError(_extension, error) { throw error; } });
      hostRpc.set(m.ExtHostContext.ExtHostLanguageFeatures, {
        async $provideCodeActions(...args) {
          const request = { handle: args[0], resource: m.URI.revive(args[1]).toString(), token: args[4] };
          requests.provide.push(request);
          const result = await features.$provideCodeActions(...args);
          if (typeof result?.cacheId === 'number') caches.add(result.cacheId);
          return result;
        },
        async $resolveCodeAction(...args) { requests.resolve.push({ handle: args[0], cacheId: args[1], token: args[2] }); return features.$resolveCodeAction(...args); },
        $releaseCodeActions(handle, id) { requests.release.push({ handle, id, cached: caches.delete(id) }); features.$releaseCodeActions(handle, id); },
      });
      const model = { uri, getLanguageId: () => 'typescript', isTooLargeForSyncing: () => false, isForSimpleWidget: false };
      function addText(version) {
        documentOwner.acceptDocumentsAndEditorsDelta({ addedDocuments: [{ uri, versionId: version, lines: ['old'], EOL: '\n',
          languageId: 'typescript', isDirty: false, encoding: 'utf8' }] });
      }
      function changeText(version, text = 'changed') {
        const document = documents.getDocument(uri);
        documents.$acceptModelChanged(uri, { versionId: version, eol: '\n', isUndoing: false, isRedoing: false, isFlush: false, isEolChange: false,
          changes: [{ range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: document.getText().length + 1 }, rangeOffset: 0,
            rangeLength: document.getText().length, text }] }, true);
      }
      async function create(provider) {
        const registration = features.registerCodeActionProvider(extension, ['typescript'], provider, { providedCodeActionKinds: [m.CodeActionKind.QuickFix] });
        await tick();
        return { registration, provider: service.codeActionProvider.ordered(model)[0], model };
      }
      const editFor = versionText => {
        const edit = new m.WorkspaceEdit(); edit.replace(uri, new m.Range(0, 0, 0, 3), versionText); return edit;
      };
      async function dispose() {
        documentOwner.acceptDocumentsAndEditorsDelta({ removedDocuments: documents.getAllDocumentData().map(data => data.document.uri) });
        documents.dispose(); features.dispose(); main.dispose(); instantiation.dispose(); mainRpc.dispose(); hostRpc.dispose();
        mainToHost.dispose(); hostToMain.dispose();
      }
      globalThis.codeActionProbe = { m, uri, range, actionContext, requests, caches, model, addText, changeText, create, editFor, tick,
        provide(runtime, token = m.CancellationToken.None) { return runtime.provider.provideCodeActions(runtime.model, range, actionContext, token); },
        currentVersion() { return documents.getDocument(uri).version; },
        providerPresent(runtime) { return service.codeActionProvider.allNoModel().includes(runtime.provider); },
        disposeList(list) { list.dispose(); list.dispose(); }, async dispose() { await dispose(); } };
      await tick();
    });
    const result = await page.evaluate(body);
    expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.codeActionProbe?.dispose()); }
    finally { await context.close(); }
  }
}

test('CodeAction provide 与 resolve 分别冻结回调开始前的文本版本并单次释放 cache', async () => {
  const result = await inBrowser('provide/resolve 版本快照与 cache 生命周期', async () => {
    const p = globalThis.codeActionProbe, m = p.m, provideEntered = Promise.withResolvers(), provideGate = Promise.withResolvers();
    const resolveEntered = Promise.withResolvers(), resolveGate = Promise.withResolvers(); let runtime, original;
    p.addText(7);
    runtime = await p.create({
      async provideCodeActions() {
        provideEntered.resolve(); await provideGate.promise;
        original = new m.CodeAction('版本编辑', m.CodeActionKind.QuickFix); original.edit = p.editFor('provide'); return [original];
      },
      async resolveCodeAction(action) {
        resolveEntered.resolve(); await resolveGate.promise; action.edit = p.editFor('resolve'); return action;
      },
    });
    const providing = p.provide(runtime); await provideEntered.promise;
    p.changeText(8, 'changed during provide'); provideGate.resolve();
    const list = await providing, action = list.actions[0], provideVersion = action.edit.edits[0].versionId;
    const resolving = runtime.provider.resolveCodeAction(action, m.CancellationToken.None); await resolveEntered.promise;
    p.changeText(9, 'changed during resolve'); resolveGate.resolve();
    const resolved = await resolving, resolveVersion = resolved.edit.edits[0].versionId, identity = resolved === action;
    p.disposeList(list); await p.tick();
    const result = { provideVersion, resolveVersion, identity, currentVersion: p.currentVersion(), releases: p.requests.release, caches: [...p.caches] };
    runtime.registration.dispose(); await p.tick(); return result;
  });
  expect(result).toEqual({ provideVersion: 7, resolveVersion: 8, identity: true, currentVersion: 9,
    releases: [{ handle: 0, id: 1, cached: true }], caches: [] });
  checks.push('CodeActionAdapter 在 provide/resolve 回调前分别捕获当前文本版本，异步变化不覆盖快照；重复 dispose 仅释放一次 cache');
});

test('注销 CodeAction provider 会取消窗口内等待并拒绝迟到结果', async () => {
  const result = await inBrowser('provider 注销取消在途 CodeAction', async () => {
    const p = globalThis.codeActionProbe, m = p.m, entered = Promise.withResolvers(), gate = Promise.withResolvers(); let runtime, callbackToken;
    p.addText(3);
    runtime = await p.create({ async provideCodeActions(_document, _range, _context, token) {
      callbackToken = token; entered.resolve(); await gate.promise;
      return [new m.CodeAction('迟到结果', m.CodeActionKind.QuickFix)];
    } });
    const providing = p.provide(runtime).then(() => 'unexpected success', error => m.isCancellationError(error) ? 'cancelled' : error.message);
    await entered.promise; runtime.registration.dispose(); await p.tick();
    const cancelled = callbackToken.isCancellationRequested, providerRemoved = !p.providerPresent(runtime);
    gate.resolve(); const outcome = await providing; await p.tick();
    const result = { cancelled, providerRemoved, outcome, releases: p.requests.release, caches: [...p.caches], requests: p.requests.provide.length };
    return result;
  });
  expect(result).toEqual({ cancelled: true, providerRemoved: true, outcome: 'cancelled', releases: [], caches: [], requests: 1 });
  checks.push('注销会取消原 RPC token，在途 CodeAction 不发布且不留下 cache');
});
