import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, readFile, symlink, writeFile } from 'node:fs/promises';
import { createConnection, createServer } from 'node:net';
import { basename, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildNodeExtensionHost } from '../../../../src/clients/workbench/scripts/nodeHost.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/test_node_host.py').prepare();
const runtime = join(run.runRoot, 'runtime'), hostDirectory = join(runtime, 'host');
const workbench = resolve('src/clients/workbench');
const node = process.env.BOXTEAM_TEST_NODE_HOST_EXECUTABLE;
const modules = process.env.BOXTEAM_TEST_NODE_HOST_MODULES;
let protocolTypes, metadata;
const receipts = [];

before(async () => {
  if (!node || !modules) throw new Error('UNMET_PREREQUISITE: 需显式配置 BOXTEAM_TEST_NODE_HOST_EXECUTABLE 与 BOXTEAM_TEST_NODE_HOST_MODULES');
  metadata = await buildNodeExtensionHost(workbench, hostDirectory);
  await symlink(resolve(modules), join(hostDirectory, 'node_modules'), 'junction');
  for (const [name, version] of Object.entries(metadata.dependencies)) {
    const installed = JSON.parse(await readFile(join(hostDirectory, 'node_modules', name, 'package.json'), 'utf8'));
    assert.equal(installed.name, name);
    assert.equal(installed.version, version, `宿主实际依赖版本与固定上游不符：${name}`);
  }
  const sources = './src/clients/workbench/upstream/src/vs/';
  const exports = {
    PersistentProtocol: 'base/parts/ipc/common/ipc.net', NodeSocket: 'base/parts/ipc/node/ipc.net',
    VSBuffer: 'base/common/buffer', URI: 'base/common/uri',
    ExtensionIdentifier: 'platform/extensions/common/extensions',
    RPCProtocol: 'workbench/services/extensions/common/rpcProtocol',
    'MainContext, ExtHostContext': 'workbench/api/common/extHost.protocol',
  };
  await build({ stdin: { contents: Object.entries(exports).map(([names, path]) =>
    `export { ${names} } from '${sources}${path}.js';`).join('\n'), resolveDir: resolve('.') },
  outfile: join(runtime, 'protocol.mjs'), bundle: true, format: 'esm', platform: 'node',
  packages: 'external', tsconfig: join(workbench, 'tsconfig.jsonc') });
  protocolTypes = await import(pathToFileURL(join(runtime, 'protocol.mjs')).href);
});

after(async () => {
  try { await run.close(); }
  finally {
    await writeFile(join(run.artifactsDir, 'node-host-checks.json'), JSON.stringify({
      boundary: '真实固定上游Node宿主及ESM扩展、原TCP/socket-transfer/PersistentProtocol/RPC和原生文件访问；主线程UI/配置/存储/注册回调及CPU负载估算为明确替身。未经过Gateway、安装管理或窗口多目标registry，不代表产品完整验收。',
      metadata, receipts,
    }, null, 2) + '\n');
  }
});

function bounded(promise, stage) {
  return Promise.race([promise, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`Node宿主阶段超时：${stage}`)), 10000);
    timer.unref();
    promise.then(() => clearTimeout(timer), () => clearTimeout(timer));
  })]);
}

async function connect(label) {
  const m = protocolTypes;
  const folder = join(run.workspaceRoot, basename(run.runRoot), label);
  const extension = join(folder, 'extension');
  await mkdir(extension, { recursive: true });
  const manifest = { publisher: 'host-probe', name: 'node', version: label === 'A' ? '1.0.0' : '2.0.0',
    type: 'module', main: './extension.mjs', engines: { vscode: `^${metadata.version}` },
    extensionKind: ['workspace'], activationEvents: ['*'] };
  await Promise.all([
    writeFile(join(extension, 'package.json'), JSON.stringify(manifest)),
    writeFile(join(extension, 'extension.mjs'), await readFile('tests/integration/workspace_services/extension_host/probe_extension.mjs')),
    writeFile(join(folder, 'identity.txt'), label),
  ]);
  const child = fork(join(hostDirectory, 'extensionHostProcess.mjs'), ['--useHostProxy=false'], {
    execPath: node, execArgv: [], stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    env: { ...process.env, VSCODE_EXTHOST_WILL_SEND_SOCKET: '1', VSCODE_RECONNECTION_GRACE_TIME: '0' },
  });
  const exit = once(child, 'exit'), ready = once(child, 'message');
  const receipt = { label, pid: child.pid, calls: [], errors: [], stdout: '', stderr: '' };
  receipts.push(receipt);
  child.stdout.on('data', data => { receipt.stdout += data; });
  child.stderr.on('data', data => { receipt.stderr += data; });
  run.addCleanup(`Node宿主${label}`, async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill();
    await bounded(exit, `${label}退出`);
  });
  const server = createServer({ pauseOnConnect: true });
  run.addCleanup(`socket listener ${label}`, () => new Promise((resolve, reject) => {
    if (!server.listening) return resolve();
    server.close(error => error ? reject(error) : resolve());
  }));
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const accepted = once(server, 'connection');
  const socket = createConnection(server.address().port, '127.0.0.1');
  const nodeSocket = new m.NodeSocket(socket, `host-probe-${label}`);
  run.addCleanup(`socket ${label}`, () => nodeSocket.dispose());
  // 本装具不验 CPU 负载判定；用原注入点避免创建窗口级、无释放入口的采样定时器。
  const protocol = new m.PersistentProtocol({ socket: nodeSocket, loadEstimator: { hasHighLoad: () => false } });
  run.addCleanup(`protocol ${label}`, () => protocol.dispose());
  protocol.sendResume();
  assert.deepEqual((await bounded(ready, 'IPC ready'))[0], { type: 'VSCODE_EXTHOST_IPC_READY' });
  const [transfer] = await accepted;
  run.addCleanup(`transfer socket ${label}`, () => transfer.destroy());
  child.send({ type: 'VSCODE_EXTHOST_IPC_SOCKET', initialDataChunk: '', skipWebSocketFrames: true,
    permessageDeflate: false, inflateBytes: '' }, transfer);
  server.close();
  const next = () => bounded(new Promise(resolve => {
    const listener = protocol.onMessage(value => { listener.dispose(); resolve(value); });
  }), `${label}握手`);
  assert.equal((await next()).readUInt8(0), 2);
  const identifier = new m.ExtensionIdentifier('host-probe.node');
  const description = { ...manifest, identifier, extensionLocation: m.URI.file(extension), isBuiltin: false,
    isUserBuiltin: false, isUnderDevelopment: false, targetPlatform: 'undefined', preRelease: false };
  const init = {
    version: metadata.version, quality: 'stable', commit: metadata.commit, parentPid: 0,
    environment: { isExtensionDevelopmentDebug: false, appName: 'Host probe', appHost: 'web', appLanguage: 'en',
      appUriScheme: 'boxteam', isExtensionTelemetryLoggingOnly: true,
      globalStorageHome: m.URI.file(join(folder, 'global')), workspaceStorageHome: m.URI.file(join(folder, 'storage')) },
    workspace: { id: label, name: label },
    extensions: { versionId: 1, allExtensions: [description], activationEvents: { 'host-probe.node': ['*'] }, myExtensions: [identifier] },
    telemetryInfo: { sessionId: label, machineId: 'probe', sqmId: 'probe', devDeviceId: 'probe', firstSessionDate: '2026-11-06' },
    logLevel: 0, loggers: [], logsLocation: m.URI.file(join(folder, 'logs')), autoStart: false,
    remote: { isRemote: false, connectionData: null }, consoleForward: { includeStack: false, logNative: false }, uiKind: 2,
  };
  async function initialize(commit = metadata.commit) {
    const initialized = commit === metadata.commit ? next() : undefined;
    protocol.send(m.VSBuffer.fromString(JSON.stringify({ ...init, commit })));
    if (commit !== metadata.commit) return bounded(exit, '版本拒绝');
    assert.equal((await initialized).readUInt8(0), 1);
    const rpc = new m.RPCProtocol(protocol);
    run.addCleanup(`RPC ${label}`, () => rpc.dispose());
    // 这些主线程边界只记录真实宿主的通知；不模拟文件读取或扩展执行结果。
    const notification = (actor, name) => (...args) => { receipt.calls.push({ actor, name, args }); };
    const register = (actor, methods) => rpc.set(m.MainContext[actor], methods);
    const notify = (actor, names) => register(actor, Object.fromEntries(names.map(name => [name, notification(actor, name)])));
    notify('MainThreadLogger', ['$registerLogger', '$deregisterLogger']);
    notify('MainThreadTelemetry', ['$publicLog2', '$publicLog']);
    notify('MainThreadConsole', ['$logExtensionHostMessage']);
    notify('MainThreadTerminalService', ['$registerProcessSupport']);
    notify('MainThreadSearch', ['$registerTextSearchProvider']);
    notify('MainThreadTask', ['$registerSupportedExecutions', '$registerTaskSystem']);
    notify('MainThreadDebugService', ['$registerDebugTypes']);
    notify('MainThreadCommands', ['$registerCommand', '$unregisterCommand']);
    notify('MainThreadFileSystem', ['$ensureActivation']);
    register('MainThreadWindow', { $getInitialState: () => ({ isFocused: true, isActive: true }) });
    register('MainThreadLanguageModelTools', { $getTools: () => [] });
    register('MainThreadStorage', { $initializeExtensionStorage: () => undefined });
    const error = (...args) => { receipt.errors.push(args); };
    register('MainThreadErrors', { $onUnexpectedError: error });
    register('MainThreadExtensionService', {
      $onWillActivateExtension: notification('MainThreadExtensionService', '$onWillActivateExtension'),
      $onDidActivateExtension: notification('MainThreadExtensionService', '$onDidActivateExtension'),
      $setPerformanceMarks: notification('MainThreadExtensionService', '$setPerformanceMarks'),
      $onExtensionActivationError: error, $onExtensionRuntimeError: error,
    });
    const empty = { contents: {}, keys: [], overrides: [] };
    await rpc.getProxy(m.ExtHostContext.ExtHostConfiguration).$initializeConfiguration({
      defaults: empty, policy: empty, application: empty, userLocal: empty, userRemote: empty,
      workspace: empty, folders: [], configurationScopes: [],
    });
    await rpc.getProxy(m.ExtHostContext.ExtHostWorkspace).$initializeWorkspace({ id: label, name: label,
      folders: [{ uri: m.URI.file(folder), name: label, index: 0 }] }, true);
    await bounded(rpc.getProxy(m.ExtHostContext.ExtHostExtensionService).$startExtensionHost({
      versionId: 2, toAdd: [], toRemove: [], addActivationEvents: {}, myToAdd: [], myToRemove: [],
    }), 'startExtensionHost');
    assert.equal(await rpc.getProxy(m.ExtHostContext.ExtHostExtensionService).$activate(identifier,
      { startup: false, extensionId: identifier, activationEvent: '*' }), true);
    return () => rpc.getProxy(m.ExtHostContext.ExtHostCommands).$executeContributedCommand('host-probe.inspect');
  }
  async function stop() {
    protocol.send(m.VSBuffer.wrap(Uint8Array.of(3)));
    const result = await bounded(exit, `${label}正常终止`);
    receipt.exit = result;
    nodeSocket.dispose();
    assert.deepEqual(result, [0, null]);
  }
  return { initialize, stop, receipt, folder, manifest };
}

test('构建原Node宿主时不引入renderer文件链，依赖跟随固定上游', () => {
  assert.equal(metadata.inputs.some(path => path.includes('/clients/web/') || path.includes('/workbench/src/')), false);
  assert.equal(metadata.dependencies['@vscode/proxy-agent'], '0.45.0');
});

test('构建拒绝覆盖已有输出或写入固定上游，不修改原文件', async () => {
  const protectedOutput = join(runtime, 'protected-output');
  await mkdir(protectedOutput);
  const original = '{"protected":true}\n';
  await writeFile(join(protectedOutput, 'package.json'), original);
  await assert.rejects(buildNodeExtensionHost(workbench, protectedOutput), { code: 'EEXIST' });
  assert.equal(await readFile(join(protectedOutput, 'package.json'), 'utf8'), original);
  await assert.rejects(buildNodeExtensionHost(workbench, join(workbench, 'upstream/node-host-probe')),
    /不能写入固定上游源码/);
});

test('原协议拒绝不同上游版本，不激活扩展', async () => {
  const host = await connect('wrong-commit');
  assert.deepEqual(await host.initialize('wrong-commit'), [55, null]);
  assert.deepEqual(host.receipt.calls, []);
});

test('真实双Node宿主各自激活同ID不同版本，原生与扩展fs访问相同目标，停止A不影响B', { timeout: 30000 }, async () => {
  const [a, b] = await Promise.all([connect('A'), connect('B')]);
  const [inspectA, inspectB] = await Promise.all([a.initialize(), b.initialize()]);
  for (const [host, inspect, label] of [[a, inspectA, 'A'], [b, inspectB, 'B']]) {
    const result = await inspect();
    host.receipt.result = result;
    assert.deepEqual(result, { id: 'host-probe.node', version: host.manifest.version, native: label, api: label,
      ownVersion: host.manifest.version, all: ['host-probe.node'], folders: [protocolTypes.URI.file(host.folder).toString()] });
    assert.equal(host.receipt.calls.some(call => call.name === '$onDidActivateExtension'), true);
    assert.deepEqual(host.receipt.errors, []);
    assert.equal(host.receipt.stderr, '');
  }
  await a.stop();
  assert.equal((await inspectB()).native, 'B');
  await b.stop();
});
