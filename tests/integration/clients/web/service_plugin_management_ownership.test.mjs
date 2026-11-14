import { t } from '../../../../src/clients/shared/core/i18n/index.js';
import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { HttpRequestError as GatewayRequestError } from '../../../../src/clients/web/src/api/http.js';
import { CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { Action } from '../../../../src/clients/workbench/upstream/src/vs/base/common/actions.js';
import { Disposable, toDisposable } from '../../../../src/clients/workbench/upstream/src/vs/base/common/lifecycle.js';
import { Emitter } from '../../../../src/clients/workbench/upstream/src/vs/base/common/event.js';
import { DeferredPromise, raceTimeout } from '../../../../src/clients/workbench/upstream/src/vs/base/common/async.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

// 提取的真实声明仍使用产品翻译入口，显式注入同一测试启动语言。
const localizedFunction = (...parts) => new Function('t', 'pluginStateLabel', ...parts).bind(undefined, t, pluginStateLabel);

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/service_plugin_management_ownership.test.mjs').prepare();
const sourcePath = 'src/clients/workbench/src/extensions/servicePluginManagement.ts';
const source = await readFile(sourcePath, 'utf8');
const scopePath = 'src/clients/workbench/src/extensions/pluginScope.ts';
const scopeSource = await readFile(scopePath, 'utf8');
const editorPath = 'src/clients/workbench/src/extensions/pluginEditor.ts';
const editorSource = await readFile(editorPath, 'utf8');
const sourceFingerprint = createHash('sha256').update(source).update(scopeSource).update(editorSource).digest('hex');
const managementTargetPath = 'src/clients/workbench/src/managementTarget.ts';
const managementTargetSource = await readFile(managementTargetPath, 'utf8');
const configurationPath = 'src/clients/workbench/src/settings/pluginConfiguration.ts';
const configurationSource = await readFile(configurationPath, 'utf8');
const mcpCommandPath = 'src/clients/workbench/src/mcp/commands.ts';
const mcpCommandSource = await readFile(mcpCommandPath, 'utf8');

function declaration(name) {
  const scopeOwner = ['pluginStateLabel', 'pluginActions', 'PluginScope'].includes(name);
  const parsed = ts.createSourceFile(scopeOwner ? scopePath : name === 'readPluginInstanceLog' ? editorPath : sourcePath, scopeOwner ? scopeSource : name === 'readPluginInstanceLog' ? editorSource : source, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name
    || (ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name)));
  if (!node) throw new Error(`实际源码缺少 ${sourcePath}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

const pluginStateLabel = new Function('t', `${declaration('pluginStateLabel')}\nreturn pluginStateLabel;`)(t);

test('插件已知运行态在展示时翻译，未知服务状态保留原文', () => {
  expect(['stopped', 'starting', 'ready', 'stopping', 'failed', 'waiting'].map(pluginStateLabel))
    .toEqual(['已停止', '正在启动', '已就绪', '正在停止', '失败', '等待中']);
  expect(pluginStateLabel('server-specific-state {0}')).toBe('server-specific-state {0}');
});

function managementDeclaration(name) {
  const parsed = ts.createSourceFile(managementTargetPath, managementTargetSource, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name);
  if (!node) throw new Error(`实际源码缺少 ${managementTargetPath}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

function configurationDeclaration(name) {
  const parsed = ts.createSourceFile(configurationPath, configurationSource, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name);
  if (!node) throw new Error(`实际源码缺少 ${configurationPath}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

function managementLifecycleHarness(state) {
  const workspaceContext = {
    getSnapshot: () => ({ gatewayId: state.gatewayId }),
    subscribe(listener) { state.context.add(listener); return () => state.context.delete(listener); },
  };
  const onGatewayTokenInvalidated = (_port, listener) => {
    state.auth.add(listener);
    return () => state.auth.delete(listener);
  };
  const create = localizedFunction('CancellationTokenSource', 'workspaceContext', 'onGatewayTokenInvalidated',
    `${managementDeclaration('createManagementTargetLifecycle')}\nreturn createManagementTargetLifecycle;`)(
    CancellationTokenSource, workspaceContext, onGatewayTokenInvalidated);
  return {
    create,
    invalidateGateway(gatewayId) {
      state.gatewayId = gatewayId;
      for (const listener of state.context) listener();
    },
    invalidateCredentials() { for (const listener of state.auth) listener(); },
  };
}

function createUninstaller(request, notifications = { info() {} }) {
  const gateway = { request };
  return localizedFunction('gateway', 'pluginGatewayLabel',
    `${declaration('uninstallPackageFromView')}\nreturn uninstallPackageFromView;`)(
    gateway, target => `原生 Gateway ${target.gateway_id}${target.gateway_connection_id ? `（连接 ${target.gateway_connection_id}）` : ''}`);
}

function classCallback(className, methodName, sourceFragment, bindings = {}) {
  const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(statement => statement.name?.text === className);
  const method = owner?.members.find(member => ts.isMethodDeclaration(member) && member.name?.getText(parsed) === methodName);
  let callback;
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'addEventListener' && node.getText(parsed).includes(sourceFragment)) callback = node.arguments[1];
    if (ts.isNewExpression(node) && node.expression.getText(parsed) === 'Action' && node.getText(parsed).includes(sourceFragment)) callback = node.arguments.at(-1);
    ts.forEachChild(node, visit);
  }
  if (method) visit(method.body);
  if (!callback || !ts.isArrowFunction(callback)) throw new Error(`实际源码缺少 ${sourcePath}#${className}.${methodName} 的 ${sourceFragment} 事件`);
  const body = ts.isBlock(callback.body) ? callback.body : ts.factory.createBlock([
    ts.factory.createReturnStatement(callback.body),
  ], true);
  const functionNode = ts.factory.createFunctionExpression(callback.modifiers, undefined, undefined, undefined,
    callback.parameters, callback.type, body);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, functionNode, parsed);
  const javascript = ts.transpile(`const callback = ${printed};`, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  const names = ['errorsText', ...Object.keys(bindings)];
  return localizedFunction(...names, `${javascript}\nreturn function(...args) { return callback.call(this, ...args); };`)(
    error => error instanceof Error ? error.message : String(error), ...Object.values(bindings));
}

function createInstallCommand(request, picks, inputs, observed, options = {}) {
  const gateway = { async request(path, requestOptions = {}) { return request(path, JSON.parse(requestOptions.body ?? '{}'), requestOptions); } };
  const requireOwner = localizedFunction(`${declaration('requireInstallationOwner')}\nreturn requireInstallationOwner;`)();
  const read = localizedFunction('gateway', 'requireInstallationOwner',
    `${declaration('readInstallation')}\nreturn readInstallation;`)(gateway, requireOwner);
  const activePluginInstallations = options.activePluginInstallations ?? new Map();
  const installationKey = localizedFunction(`${declaration('installationKey')}\nreturn installationKey;`)();
  const installationFailureText = localizedFunction(`${declaration('installationFailureText')}\nreturn installationFailureText;`)();
  const runPluginInstallation = localizedFunction('activePluginInstallations', 'installationKey',
    `${declaration('runPluginInstallation')}\nreturn runPluginInstallation;`)(activePluginInstallations, installationKey);
  const pluginGatewayLabel = localizedFunction(`${declaration('pluginGatewayLabel')}\nreturn pluginGatewayLabel;`)();
  const notifications = options.notifications ?? { info() {}, error() {} };
  const observe = localizedFunction('gateway', 'timeout', 'ProgressLocation', 'DEFAULT_API_REQUEST_TIMEOUT_MS', 'readInstallation', 'errorsText', 'installationFailureText', 'pluginGatewayLabel',
    `${declaration('observeInstallation')}\nreturn observeInstallation;`)(gateway, async () => {}, { Notification: 1 }, 120_000, read,
    error => error instanceof Error ? error.message : String(error), installationFailureText, pluginGatewayLabel);
  const observeInstallation = options.observeInstallation
    ?? (async (progress, notificationService, target, operationId, result) => {
      observed.push({ operationId, operation: result });
      return observe(progress, notificationService, target, operationId, result);
    });
  const lifecycle = options.lifecycle ?? { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
  const context = { getSnapshot: () => ({ gatewayId: lifecycle.gatewayId }),
    subscribe(listener) { lifecycle.context.add(listener); return () => lifecycle.context.delete(listener); } };
  const createManagementTargetLifecycle = localizedFunction('CancellationTokenSource', 'workspaceContext', 'onGatewayTokenInvalidated',
    `${managementDeclaration('createManagementTargetLifecycle')}\nreturn createManagementTargetLifecycle;`)(
    CancellationTokenSource, context, (_port, listener) => {
      lifecycle.auth.add(listener);
      return () => lifecycle.auth.delete(listener);
    });
  const install = localizedFunction('GatewayRequestError', 'createManagementTargetLifecycle', 'gateway', 'generateUuid', 'observeInstallation', 'errorsText', 'requireInstallationOwner',
    'readInstallation', 'installationKey', 'runPluginInstallation', 'installationFailureText', 'pluginGatewayLabel',
    `${declaration('installPackageFromView')}\nreturn installPackageFromView;`)(
    GatewayRequestError, createManagementTargetLifecycle, gateway, options.generateUuid ?? (() => 'operation-original'), observeInstallation,
    error => error instanceof Error ? error.message : String(error), requireOwner, read, installationKey, runPluginInstallation,
    installationFailureText, pluginGatewayLabel);
  const quickInputToken = {}, notificationToken = {}, progressToken = {};
  const run = localizedFunction('managementGateway', 'IQuickInputService', 'INotificationService', 'IProgressService', 'installPackageFromView', 'workspaceContext',
    `${declaration('installServicePluginPackage')}\nreturn installServicePluginPackage;`)(
    () => typeof options.target === 'function' ? options.target() : options.target ?? {
      gateway_id: 'gateway-original', gateway_connection_id: 'connection-original',
    }, quickInputToken, notificationToken, progressToken, install, { ensureReady: options.ensureReady ?? (async () => {}) });
  let accessorActive = false;
  const accessor = { get(token) {
    if (!accessorActive) throw new Error('服务 accessor 仅在同步命令调用期间有效');
    if (token === quickInputToken) return {
      pick: async (items, dialog, cancellation) => { await options.beforeInput?.(dialog, cancellation, items); return picks.shift(); },
      input: async (dialog, cancellation) => { await options.beforeInput?.(dialog, cancellation); return inputs.shift(); },
    };
    if (token === notificationToken) return notifications;
    if (token === progressToken) return { withProgress: (_options, operation) => operation({ report() {} }) };
    throw new Error('测试收到未知 Workbench 服务');
  } };
  return () => {
    accessorActive = true;
    try { return run(accessor, options.explicitTarget); }
    finally { accessorActive = false; }
  };
}

function createInstaller(request, picks, inputs, observed) {
  return createInstallCommand(request, picks, inputs, observed)();
}

function operation(gatewayId = 'gateway-original', operationId = 'operation-original') {
  return { gateway_id: gatewayId, operation_id: operationId, status: 'succeeded', artifact: {
    package_id: 'sample.plugin', package_version: '2.4.1', target: undefined,
    source_kind: 'local_zip', digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000',
  }, cancel_requested: false };
}

afterAll(() => writeFile(join(run.artifactsDir, 'source-fingerprint.json'), `${JSON.stringify({
  paths: [sourcePath, scopePath, editorPath], sha256: sourceFingerprint,
}, null, 2)}\n`));

test('安装入口只为当前原生 Gateway 提供本机包，远端只提供自有来源与原操作查询', async () => {
  for (const native of [true, false]) {
    const calls = [], menus = [];
    await createInstallCommand(async path => { calls.push(path); }, [], [], [], {
      lifecycle: { gatewayId: native ? 'gateway-original' : 'entry-gateway', context: new Set(), auth: new Set() },
      beforeInput: (_dialog, _token, items) => menus.push(items.map(item => item.kind)),
    })();
    expect(menus).toEqual([native ? ['zip', 'directory', 'source', 'query'] : ['source', 'query']]);
    expect(calls).toEqual([]);
  }
});

test('Gateway 就绪前不打开安装向导，就绪后使用同步取得的原命令服务', async () => {
  const ready = Promise.withResolvers(), entered = Promise.withResolvers();
  const calls = [], observed = [], picks = [{ kind: 'zip' }], inputs = ['/tmp/plugin.zip'];
  const command = createInstallCommand(async (path, body) => {
    calls.push({ path, body });
    return operation();
  }, picks, inputs, observed, { ensureReady: async () => { entered.resolve(); await ready.promise; } });
  const pending = command();
  await entered.promise;
  expect(picks).toHaveLength(1);
  expect(calls).toEqual([]);
  ready.resolve();
  await pending;
  expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install']);
  expect(observed).toHaveLength(1);
});

test('快速访问冻结的安装目标不随初始化等待或后续管理选择改变', async () => {
  const explicitTarget = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const ready = Promise.withResolvers(), entered = Promise.withResolvers(), calls = [], observed = [];
  const command = createInstallCommand(async (path, body) => { calls.push({ path, body }); return operation(); },
    [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed, { explicitTarget,
      target: { gateway_id: 'gateway-other', gateway_connection_id: 'connection-other' },
      ensureReady: async () => { entered.resolve(); await ready.promise; } });
  const pending = command(); await entered.promise;
  explicitTarget.gateway_id = 'gateway-mutated'; ready.resolve(); await pending;
  expect(calls[0].body.gateway_id).toBe('gateway-original');
  expect(calls[0].body.gateway_connection_id).toBe('connection-original');
  expect(observed).toHaveLength(1);
});

test('安装受理前入口或凭据失效会取消现有输入，迟到确认不写入', async () => {
  for (const reason of ['gateway', 'credential']) {
    const lifecycle = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
    const calls = [], observed = [];
    const command = createInstallCommand(async (path, body) => { calls.push({ path, body }); return operation(); },
      [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed, { lifecycle,
        beforeInput: async (dialog, cancellation) => {
          if (!dialog.title.includes('原生绝对路径')) return;
          if (reason === 'gateway') {
            lifecycle.gatewayId = 'entry-other';
            for (const listener of lifecycle.context) listener();
          } else for (const listener of lifecycle.auth) listener();
          expect(cancellation.isCancellationRequested).toBe(true);
        } });
    await expect(command()).rejects.toThrow('原 Gateway 或访问凭据已改变');
    expect(calls).toEqual([]); expect(observed).toEqual([]);
    expect(lifecycle.context.size).toBe(0); expect(lifecycle.auth.size).toBe(0);
  }
});

test('来源目录迟到响应不恢复安装资格，普通管理选择改变仍使用冻结目标', async () => {
  const lifecycle = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
  const waiting = Promise.withResolvers(), release = Promise.withResolvers();
  const calls = [], observed = [], target = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const command = createInstallCommand(async (path, body, options) => {
    calls.push({ path, body });
    if (path.endsWith('/inventory')) return { sources: { entries: [{ source_id: 'source-original', enabled: true }] } };
    if (path.endsWith('/source-catalog')) {
      waiting.resolve(options.signal);
      await release.promise;
      return { gateway_id: 'gateway-original', source_id: 'source-original', catalog_revision: 'revision-1',
        items: [{ package_id: 'sample.plugin', package_version: '2.4.1' }] };
    }
    throw new Error('过期目录不能继续安装');
  }, [{ kind: 'source' }, { source: { source_id: 'source-original' } }], ['sample'], observed, { lifecycle, target });
  const pending = command(), signal = await waiting.promise;
  target.gateway_id = 'changed-management-selection';
  expect(calls.every(call => call.body.gateway_id === 'gateway-original')).toBe(true);
  for (const listener of lifecycle.auth) listener();
  expect(signal.aborted).toBe(true);
  release.resolve();
  await expect(pending).rejects.toThrow('原 Gateway 或访问凭据已改变');
  expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['inventory', 'source-catalog']);
  expect(observed).toEqual([]); expect(lifecycle.context.size + lifecycle.auth.size).toBe(0);
});

test('已受理安装在入口变化后只结算原 operation，不改投、取消或重放', async () => {
  const lifecycle = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
  const calls = [], observed = [], target = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const command = createInstallCommand(async (path, body) => {
    calls.push({ path, body });
    if (path.endsWith('/install')) {
      target.gateway_id = 'gateway-other'; lifecycle.gatewayId = 'entry-other';
      for (const listener of lifecycle.context) listener();
      return { ...operation(), status: 'running' };
    }
    if (path.endsWith('/install-operation')) return operation();
    throw new Error('不应取消或重放已受理操作');
  }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed, { lifecycle, target });
  await command();
  expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install', 'install-operation']);
  expect(calls.every(call => call.body.gateway_id === 'gateway-original' && call.body.operation_id === 'operation-original')).toBe(true);
  expect(lifecycle.context.size + lifecycle.auth.size).toBe(0);
});

test('安装回执 owner 不匹配时只读取目标 Gateway 的原 operation_id', async () => {
  for (const invalid of [operation('gateway-other'), operation('gateway-original', 'operation-other')]) {
    const calls = [], observed = [];
    await createInstaller(async (path, body) => {
      calls.push({ path, body });
      if (path.endsWith('/install')) return invalid;
      if (path.endsWith('/install-operation')) return operation();
      throw new Error(`不应请求 ${path}`);
    }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed);

    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install', 'install-operation']);
    expect(calls[0].body.operation_id).toBe('operation-original');
    expect(calls[1].body.operation_id).toBe('operation-original');
    expect(observed).toEqual([{ operationId: 'operation-original', operation: operation() }]);
  }
});

test('安装明确拒绝直接保留原状态、原因和 request_id，不发冗余结果查询', async () => {
  for (const status of [401, 403, 409, 422]) {
    const calls = [], observed = [];
    const failure = new GatewayRequestError(status, 'Rejected', '目标插件运行环境不匹配',
      '/api/gateway/service-plugins/install', 'original-rejection');
    await expect(createInstaller(async (path, body) => { calls.push({ path, body }); throw failure; },
      [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed)).rejects.toBe(failure);
    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install']);
    expect(failure.requestId).toBe('original-rejection'); expect(observed).toEqual([]);
  }
});

test('安装响应未知时只查询一次原 operation_id，不重放安装', async () => {
  const calls = [], observed = [];
  await createInstaller(async (path, body) => {
    calls.push({ path, body });
    if (path.endsWith('/install')) throw new Error('安装响应丢失');
    if (path.endsWith('/install-operation')) return operation();
    throw new Error(`不应请求 ${path}`);
  }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], observed);

  expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install', 'install-operation']);
  expect(calls.every(call => call.body.operation_id === 'operation-original')).toBe(true);
  expect(observed).toEqual([{ operationId: 'operation-original', operation: operation() }]);
});

test('失败、未知和取消结果保留原包、目标 Gateway、环境详情及下一步提示', async () => {
  const outcomes = [
    { status: 'failed', error: '插件 Node 运行时不匹配: package=22.14.0/127, gateway=20.18.0/115；请获取匹配版本的完整插件 ZIP',
      action: '请根据 Gateway 错误修复问题后，再对该包版本重新安装' },
    { status: 'unknown', error: '包原子发布已发生，但操作终态未确认；不会自动重放', action: '先检查目标 Gateway 当前插件包' },
    { status: 'cancelled', error: undefined, action: '确认目标 Gateway 未发布此版本后，可以重新安装' },
  ];
  for (const outcome of outcomes) {
    const artifact = { package_id: 'sample.plugin', package_version: '2.4.1',
      target: { os: 'linux', architecture: 'x64', system_baseline: 'glibc-2.28',
        node: { version: '22.14.0', abi: '127' }, python: undefined },
      source_kind: 'local_zip', digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000' };
    const result = { ...operation(), status: outcome.status, artifact, error: outcome.error };
    const calls = [];
    const failure = await createInstaller(async (path, body) => {
      calls.push({ path, body });
      return result;
    }, [{ kind: 'zip' }], ['/tmp/plugin.zip'], []).then(() => undefined, error => error);
    expect(failure).toBeInstanceOf(Error);
    expect(failure.message).toContain(`插件安装${outcome.status === 'cancelled' ? '已取消' : outcome.status === 'unknown' ? '结果未知' : '失败'}：sample.plugin@2.4.1`);
    expect(failure.message).toContain('目标原生 Gateway：gateway-original');
    expect(failure.message).toContain('原操作：operation-original');
    expect(failure.message).toContain('所需运行环境：linux/x64 · glibc-2.28 · Node 22.14.0/ABI 127');
    if (outcome.error) expect(failure.message).toContain(`Gateway 返回错误或环境详情：${outcome.error}`);
    expect(failure.message).toContain(outcome.action);
    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['install']);
  }
});

test('缺少必需 artifact 时报告协议回执缺字段并保留原 owner 与 Gateway 错误', async () => {
  const result = { ...operation(), status: 'failed', artifact: undefined, error: '操作回执没有制品详情' };
  const failure = await createInstaller(async () => result, [{ kind: 'zip' }], ['/tmp/plugin.zip'], [])
    .then(() => undefined, error => error);

  expect(failure).toBeInstanceOf(Error);
  expect(failure.message).toContain('插件安装失败，但 Gateway 回执缺少必需制品详情（artifact）');
  expect(failure.message).toContain('目标原生 Gateway：gateway-original');
  expect(failure.message).toContain('原操作：operation-original');
  expect(failure.message).toContain('Gateway 返回错误或环境详情：操作回执没有制品详情');
  expect(failure.message).toContain('请重新查询原操作');
});

test('插件目录 owner 错误或分页 revision 变化时不提交安装', async () => {
  for (const secondPage of [
    { gateway_id: 'gateway-other', source_id: 'source-original', catalog_revision: 'revision-1', items: [] },
    { gateway_id: 'gateway-original', source_id: 'source-original', catalog_revision: 'revision-2', items: [] },
  ]) {
    const calls = [], observed = [];
    const pages = [
      { gateway_id: 'gateway-original', source_id: 'source-original', catalog_revision: 'revision-1',
        items: [], next_cursor: 'next-page' },
      secondPage,
    ];
    await expect(createInstaller(async (path, body) => {
      calls.push({ path, body });
      if (path.endsWith('/inventory')) return { sources: { entries: [{ source_id: 'source-original', enabled: true }] } };
      if (path.endsWith('/source-catalog')) return pages.shift();
      throw new Error(`目录不一致时不应请求 ${path}`);
    }, [{ kind: 'source' }, { source: { source_id: 'source-original' } }, { artifact: undefined }], ['plugin'], observed))
      .rejects.toThrow(/不属于原 Gateway|版本已变化/);

    expect(calls.map(call => call.path.split('/').at(-1))).toEqual(['inventory', 'source-catalog', 'source-catalog']);
    expect(calls[1].body.cursor).toBeUndefined();
    expect(calls[2].body.cursor).toBe('next-page');
    expect(observed).toEqual([]);
  }
});

test('View 安装按钮委派原安装命令，与 F1 对同一 Gateway 制品共用原安装操作', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers(), duplicate = Promise.withResolvers();
  const calls = [], observed = [], ids = [], messages = [];
  const notifications = { info(message) { messages.push(message); duplicate.resolve(); }, error() {} };
  const installCommand = createInstallCommand(async (path, body) => {
    calls.push({ path, body });
    return { gateway_id: body.gateway_id, operation_id: body.operation_id, status: 'running', artifact: undefined, cancel_requested: false };
  }, [{ kind: 'zip' }, { kind: 'zip' }], ['/tmp/plugin.zip', '/tmp/plugin.zip'], observed, {
    notifications, generateUuid: () => { const id = `operation-${ids.length + 1}`; ids.push(id); return id; },
    observeInstallation: async (_progress, _notifications, _target, operationId, result) => {
      observed.push({ operationId, operation: result }); entered.resolve(); await release.promise;
    },
  });
  const f1 = installCommand();
  await entered.promise;

  let viewCommand;
  const frozenTarget = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const onInstallClick = classCallback('ServicePluginPackagesView', 'renderBody', "boxteam.plugins.installPackage", { target: frozenTarget });
  let refreshCount = 0;
  const view = { commands: { executeCommand(id, target) {
    expect(id).toBe('boxteam.plugins.installPackage');
    expect(target).toEqual(frozenTarget);
    viewCommand = installCommand();
    return viewCommand;
  } }, notifications, async refresh() { refreshCount += 1; } };
  onInstallClick.call(view);
  await duplicate.promise;

  expect(calls.filter(call => call.path.endsWith('/install'))).toHaveLength(1);
  expect(ids).toEqual(['operation-1']);
  expect(messages).toEqual(['相同插件制品的原安装操作仍在进行，正在等待该操作']);
  release.resolve();
  await Promise.all([f1, viewCommand]);
  expect(observed).toHaveLength(1);
  expect(refreshCount).toBe(0);
});

test('相同制品失败或取消结清后可重新创建安装操作', async () => {
  for (const outcome of ['failed', 'cancelled']) {
    const calls = [], observed = [], ids = [];
    const artifact = { package_id: 'sample.plugin', package_version: '1.0.0', target: undefined,
      source_kind: 'local_zip', digest: 'sha256:0000000000000000000000000000000000000000000000000000000000000000' };
    const installCommand = createInstallCommand(async (path, body) => {
      calls.push({ path, body });
      if (path.endsWith('/install')) {
        const status = calls.filter(call => call.path.endsWith('/install')).length === 1 ? 'running' : 'succeeded';
        return { gateway_id: body.gateway_id, operation_id: body.operation_id, status, artifact, cancel_requested: false };
      }
      if (path.endsWith('/install-operation') && calls.filter(call => call.path.endsWith('/install')).length === 1) {
        return { gateway_id: body.gateway_id, operation_id: body.operation_id, status: outcome, artifact,
          cancel_requested: outcome === 'cancelled' };
      }
      throw new Error(`不应请求 ${path}`);
    }, [{ kind: 'zip' }, { kind: 'zip' }], ['/tmp/plugin.zip', '/tmp/plugin.zip'], observed, {
      generateUuid: () => { const id = `operation-${ids.length + 1}`; ids.push(id); return id; },
    });
    await expect(installCommand()).rejects.toThrow(`插件安装${outcome === 'cancelled' ? '已取消' : '失败'}：sample.plugin@1.0.0`);
    await installCommand();
    expect(calls.filter(call => call.path.endsWith('/install')).map(call => call.body.operation_id)).toEqual(['operation-1', 'operation-2']);
  }
});

test('不同 Gateway 或不同本地制品的安装操作可并发进入原 owner', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers(), calls = [], observed = [], ids = [];
  let enteredCount = 0;
  const targets = [
    { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' },
    { gateway_id: 'gateway-other', gateway_connection_id: 'connection-other' },
    { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' },
  ];
  const installCommand = createInstallCommand(async (path, body) => {
    calls.push({ path, body });
    return { gateway_id: body.gateway_id, operation_id: body.operation_id, status: 'running', artifact: undefined, cancel_requested: false };
  }, [{ kind: 'zip' }, { kind: 'zip' }, { kind: 'zip' }], ['/tmp/plugin.zip', '/tmp/plugin.zip', '/tmp/plugin-other.zip'], observed, {
    target: () => targets.shift(),
    generateUuid: () => { const id = `operation-${ids.length + 1}`; ids.push(id); return id; },
    observeInstallation: async (_progress, _notifications, target, operationId, result) => {
      observed.push({ target, operationId, operation: result });
      if (++enteredCount === 3) entered.resolve();
      await release.promise;
    },
  });
  const first = installCommand(), second = installCommand(), third = installCommand();
  await entered.promise;
  expect(calls.filter(call => call.path.endsWith('/install')).map(call => call.body.gateway_id).sort())
    .toEqual(['gateway-original', 'gateway-original', 'gateway-other']);
  expect(calls.filter(call => call.path.endsWith('/install')).map(call => call.body.path).sort())
    .toEqual(['/tmp/plugin-other.zip', '/tmp/plugin.zip', '/tmp/plugin.zip']);
  expect(ids).toEqual(['operation-1', 'operation-2', 'operation-3']);
  release.resolve();
  await Promise.all([first, second, third]);
});

test('批量开关保留各插件绑定版本，未绑定项采用所选版本并在确认中列明', async () => {
  for (const scope of ['gateway', 'workspace', 'defaults']) {
    const target = { gateway_id: 'gateway-original' };
    const workspace = scope === 'workspace' ? { ...target, workspace_id: 'workspace-a' } : undefined;
    const pluginScope = scope === 'gateway' ? 'gateway' : 'workspace';
    const matching = [{ id: 'bound', instance_scope: pluginScope }, { id: 'new', instance_scope: pluginScope }];
    const manifest = { package_id: 'sample', version: '2.0.0', plugins: [...matching,
      { id: 'excluded', instance_scope: pluginScope === 'gateway' ? 'workspace' : 'gateway' }] };
    for (const enabled of [true, false]) {
      const batches = [], confirmations = [], batchButtons = [{ enabled: true }, { enabled: true }];
      const click = classCallback('ServicePluginPackagesView', 'renderPackages', 'setPluginBindings', {
        target, workspace, scope, pluginScope, matching, manifest, enabled, batchButtons,
        pluginTarget: target, entryGatewayId: target.gateway_id,
        state: { binding: (_packageId, pluginId) => pluginId === 'bound' ? { package_version: '1.0.0' } : undefined },
        async setPluginBindings(...args) { batches.push(args[4]); return undefined; },
      });
      let completed;
      click.call({
        runTargetOperation(operation) { completed = operation({ check() {} }); return completed; },
        dialogs: { async confirm(options) { confirmations.push(options.detail); return { confirmed: true }; } },
      });
      await completed;
      expect(batches).toEqual([[{ package_id: 'sample', plugin_id: 'bound', package_version: '1.0.0', enabled },
        { package_id: 'sample', plugin_id: 'new', package_version: '2.0.0', enabled }]]);
      expect(confirmations[0]).toContain('bound@1.0.0'); expect(confirmations[0]).toContain('new@2.0.0');
      expect(confirmations[0]).toContain('保留已有绑定版本');
      expect(batchButtons.every(button => button.enabled)).toBe(true);
    }
  }
});

test('卸载菜单在发起确认与请求流程前立即禁用，结算后恢复', async () => {
  const uninstall = { enabled: true }, release = Promise.withResolvers();
  let operationStarted = false;
  const click = classCallback('ServicePluginPackagesView', 'renderPackages', 'uninstallPackageFromView', {
    uninstall, entryGatewayId: 'entry-gateway', pluginTarget: { gateway_id: 'gateway-original' },
    manifest: { package_id: 'sample', version: '1.0.0' }, uninstallPackageFromView: async () => {},
  });
  const pending = click.call({ runTargetOperation() { operationStarted = !uninstall.enabled; return release.promise; } });
  expect(uninstall.enabled).toBe(false); expect(operationStarted).toBe(true);
  release.resolve(); await pending; expect(uninstall.enabled).toBe(true);
});

function actionHarness(state, request = async () => {}, manage = async () => {}) {
  const make = localizedFunction('Action', 'manageConfig', 'writeOnlyFields', 'gateway', 'pluginError',
    `${declaration('pluginActions')}\nreturn pluginActions;`)(Action, manage,
    () => [{ pointer: '/credential' }], { request }, error => error.message);
  return make(state, { package_id: 'sample', version: state.version ?? '1.0.0' },
    { id: 'command', runtime: 'python' }, {}, { error: message => state.errors.push(message) });
}
function actionState() {
  const controller = new AbortController();
  return { scope: 'gateway', target: { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' },
    errors: [], desired: { package_version: '1.0.0', enabled: true },
    active: { revision: 'r1', binding: { package_version: '1.0.0', enabled: true } },
    binding() { return this.desired; }, slot() { return this.active; },
    lifecycle: { signal: controller.signal, check() { if (controller.signal.aborted) throw new Error('原入口失效'); } },
    async refreshBindings() { this.refreshes = (this.refreshes ?? 0) + 1; }, controller };
}
for (const id of ['plugin.restart', 'plugin.secret./credential']) test(`${id} 请求或向导开始前立即禁用，结算后恢复`, async () => {
  const state = actionState(), release = Promise.withResolvers();
  let disabledAtEntry = false, action;
  const operation = async () => { disabledAtEntry = !action.enabled; await release.promise; };
  action = actionHarness(state, operation, operation).find(item => item.id === id);
  const pending = action.run(); expect(action.enabled).toBe(false); expect(disabledAtEntry).toBe(true);
  release.resolve(); await pending;
  expect(action.enabled).toBe(true); expect(state.refreshes).toBe(1);
});

test('旧操作结算不刷新用户已切换的范围，当前视图操作完成仍读取权威状态', async () => {
  const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(statement => statement.name?.text === 'ServicePluginPackagesView');
  const member = owner.members.find(member => ts.isMethodDeclaration(member) && member.name?.getText(parsed) === 'runOperation');
  const compiled = ts.transpile(`class View { ${ts.createPrinter().printNode(ts.EmitHint.Unspecified, member, parsed)} }`,
    { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  const View = localizedFunction('errorsText', `${compiled}\nreturn View;`)(error => error.message);
  const view = new View(), submitted = Promise.withResolvers(), errors = [];
  let refreshes = 0;
  view.refreshRevision = 1;
  view.notifications = { error: message => errors.push(message) };
  view.refresh = async () => { refreshes++; };
  const settled = view.runOperation(() => submitted.promise);
  view.refreshRevision++;
  submitted.resolve();
  await settled;
  expect(refreshes).toBe(0);
  await view.runOperation(async () => {});
  expect(refreshes).toBe(1);
  await view.runOperation(async () => { throw new Error('原操作失败'); });
  expect(errors).toEqual(['原操作失败']);
  expect(refreshes).toBe(2);
});

test('动作跟随来源绑定，pending 期望不被旧 active 状态覆盖，版本冲突不暴露参数或重启', async () => {
  const state = actionState(), calls = [];
  const actions = () => actionHarness(state, async () => {}, async (...args) => calls.push(args));
  expect(actions().map(action => action.label)).toContain('停用此范围');
  expect(actions().map(action => action.label)).toContain('重启实例');
  state.desired = { ...state.desired, enabled: false };
  expect(actions().map(action => action.label)).toContain('启用此范围');
  expect(actions().map(action => action.label)).not.toContain('重启实例');
  await actions().find(action => action.id === 'plugin.toggle').run(); expect(calls.at(-1)[6]).toBe(true);
  state.desired = { package_version: '2.0.0', enabled: false };
  expect(actions().map(action => action.label)).toEqual(['切换到此版本（保留开关）']);
  await actions()[0].run(); expect(calls.at(-1)[6]).toBe('preserve');
  state.version = '2.0.0'; state.scope = 'workspace'; state.workspace = { ...state.target, workspace_id: 'workspace-a' };
  const labels = actions().map(action => action.label);
  expect(labels).toContain('启用此范围'); expect(labels).toContain('解除此范围绑定');
  expect(labels).not.toContain('切换到此版本（保留开关）'); expect(labels).not.toContain('重启实例');
  await actions()[0].run(); expect(calls.at(-1)[5].package_version).toBe('2.0.0'); expect(calls.at(-1)[6]).toBe(true);
  state.desired = undefined;
  expect(actions().map(action => action.label)).not.toContain('解除此范围绑定');
});

function runtimeScope(workspaceId, gatewayId = 'gateway-original') {
  const streams = [], errors = [], rendered = [], reads = [];
  const controller = new AbortController();
  const Scope = localizedFunction('Disposable', 'toDisposable', 'Emitter', 'DeferredPromise', 'raceTimeout',
    'DEFAULT_API_REQUEST_TIMEOUT_MS', 'createManagementTargetLifecycle', 'watchPluginRuntimeStatus', 'readPluginBindings', 'pluginError',
    `${declaration('PluginScope')}\nreturn PluginScope;`)(Disposable, toDisposable, Emitter, DeferredPromise, raceTimeout, 1000,
    () => ({ signal: controller.signal, dispose() { controller.abort(); } }),
    (target, signal, next) => new Promise((resolve, reject) => {
      streams.push({ target, signal, next, reject }); signal.addEventListener('abort', resolve, { once: true });
    }), async (scope, target) => { reads.push({ scope, target }); return []; }, error => error.message);
  const target = { gateway_id: gatewayId };
  const state = new Scope(workspaceId ? 'workspace' : 'gateway', target,
    workspaceId ? { ...target, workspace_id: workspaceId } : undefined, { error: error => errors.push(error) });
  state.onDidChange(() => rendered.push(state.runtime));
  return { state, streams, errors, rendered, reads };
}
const snapshot = { plugins: [], configured_bindings: [], workspace_configurations: [] };

test('管理初始化等同一状态流首快照，后续投影更新；释放后迟到帧不可覆盖', async () => {
  const { state, streams, errors, rendered } = runtimeScope();
  const initial = state.load(); expect(state.runtime).toBeUndefined(); expect(streams).toHaveLength(1);
  streams[0].next(snapshot); await initial; expect(state.runtime).toBe(snapshot);
  const next = { ...snapshot, workspace_configurations: [{ workspace_id: 'a', state: 'waiting' }] };
  streams[0].next(next); expect(state.runtime).toBe(next);
  state.dispose(); expect(streams[0].signal.aborted).toBe(true);
  streams[0].next(snapshot); expect(state.runtime).toBe(next); expect(rendered.at(-1)).toBe(next); expect(errors).toEqual([]);
});

test('仅本范围 active 绑定变化重读来源；兄弟与实例变化不重订阅或重读库存', async () => {
  for (const workspaceId of [undefined, 'workspace-a']) {
    const { state, streams, reads, errors } = runtimeScope(workspaceId);
    const binding = { package_id: 'sample', plugin_id: 'command', package_version: '1.0.0', enabled: true, workspace_id: workspaceId };
    const first = { ...snapshot, configured_bindings: [binding] };
    const initial = state.load(); streams[0].next(first); await initial; expect(reads).toHaveLength(1);
    const sibling = { ...binding, workspace_id: 'workspace-b', enabled: false };
    streams[0].next({ ...first, configured_bindings: [binding, sibling], plugins: [{ revision: 'instance-next' }] });
    expect(reads).toHaveLength(1);
    streams[0].next({ ...first, configured_bindings: [{ ...binding, enabled: false }, sibling] });
    await Promise.resolve(); expect(reads).toHaveLength(2); expect(streams).toHaveLength(1);
    expect(streams[0].signal.aborted).toBe(false); expect(errors).toEqual([]); state.dispose();
  }
});

test('首快照前销毁取消等待，旧范围不能覆盖新范围；流失败保留原原因', async () => {
  const old = runtimeScope(undefined, 'gateway-old');
  const cancelled = old.state.load().catch(error => error); old.state.dispose();
  expect((await cancelled).message).toBe('Canceled');
  old.streams[0].next(snapshot); expect(old.state.runtime).toBeUndefined();
  const current = runtimeScope(undefined, 'gateway-new');
  const ready = current.state.load(); current.streams[0].next(snapshot); await ready;
  current.streams[0].reject(new Error('原目标连接中断')); await new Promise(resolve => setTimeout(resolve, 0));
  expect(current.errors).toEqual(['原目标连接中断']); expect(current.state.error).toBe('原目标连接中断');
  expect(current.state.runtime).toBe(snapshot); current.state.dispose(); expect(current.streams[0].signal.aborted).toBe(true);
  const failed = runtimeScope();
  const rejected = failed.state.load().catch(error => error);
  failed.streams[0].reject(new Error('原目标拒绝订阅'));
  expect((await rejected).message).toBe('原目标拒绝订阅'); failed.state.dispose(); expect(failed.errors).toEqual([]);
});

test('插件管理生命周期仅由入口 Gateway 或凭据失效，管理目标选择保持原 owner', () => {
  const state = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set(), selectedTarget: 'gateway-original' };
  const harness = managementLifecycleHarness(state);

  const ordinary = harness.create('原 Gateway 已改变', 'entry-gateway');
  state.selectedTarget = 'gateway-other';
  expect(() => ordinary.check()).not.toThrow();
  expect(ordinary.signal.aborted).toBe(false);
  ordinary.dispose();

  const entry = harness.create('原 Gateway 已改变', 'entry-gateway');
  harness.invalidateGateway('entry-other');
  expect(() => entry.check()).toThrow('原 Gateway 已改变');
  expect(entry.signal.aborted).toBe(true);
  entry.dispose();
  expect(state.context.size + state.auth.size).toBe(0);

  state.gatewayId = 'entry-gateway';
  const credential = harness.create('凭据已改变', 'entry-gateway');
  harness.invalidateCredentials();
  expect(() => credential.check()).toThrow('凭据已改变');
  credential.dispose();
  expect(state.context.size + state.auth.size).toBe(0);
  expect(() => harness.create('入口目标已改变', 'stale-entry')).toThrow('入口目标已改变');
});

test('卸载确认前失效不提交，已发送卸载请求在入口变化后结算原目标', async () => {
  const state = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
  const harness = managementLifecycleHarness(state);
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const notifications = { info() { notifications.infos++; }, infos: 0 };
  const calls = [];
  const uninstall = createUninstaller(async (path, options) => {
    calls.push({ path, options });
    harness.invalidateGateway('entry-other');
    return { uninstalled: true, package_id: 'sample', package_version: '1.0.0' };
  }, notifications);

  const stale = harness.create('原 Gateway 已改变', 'entry-gateway');
  await expect(uninstall({ pick: async (_items, _options, token) => {
    harness.invalidateGateway('entry-other');
    expect(token.isCancellationRequested).toBe(true);
    return { accept: true };
  } }, notifications, target, 'sample', '1.0.0', stale)).rejects.toThrow('原 Gateway 已改变');
  stale.dispose();
  expect(calls).toEqual([]);

  harness.invalidateGateway('entry-gateway');
  const accepted = harness.create('原 Gateway 已改变', 'entry-gateway');
  await uninstall({ pick: async () => ({ accept: true }) }, notifications, target, 'sample', '1.0.0', accepted);
  accepted.dispose();
  expect(calls).toHaveLength(1);
  expect(calls[0].path).toBe('/api/gateway/service-plugins/uninstall');
  expect(calls[0].options.signal).toBeUndefined();
  expect(JSON.parse(calls[0].options.body)).toMatchObject({ gateway_id: 'gateway-original',
    gateway_connection_id: 'connection-original', package_id: 'sample', package_version: '1.0.0' });
  expect(notifications.infos).toBe(1);
});

test('重启提交前核入口，已经提交后不因入口变化改投目标', async () => {
  const state = actionState(), calls = [];
  const action = actionHarness(state, async (path, options) => {
    calls.push({ path, options }); state.controller.abort();
  }).find(item => item.id === 'plugin.restart');
  await action.run(); expect(calls).toHaveLength(1);
  expect(calls[0].path).toBe('/api/gateway/service-plugins/restart'); expect(calls[0].options.signal).toBeUndefined();
  expect(JSON.parse(calls[0].options.body)).toEqual({ ...state.target, package_id: 'sample', package_version: '1.0.0', plugin_id: 'command', revision: 'r1' });
  expect(state.refreshes).toBeUndefined();
  await action.run(); expect(calls).toHaveLength(1);
});

test('秘密写入已返回原 owner 回执后，入口失效仍提交该回执而不产生无主引用', async () => {
  const state = { gatewayId: 'entry-gateway', context: new Set(), auth: new Set() };
  const harness = managementLifecycleHarness(state);
  const lifecycle = harness.create('原 Gateway 已改变', 'entry-gateway');
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' };
  const reference = { kind: 'plugin_private_file', gateway_id: target.gateway_id, package_id: 'sample', plugin_id: 'sample.plugin',
    parameter_pointer: '/credential', version: 'private-version-original' };
  const calls = [], notifications = [];
  const document = { writable: true, source_key: 'gateway-source-original', jsonc: '{}', effective: {},
    file_digest: 'file-original', layer_revision: 1, layer_digest: 'layer-original', active_revision: 1, active_digest: 'active-original',
    origins: [], status: {} };
  const owner = {
    async list(signal) { calls.push({ kind: 'list', signal }); return { sources: [] }; },
    async read(sourceKey, key, signal) { calls.push({ kind: 'read', sourceKey, key, signal }); return document; },
    intent(sourceKey, jsonc, baseline) { return { source_key: sourceKey, jsonc, baseline, idempotency_key: 'edit-original' }; },
    async preflight(intent, signal) { calls.push({ kind: 'preflight', intent, signal }); return { changed_paths: ['/credential'] }; },
    async save(intent, signal) { calls.push({ kind: 'save', intent, signal }); return { ...document, edit: {
      source_saved: true, state: 'candidate_validated', idempotency_key: intent.idempotency_key,
    } }; },
  };
  const saveSourceEdit = localizedFunction(`${configurationDeclaration('saveSourceEdit')}\nreturn saveSourceEdit;`)();
  const manage = localizedFunction('ConfigurationSourceOwner', 'chooseSource', 'sameBinding', 'bindingsAt', 'sourceBaseline',
    'saveSourceEdit', 'privateParameterLocation', 'privateParameterStatus', 'updateJsonc', 'gateway',
    'gatewayWorkspaceRequestHeaders', 'targetHeader', 'notification',
    `${configurationDeclaration('manageConfig')}\nreturn manageConfig;`)(
    class { constructor() { return owner; } }, async () => ({ source_key: 'gateway-source-original' }),
    (a, b) => a.package_id === b.package_id && a.plugin_id === b.plugin_id, () => [], () => ({}), saveSourceEdit,
    () => ({ path: ['credential'], current: undefined }), () => '秘密值已隐藏', () => '{"credential":{"kind":"plugin_private_file"}}',
    { async request(path, options) {
      calls.push({ kind: 'private-input', path, options });
      harness.invalidateGateway('entry-other');
      return reference;
    } }, () => ({}), () => ({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id }),
    { info: message => notifications.push(message), error: message => notifications.push(message) });
  let picks = 0;
  const quickInput = {
    async pick(_items, _options, token) { picks++; return picks === 1 ? { clear: false } : { save: true }; },
    async input() { return 'secret-sentinel'; },
  };

  try {
    await manage('gateway', target, undefined, quickInput, { info: message => notifications.push(message),
      error: message => notifications.push(message) }, { package_id: 'sample', plugin_id: 'sample.plugin', package_version: '1.0.0' },
    null, false, { pointer: '/credential', schema: { type: 'string' } }, lifecycle.check, lifecycle.token, lifecycle.signal);
  } finally { lifecycle.dispose(); }

  const secretWrite = calls.find(call => call.kind === 'private-input');
  const preflight = calls.find(call => call.kind === 'preflight');
  const save = calls.find(call => call.kind === 'save');
  expect(secretWrite.path).toBe('/api/gateway/service-plugins/private-inputs');
  expect(secretWrite.options.signal).toBeUndefined();
  expect(JSON.parse(secretWrite.options.body)).toMatchObject({ gateway_id: 'gateway-original', source_key: 'gateway-source-original' });
  expect(preflight.signal).toBeUndefined();
  expect(save.signal).toBeUndefined();
  expect(save.intent.jsonc).toBe('{"credential":{"kind":"plugin_private_file"}}');
  expect(save.intent.idempotency_key).toBe('edit-original');
  expect(picks).toBe(2);
  expect(notifications.some(message => message.includes('来源已保存'))).toBe(true);
});

test('实例日志只读取原 Gateway 回执的精确日志，保留限长及不可用错误', async () => {
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'route-original' };
  const calls = [];
  const signal = new AbortController().signal;
  let receipt = { gateway_id: target.gateway_id, selected_log_id: 'opaque-log-id',
    logs: [{ log_id: 'opaque-log-id', status: 'available', truncated: true, tail: 'probe stdout\nprobe stderr' }] };
  const read = localizedFunction('gateway', `${declaration('readPluginInstanceLog')}\nreturn readPluginInstanceLog;`)({
    request: async (path, options) => { calls.push({ path, options }); return receipt; },
  });
  expect(await read(target, 'opaque-log-id', signal)).toBe('仅显示最近的日志尾部。\nprobe stdout\nprobe stderr');
  const query = new URL(calls[0].path, 'http://local.invalid');
  expect(query.pathname).toBe('/api/gateway/diagnostics');
  expect(query.searchParams.get('log_id')).toBe('opaque-log-id');
  expect(query.searchParams.get('gateway_connection_id')).toBe('route-original');
  expect(calls[0].options.headers).toEqual({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id });
  expect(calls[0].options.signal).toBe(signal);
  receipt = { ...receipt, gateway_id: 'other' };
  await expect(read(target, 'opaque-log-id', signal)).rejects.toThrow('不属于原 Gateway 或日志');
  receipt = { ...receipt, gateway_id: target.gateway_id, selected_log_id: 'other' };
  await expect(read(target, 'opaque-log-id', signal)).rejects.toThrow('不属于原 Gateway 或日志');
  receipt = { ...receipt, selected_log_id: 'opaque-log-id', logs: [] };
  await expect(read(target, 'opaque-log-id', signal)).rejects.toThrow('原实例日志不在目标诊断回执中');
  receipt.logs = [{ log_id: 'opaque-log-id', status: 'empty' }];
  expect(await read(target, 'opaque-log-id', signal)).toBe('当前实例日志为空。');
  receipt.logs = [{ log_id: 'opaque-log-id', status: 'unavailable', error: '原日志已删除' }];
  await expect(read(target, 'opaque-log-id', signal)).rejects.toThrow('原日志已删除');
  expect(calls).toHaveLength(6);
});

test('关闭原插件范围后，迟到日志不再交付正文', async () => {
  const pending = Promise.withResolvers(), controller = new AbortController();
  const read = localizedFunction('gateway', `${declaration('readPluginInstanceLog')}\nreturn readPluginInstanceLog;`)({ request: () => pending.promise });
  const reading = read({ gateway_id: 'gateway-original' }, 'opaque-log-id', controller.signal);
  controller.abort(new Error('原插件范围已关闭'));
  pending.resolve({ gateway_id: 'gateway-original', selected_log_id: 'opaque-log-id',
    logs: [{ log_id: 'opaque-log-id', status: 'available', tail: '迟到正文' }] });
  await expect(reading).rejects.toThrow('原插件范围已关闭');
});

test('精确插件参数入口在首次等待前冻结 owner 与版本，不再询问范围或工作区', async () => {
  const waiting = Promise.withResolvers(), calls = [], notifications = [];
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'route-original', workspace_id: 'workspace-a',
    package_id: 'sample', package_version: '1.0.0', plugin_id: 'probe' };
  const plugin = { ...target, instance_scope: 'workspace', configuration_schema: { type: 'object' } };
  class Owner {
    constructor(target) { calls.push({ owner: target }); }
    async configurationSchemas() { return { gateway_id: 'gateway-original', plugins: [plugin] }; }
  }
  const quickToken = {}, notificationToken = {}, dialogToken = {};
  const lifecycle = { check() {}, token: {}, signal: new AbortController().signal, dispose() {} };
  const run = localizedFunction('IQuickInputService', 'INotificationService', 'IDialogService', 'workspaceContext',
    'createManagementTargetLifecycle', 'managementGateway', 'managementTargetDisplayName', 'ConfigurationSourceOwner',
    'choosePluginWorkspace', 'writeOnlyFields', 'manageConfig', `${configurationDeclaration('configureServicePlugin')}\nreturn configureServicePlugin;`)(
    quickToken, notificationToken, dialogToken, { ensureReady: () => waiting.promise }, () => lifecycle,
    () => { throw new Error('精确入口不读当前管理选择'); }, () => '', Owner,
    () => { throw new Error('精确入口不重选工作区'); }, () => [{ pointer: '/credential' }], async (...args) => calls.push({ manage: args }));
  const accessor = { get(token) {
    if (token === quickToken) return { async pick(items) { calls.push({ picks: items.map(item => item.label) }); return items.find(item => item.action === 'secret'); } };
    if (token === notificationToken) return { info: text => notifications.push(text) };
    if (token === dialogToken) return {};
    throw new Error('未知服务');
  } };
  const promise = run(accessor, target);
  target.gateway_id = 'gateway-later'; target.workspace_id = 'workspace-b'; target.package_version = '9.0.0';
  waiting.resolve({ gatewayId: 'entry-gateway' });
  await promise;
  expect(calls[0].owner).toEqual({ gateway_id: 'gateway-original', gateway_connection_id: 'route-original', workspace_id: 'workspace-a' });
  expect(calls.filter(call => call.picks)).toHaveLength(1);
  const managed = calls.find(call => call.manage).manage;
  expect(managed[0]).toBe('workspace');
  expect(managed[1]).toEqual({ gateway_id: 'gateway-original', gateway_connection_id: 'route-original' });
  expect(managed[2].workspace_id).toBe('workspace-a');
  expect(managed[5]).toEqual({ package_id: 'sample', package_version: '1.0.0', plugin_id: 'probe', enabled: false });
  expect(managed[8]).toEqual({ pointer: '/credential' });
  expect(notifications).toEqual([]);
});

test('下载源入口冻结 Gateway 路由与发起组，只打开同一正式来源的原 Settings', async () => {
  const waiting = Promise.withResolvers(), calls = [], parentToken = {}, editorToken = {}, groupsToken = {};
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'route-original' };
  const captured = { location: { gateway_id: 'gateway-original', source_key: 'gateway_mutable_override' }, document: { jsonc: '{}' } };
  const lifecycle = { entryGatewayId: 'entry-gateway', token: {}, check() {}, dispose() { calls.push('disposed'); } };
  const parent = { createInstance(...args) { calls.push({ created: args }); return captured; } };
  const editors = { async openEditor(...args) { calls.push({ opened: args }); } };
  const groups = { activeGroup: { id: 17 }, getGroup: id => id === 17 ? {} : undefined };
  class Input {}
  const run = localizedFunction('IInstantiationService', 'IEditorService', 'IEditorGroupsService', 'createManagementTargetLifecycle',
    'managementGateway', 'captureSettingsLocation', 'SettingsEditor2Input', `${declaration('manageServicePluginSources')}\nreturn manageServicePluginSources;`)(
    parentToken, editorToken, groupsToken, () => lifecycle, () => { throw new Error('明确目标不能读取另一入口'); },
    async (_parent, _token, target, scope) => { calls.push({ target, scope }); return waiting.promise; }, Input);
  const promise = run({ get: token => token === parentToken ? parent : token === editorToken ? editors : groups }, target);
  target.gateway_id = 'gateway-later'; target.gateway_connection_id = 'route-later'; groups.activeGroup.id = 44;
  waiting.resolve(captured); await promise;
  expect(calls[0]).toEqual({ target: { gateway_id: 'gateway-original', gateway_connection_id: 'route-original',
    name: 'gateway-original', connection_kind: 'remote_gateway' }, scope: {} });
  expect(calls.find(call => call.created).created).toEqual([Input, captured.location, captured.document]);
  expect(calls.find(call => call.opened).opened).toEqual([captured, { pinned: true, query: 'plugins.sources.entries' }, 17]);
  expect(calls.at(-1)).toBe('disposed');
});

test('MCP 配置和插件私有参数交给各自原命令，Gateway 插件不误写工作区实例', async () => {
  const parsed = ts.createSourceFile(mcpCommandPath, mcpCommandSource, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === 'configureWorkspaceMcpServer');
  const compiled = ts.transpile(ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, ''),
    { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
  class Service { constructor(server) { this.servers = { get: () => [server] }; } }
  const serviceToken = {}, commandToken = {}, commands = [];
  const server = { data: { service: { gateway_id: 'gateway-original', workspace_id: null, package_id: 'sample', package_version: '1.0.0', plugin_id: 'probe' } },
    catalog: { target: { gateway_id: 'gateway-original', gateway_connection_id: 'route-original', workspace_id: 'workspace-a' }, snapshot: {} } };
  const service = new Service(server);
  const run = localizedFunction('IMcpService', 'ICommandService', 'WorkspaceMcpService', 'workspaceContext', compiled + '\nreturn configureWorkspaceMcpServer;')(
    serviceToken, commandToken, Service, { getSnapshot: () => ({ gatewayId: 'entry-gateway' }) });
  const accessor = { get: token => token === serviceToken ? service : { async executeCommand(...args) { commands.push(args); } } };
  await run(accessor, server);
  expect(commands[0]).toEqual(['boxteam.settings.openTarget', { gateway_id: 'gateway-original', gateway_connection_id: 'route-original',
    name: 'gateway-original', connection_kind: 'remote_gateway' }, { workspace_id: 'workspace-a' }]);
  await run(accessor, server, true);
  expect(commands[1]).toEqual(['boxteam.settings.pluginParameters', { gateway_id: 'gateway-original', gateway_connection_id: 'route-original',
    workspace_id: undefined, package_id: 'sample', package_version: '1.0.0', plugin_id: 'probe' }]);
  server.data.service.workspace_id = 'workspace-a';
  await run(accessor, server, true);
  expect(commands[2][1].workspace_id).toBe('workspace-a');
  server.catalog.snapshot.error = '原工作区已移除';
  await expect(run(accessor, server, true)).rejects.toThrow('原工作区已移除');
  expect(commands).toHaveLength(3);
});
