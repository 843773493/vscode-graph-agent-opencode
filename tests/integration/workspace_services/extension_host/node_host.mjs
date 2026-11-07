import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, readFile, readdir, symlink, writeFile } from 'node:fs/promises';
import { createRequire, isBuiltin } from 'node:module';
import { createConnection } from 'node:net';
import { basename, dirname, join, posix, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildNodeExtensionHost, nodeHostDependencyLock } from '../../../../src/clients/workbench/scripts/nodeHost.mjs';
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
      boundary: '真实Backend registry/宿主owner/关闭生命周期、专用manifest、目标私有IPC、固定上游Node宿主、原PersistentProtocol/RPC与ESM扩展；TCP是装具到通道的字节桥。gateway/relay模式额外经过真实Uvicorn、Gateway路由、认证与宿主管理通道，ipc模式直接接owner。主线程UI/配置/存储/注册回调及CPU负载估算为明确替身；未装配完整业务图、SSH、安装管理或窗口多目标registry，不代表产品完整验收。',
      platform: process.platform,
      transport: process.env.BOXTEAM_TEST_HOST_TRANSPORT ?? 'ipc',
      // gateway/relay由Python装具以真实Uvicorn路由和原认证owner承载；未装配完整工作区服务图或SSH隧道。
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
  const receipt = { label, calls: [], errors: [] };
  receipts.push(receipt);
  const socket = createConnection(Number(process.env.BOXTEAM_TEST_HOST_BACKEND_PORT), '127.0.0.1');
  const exit = once(socket, 'close');
  // 装具只递送产品owner/认证WS已经生成的初始化；原协议字节仍留在同一socket缓冲区。
  socket.write(`${label}\n`);
  const bindingBytes = [];
  for (;;) {
    const byte = socket.read(1);
    if (byte === null) { await bounded(once(socket, 'readable'), `${label}目标初始化`); continue; }
    if (byte[0] === 10) break;
    bindingBytes.push(byte[0]);
  }
  const initialization = JSON.parse(Buffer.from(bindingBytes).toString('utf8'));
  assert.equal(initialization.workspace.folders.length, 1);
  assert.equal(initialization.workspace.folders[0].name, label);
  assert.equal(initialization.workspace.folders[0].native_uri, pathToFileURL(folder).href);
  receipt.initialization = initialization;
  const nodeSocket = new m.NodeSocket(socket, `host-probe-${label}`);
  run.addCleanup(`socket ${label}`, () => nodeSocket.dispose());
  // 本装具不验 CPU 负载判定；用原注入点避免创建窗口级、无释放入口的采样定时器。
  const protocol = new m.PersistentProtocol({ socket: nodeSocket, loadEstimator: { hasHighLoad: () => false } });
  run.addCleanup(`protocol ${label}`, () => protocol.dispose());
  protocol.sendResume();
  const next = () => bounded(new Promise(resolve => {
    const listener = protocol.onMessage(value => { listener.dispose(); resolve(value); });
  }), `${label}握手`);
  assert.equal((await next()).readUInt8(0), 2);
  const identifier = new m.ExtensionIdentifier('host-probe.node');
  const description = { ...manifest, identifier, extensionLocation: m.URI.file(extension), isBuiltin: false,
    isUserBuiltin: false, isUnderDevelopment: false, targetPlatform: 'undefined', preRelease: false };
  const init = {
    version: metadata.version, quality: 'stable', commit: metadata.commit, parentPid: initialization.parent_pid,
    environment: { isExtensionDevelopmentDebug: false, appName: 'Host probe', appHost: 'web', appLanguage: 'en',
      appUriScheme: 'boxteam', isExtensionTelemetryLoggingOnly: true,
      appRoot: m.URI.parse(initialization.environment.app_root_uri),
      globalStorageHome: m.URI.parse(initialization.environment.global_storage_home_uri),
      workspaceStorageHome: m.URI.parse(initialization.environment.workspace_storage_home_uri) },
    workspace: { id: initialization.workspace.id, name: initialization.workspace.name },
    extensions: { versionId: 1, allExtensions: [description], activationEvents: { 'host-probe.node': ['*'] }, myExtensions: [identifier] },
    telemetryInfo: { sessionId: label, machineId: 'probe', sqmId: 'probe', devDeviceId: 'probe', firstSessionDate: '2026-11-06' },
    logLevel: 0, loggers: [], logsLocation: m.URI.parse(initialization.logs_location_uri), autoStart: false,
    remote: { isRemote: false, connectionData: null }, consoleForward: { includeStack: false, logNative: false }, uiKind: 2,
  };
  let extensionService;
  async function initialize(commit = metadata.commit) {
    const initialized = commit === metadata.commit ? next() : undefined;
    protocol.send(m.VSBuffer.fromString(JSON.stringify({ ...init, commit })));
    if (commit !== metadata.commit) return bounded(exit, '版本拒绝');
    assert.equal((await initialized).readUInt8(0), 1);
    const rpc = new m.RPCProtocol(protocol);
    socket.once('close', () => rpc.dispose());
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
    await rpc.getProxy(m.ExtHostContext.ExtHostWorkspace).$initializeWorkspace({
      id: initialization.workspace.id, name: initialization.workspace.name,
      folders: initialization.workspace.folders.map((folder, index) => ({ uri: m.URI.parse(folder.native_uri), name: folder.name, index })) }, true);
    extensionService = rpc.getProxy(m.ExtHostContext.ExtHostExtensionService);
    await bounded(extensionService.$startExtensionHost({
      versionId: label === 'A' ? 1 : 2, toAdd: [], toRemove: [], addActivationEvents: {}, myToAdd: [], myToRemove: [],
    }), 'startExtensionHost');
    assert.equal(await rpc.getProxy(m.ExtHostContext.ExtHostExtensionService).$activate(identifier,
      { startup: false, extensionId: identifier, activationEvent: '*' }), true);
    return (command = 'host-probe.inspect') => rpc.getProxy(m.ExtHostContext.ExtHostCommands).$executeContributedCommand(command);
  }
  async function stop() {
    protocol.send(m.VSBuffer.wrap(Uint8Array.of(3)));
    const result = await bounded(exit, `${label}正常终止`);
    receipt.exit = result;
    nodeSocket.dispose();
    assert.deepEqual(result, [false]);
  }
  return { initialize, stop, receipt, folder, manifest, disconnect: () => socket.destroy(), exit,
    delta: delta => extensionService.$deltaExtensions(delta),
    start: delta => extensionService.$startExtensionHost(delta),
    activate: identifier => extensionService.$activate(identifier, { startup: false, extensionId: identifier, activationEvent: 'api' }) };
}

test('宿主构建仅复用纯MCP注册模块，不引入renderer文件链，依赖跟随固定上游', () => {
  assert.deepEqual(metadata.inputs.filter(path => path.includes('/clients/web/') || path.includes('/workbench/src/')),
    ['src/clients/workbench/src/mcp/providerRegistration.ts']);
  assert.equal(metadata.dependencies['@vscode/proxy-agent'], '0.45.0');
});

test('依赖闭包保留嵌套版本、peer归属、optional平台条件及必需引用', () => {
  const pkg = (version, extra = {}) => ({ version, resolved: `https://registry.example/package-${version}.tgz`, integrity: `sha512-${version}`, ...extra });
  const manifest = { name: 'lock-probe', engines: { node: metadata.nodeVersion }, dependencies: { a: '1.0.0', z: '1.0.0' } };
  const source = { lockfileVersion: 3, packages: {
    '': { scripts: { install: '不得继承的上游脚本' }, devDependencies: { unused: '1.0.0' } },
    'node_modules/a': pkg('1.0.0', { dependencies: { nested: '^2' }, optionalDependencies: { shared: '^1', platform: '^1' } }),
    'node_modules/a/node_modules/nested': pkg('2.0.0', { peerDependencies: { peer: '^1', absent: '^1' }, peerDependenciesMeta: { absent: { optional: true } } }),
    'node_modules/a/node_modules/peer': pkg('1.0.0'),
    // peer 必须在其父级解析，不能借用被测包自己的 node_modules。
    'node_modules/a/node_modules/nested/node_modules/peer': pkg('9.0.0'),
    'node_modules/peer': pkg('3.0.0'),
    'node_modules/nested': pkg('8.0.0'),
    'node_modules/shared': pkg('1.0.0', { dev: true, optional: true, dependencies: { child: '^1' } }),
    'node_modules/child': pkg('1.0.0', { devOptional: true, optional: true, dependencies: { shared: '^1' } }),
    'node_modules/platform': pkg('1.0.0', { os: ['win32'], cpu: ['x64'], hasInstallScript: true }),
    'node_modules/z': pkg('1.0.0', { dependencies: { shared: '^1' } }),
    'node_modules/unused': pkg('1.0.0'),
  } };
  const original = structuredClone(source);
  const result = nodeHostDependencyLock(manifest, source);
  assert.deepEqual(source, original);
  assert.deepEqual(result.packages[''], { name: manifest.name, engines: manifest.engines, dependencies: manifest.dependencies });
  assert.equal(result.packages['node_modules/a/node_modules/nested'].version, '2.0.0');
  assert.equal(result.packages['node_modules/a/node_modules/peer'].version, '1.0.0');
  assert.deepEqual(result.packages['node_modules/platform'], { ...source.packages['node_modules/platform'], optional: true });
  for (const key of ['node_modules/shared', 'node_modules/child']) {
    assert.equal(result.packages[key].optional, undefined);
    assert.equal(result.packages[key].dev, undefined);
    assert.equal(result.packages[key].devOptional, undefined);
  }
  for (const key of ['node_modules/unused', 'node_modules/nested', 'node_modules/peer', 'node_modules/a/node_modules/nested/node_modules/peer']) {
    assert.equal(result.packages[key], undefined);
  }
  delete source.packages['node_modules/shared'];
  assert.throws(() => nodeHostDependencyLock(manifest, source), /依赖锁缺少.*shared/);
  assert.throws(() => nodeHostDependencyLock(manifest, { ...original, lockfileVersion: 2 }), /npm v3/);
  delete original.packages['node_modules/z'].integrity;
  assert.throws(() => nodeHostDependencyLock(manifest, original), /没有锁定的可发布制品.*node_modules\/z/);
});

test('实际Bun冻结安装消费构建生成的完整锁，不重新解析传递版本或执行原生安装脚本', async () => {
  const installation = join(runtime, 'locked-dependencies');
  await mkdir(installation);
  for (const name of ['package.json', 'package-lock.json']) {
    await writeFile(join(installation, name), await readFile(join(hostDirectory, name)));
  }
  const source = JSON.parse(await readFile(join(workbench, 'upstream/package-lock.json'), 'utf8'));
  const lockedText = await readFile(join(installation, 'package-lock.json'), 'utf8');
  const lock = JSON.parse(lockedText);
  assert.equal(lock.packages['node_modules/@vscode/spdlog/node_modules/node-addon-api'].version, '7.1.0');
  assert.equal(source.packages['node_modules/node-addon-api'].version, '6.1.0');
  const result = spawnSync('bun', ['install', '--frozen-lockfile', '--ignore-scripts'], {
    cwd: installation, encoding: 'utf8', timeout: 30000,
  });
  await Promise.all([
    writeFile(join(run.artifactsDir, 'frozen-install.stdout.raw'), result.stdout ?? ''),
    writeFile(join(run.artifactsDir, 'frozen-install.stderr.raw'), result.stderr ?? ''),
    writeFile(join(run.artifactsDir, 'frozen-install.json'), JSON.stringify({
      command: ['bun', 'install', '--frozen-lockfile', '--ignore-scripts'], cwd: installation,
      status: result.status, signal: result.signal, error: result.error?.message,
    }, null, 2) + '\n'),
  ]);
  assert.equal(result.status, 0, `${result.error?.message ?? ''}\n${result.stdout}\n${result.stderr}`);
  assert.equal(await readFile(join(installation, 'package-lock.json'), 'utf8'), lockedText);
  assert.match(await readFile(join(installation, 'bun.lock'), 'utf8'), /lockfileVersion/);
  const installed = new Set();
  async function collect(directory) {
    const entries = await readdir(directory, { withFileTypes: true }).catch(error => {
      if (error.code === 'ENOENT') return [];
      throw error;
    });
    for (const entry of entries) {
      if (entry.name.startsWith('.')) continue;
      const location = join(directory, entry.name);
      if (entry.name.startsWith('@')) { await collect(location); continue; }
      const pkg = JSON.parse(await readFile(join(location, 'package.json'), 'utf8'));
      installed.add(`${pkg.name}@${pkg.version}`);
      await collect(join(location, 'node_modules'));
    }
  }
  await collect(join(installation, 'node_modules'));
  const expected = new Set();
  for (const [location, entry] of Object.entries(lock.packages)) {
    if (!location) continue;
    const name = entry.name ?? location.split('node_modules/').at(-1);
    const key = `${name}@${entry.version}`;
    expected.add(key);
    if (!entry.optional) assert.equal(installed.has(key), true, `缺少锁定的必需依赖 ${key}`);
    const { optional, ...value } = entry;
    const { dev, devOptional, optional: originalOptional, ...original } = source.packages[location];
    assert.deepEqual(value, original, `制品或解析元数据改变：${location}`);
  }
  for (const key of installed) assert.equal(expected.has(key), true, `安装器引入未锁定版本：${key}`);
  const fromSpdlog = createRequire(join(installation, 'node_modules/@vscode/spdlog/package.json'));
  assert.equal(fromSpdlog('node-addon-api/package.json').version, '7.1.0');
  const resolution = [];
  const visited = new Set();
  async function inspect(owner, actualOwner) {
    const pair = JSON.stringify([owner, actualOwner]);
    if (visited.has(pair)) return;
    visited.add(pair);
    const entry = lock.packages[owner];
    const dependencies = { ...entry.peerDependencies, ...entry.dependencies, ...entry.optionalDependencies };
    for (const name of Object.keys(dependencies)) {
      const optional = name in (entry.optionalDependencies ?? {})
        || !(name in (entry.dependencies ?? {})) && entry.peerDependenciesMeta?.[name]?.optional === true;
      const peer = name in (entry.peerDependencies ?? {}) && !(name in (entry.dependencies ?? {}))
        && !(name in (entry.optionalDependencies ?? {}));
      // 按原锁的物理布局独立取得期望；安装器可提升目录，但每一条依赖解析必须保留原版本。
      let ancestor = peer ? owner.slice(0, owner.lastIndexOf('node_modules/')).replace(/\/$/, '') : owner;
      let expectedLocation;
      for (;;) {
        const candidate = posix.join(ancestor, 'node_modules', name);
        if (lock.packages[candidate]) { expectedLocation = candidate; break; }
        if (!ancestor) break;
        ancestor = posix.dirname(ancestor);
        if (ancestor === '.') ancestor = '';
      }
      if (!expectedLocation) { assert.equal(optional, true, `原锁缺少 ${owner} -> ${name}`); continue; }
      const expectedEntry = lock.packages[expectedLocation];
      // buffer 等 npm 包可与 Node 内置模块同名；锁中制品仍已检查，裸模块名的真实解析属于 Node。
      if (isBuiltin(name)) { resolution.push({ owner, dependency: name, builtin: true }); continue; }
      const applies = (values, current) => !values || !values.includes(`!${current}`)
        && (values.includes(current) || values.every(value => value.startsWith('!')));
      const supported = applies(expectedEntry.os, process.platform) && applies(expectedEntry.cpu, process.arch);
      const require = createRequire(join(peer ? dirname(actualOwner) : actualOwner, 'package.json'));
      let actual;
      for (const directory of require.resolve.paths(name)) {
        if (!directory.startsWith(installation + sep)) continue;
        try {
          const location = join(directory, name);
          actual = { location, pkg: JSON.parse(await readFile(join(location, 'package.json'), 'utf8')) };
          break;
        } catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
      if (!actual && optional && !supported) continue;
      assert.ok(actual, `安装后缺少 ${owner} -> ${name}`);
      assert.equal(actual.pkg.version, expectedEntry.version, `安装器改变 ${owner} -> ${name} 的解析`);
      assert.equal(actual.pkg.name, expectedEntry.name ?? name);
      resolution.push({ owner, dependency: name, expectedLocation, actual: actual.location, version: actual.pkg.version });
      await inspect(expectedLocation, actual.location);
    }
  }
  await inspect('', installation);
  await writeFile(join(run.artifactsDir, 'frozen-install-resolution.json'), JSON.stringify(resolution, null, 2) + '\n');
});

test('指定Node实际载入native模块，原生日志写盘且ripgrep读取目标文件', async () => {
  const require = createRequire(join(hostDirectory, 'package.json'));
  assert.equal(typeof require('@vscode/native-watchdog').start, 'function');
  assert.equal(typeof require('kerberos').initializeClient, 'function');
  const spdlog = require('@vscode/spdlog');
  const logfile = join(run.artifactsDir, 'native-log-probe.log');
  const logger = await spdlog.createRotatingLogger('node-host-native-probe', logfile, 1024, 1);
  try { logger.info('node-host-native-probe'); logger.flush(); }
  finally { logger.drop(); }
  assert.match(await readFile(logfile, 'utf8'), /node-host-native-probe/);
  const { rgPath } = await import(pathToFileURL(require.resolve('@vscode/ripgrep-universal')).href);
  const target = join(runtime, 'ripgrep-probe.txt');
  await writeFile(target, '目标资产 node-host-native-probe\n');
  const result = spawnSync(rgPath, ['--fixed-strings', 'node-host-native-probe', target], { encoding: 'utf8', timeout: 5000 });
  assert.equal(result.status, 0, `${result.error?.message ?? ''}\n${result.stderr}`);
  assert.equal(result.stdout.trim(), '目标资产 node-host-native-probe');
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
  assert.deepEqual(await host.initialize('wrong-commit'), [false]);
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
    const storage = await inspect('host-probe.storage'), initialization = host.receipt.initialization;
    host.receipt.storage = storage;
    assert.equal(storage.syncError, '目标 Node 扩展暂不支持 setKeysForSync 跨设备状态同步');
    assert.deepEqual(storage.secretErrors, Array(4).fill('目标扩展暂不支持 SecretStorage；不能访问工作台的秘密存储'));
    assert.equal(storage.extensionUri, protocolTypes.URI.file(join(host.folder, 'extension')).toString());
    const expected = {
      globalStorageUri: protocolTypes.URI.joinPath(protocolTypes.URI.parse(initialization.environment.global_storage_home_uri), 'host-probe.node'),
      storageUri: protocolTypes.URI.joinPath(protocolTypes.URI.parse(initialization.environment.workspace_storage_home_uri), initialization.workspace.id, 'host-probe.node'),
      logUri: protocolTypes.URI.joinPath(protocolTypes.URI.parse(initialization.logs_location_uri), 'host-probe.node'),
    };
    for (const [name, uri] of Object.entries(expected)) {
      const location = storage.locations[name];
      assert.equal(location.uri, uri.toString());
      assert.equal(location.value, label);
      assert.equal(await readFile(fileURLToPath(location.file), 'utf8'), label);
      assert.notEqual(location.uri, storage.extensionUri);
    }
    assert.equal(host.receipt.calls.some(call => call.name === '$onDidActivateExtension'), true);
    assert.deepEqual(host.receipt.errors, []);
  }
  const delta = (versionId, fields = {}) => ({ versionId, toAdd: [], toRemove: [], myToAdd: [], myToRemove: [], addActivationEvents: {}, ...fields });
  const identifier = new protocolTypes.ExtensionIdentifier('host-probe.dynamic');
  async function dynamic(version) {
    const directory = join(a.folder, 'dynamic-' + version);
    await mkdir(directory);
    const manifest = { publisher: 'host-probe', name: 'dynamic', version, type: 'module', main: './extension.mjs',
      engines: { vscode: `^${metadata.version}` }, extensionKind: ['workspace'], activationEvents: [] };
    await writeFile(join(directory, 'package.json'), JSON.stringify(manifest));
    await writeFile(join(directory, 'extension.mjs'), `import * as vscode from 'vscode';
export function activate(context) {
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.dynamic', () => '${version}'));
}`);
    return { ...manifest, identifier, extensionLocation: protocolTypes.URI.file(directory),
      isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined' };
  }
  const [v1, v2] = await Promise.all([dynamic('1.0.0'), dynamic('2.0.0')]);
  await a.delta(delta(3, { toAdd: [v1], myToAdd: [identifier] }));
  assert.deepEqual((await inspectA('host-probe.extension-state')).current, [['host-probe.dynamic', '1.0.0'], ['host-probe.node', '1.0.0']]);
  await a.delta(delta(4, { toRemove: [identifier], toAdd: [v2] }));
  const updated = await inspectA('host-probe.extension-state');
  assert.deepEqual(updated.current, [['host-probe.dynamic', '2.0.0'], ['host-probe.node', '1.0.0']]);
  await a.delta(delta(3, { toRemove: [identifier], myToRemove: [identifier] }));
  await a.delta(delta(4, { toRemove: [identifier], myToRemove: [identifier] }));
  await assert.rejects(a.delta(delta(5, { toAdd: [v1, v2] })), /Duplicate extension description/);
  assert.deepEqual(await inspectA('host-probe.extension-state'), updated);
  await a.delta(delta(5, { toRemove: [identifier], myToRemove: [identifier] }));
  assert.deepEqual((await inspectA('host-probe.extension-state')).current, [['host-probe.node', '1.0.0']]);
  await a.delta(delta(6, { toAdd: [v2], myToAdd: [identifier] }));
  assert.equal(await a.activate(identifier), true);
  assert.equal(await inspectA('host-probe.dynamic'), '2.0.0');
  await assert.rejects(a.start(delta(7)), /already started/);
  const final = await inspectA('host-probe.extension-state');
  assert.equal(final.changes.length, 4);
  assert.deepEqual(final.changes.at(-1), updated.current);
  assert.deepEqual(await inspectB('host-probe.extension-state'), { current: [['host-probe.node', '2.0.0']], changes: [] });
  a.receipt.extensionUpdates = final;
  await a.stop();
  assert.equal((await inspectB()).native, 'B');
  await b.stop();
});

test('客户端断连回收自己的宿主，实际崩溃沿原通道结束且不重放命令', async () => {
  const disconnected = await connect('disconnect');
  await disconnected.initialize();
  disconnected.disconnect();
  await bounded(disconnected.exit, '客户端断连');
  const crashed = await connect('crash');
  const inspect = await crashed.initialize();
  assert.equal((await inspect()).native, 'crash');
  // 原宿主会拦截process.exit；用真实OS信号终止，Python核实原Process.wait和进程组。
  await assert.rejects(inspect('host-probe.crash'));
  await bounded(crashed.exit, '宿主崩溃');
});

test('Backend原关闭生命周期回收仍在运行的宿主，保留目标输出', async () => {
  const host = await connect('backend-shutdown');
  const inspect = await host.initialize();
  assert.equal((await inspect()).native, 'backend-shutdown');
  const control = createConnection({ host: '127.0.0.1', port: Number(process.env.BOXTEAM_TEST_HOST_BACKEND_PORT) });
  try {
    await once(control, 'connect');
    control.write('shutdown-owner\n');
    const [data] = await bounded(once(control, 'data'), 'Backend关闭原owner');
    assert.equal(data.toString(), 'closed\n');
    assert.deepEqual(await bounded(host.exit, 'Backend关闭宿主'), [false]);
  } finally { control.destroy(); }
});
