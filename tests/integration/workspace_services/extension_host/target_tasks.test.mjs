import { afterAll, beforeAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { extensionTasksSourcePaths } from '../../../../src/clients/workbench/scripts/extensions/extensionTasksTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/target_tasks.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'tasks.fixture.mjs'), fingerprints = [], checks = [];
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
for (const runtime of ['browser', 'node']) {
  const convert = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime });
  for (const relative of extensionTasksSourcePaths) {
    const path = join(root, 'upstream', relative), original = await readFile(path, 'utf8'), converted = await convert(path, original);
    const parsed = ts.createSourceFile(path, converted, ts.ScriptTarget.Latest, true);
    if (parsed.parseDiagnostics.length) throw new Error('任务转换源码不能解析：' + relative);
    fingerprints.push({ runtime, path: relative, original: createHash('sha256').update(original).digest('hex'),
      converted: createHash('sha256').update(converted).digest('hex') });
    await writeFile(join(run.artifactsDir, runtime + '__' + relative.replaceAll('/', '__')), converted);
  }
}
for (const relative of ['scripts/sourceTransform.mjs', 'scripts/extensions/extensionTasksTransform.mjs']) {
  fingerprints.push({ path: relative, original: createHash('sha256').update(await readFile(join(root, relative))).digest('hex') });
}
await writeFile(join(run.artifactsDir, 'test-source.mjs'), await readFile(import.meta.path, 'utf8'));
await build({ stdin: { resolveDir: resolve('.'), contents: `
export { MainThreadTask } from '${upstream}workbench/api/browser/mainThreadTask.js';
export { WorkerExtHostTask, ExtHostTaskBase } from '${upstream}workbench/api/common/extHostTask.js';
export { Task, TaskScope, CustomExecution, ProcessExecution } from '${upstream}workbench/api/common/extHostTypes.js';
export { TaskEventKind } from '${upstream}workbench/api/common/shared/tasks.js';
export { ExtensionError } from '${upstream}platform/extensions/common/extensions.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { NotSupportedError } from '${upstream}base/common/errors.js';
` }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });

const apiPath = join(root, 'upstream/src/vs/workbench/api/common/extHost.api.impl.ts');
const apiSource = ts.createSourceFile(apiPath, await transform(apiPath, await readFile(apiPath, 'utf8')), ts.ScriptTarget.Latest, true);
const namespaces = {};
let eventWrapper;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === '_asExtensionEvent') eventWrapper = node.getText(apiSource);
  if (ts.isVariableDeclaration(node) && ['tasks', 'workspace'].includes(node.name.getText(apiSource)) && ts.isObjectLiteralExpression(node.initializer)) {
    // 提取原公开 API 函数体，废弃入口也保留同一原调用链。
    const members = node.initializer.properties.filter(member => node.name.getText(apiSource) === 'tasks' || member.name?.getText(apiSource) === 'registerTaskProvider');
    namespaces[node.name.getText(apiSource)] = `function originalApi() { return ({${members.map(member => member.getText(apiSource)).join(',')}}); }`;
  }
  ts.forEachChild(node, visit);
}
visit(apiSource);
if (!eventWrapper || !namespaces.tasks || !namespaces.workspace) throw new Error('原任务公开 API 或事件包装器缺失');
for (const name of Object.keys(namespaces)) namespaces[name] = ts.transpileModule(eventWrapper + '\n' + namespaces[name],
  { compilerOptions: { target: ts.ScriptTarget.ES2024, module: ts.ModuleKind.ESNext } }).outputText;

let server, browser;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch: request => new URL(request.url).pathname === '/fixture.mjs'
    ? new Response(Bun.file(bundle), { headers: { 'Content-Type': 'text/javascript' } }) : new Response('<!doctype html><title>目标任务边界</title>') });
  run.addCleanup('组件 HTTP', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'receipt.json'), JSON.stringify({ fingerprints, checks,
    boundary: '真实 Chromium 运行转换后的 MainThreadTask、WorkerExtHostTask／Base、Task／CustomExecution 和原公开 tasks／废弃 workspace 入口及事件包装器；窗口任务、终端和配置解析服务为显式调用记录边界。Browser／Node 四文件均经唯一转换并保存指纹，但此组不执行 Node 宿主、真实目标进程／任务、Gateway 或完整 Workbench。' }, null, 2)); }
});

async function scenario(body) {
  const page = await browser.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`http://127.0.0.1:${server.port}`);
    const result = await page.evaluate(body, namespaces);
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 0)));
    expect(errors).toEqual([]);
    checks.push(result);
    return result;
  } finally { await page.close(); }
}

test('目标 MainThread 不订阅窗口任务，全部 RPC 在窗口服务及任务对象创建前拒绝', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const forbidden = new Proxy({}, { get(_target, key) { calls.push(String(key)); throw new Error('访问了窗口任务：' + String(key)); } });
    for (const kind of ['web', 'node']) {
      const owner = new m.MainThreadTask({ extensionHost: { targetContext: { kind } }, getProxy: () => forbidden }, forbidden, forbidden, forbidden);
      for (const name of Object.getOwnPropertyNames(m.MainThreadTask.prototype).filter(name => name.startsWith('$'))) {
        try { await owner[name]('window-id', {}); failures.push({ kind, name, succeeded: true }); }
        catch (error) { failures.push({ kind, name, unsupported: error instanceof m.NotSupportedError, message: error.message }); }
      }
      owner.dispose();
    }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]);
  expect(result.failures).toHaveLength(20);
  expect(result.failures.every(value => value.unsupported && value.message.includes('目标任务执行服务'))).toBe(true);
});

test('目标 Worker bootstrap 零能力及系统登记，内部入口拒绝且不创建执行或回调状态', async () => {
  const result = await scenario(async () => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const rpc = { getProxy: () => new Proxy({}, { get(_target, key) { return () => {
      calls.push(String(key)); return Promise.reject(new Error('目标不应发起窗口任务 RPC')); }; } }) };
    const forbidden = new Proxy({}, { get(_target, key) { calls.push(String(key)); throw new Error('读取了执行边界'); } });
    const service = new m.WorkerExtHostTask(rpc, { isTargetExtensionHost: true }, forbidden, forbidden, forbidden, forbidden, forbidden, forbidden);
    const names = new Set([...Object.getOwnPropertyNames(m.ExtHostTaskBase.prototype), ...Object.getOwnPropertyNames(m.WorkerExtHostTask.prototype)]);
    for (const name of names) {
      const descriptor = Object.getOwnPropertyDescriptor(m.WorkerExtHostTask.prototype, name) ?? Object.getOwnPropertyDescriptor(m.ExtHostTaskBase.prototype, name);
      if (!name.startsWith('$') && !descriptor.get && !['registerTaskProvider', 'registerTaskSystem', 'fetchTasks', 'executeTask',
        'terminateTask', 'getTaskExecution', 'addCustomExecution'].includes(name)) continue;
      try { const value = service[name]; if (typeof value === 'function') await value.call(service, {}, {}); failures.push({ name, succeeded: true }); }
      catch (error) { failures.push({ name, unsupported: error instanceof m.NotSupportedError }); }
    }
    await new Promise(resolve => setTimeout(resolve, 0));
    const sizes = ['_handlers', '_taskExecutions', '_taskExecutionPromises', '_providedCustomExecutions2', '_activeCustomExecutions2']
      .map(name => service[name].size);
    return { calls, failures, sizes };
  });
  expect(result.calls).toEqual([]);
  expect(result.failures.length).toBeGreaterThan(20);
  expect(result.failures.every(value => value.unsupported)).toBe(true);
  expect(result.sizes).toEqual([0, 0, 0, 0, 0]);
});

test('公开任务与废弃 Provider API 在服务、proposal 检查及用户回调前明确拒绝', async () => {
  const result = await scenario(async namespaces => {
    const m = await import('/fixture.mjs'), calls = [], failures = [];
    const forbidden = new Proxy({}, { get(_target, key) { calls.push(String(key)); throw new Error('访问了窗口任务 API'); } });
    for (const source of Object.values(namespaces)) {
      const api = new Function('initData', 'errors', 'extHostTask', 'checkProposedApiEnabled', 'isProposedApiEnabled', 'extHostApiDeprecation',
        'extension', 'ExtensionError', source + '\nreturn originalApi();')({ isTargetExtensionHost: true }, { NotSupportedError: m.NotSupportedError },
        forbidden, () => calls.push('proposal'), () => calls.push('proposal'), { report: () => calls.push('deprecation') }, {}, m.ExtensionError);
      for (const name of Reflect.ownKeys(api)) {
        try { const value = api[name]; if (typeof value === 'function') value(() => calls.push('callback')); failures.push({ name, succeeded: true }); }
        catch (error) { failures.push({ name, unsupported: error instanceof m.NotSupportedError }); }
      }
    }
    return { calls, failures };
  });
  expect(result.calls).toEqual([]);
  expect(result.failures).toHaveLength(11);
  expect(result.failures.every(value => value.unsupported)).toBe(true);
});

test('窗口内置 Worker 保留原能力、任务系统、Provider、执行对象、查询与完整任务事件', async () => {
  const result = await scenario(async namespaces => {
    const m = await import('/fixture.mjs'), calls = [], state = new m.Emitter(), events = [], terminal = { name: 'window' };
    let worker, running;
    const taskService = {
      onDidStateChange: state.event,
      registerSupportedExecutions: (...args) => { calls.push(['support', ...args]); return Promise.resolve(); },
      registerTaskSystem: (scheme, info) => calls.push(['system', scheme, info.platform, info.context]),
      registerTaskProvider: (_provider, type) => { calls.push(['provider', type]); return { dispose: () => calls.push(['unregister', type]) }; },
      run: task => { running = task; calls.push(['run', task.configurationProperties.name]); return Promise.resolve({ exitCode: 0 }); },
      tasks: async () => [running], getActiveTasks: async () => [running],
      terminate: async task => { calls.push(['terminate', task._id]); },
    };
    const main = new m.MainThreadTask({ extensionHost: {}, getProxy: () => new Proxy({}, {
      get(_target, key) { return (...args) => worker[key](...args); } }) }, taskService, { getWorkspace: () => ({ folders: [] }) }, {});
    const rpc = { getProxy: () => new Proxy({}, { get(_target, key) { return (...args) => main[key](...args); } }) };
    worker = new m.WorkerExtHostTask(rpc, { remote: {} }, {}, {}, {}, {
      attachPtyToTerminal: id => calls.push(['pty', id]), getTerminalById: () => ({ value: terminal }),
    }, {}, { report() {} });
    const extension = { identifier: { value: 'probe.window-tasks' } };
    const api = new Function('initData', 'errors', 'extHostTask', 'checkProposedApiEnabled', 'isProposedApiEnabled', 'extHostApiDeprecation',
      'extension', 'ExtensionError', namespaces.tasks + '\nreturn originalApi();')({}, {}, worker, () => {}, () => false, {}, extension, m.ExtensionError);
    const legacy = new Function('initData', 'errors', 'extHostTask', 'extHostApiDeprecation', 'extension', namespaces.workspace + '\nreturn originalApi();')(
      {}, {}, worker, { report: name => calls.push(['deprecation', name]) }, extension);
    const disposables = [];
    for (const name of Reflect.ownKeys(api).filter(name => name.startsWith('onDid'))) {
      api[name](event => events.push([name, event.execution.task.name, event.execution.terminal?.name]), undefined, disposables);
    }
    const custom = new m.CustomExecution(async () => { calls.push(['custom']); return {}; });
    const task = new m.Task({ type: 'probe' }, m.TaskScope.Global, 'window-task', 'Window', custom);
    const provider = api.registerTaskProvider('probe', { provideTasks: () => [task] });
    const oldProvider = legacy.registerTaskProvider('legacy', { provideTasks: () => [task] });
    const execution = await api.executeTask(task);
    await Promise.resolve();
    const sameExecution = api.taskExecutions[0] === execution, fetched = await api.fetchTasks();
    execution.terminate();
    for (const kind of [m.TaskEventKind.Start, m.TaskEventKind.ProcessStarted, m.TaskEventKind.ProcessEnded,
      m.TaskEventKind.ProblemMatcherStarted, m.TaskEventKind.ProblemMatcherEnded, m.TaskEventKind.End]) {
      state.fire({ kind, __task: running, terminalId: 7, processId: 42, exitCode: 0 });
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    const activeAfterEnd = api.taskExecutions.length;
    provider.dispose(); oldProvider.dispose(); disposables.forEach(value => value.dispose()); main.dispose();
    const subscribedAfterDispose = state.hasListeners();
    state.dispose();
    return { calls, events, sameExecution, activeAfterEnd, fetched: fetched.map(task => task.name), subscribedAfterDispose };
  });
  expect(result.calls.filter(([name]) => name === 'support')).toEqual([['support', true]]);
  expect(result.calls.filter(([name]) => name === 'system')).toEqual([['system', 'vscode-remote', 0, undefined]]);
  expect(result.calls.filter(([name]) => name === 'deprecation')).toEqual([['deprecation', 'window.registerTaskProvider']]);
  expect(result.calls.some(([name]) => name === 'run')).toBe(true);
  expect(result.calls.filter(([name]) => name === 'terminate')).toHaveLength(1);
  expect(result.calls.filter(([name]) => name === 'custom')).toHaveLength(1);
  expect(result.calls.filter(([name]) => name === 'pty')).toEqual([['pty', 7]]);
  expect(result.calls.some(([name]) => name === 'unregister')).toBe(true);
  expect(result.events.map(([name]) => name)).toEqual(['onDidStartTask', 'onDidStartTaskProcess', 'onDidEndTaskProcess',
    'onDidStartTaskProblemMatchers', 'onDidEndTaskProblemMatchers', 'onDidEndTask']);
  expect(result.events.every(([, name, terminal]) => name === 'window-task' && terminal === undefined)).toBe(true);
  expect(result.sameExecution).toBe(true);
  expect(result.activeAfterEnd).toBe(0);
  expect(result.fetched).toEqual(['window-task']);
  expect(result.subscribedAfterDispose).toBe(false);
});
