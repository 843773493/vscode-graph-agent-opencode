import { afterAll, expect, test } from 'bun:test';
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { HttpRequestError as GatewayRequestError } from '../../../../src/clients/web/src/api/http.js';
import { CancellationTokenSource } from '../../../../src/clients/workbench/upstream/src/vs/base/common/cancellation.js';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/service_plugin_management_ownership.test.mjs').prepare();
const sourcePath = 'src/clients/workbench/src/extensions/servicePluginManagement.ts';
const source = await readFile(sourcePath, 'utf8');
const sourceFingerprint = createHash('sha256').update(source).digest('hex');

function declaration(name) {
  const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true);
  const node = parsed.statements.find(statement => statement.name?.text === name
    || (ts.isVariableStatement(statement)
      && statement.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === name)));
  if (!node) throw new Error(`实际源码缺少 ${sourcePath}#${name}`);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, node, parsed).replace(/^export /gm, '');
  return ts.transpile(printed, { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None });
}

function classCallback(className, methodName, sourceFragment, bindings = {}) {
  const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true);
  const owner = parsed.statements.find(statement => statement.name?.text === className);
  const method = owner?.members.find(member => ts.isMethodDeclaration(member) && member.name?.getText(parsed) === methodName);
  let callback;
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === 'addEventListener' && node.getText(parsed).includes(sourceFragment)) callback = node.arguments[1];
    ts.forEachChild(node, visit);
  }
  if (method) visit(method.body);
  if (!callback || !ts.isArrowFunction(callback)) throw new Error(`实际源码缺少 ${sourcePath}#${className}.${methodName} 的 ${sourceFragment} 事件`);
  const body = ts.isBlock(callback.body) ? callback.body : ts.factory.createBlock([
    ts.factory.createReturnStatement(callback.body),
  ], true);
  const functionNode = ts.factory.createFunctionExpression(undefined, undefined, undefined, undefined,
    callback.parameters, callback.type, body);
  const printed = ts.createPrinter().printNode(ts.EmitHint.Unspecified, functionNode, parsed);
  const names = ['errorsText', ...Object.keys(bindings)];
  return new Function(...names, `return function(...args) { return (${printed}).call(this, ...args); };`)(
    error => error instanceof Error ? error.message : String(error), ...Object.values(bindings));
}

function createInstallCommand(request, picks, inputs, observed, options = {}) {
  const gateway = { async request(path, requestOptions = {}) { return request(path, JSON.parse(requestOptions.body ?? '{}'), requestOptions); } };
  const requireOwner = new Function(`${declaration('requireInstallationOwner')}\nreturn requireInstallationOwner;`)();
  const read = new Function('gateway', 'requireInstallationOwner',
    `${declaration('readInstallation')}\nreturn readInstallation;`)(gateway, requireOwner);
  const activePluginInstallations = options.activePluginInstallations ?? new Map();
  const installationKey = new Function(`${declaration('installationKey')}\nreturn installationKey;`)();
  const installationFailureText = new Function(`${declaration('installationFailureText')}\nreturn installationFailureText;`)();
  const runPluginInstallation = new Function('activePluginInstallations', 'installationKey',
    `${declaration('runPluginInstallation')}\nreturn runPluginInstallation;`)(activePluginInstallations, installationKey);
  const pluginGatewayLabel = new Function(`${declaration('pluginGatewayLabel')}\nreturn pluginGatewayLabel;`)();
  const notifications = options.notifications ?? { info() {}, error() {} };
  const observe = new Function('gateway', 'timeout', 'ProgressLocation', 'DEFAULT_API_REQUEST_TIMEOUT_MS', 'readInstallation', 'errorsText', 'installationFailureText', 'pluginGatewayLabel',
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
  const install = new Function('GatewayRequestError', 'CancellationTokenSource', 'onGatewayTokenInvalidated', 'workspaceContext', 'gateway', 'generateUuid', 'observeInstallation', 'errorsText', 'requireInstallationOwner',
    'readInstallation', 'installationKey', 'runPluginInstallation', 'installationFailureText', 'pluginGatewayLabel',
    `${declaration('installPackageFromView')}\nreturn installPackageFromView;`)(
    GatewayRequestError, CancellationTokenSource, (_port, listener) => { lifecycle.auth.add(listener); return () => lifecycle.auth.delete(listener); }, context,
    gateway, options.generateUuid ?? (() => 'operation-original'), observeInstallation,
    error => error instanceof Error ? error.message : String(error), requireOwner, read, installationKey, runPluginInstallation,
    installationFailureText, pluginGatewayLabel);
  const quickInputToken = {}, notificationToken = {}, progressToken = {};
  const run = new Function('managementGateway', 'IQuickInputService', 'INotificationService', 'IProgressService', 'installPackageFromView', 'workspaceContext',
    `${declaration('installServicePluginPackage')}\nreturn installServicePluginPackage;`)(
    () => typeof options.target === 'function' ? options.target() : options.target ?? {
      gateway_id: 'gateway-original', gateway_connection_id: 'connection-original',
    }, quickInputToken, notificationToken, progressToken, install, { ensureReady: options.ensureReady ?? (async () => {}) });
  let accessorActive = false;
  const accessor = { get(token) {
    if (!accessorActive) throw new Error('服务 accessor 仅在同步命令调用期间有效');
    if (token === quickInputToken) return {
      pick: async (_items, dialog, cancellation) => { await options.beforeInput?.(dialog, cancellation); return picks.shift(); },
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
  path: sourcePath, sha256: sourceFingerprint,
}, null, 2)}\n`));

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
  const onInstallClick = classCallback('ServicePluginPackagesView', 'renderBody', "boxteam.plugins.installPackage");
  let refreshCount = 0;
  const view = { commands: { executeCommand(id) {
    expect(id).toBe('boxteam.plugins.installPackage');
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

test('卸载按钮在发起确认与请求流程前立即禁用，阻止同一按钮重复提交', () => {
  const uninstall = { disabled: false };
  let operationStarted = false;
  const onUninstallClick = classCallback('ServicePluginPackagesView', 'renderPackages', 'uninstallPackageFromView', {
    uninstall,
    pluginTarget: { gateway_id: 'gateway-original', gateway_connection_id: 'connection-original' },
    manifest: { package_id: 'sample', version: '1.0.0' },
    uninstallPackageFromView: async () => {},
  });

  onUninstallClick.call({ runOperation() { operationStarted = uninstall.disabled; return Promise.resolve(); } });

  expect(uninstall.disabled).toBe(true);
  expect(operationStarted).toBe(true);
});

test('重启按钮在请求运行时立即禁用，阻止同一 revision 重复重启', () => {
  const restart = { disabled: false };
  let operationStarted = false;
  const onRestartClick = classCallback('ServicePluginPackagesView', 'showDetails', '/api/gateway/service-plugins/restart', { restart });

  onRestartClick.call({ runOperation() { operationStarted = restart.disabled; return Promise.resolve(); } });

  expect(restart.disabled).toBe(true);
  expect(operationStarted).toBe(true);
});

test('秘密参数按钮在向导启动前立即禁用，避免重复创建不可变秘密版本', () => {
  const secret = { disabled: false };
  let operationStarted = false;
  const onSecretClick = classCallback('ServicePluginPackagesView', 'showDetails', 'false, field', { secret });

  onSecretClick.call({ runOperation() { operationStarted = secret.disabled; return Promise.resolve(); } });

  expect(secret.disabled).toBe(true);
  expect(operationStarted).toBe(true);
});

test('实例日志只读取原 Gateway 回执的精确日志，保留限长及不可用错误', async () => {
  const target = { gateway_id: 'gateway-original', gateway_connection_id: 'route-original' };
  const calls = [];
  let receipt = { gateway_id: target.gateway_id, selected_log_id: 'opaque-log-id',
    logs: [{ log_id: 'opaque-log-id', status: 'available', truncated: true, tail: 'probe stdout\nprobe stderr' }] };
  const read = new Function('gateway', `${declaration('readPluginInstanceLog')}\nreturn readPluginInstanceLog;`)({
    request: async (path, options) => { calls.push({ path, options }); return receipt; },
  });
  expect(await read(target, 'opaque-log-id')).toBe('仅显示最近的日志尾部。\nprobe stdout\nprobe stderr');
  const query = new URL(calls[0].path, 'http://local.invalid');
  expect(query.pathname).toBe('/api/gateway/diagnostics');
  expect(query.searchParams.get('log_id')).toBe('opaque-log-id');
  expect(query.searchParams.get('gateway_connection_id')).toBe('route-original');
  expect(calls[0].options.headers).toEqual({ 'X-BoxTeam-Target-Gateway-Id': target.gateway_id });
  receipt = { ...receipt, gateway_id: 'other' };
  await expect(read(target, 'opaque-log-id')).rejects.toThrow('不属于原 Gateway 或日志');
  receipt = { ...receipt, gateway_id: target.gateway_id, selected_log_id: 'other' };
  await expect(read(target, 'opaque-log-id')).rejects.toThrow('不属于原 Gateway 或日志');
  receipt = { ...receipt, selected_log_id: 'opaque-log-id', logs: [] };
  await expect(read(target, 'opaque-log-id')).rejects.toThrow('原实例日志不在目标诊断回执中');
  receipt.logs = [{ log_id: 'opaque-log-id', status: 'empty' }];
  expect(await read(target, 'opaque-log-id')).toBe('当前实例日志为空。');
  receipt.logs = [{ log_id: 'opaque-log-id', status: 'unavailable', error: '原日志已删除' }];
  await expect(read(target, 'opaque-log-id')).rejects.toThrow('原日志已删除');
  expect(calls).toHaveLength(6);
});
