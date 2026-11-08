import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const testFile = 'tests/integration/workspace_services/extension_host/extension_notebook_scope.test.mjs';
const run = await TestRunContext.fromTestFile(testFile).prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const owners = [
  ['mainThreadNotebookDocumentsAndEditors', 'MainThreadNotebooksAndEditors'],
  ['mainThreadNotebookDocuments', 'MainThreadNotebookDocuments', 'MainThreadNotebookDocuments'],
  ['mainThreadNotebookEditors', 'MainThreadNotebookEditors', 'MainThreadNotebookEditors'],
  ['mainThreadNotebook', 'MainThreadNotebooks', 'MainThreadNotebook'],
  ['mainThreadNotebookKernels', 'MainThreadNotebookKernels', 'MainThreadNotebookKernels'],
  ['mainThreadNotebookRenderers', 'MainThreadNotebookRenderers', 'MainThreadNotebookRenderers'],
  ['mainThreadNotebookSaveParticipant', 'SaveParticipant'],
];
const sourcePaths = [testFile, ...['scripts/sourceTransform.mjs', 'scripts/extensions/extensionNotebookTransform.mjs',
  'scripts/extensions/extensionFileEventsTransform.mjs', 'src/extensions/targetUris.ts',
  'upstream/src/vs/workbench/api/browser/mainThreadBulkEdits.ts', 'upstream/src/vs/workbench/api/common/extHostBulkEdits.ts',
  'upstream/src/vs/workbench/api/common/extHostTypes.ts', 'upstream/src/vs/workbench/api/common/extHostTypeConverters.ts',
  'upstream/src/vs/workbench/api/common/extHost.api.impl.ts',
  ...owners.map(([file]) => `upstream/src/vs/workbench/api/browser/${file}.ts`),
  ...['extHostNotebook', 'extHostNotebookDocuments', 'extHostNotebookEditors', 'extHostNotebookDocument',
    'extHostDocumentsAndEditors', 'extHostDocuments'].map(file => `upstream/src/vs/workbench/api/common/${file}.ts`),
].map(path => 'src/clients/workbench/' + path)];
const fingerprint = async () => Object.fromEntries(await Promise.all(sourcePaths.map(async path =>
  [path, createHash('sha256').update(await readFile(path)).digest('hex')])));
const fingerprints = await fingerprint(), convertedSources = [], checks = [], observations = [], browserEvents = [];
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const parse = async (path, convert = transform) => ts.createSourceFile(path,
  await convert(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
const surfaces = {}, publicFactories = [];
for (const runtime of ['browser', 'node']) {
  const parsed = await parse(join(root, 'upstream/src/vs/workbench/api/common/extHost.api.impl.ts'),
    sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime }));
  const namespaces = new Map();
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ['workspace', 'window', 'notebooks'].includes(node.name.getText(parsed))
      && ts.isObjectLiteralExpression(node.initializer)) {
      const namespace = node.name.getText(parsed);
      if (namespaces.has(namespace)) throw new Error('原公开命名空间重复：' + namespace);
      const members = node.initializer.properties.filter(member => namespace === 'notebooks'
        || member.name?.getText(parsed).toLowerCase().includes('notebook'));
      namespaces.set(namespace, members);
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  if (namespaces.size !== 3) throw new Error('原 Notebook 公开命名空间不完整');
  surfaces[runtime] = [...namespaces].flatMap(([namespace, members]) => members.map(member => ({
    namespace, name: member.name.getText(parsed), getter: ts.isGetAccessorDeclaration(member),
  })));
  const factory = `export function ${runtime}NotebookApi(initData, boundary) {
    const { extHostNotebook, extHostNotebookDocuments, extHostNotebookEditors, extHostNotebookKernels,
      extHostNotebookRenderers, extHostNotebookDocumentSaveParticipant, extension, _asExtensionEvent,
      isProposedApiEnabled, checkProposedApiEnabled } = boundary;
    return { ${[...namespaces].map(([namespace, members]) => `${namespace}: {${members.map(member => member.getText(parsed)).join(',\n')}}`).join(',\n')} };
  }`;
  publicFactories.push(factory);
  await writeFile(join(run.artifactsDir, `original-public-notebook-${runtime}.ts`), factory);
}
const rpcOwners = [];
for (const [file, name, context] of owners) {
  const parsed = await parse(join(root, `upstream/src/vs/workbench/api/browser/${file}.ts`));
  const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === name);
  if (!owner) throw new Error('原 Notebook owner 不存在：' + name);
  rpcOwners.push({ name, context, methods: owner.members.filter(member => ts.isMethodDeclaration(member)
    && member.name.getText(parsed).startsWith('$')).map(member => member.name.getText(parsed)) });
}
const saveParsed = await parse(join(root, 'upstream/src/vs/workbench/api/browser/mainThreadNotebookSaveParticipant.ts'));
const saveClass = saveParsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtHostNotebookDocumentSaveParticipant');
if (!saveClass) throw new Error('原内部 Notebook 保存参与者不存在');
const bundle = join(run.artifactsDir, 'notebook-scope.fixture.mjs');
const built = await build({ stdin: { loader: 'ts', resolveDir: resolve('.'), contents: `
import * as errors from '${upstream}base/common/errors.js';
import { NotSupportedError } from '${upstream}base/common/errors.js';
import { URI } from '${upstream}base/common/uri.js';
import { CancellationToken } from '${upstream}base/common/cancellation.js';
import { localize } from '${upstream}nls.js';
import { raceCancellationError } from '${upstream}base/common/async.js';
import { NotebookFileWorkingCopyModel } from '${upstream}workbench/contrib/notebook/common/notebookEditorModel.js';
import { MainContext, ExtHostContext } from '${upstream}workbench/api/common/extHost.protocol.js';
import { hostOwnsFileEventResource } from './src/clients/workbench/src/extensions/targetUris.js';
${publicFactories.join('\n')}
${saveClass.getText(saveParsed)}
export { ExtHostNotebookDocumentSaveParticipant, URI, CancellationToken, NotebookFileWorkingCopyModel };
export const publicSurfaces = ${JSON.stringify(surfaces)}, rpcOwners = ${JSON.stringify(rpcOwners)};
${owners.map(([file, name]) => `export { ${name} } from '${upstream}workbench/api/browser/${file}.js';`).join('\n')}
export { ExtHostNotebookController } from '${upstream}workbench/api/common/extHostNotebook.js';
export { ExtHostNotebookDocuments } from '${upstream}workbench/api/common/extHostNotebookDocuments.js';
export { ExtHostNotebookEditors } from '${upstream}workbench/api/common/extHostNotebookEditors.js';
export { ExtHostDocumentsAndEditors } from '${upstream}workbench/api/common/extHostDocumentsAndEditors.js';
export { ExtHostDocuments } from '${upstream}workbench/api/common/extHostDocuments.js';
export { CommandsConverter } from '${upstream}workbench/api/common/extHostCommands.js';
export { MainContext, ExtHostContext };
export { ExtHostBulkEdits } from '${upstream}workbench/api/common/extHostBulkEdits.js';
export { MainThreadBulkEdits } from '${upstream}workbench/api/browser/mainThreadBulkEdits.js';
export { WorkspaceEdit, NotebookEdit, NotebookData, NotebookCellData, NotebookCellKind } from '${upstream}workbench/api/common/extHostTypes.js';
export { RPCProtocol } from '${upstream}workbench/services/extensions/common/rpcProtocol.js';
export { SerializableObjectWithBuffers } from '${upstream}workbench/services/extensions/common/proxyIdentifier.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { extUri } from '${upstream}base/common/resources.js';
export { CellUri, CellKind, NotebookCellsChangeType, NOTEBOOK_EDITOR_ID } from '${upstream}workbench/contrib/notebook/common/notebookCommon.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024', metafile: true,
  loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file' }, tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent',
  plugins: [buildSourcePlugin(root, async (path, source) => {
    const converted = await transform(path, source), relative = path.slice(resolve('.').length + 1);
    if (sourcePaths.includes(relative)) convertedSources.push({ path: relative,
      original: createHash('sha256').update(source).digest('hex'), converted: createHash('sha256').update(converted).digest('hex') });
    return converted;
  })] });
await writeFile(join(run.artifactsDir, 'bundle-meta.json'), JSON.stringify(built.metafile));
await writeFile(join(run.artifactsDir, 'test-source.mjs'), await readFile(testFile, 'utf8'));
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    return new URL(request.url).pathname === '/fixture.mjs' ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>Notebook 目标边界组合验证</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('Notebook 组件 HTTP 来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Notebook 组件 Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally {
    const fingerprintsAfter = await fingerprint();
    await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ pinnedUpstreamCommit, fingerprints,
      fingerprintsAfter, convertedSources, surfaces, rpcOwners, checks,
      boundary: '真实 Chromium 内运行原七个 MainThread Notebook owner、原 RPCProtocol、原 ExtHost Notebook/文档/编辑器接收 owner，以及 AST 提取的转换后原公开 API 与内部保存参与者完整类。认证初始化、字节输送、窗口模型/resolver/editor/serializer/kernel/renderer/工作副本服务及公开 API 委派接收端为具名装具。验证目标拒绝与零全窗口投影，不代表目标 Notebook 支持、Cell URI 映射、真实 Gateway/Node/SSH 或完整窗口验收。',
    }, null, 2));
    await writeFile(join(run.artifactsDir, 'observations.json'), JSON.stringify(observations, null, 2));
    await writeFile(join(run.artifactsDir, 'browser-events.json'), JSON.stringify(browserEvents, null, 2));
    expect(fingerprintsAfter).toEqual(fingerprints);
  }
});

async function inBrowser(name, body, argument) {
  const context = await browser.newContext(), page = await context.newPage(), errors = [];
  page.on('pageerror', error => { errors.push(error.message); browserEvents.push({ name, message: error.message }); });
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    await page.evaluate(async () => {
      globalThis._VSCODE_FILE_ROOT = location.origin + '/';
      const m = await import('/fixture.mjs'), lifetimes = [];
      const outcome = async action => { try { return { value: await action() }; } catch (error) { return { error: error.message, errorName: error.name }; } };
      const target = kind => ({ targetContext: { kind, connection: { gateway_id: 'notebook-gateway', backend_id: 'notebook-backend' } }, runningLocation: { kind: kind === 'node' ? 3 : 2 } });
      function publicFixture(runtime, scoped) {
        const accesses = [], calls = [], document = { uri: m.URI.file('/window/original.ipynb'), notebookType: 'original' }, editor = { notebook: document };
        const event = name => (...args) => { calls.push({ name, count: args.length }); return name; };
        const owner = name => new Proxy({}, { get(_object, property) {
          accesses.push(name + '.' + String(property));
          if (property === 'notebookDocuments') return [{ apiNotebook: document }];
          if (property === 'activeNotebookEditor') return editor;
          if (property === 'visibleNotebookEditors') return [editor];
          if (String(property).startsWith('on')) return event(name + '.' + String(property));
          return (...args) => {
            calls.push({ name: name + '.' + String(property), count: args.length });
            if (property === 'getNotebookDocument') return { apiNotebook: document };
            if (property === 'createNotebookDocument') return Promise.resolve(document.uri);
            if (property === 'openNotebookDocument') return Promise.resolve(document);
            if (property === 'showNotebookDocument') return Promise.resolve(editor);
            if (property === 'getOnWillSaveNotebookDocumentEvent') return event('willSave');
            return name + '.' + String(property);
          };
        } });
        const boundary = Object.fromEntries(['extHostNotebook', 'extHostNotebookDocuments', 'extHostNotebookEditors', 'extHostNotebookKernels',
          'extHostNotebookRenderers', 'extHostNotebookDocumentSaveParticipant'].map(name => [name, owner(name)]));
        Object.assign(boundary, { extension: { identifier: { value: 'probe.notebook-scope' } },
          _asExtensionEvent(value) { accesses.push('_asExtensionEvent'); return value; },
          isProposedApiEnabled() { accesses.push('isProposedApiEnabled'); return true; },
          checkProposedApiEnabled() { accesses.push('checkProposedApiEnabled'); } });
        const api = m[runtime + 'NotebookApi']({ isTargetExtensionHost: scoped }, boundary);
        const args = name => name.startsWith('on') ? [() => {}, { listener: true }, []]
          : name === 'openNotebookDocument' ? [document.uri]
          : name === 'showNotebookDocument' ? [document, { preserveFocus: true }]
          : name === 'registerNotebookSerializer' ? ['original', {}, {}, {}]
          : name === 'createNotebookController' ? ['controller', 'original', 'Original', () => {}, []]
          : name === 'registerNotebookCellStatusBarItemProvider' ? ['original', {}]
          : name === 'createRendererMessaging' ? ['renderer'] : ['original', {}];
        return { api, accesses, calls, document, editor, args };
      }
      function blockedOwner(kind, description) {
        const effects = { services: [], outgoing: [], set: [], instances: [] }, host = target(kind);
        // 窗口服务是具名边界；任何列表读取、订阅或注册都会立即留下证据并失败。
        const forbidden = name => new Proxy({}, { get(_object, property) {
          const access = name + '.' + String(property); effects.services.push(access); throw new Error('读取了窗口服务：' + access);
        } });
        const services = Object.fromEntries(['notebooks', 'editors', 'editor', 'groups', 'configuration', 'resolver', 'statusBar',
          'language', 'kernels', 'execution', 'messaging', 'workingCopies'].map(name => [name, forbidden(name)]));
        const toMain = new m.Emitter(), toHost = new m.Emitter();
        const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: data => queueMicrotask(() => toHost.fire(data)), drain: async () => {} });
        const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: data => queueMicrotask(() => toMain.fire(data)), drain: async () => {} });
        const context = { extensionHost: host, getProxy: id => new Proxy({}, { get(_object, name) { return (...args) => {
          effects.outgoing.push({ id: id.sid, name: String(name), args }); return Promise.resolve();
        }; } }), set(id, actor) { effects.set.push(id.sid); mainRpc.set(id, actor); } };
        const log = new m.NullLogService(), identity = { extUri: m.extUri };
        const instantiation = { createInstance(type, ...args) { effects.instances.push(type.name);
          if (type === m.MainThreadNotebookDocuments) return new type(...args, services.resolver, identity);
          if (type === m.MainThreadNotebookEditors) return new type(...args, services.editor, services.editors, services.groups, services.configuration);
          return new type(...args);
        } };
        const parameters = {
          MainThreadNotebooksAndEditors: [context, instantiation, services.notebooks, services.editors, services.editor, services.groups, log],
          MainThreadNotebookDocuments: [context, services.resolver, identity],
          MainThreadNotebookEditors: [context, services.editor, services.editors, services.groups, services.configuration],
          MainThreadNotebooks: [context, services.notebooks, services.statusBar, log],
          MainThreadNotebookKernels: [context, services.language, services.kernels, services.execution, services.notebooks, services.editors],
          MainThreadNotebookRenderers: [context, services.messaging],
          SaveParticipant: [context, instantiation, services.workingCopies],
        };
        const owner = new m[description.name](...parameters[description.name]);
        if (description.context) mainRpc.set(m.MainContext[description.context], owner);
        lifetimes.push(() => { owner.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
        return { effects, owner, remote: description.context && hostRpc.getProxy(m.MainContext[description.context]) };
      }
      globalThis.notebookProbe = { m, lifetimes, outcome, target, publicFixture, blockedOwner,
        dispose() { for (const dispose of lifetimes.reverse()) dispose(); } };
    });
    const result = await page.evaluate(body, argument); expect(errors).toEqual([]); observations.push({ name, result }); return result;
  } finally {
    try { await page.evaluate(() => globalThis.notebookProbe?.dispose()); }
    finally { await context.close(); }
  }
}

for (const runtime of ['browser', 'node']) test(`${runtime} 原公开 Notebook API 在读取窗口对象、订阅及注册前明确拒绝目标宿主`, async () => {
  const result = await inBrowser(runtime + ' 公开目标拒绝', async runtime => {
    const p = globalThis.notebookProbe, fixture = p.publicFixture(runtime, true), outcomes = [];
    for (const { namespace, name, getter } of p.m.publicSurfaces[runtime]) {
      outcomes.push({ namespace, name, ...await p.outcome(() => getter ? fixture.api[namespace][name] : fixture.api[namespace][name](...fixture.args(name))) });
    }
    for (const args of [['original', {}], [], [null]]) outcomes.push({ name: 'openNotebookDocument overload',
      ...await p.outcome(() => fixture.api.workspace.openNotebookDocument(...args)) });
    return { outcomes, accesses: fixture.accesses, calls: fixture.calls };
  }, runtime);
  expect(result.outcomes).toHaveLength(23);
  for (const outcome of result.outcomes) { expect(outcome.error).toContain('目标扩展暂不支持 Notebook API'); expect(outcome.value).toBeUndefined(); }
  expect(result.accesses).toEqual([]); expect(result.calls).toEqual([]);
  checks.push(`${runtime} 原公开20入口及open三种附加重载在对象/事件/注册/proposed检查前拒绝`);
});

for (const runtime of ['browser', 'node']) test(`${runtime} 窗口内置公开 Notebook API 保留原对象、事件与委派`, async () => {
  const result = await inBrowser(runtime + ' 公开窗口行为', async runtime => {
    const p = globalThis.notebookProbe, fixture = p.publicFixture(runtime, false), results = [];
    for (const { namespace, name, getter } of p.m.publicSurfaces[runtime]) {
      const value = getter ? fixture.api[namespace][name] : await fixture.api[namespace][name](...fixture.args(name));
      results.push({ namespace, name, defined: value !== undefined,
        original: name === 'notebookDocuments' ? value[0] === fixture.document : name === 'activeNotebookEditor' || name === 'showNotebookDocument'
          ? value === fixture.editor : name === 'visibleNotebookEditors' ? value[0] === fixture.editor : name === 'openNotebookDocument' ? value === fixture.document : true });
    }
    const created = await fixture.api.workspace.openNotebookDocument('original', {});
    return { results, created: created === fixture.document, calls: fixture.calls, accesses: fixture.accesses };
  }, runtime);
  expect(result.results).toHaveLength(20); expect(result.results.every(item => item.defined && item.original)).toBe(true); expect(result.created).toBe(true);
  for (const name of ['extHostNotebook.registerNotebookSerializer', 'extHostNotebookKernels.createNotebookController',
    'extHostNotebookRenderers.createRendererMessaging', 'extHostNotebookKernels.createNotebookControllerDetectionTask',
    'extHostNotebookKernels.registerKernelSourceActionProvider', 'willSave']) expect(result.calls.some(call => call.name === name)).toBe(true);
  checks.push(`${runtime} 窗口内置公开20入口保留原委派、对象/事件与create重载`);
});

for (const kind of ['node', 'web']) test(`${kind} 七个原 MainThread owner 零窗口 bootstrap/订阅，全部入站 RPC 在解析参数前拒绝`, async () => {
  const result = await inBrowser(kind + ' 原 MainThread 目标拒绝', async kind => {
    const p = globalThis.notebookProbe, owners = [];
    for (const description of p.m.rpcOwners) {
      const fixture = p.blockedOwner(kind, description), outcomes = [];
      for (const method of description.methods) outcomes.push({ method, ...await p.outcome(() => fixture.remote[method]()) });
      owners.push({ name: description.name, outcomes, effects: fixture.effects });
    }
    return owners;
  }, kind);
  expect(result).toHaveLength(7);
  for (const owner of result) {
    expect(owner.effects.services).toEqual([]); expect(owner.effects.outgoing).toEqual([]);
    for (const outcome of owner.outcomes) expect(outcome.error).toContain('目标扩展暂不支持 Notebook API');
  }
  expect(result.reduce((sum, owner) => sum + owner.outcomes.length, 0)).toBeGreaterThan(25);
  checks.push(`${kind} 七原owner构造零窗口服务/正文/事件；全部原$入口经真实RPC在参数访问前拒绝`);
});

test('原内部 Notebook 保存参与者直接调用也拒绝目标，窗口内置保持类型筛选与原 RPC', async () => {
  const result = await inBrowser('原保存参与者目标拒绝与窗口行为', async () => {
    const p = globalThis.notebookProbe, m = p.m, calls = [], outcomes = [];
    for (const kind of ['node', 'web']) {
      const participant = new m.ExtHostNotebookDocumentSaveParticipant({ extensionHost: p.target(kind), getProxy: () => ({
        $participateInSave(...args) { calls.push(args); return Promise.resolve(); },
      }) });
      outcomes.push(await p.outcome(() => participant.participate(undefined, undefined, undefined, m.CancellationToken.None)));
    }
    const participant = new m.ExtHostNotebookDocumentSaveParticipant({ extensionHost: { runningLocation: { kind: 2 } }, getProxy: () => ({
      $participateInSave(...args) { calls.push(args); return Promise.resolve(); },
    }) });
    await participant.participate({ model: {} }, {}, {}, m.CancellationToken.None);
    const filtered = calls.length, uri = m.URI.file('/window/save.ipynb');
    // 这里只具名替换工作副本对象；原 instanceof、期限、取消和RPC流程保持。
    await participant.participate({ resource: uri, model: Object.create(m.NotebookFileWorkingCopyModel.prototype) }, { reason: 1 }, {}, m.CancellationToken.None);
    return { outcomes, filtered, calls: calls.map(args => ({ uri: args[0].toString(), reason: args[1], token: args[2] === m.CancellationToken.None })) };
  });
  expect(result.outcomes.map(item => item.error)).toEqual(Array(2).fill('目标扩展暂不支持 Notebook API；文档、Cell URI、serializer 与执行服务尚未完成目标绑定'));
  expect(result.filtered).toBe(0); expect(result.calls).toEqual([{ uri: 'file:///window/save.ipynb', reason: 1, token: true }]);
  checks.push('原内部保存参与者目标直调拒绝；窗口类型筛选、原参数/RPC及期限收尾保持');
});

test('纯值 Notebook 类型保持可构造，公开 applyEdit 的 Notebook 分支在 canonical 和 apply 前拒绝目标', async () => {
  const result = await inBrowser('纯值与原 BulkEdits Notebook 入口', async () => {
    const p = globalThis.notebookProbe, m = p.m, results = [];
    const cell = new m.NotebookCellData(m.NotebookCellKind.Code, 'original cell', 'plaintext'), data = new m.NotebookData([cell]);
    const notebookEdit = m.NotebookEdit.updateNotebookMetadata({ original: true });
    for (const kind of ['node', 'web', 'builtin']) {
      const canonical = [], applied = [], calls = [], toMain = new m.Emitter(), toHost = new m.Emitter();
      const mainRpc = new m.RPCProtocol({ onMessage: toMain.event, send: value => queueMicrotask(() => toHost.fire(value)), drain: async () => {} });
      const hostRpc = new m.RPCProtocol({ onMessage: toHost.event, send: value => queueMicrotask(() => toMain.fire(value)), drain: async () => {} });
      const main = new m.MainThreadBulkEdits({ extensionHost: kind === 'builtin' ? { runningLocation: { kind: 2 } } : p.target(kind) },
        { async apply(edit) { applied.push(edit.edits.map(value => ({ resource: value.resource.toString(), cellEdit: value.cellEdit }))); return { isApplied: true }; } },
        { warn(message) { calls.push(message); } }, { asCanonicalUri(uri) { canonical.push(uri.toString()); return uri; } });
      mainRpc.set(m.MainContext.MainThreadBulkEdits, main);
      const ext = new m.ExtHostBulkEdits({ getProxy: hostRpc.getProxy.bind(hostRpc) }, { getDocument: () => undefined }, { getNotebookDocument: () => undefined });
      p.lifetimes.push(() => { main.dispose(); mainRpc.dispose(); hostRpc.dispose(); toMain.dispose(); toHost.dispose(); });
      const edit = new m.WorkspaceEdit(), uri = m.URI.file('/window/notebook.ipynb');
      edit.set(uri, [notebookEdit]);
      const outcome = await p.outcome(() => ext.applyWorkspaceEdit(edit, { identifier: { value: 'probe.notebook' } }));
      results.push({ kind, ...outcome, canonical, applied, calls });
    }
    return { values: { count: data.cells.length, kind: data.cells[0].kind, value: data.cells[0].value, metadata: notebookEdit.newNotebookMetadata }, results };
  });
  expect(result.values).toEqual({ count: 1, kind: 2, value: 'original cell', metadata: { original: true } });
  for (const outcome of result.results.slice(0, 2)) {
    // 原 MainThreadBulkEdits 合同将拒绝报告为 false，同时保留明确原因，不能进入 canonical 或业务 apply。
    expect(outcome.value).toBe(false); expect(outcome.canonical).toEqual([]); expect(outcome.applied).toEqual([]);
    expect(outcome.calls).toHaveLength(1); expect(outcome.calls[0]).toContain('目标扩展暂不支持 Notebook 编辑');
  }
  const builtin = result.results[2]; expect(builtin.value).toBe(true); expect(builtin.canonical).toEqual(['file:///window/notebook.ipynb']);
  expect(builtin.applied).toHaveLength(1); expect(builtin.applied[0][0].cellEdit.metadata).toEqual({ original: true }); expect(builtin.calls).toEqual([]);
  checks.push('NotebookData/CellData/NotebookEdit纯值保持；公开ExtHostBulkEdits→真实RPC→MainThread在canonical/apply前拒绝目标，窗口原edit保持');
});

test('窗口内置原 Kernel、Renderer 与保存参与者仍订阅、注册、转发并释放', async () => {
  const result = await inBrowser('窗口原 Kernel/Renderer/SaveParticipant', async () => {
    const p = globalThis.notebookProbe, m = p.m, subscriptions = [], registrations = [], forwarded = [], disposed = [], posted = [];
    const signal = name => { const emitter = new m.Emitter(); p.lifetimes.push(() => emitter.dispose());
      return { fire: value => emitter.fire(value), event(listener, thisArgs, disposables) { subscriptions.push(name); return emitter.event(listener, thisArgs, disposables); } }; };
    const selected = signal('selected'), message = signal('editor message'), add = signal('editor add'), remove = signal('editor remove'), rendererMessage = signal('renderer message');
    const uri = m.URI.file('/window/kernel.ipynb');
    const editor = { getId: () => 'original-editor', hasModel: () => true, textModel: { uri }, onDidReceiveMessage: message.event,
      postMessage(value) { posted.push(value); } };
    const context = { extensionHost: { runningLocation: { kind: 2 } }, getProxy: () => new Proxy({}, { get(_object, method) {
      return (...args) => { forwarded.push({ method: String(method), args: args.map(value => m.URI.isUri(value) ? value.toString() : value) }); return Promise.resolve(); };
    } }) };
    let selectedKernel;
    const registration = name => { registrations.push(name); return { dispose() { disposed.push(name); } }; };
    const kernelService = { onDidChangeSelectedNotebooks: selected.event, getMatchingKernel: () => ({ selected: selectedKernel }),
      registerKernel(kernel) { selectedKernel = kernel; return registration('kernel'); },
      registerNotebookKernelDetectionTask: () => registration('detection'), registerKernelSourceActionProvider: () => registration('source') };
    const kernels = new m.MainThreadNotebookKernels(context, { getRegisteredLanguageIds: () => ['plaintext'] }, kernelService, {}, {},
      { listNotebookEditors: () => [editor], onDidAddNotebookEditor: add.event, onDidRemoveNotebookEditor: remove.event });
    const renderers = new m.MainThreadNotebookRenderers(context, { onShouldPostMessage: rendererMessage.event,
      async receiveMessage(editorId, rendererId, value) { posted.push({ editorId, rendererId, value }); return true; } });
    let participant;
    const save = new m.SaveParticipant(context, { createInstance(type, ...args) { return new type(...args); } },
      { addSaveParticipant(value) { participant = value; return registration('save'); } });
    let closed = false;
    const close = () => { if (closed) return; closed = true; save.dispose(); renderers.dispose(); kernels.dispose(); };
    p.lifetimes.push(close);
    await kernels.$addKernel(1, { id: 'original-kernel', notebookType: 'original', label: 'Original', extensionId: { value: 'probe.original' },
      extensionLocation: m.URI.file('/window/extensions/original') });
    await kernels.$addKernelDetectionTask(2, 'original'); await kernels.$addKernelSourceActionProvider(3, 4, 'original');
    selected.fire({ newKernel: 'original-kernel', notebook: uri }); message.fire({ message: 'from renderer' });
    const kernelPosted = await kernels.$postMessage(1, undefined, 'from kernel');
    rendererMessage.fire({ editorId: 'original-editor', rendererId: 'original-renderer', message: 'renderer event' });
    const rendererPosted = await renderers.$postMessage('original-editor', 'original-renderer', 'renderer input');
    await participant.participate({ resource: uri, model: {} }, {}, {}, m.CancellationToken.None);
    close();
    return { subscriptions, registrations, forwarded, posted, disposed, kernelPosted, rendererPosted };
  });
  expect(result.subscriptions).toEqual(['editor message', 'editor add', 'editor remove', 'selected', 'renderer message']);
  expect(result.registrations).toEqual(['save', 'kernel', 'detection', 'source']);
  expect(result.forwarded).toEqual([
    { method: '$acceptNotebookAssociation', args: [1, 'file:///window/kernel.ipynb', true] },
    { method: '$acceptKernelMessageFromRenderer', args: [1, 'original-editor', 'from renderer'] },
    { method: '$postRendererMessage', args: ['original-editor', 'original-renderer', 'renderer event'] },
  ]);
  expect([result.kernelPosted, result.rendererPosted]).toEqual([true, true]);
  expect(result.posted).toEqual(['from kernel', { editorId: 'original-editor', rendererId: 'original-renderer', value: 'renderer input' }]);
  expect(result.disposed).toEqual(['save', 'kernel', 'detection', 'source']);
  checks.push('窗口原Kernel/Renderer/SaveParticipant构造订阅、注册/消息转发与全部注册释放保持');
});

test('窗口内置原协调器、文档/编辑器与 ExtHost owner 保持初始、变更和公开保存/展示流程', async () => {
  const result = await inBrowser('窗口原Notebook owner组合', async () => {
    const p = globalThis.notebookProbe, m = p.m, subscriptions = [], effects = [], events = {}, models = [], editors = [], mains = new Map(), hosts = new Map();
    const signal = name => { const emitter = new m.Emitter(); p.lifetimes.push(() => emitter.dispose());
      return { fire: value => emitter.fire(value), event(listener, thisArgs, disposables) { subscriptions.push(name); return emitter.event(listener, thisArgs, disposables); } }; };
    const signals = Object.fromEntries(['addDocument', 'removeDocument', 'addEditor', 'removeEditor', 'active', 'visible', 'dirty', 'saved', 'conflict',
      'removeGroup', 'moveGroup', 'selectedKernel', 'renderer'].map(name => [name, signal(name)]));
    const group = { id: 1 }, groups = { getGroups: () => [group], activeGroup: group, getGroup: () => group,
      onDidRemoveGroup: signals.removeGroup.event, onDidMoveGroup: signals.moveGroup.event };
    const notebookService = { listNotebookDocuments: () => models, onWillAddNotebookDocument: signals.addDocument.event, onDidRemoveNotebookDocument: signals.removeDocument.event,
      registerNotebookSerializer(viewType) { effects.push('serializer:' + viewType); return { dispose() { effects.push('serializer disposed'); } }; } };
    const editorService = { activeEditorPane: undefined, visibleEditorPanes: [], onDidActiveEditorChange: signals.active.event, onDidVisibleEditorsChange: signals.visible.event,
      async openEditor(input) { effects.push('show:' + input.resource.toString()); const editor = editors.find(value => value.textModel.uri.toString() === input.resource.toString());
        this.activeEditorPane = editor.pane; this.visibleEditorPanes = [editor.pane]; signals.active.fire(); signals.visible.fire(); return editor.pane; } };
    const notebookEditors = { listNotebookEditors: () => editors, getNotebookEditor: id => editors.find(editor => editor.getId() === id),
      onDidAddNotebookEditor: signals.addEditor.event, onDidRemoveNotebookEditor: signals.removeEditor.event };
    const addModel = name => {
      const uri = m.URI.file('/window/' + name + '.ipynb'), content = signal('content:' + name), disposal = signal('dispose:' + name);
      const cell = { handle: 1, uri: m.CellUri.generate(uri, 1), textBuffer: { getLinesContent: () => [name], getEOL: () => '\n' },
        language: 'plaintext', cellKind: m.CellKind.Code, outputs: [], metadata: {}, internalMetadata: {} };
      const model = { uri, viewType: 'original', versionId: 1, cells: [cell], metadata: {}, onDidChangeContent: content.event, onWillDispose: disposal.event,
        content, disposal, dirty: false };
      models.push(model); return model;
    };
    const addEditor = (name, model) => {
      const selection = signal('selection:' + name), ranges = signal('ranges:' + name), focus = signal('focus:' + name), change = signal('model:' + name), message = signal('message:' + name);
      const editor = { textModel: model, getId: () => name, hasModel: () => true, getSelections: () => [{ start: 0, end: 1 }], visibleRanges: [{ start: 0, end: 1 }],
        getViewModel: () => ({ viewType: 'original' }), onDidChangeModel: change.event, onDidFocusWidget: focus.event, onDidChangeVisibleRanges: ranges.event,
        onDidChangeSelection: selection.event, onDidReceiveMessage: message.event, selection, ranges,
        setSelections(value) { effects.push('selection:' + value[0].start); }, setFocus() {}, getLength: () => model.cells.length,
        cellAt: index => model.cells[index], revealCellRangeInView(range) { effects.push('reveal:' + range.start); } };
      editor.pane = { getId: () => m.NOTEBOOK_EDITOR_ID, getControl: () => editor, group }; editors.push(editor); return editor;
    };
    const first = addModel('first'), second = addModel('second'), firstEditor = addEditor('first-editor', first), secondEditor = addEditor('second-editor', second);
    editorService.visibleEditorPanes = [firstEditor.pane, secondEditor.pane]; editorService.activeEditorPane = firstEditor.pane;
    const resolver = { onDidChangeDirty: signals.dirty.event, onDidSaveNotebook: signals.saved.event, onWillFailWithConflict: signals.conflict.event,
      isDirty: uri => models.find(model => model.uri.toString() === uri.toString()).dirty,
      async resolve(uri) { const model = models.find(value => value.uri.toString() === uri.toString()); effects.push('resolve:' + uri.toString());
        return { object: { resource: uri, notebook: model, async save() { effects.push('save:' + uri.toString()); signals.saved.fire(uri); return true; } },
          dispose() { effects.push('reference disposed:' + uri.toString()); } }; } };
    const bridge = { getProxy: id => new Proxy({}, { get(_object, method) { return (...args) => mains.get(id.sid)[method](...args); } }) };
    const log = new m.NullLogService(), textOwner = new m.ExtHostDocumentsAndEditors(bridge, log), texts = new m.ExtHostDocuments(bridge, textOwner);
    const commands = { converter: new m.CommandsConverter({ registerCommand: () => ({ dispose() {} }) }, () => undefined, log), registerArgumentProcessor() {}, registerApiCommand() {} };
    const notebooks = new m.ExtHostNotebookController(bridge, commands, textOwner, texts, undefined, undefined, log);
    const documents = new m.ExtHostNotebookDocuments(notebooks), notebookEditorOwner = new m.ExtHostNotebookEditors(log, notebooks);
    hosts.set(m.ExtHostContext.ExtHostNotebook.sid, notebooks); hosts.set(m.ExtHostContext.ExtHostNotebookDocuments.sid, documents); hosts.set(m.ExtHostContext.ExtHostNotebookEditors.sid, notebookEditorOwner);
    const record = (name, project) => value => { (events[name] ??= []).push(project(value)); };
    notebooks.onDidOpenNotebookDocument(record('opened', value => value.uri.toString()));
    documents.onDidChangeNotebookDocument(record('changed', value => ({ uri: value.notebook.uri.toString(), version: value.notebook.version })));
    documents.onDidSaveNotebookDocument(record('saved', value => value.uri.toString()));
    notebooks.onDidChangeActiveNotebookEditor(record('active', value => value?.notebook.uri.toString() ?? null));
    notebooks.onDidChangeVisibleNotebookEditors(record('visible', value => value.map(editor => editor.notebook.uri.toString())));
    notebookEditorOwner.onDidChangeNotebookEditorSelection(record('selection', value => value.notebookEditor.notebook.uri.toString()));
    notebookEditorOwner.onDidChangeNotebookEditorVisibleRanges(record('ranges', value => value.notebookEditor.notebook.uri.toString()));
    const context = { extensionHost: { runningLocation: { kind: 2 } }, getProxy: id => hosts.get(id.sid), set(id, actor) { mains.set(id.sid, actor); } };
    const instantiation = { createInstance(type, ...args) { if (type === m.MainThreadNotebookDocuments) return new type(...args, resolver, { extUri: m.extUri });
      if (type === m.MainThreadNotebookEditors) return new type(...args, editorService, notebookEditors, groups, {}); return new type(...args); } };
    const coordinator = new m.MainThreadNotebooksAndEditors(context, instantiation, notebookService, notebookEditors, editorService, groups, log);
    const original = new m.MainThreadNotebooks(context, notebookService, { registerCellStatusBarItemProvider() { effects.push('statusBar'); return { dispose() {} }; } }, log);
    p.lifetimes.push(() => { original.dispose(); coordinator.dispose(); texts.dispose(); });
    const initial = notebooks.notebookDocuments.map(data => data.apiNotebook.uri.toString()), textInitial = texts.getAllDocumentData().map(data => data.document.uri.toString());
    second.versionId = 2; second.dirty = true; second.content.fire({ versionId: 2, rawEvents: [{ kind: m.NotebookCellsChangeType.ChangeDocumentMetadata }] });
    signals.dirty.fire({ resource: first.uri, isDirty: () => true });
    secondEditor.selection.fire(); secondEditor.ranges.fire();
    editorService.activeEditorPane = secondEditor.pane; signals.active.fire();
    const api = await notebooks.openNotebookDocument(first.uri), saved = await api.save(), shown = await notebooks.showNotebookDocument(api);
    const mainEditor = mains.get(m.MainContext.MainThreadNotebookEditors.sid);
    mainEditor.$trySetSelections('first-editor', [{ start: 0, end: 1 }]); await mainEditor.$tryRevealRange('first-editor', { start: 0, end: 1 }, 0);
    original.$registerNotebookSerializer(1, { id: { value: 'probe.original' } }, 'original', {}, undefined);
    await original.$registerNotebookCellStatusBarItemProvider(2, undefined, 'original');
    const added = addModel('added'); signals.addDocument.fire(added); const afterAdd = notebooks.notebookDocuments.length;
    models.splice(models.indexOf(added), 1); signals.removeDocument.fire(added);
    return { initial, textInitial, afterAdd, afterRemove: notebooks.notebookDocuments.length, events, effects, subscriptions,
      dirty: api.isDirty, version: notebooks.getNotebookDocument(second.uri).apiNotebook.version, saved, shown: shown.notebook === api,
      active: notebooks.activeNotebookEditor === shown, visible: notebooks.visibleNotebookEditors.map(editor => editor.notebook.uri.toString()) };
  });
  expect(result.initial).toEqual(['file:///window/first.ipynb', 'file:///window/second.ipynb']); expect(result.textInitial).toHaveLength(2);
  expect(result.afterAdd).toBe(3); expect(result.afterRemove).toBe(2); expect(result.events.opened).toHaveLength(3);
  expect(result.events.changed).toEqual([{ uri: 'file:///window/second.ipynb', version: 2 }]); expect(result.events.saved).toEqual(['file:///window/first.ipynb']);
  expect(result.events.selection).toEqual(['file:///window/second.ipynb']); expect(result.events.ranges).toEqual(['file:///window/second.ipynb']);
  expect(result.dirty).toBe(true); expect(result.version).toBe(2); expect([result.saved, result.shown, result.active]).toEqual([true, true, true]);
  expect(result.visible).toEqual(['file:///window/first.ipynb']); expect(result.effects).toContain('serializer:original'); expect(result.effects).toContain('statusBar');
  expect(result.effects).toContain('reference disposed:file:///window/first.ipynb'); expect(result.effects).toContain('selection:0'); expect(result.effects).toContain('reveal:0');
  checks.push('窗口原协调器/ExtHost初始全量、增删/content/dirty/save/active/visible/range/selection及公开保存/展示、serializer/statusBar保持');
});
