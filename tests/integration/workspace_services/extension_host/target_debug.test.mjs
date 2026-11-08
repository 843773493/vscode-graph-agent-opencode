import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/target_debug.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'debug.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []), fingerprints = [], checks = [];
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadDebugService } from '${upstream}workbench/api/browser/mainThreadDebugService.js';
export { WorkerExtHostDebugService } from '${upstream}workbench/api/common/extHostDebugService.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { NotSupportedError } from '${upstream}base/common/errors.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });

async function converted(relative) {
  const path = join(root, 'upstream', relative), original = await readFile(path, 'utf8'), source = await transform(path, original);
  fingerprints.push({ path, original: createHash('sha256').update(original).digest('hex'), converted: createHash('sha256').update(source).digest('hex') });
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  if (parsed.parseDiagnostics.length) throw new Error('转换源码不能解析：' + relative);
  return parsed;
}
const api = await converted('src/vs/workbench/api/common/extHost.api.impl.ts');
let publicApi;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(api) === 'debug' && ts.isObjectLiteralExpression(node.initializer)) {
    // 提取实际公开成员，服务依赖显式注入，不复制另一份 API 实现。
    publicApi = ts.transpileModule(`function originalApi() { return (${node.initializer.getText(api)}); }`,
      { compilerOptions: { target: ts.ScriptTarget.ES2024 } }).outputText;
  }
  ts.forEachChild(node, visit);
}
visit(api);
if (!publicApi) throw new Error('未找到原公开调试 API');
const host = await converted('src/vs/workbench/api/common/extHostDebugService.ts');
const owner = host.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtHostDebugServiceBase');
const hostMembers = owner.members.filter(member => member.body && member.name && member.name.getText(host) !== 'dispose'
  && !member.modifiers?.some(modifier => [ts.SyntaxKind.PrivateKeyword, ts.SyntaxKind.ProtectedKeyword].includes(modifier.kind)))
  .map(member => ({ name: member.name.getText(host), getter: ts.isGetAccessorDeclaration(member) }));
const nodeHost = await converted('src/vs/workbench/api/node/extHostDebugService.ts');
const nodeOwner = nodeHost.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtHostDebugService');
const nodeRun = nodeOwner.members.find(member => member.name?.getText(nodeHost) === '$runInTerminal');
const nodeMethod = ts.transpileModule(`function originalApi() { return ({${nodeRun.getText(nodeHost).replace('public override ', '')}}); }`,
  { compilerOptions: { target: ts.ScriptTarget.ES2024 } }).outputText;
let server, browser;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => new URL(request.url).pathname === '/fixture.mjs'
    ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } }) : new Response('<!doctype html><title>目标调试边界</title>') });
  run.addCleanup('组件 HTTP', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, checks,
    boundary: '真实 Chromium 运行转换后的原 MainThread／Worker 调试类和原公开 API；Node runInTerminal 仅执行从实际转换源码提取的原方法，Node 初始化及公开 API 另由真实进程 probe 验证。窗口调试服务、RPC 与扩展 registry 为具名装具，不代表完整 Workbench、适配器或目标调试已经接通。' }, null, 2)); }
});
async function scenario(body) {
  const page = await browser.newPage();
  try { await page.goto(`http://127.0.0.1:${server.port}`); return await page.evaluate(body, { publicApi, hostMembers, nodeMethod }); }
  finally { await page.close(); }
}

test('目标 MainThread 零窗口订阅，所有调试 RPC 和适配器入口拒绝', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const forbidden = new Proxy({}, { get(_object, name) { calls.push(String(name)); throw new Error('触及窗口调试服务'); } });
    for (const kind of ['web', 'node']) {
      const main = new m.MainThreadDebugService({ extensionHost: { targetContext: { kind } }, getProxy: () => forbidden }, forbidden, forbidden);
      const names = Object.getOwnPropertyNames(Object.getPrototypeOf(main)).filter(name => name.startsWith('$')
        || ['createDebugAdapter', 'substituteVariables', 'runInTerminal'].includes(name));
      for (const name of names) {
        try { await main[name](forbidden, forbidden, forbidden); failures.push({ kind, name, succeeded: true }); }
        catch (error) { failures.push({ kind, name, unsupported: error instanceof m.NotSupportedError }); }
      }
      main.dispose();
    }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]); expect(result.failures.length).toBeGreaterThan(40);
  expect(result.failures.every(failure => failure.unsupported)).toBe(true); checks.push(result);
});

test('目标 Worker 不注册调试类型，事件、回包及内部启动均在访问依赖前拒绝', async () => {
  const result = await scenario(async ({ hostMembers, nodeMethod }) => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const forbidden = new Proxy({}, { get(_object, name) { calls.push(String(name)); throw new Error('触及未授权依赖'); } });
    const rpc = { getProxy: () => new Proxy({}, { get(_object, name) { return (...args) => calls.push([name, args]); } }) };
    const host = new m.WorkerExtHostDebugService(rpc, forbidden, forbidden, forbidden, forbidden, forbidden, forbidden, forbidden,
      { isTargetExtensionHost: true });
    for (const member of hostMembers) {
      try { const value = host[member.name]; if (!member.getter) await value.call(host, forbidden, forbidden, forbidden); failures.push({ name: member.name, succeeded: true }); }
      catch (error) { failures.push({ name: member.name, unsupported: error instanceof m.NotSupportedError }); }
    }
    // Node override 必须在 integrated／external 分流前拒绝，不由 Worker 基类检查替代。
    const node = new Function('NotSupportedError', nodeMethod + '\nreturn originalApi();')(m.NotSupportedError);
    for (const args of [{ kind: 'integrated' }, { kind: 'external' }, forbidden]) {
      try { await node.$runInTerminal.call(host, args, 'window-session'); failures.push({ name: 'node', succeeded: true }); }
      catch (error) { failures.push({ name: 'node', unsupported: error instanceof m.NotSupportedError }); }
    }
    await Promise.resolve(); host.dispose();
    return { calls, failures };
  });
  expect(result.calls).toEqual([]); expect(result.failures.length).toBeGreaterThan(40);
  expect(result.failures.every(failure => failure.unsupported)).toBe(true); checks.push(result);
});

test('公开 debug 全部成员在提案检查、回调和原服务前拒绝', async () => {
  const result = await scenario(async ({ publicApi }) => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const service = new Proxy({}, { get(_object, name) { calls.push(String(name)); throw new Error('触及窗口服务'); } });
    const api = new Function('initData', 'errors', 'extHostDebugService', publicApi + '\nreturn originalApi();')(
      { isTargetExtensionHost: true }, { NotSupportedError: m.NotSupportedError }, service);
    for (const name of Reflect.ownKeys(api)) {
      try { const value = api[name]; if (typeof value === 'function') await value(); failures.push({ name, succeeded: true }); }
      catch (error) { failures.push({ name, unsupported: error instanceof m.NotSupportedError }); }
    }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]); expect(result.failures).toHaveLength(20);
  expect(result.failures.every(failure => failure.unsupported)).toBe(true); checks.push(result);
});

test('窗口内置宿主保留调试类型、Provider、启动停止和会话通知', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], started = new m.Emitter();
    const rpc = { getProxy: () => new Proxy({}, { get(_object, name) { return (...args) => { calls.push([name, ...args]); return Promise.resolve(true); }; } }) };
    const registry = { onDidChange: m.Event.None, getAllExtensionDescriptions: () => [] };
    const host = new m.WorkerExtHostDebugService(rpc, {}, { getExtensionRegistry: async () => registry }, {}, {}, {}, {}, {}, {});
    const registration = host.registerDebugConfigurationProvider('window', { provideDebugConfigurations: () => [] }, 1);
    const success = await host.startDebugging(undefined, 'window', {}); await host.stopDebugging(); registration.dispose();
    const model = { onDidChangeBreakpoints: m.Event.None, getBreakpoints: () => [], getFunctionBreakpoints: () => [], getDataBreakpoints: () => [], getSessions: () => [] };
    const viewModel = { onDidFocusSession: m.Event.None, onDidFocusStackFrame: m.Event.None, onDidFocusThread: m.Event.None };
    const debug = { onDidNewSession: started.event, onWillNewSession: m.Event.None, onDidEndSession: m.Event.None,
      getModel: () => model, getViewModel: () => viewModel,
      getAdapterManager: () => ({ registerDebugAdapterFactory: types => { calls.push(['factory', types]); return { dispose() {} }; } }) };
    const main = new m.MainThreadDebugService({ extensionHost: {}, getProxy: rpc.getProxy }, debug, {});
    main.$registerDebugTypes(['window']); started.fire({ getId: () => 'window-session', configuration: { type: 'window' }, name: 'Window' });
    await Promise.resolve(); main.dispose(); host.dispose(); started.dispose();
    return { calls, success };
  });
  expect(result.success).toBe(true);
  for (const name of ['$registerDebugTypes', '$registerDebugConfigurationProvider', '$unregisterDebugConfigurationProvider', '$startDebugging', '$stopDebugging', 'factory', '$acceptDebugSessionStarted']) {
    expect(result.calls.some(([actual]) => actual === name)).toBe(true);
  }
  expect(result.calls.find(([name]) => name === '$acceptDebugSessionStarted')[1].id).toBe('window-session'); checks.push(result);
});
