import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/target_terminal.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'terminal.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []), fingerprints = [], checks = [];
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadTerminalService } from '${upstream}workbench/api/browser/mainThreadTerminalService.js';
export { MainThreadTerminalShellIntegration } from '${upstream}workbench/api/browser/mainThreadTerminalShellIntegration.js';
export { ExtHostTerminalService } from '${upstream}workbench/api/node/extHostTerminalService.js';
export { WorkerExtHostTerminalService } from '${upstream}workbench/api/common/extHostTerminalService.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { NotSupportedError } from '${upstream}base/common/errors.js';
export { URI } from '${upstream}base/common/uri.js';
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
const namespaces = {};
function visit(node) {
  if (ts.isVariableDeclaration(node) && ['window', 'env'].includes(node.name.getText(api)) && ts.isObjectLiteralExpression(node.initializer)) {
    // 只选择原终端成员；不复制函数体，也不装配其它 API 的无关服务。
    const members = node.initializer.properties.filter(member => /Terminal|^terminals$|^createTerminal$|^shell$|^onDidChangeShell$/.test(member.name?.getText(api)));
    namespaces[node.name.getText(api)] = ts.transpileModule(`function originalApi() { return ({${members.map(member => member.getText(api)).join(',')}}); }`,
      { compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.ESNext } }).outputText;
  }
  ts.forEachChild(node, visit);
}
visit(api);
const context = await converted('src/vs/workbench/api/common/extHostExtensionService.ts');
let collection;
function contextVisit(node) {
  if (ts.isGetAccessorDeclaration(node) && node.name.getText(context) === 'environmentVariableCollection') collection = node.getText(context);
  ts.forEachChild(node, contextVisit);
}
contextVisit(context);
const contextGetter = ts.transpileModule(`function originalApi() { return ({${collection}}); }`, { compilerOptions: { target: ts.ScriptTarget.ES2024 } }).outputText;
let server, browser;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => new URL(request.url).pathname === '/fixture.mjs'
    ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } }) : new Response('<!doctype html><title>目标终端边界</title>') });
  run.addCleanup('组件 HTTP', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, checks,
    boundary: '真实转换 MainThreadTerminalService／ShellIntegration、Node／Worker 终端类及原公开 API 成员；窗口终端与外部服务为显式调用记录装具。Chromium 运行真实类，不代替完整 Gateway、实际 PTY、任务或调试验收。' }, null, 2)); }
});

async function scenario(body) {
  const page = await browser.newPage();
  try { await page.goto(`http://127.0.0.1:${server.port}`); return await page.evaluate(body, { namespaces, contextGetter }); }
  finally { await page.close(); }
}

test('目标 MainThread 不读取或订阅窗口终端，所有 RPC 拒绝猜 ID 控制和环境写入', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const forbidden = new Proxy({}, { get(_target, key) { calls.push(String(key)); throw new Error('访问了窗口终端：' + String(key)); } });
    for (const kind of ['node', 'web']) {
      const context = { extensionHost: { targetContext: { kind } }, getProxy: () => forbidden };
      const main = new m.MainThreadTerminalService(context, ...Array(13).fill(forbidden));
      const integration = new m.MainThreadTerminalShellIntegration(context, forbidden, forbidden, forbidden);
      for (const owner of [main, integration]) {
        for (const name of Object.getOwnPropertyNames(Object.getPrototypeOf(owner)).filter(name => name.startsWith('$'))) {
          try { await owner[name](17, 'wrong-window'); failures.push({ kind, name, succeeded: true }); }
          catch (error) { failures.push({ kind, name, unsupported: error instanceof m.NotSupportedError, message: error.message }); }
        }
        owner.dispose();
      }
    }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]); expect(result.failures.length).toBeGreaterThan(40);
  expect(result.failures.every(failure => failure.unsupported && failure.message.includes('目标终端服务'))).toBe(true);
  checks.push(result);
});

test('真实 Node／Worker 终端类不发布目标进程能力，内部和公开创建均先拒绝', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const rpc = { getProxy: () => new Proxy({}, { get(_target, key) { return (...args) => calls.push([key, args]); } }) };
    const commands = { registerArgumentProcessor() {} };
    for (const Type of [m.ExtHostTerminalService, m.WorkerExtHostTerminalService]) {
      const service = new Type(commands, rpc, { isTargetExtensionHost: true, remote: { authority: 'target' } });
      for (const operation of [() => service.createTerminal('target'), () => service.createTerminalFromOptions({ name: 'debug-internal' }),
        () => service.createExtensionTerminal({ name: 'pty', pty: {} })]) {
        try { operation(); failures.push(false); } catch (error) { failures.push(error instanceof m.NotSupportedError); }
      }
      if (service.terminals.length) throw new Error('拒绝前已创建本地终端对象');
      service.dispose();
    }
    const normal = new m.ExtHostTerminalService(commands, rpc, { remote: {} });
    const terminal = normal.createTerminal('normal'); normal.dispose();
    return { calls, failures, terminalName: terminal.name };
  });
  expect(result.failures).toEqual(Array(6).fill(true)); expect(result.terminalName).toBe('normal');
  expect(result.calls.map(([name]) => name)).toEqual(['$registerProcessSupport', '$createTerminal']);
  checks.push(result);
});

test('公开终端 API、shell 与 context 环境集合在本地服务及回调前明确拒绝', async () => {
  const result = await scenario(async ({ namespaces, contextGetter }) => {
    const m = await import('/fixture.mjs'), calls = [], errors = { NotSupportedError: m.NotSupportedError };
    const service = new Proxy({}, { get(_target, key) { calls.push(String(key)); throw new Error('访问了本地服务'); } });
    const initData = { isTargetExtensionHost: true }, failures = [];
    for (const source of Object.values(namespaces)) {
      const api = new Function('initData', 'errors', 'extHostTerminalService', 'extHostTerminalShellIntegration',
        source + '\nreturn originalApi();')(initData, errors, service, service);
      for (const key of Reflect.ownKeys(api)) {
        try { const value = api[key]; if (typeof value === 'function') value(); failures.push({ key, succeeded: true }); }
        catch (error) { failures.push({ key, unsupported: error instanceof m.NotSupportedError }); }
      }
    }
    const get = target => new Function('that', 'extensionDescription', 'errors', contextGetter + '\nreturn originalApi();')(
      { _initData: { isTargetExtensionHost: target }, _extHostTerminalService: service }, {}, errors);
    try { get(true).environmentVariableCollection; failures.push({ key: 'context', succeeded: true }); }
    catch (error) { failures.push({ key: 'context', unsupported: error instanceof m.NotSupportedError }); }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]); expect(result.failures).toHaveLength(20);
  expect(result.failures.every(failure => failure.unsupported)).toBe(true); checks.push(result);
});

test('窗口内置宿主继续获得原终端初始化、事件、进程能力及环境写入', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], created = new m.Emitter();
    const terminal = { instanceId: 17, title: 'window', cols: 80, rows: 24, shellLaunchConfig: { env: { WINDOW: 'A' } },
      processReady: Promise.resolve(), processId: 100, shellType: undefined };
    const terminalService = new Proxy({ instances: [terminal], onDidCreateInstance: created.event,
      registerProcessSupport: value => calls.push(['process', value]) }, { get(object, key) { return key in object ? object[key] : m.Event.None; } });
    const context = { extensionHost: {}, remoteAuthority: null, getProxy: () => new Proxy({}, {
      get(_target, key) { return (...args) => calls.push([key, ...args]); } }) };
    const env = { collections: new Map(), set: (...args) => calls.push(['env', ...args]) };
    const main = new m.MainThreadTerminalService(context, terminalService, {}, {}, {}, env, {}, { getDefaultProfile: () => ({ path: '/window/sh' }) },
      { getEnvironment: async () => undefined }, {}, {}, { onDidChangeAvailableProfiles: m.Event.None }, {}, {});
    main.$registerProcessSupport(true);
    main.$setEnvironmentVariableCollection('window.extension', true, [['VAR', { type: 1, value: 'window' }]], undefined);
    created.fire({ ...terminal, instanceId: 18 });
    await Promise.resolve(); await Promise.resolve(); main.dispose(); created.dispose();
    return calls;
  });
  expect(result.filter(([name]) => name === '$acceptTerminalOpened').map(([, id]) => id)).toEqual([17, 18]);
  expect(result.some(([name]) => name === 'env')).toBe(true); expect(result.some(([name, value]) => name === 'process' && value === true)).toBe(true);
  checks.push(result);
});
