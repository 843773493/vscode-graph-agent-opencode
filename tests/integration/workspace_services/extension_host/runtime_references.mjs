import assert from 'node:assert/strict';
import { once } from 'node:events';
import { readFile, writeFile } from 'node:fs/promises';
import { createConnection } from 'node:net';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';

const [configuration] = process.argv.slice(2);
const { port, runtime, artifacts } = JSON.parse(await readFile(configuration, 'utf8'));
const metadata = JSON.parse(await readFile(join(runtime, 'version.json'), 'utf8'));
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
outfile: join(artifacts, 'protocol.mjs'), bundle: true, format: 'esm', platform: 'node',
packages: 'external', tsconfig: resolve('src/clients/workbench/tsconfig.jsonc') });
const m = await import(pathToFileURL(join(artifacts, 'protocol.mjs')).href);
const hosts = [], errors = [], receipts = [];

async function line(socket) {
  const bytes = [];
  for (;;) {
    const byte = socket.read(1);
    if (byte === null) { await once(socket, 'readable'); continue; }
    if (byte[0] === 10) return JSON.parse(Buffer.from(bytes).toString('utf8'));
    bytes.push(byte[0]);
  }
}
async function control(operation) {
  const socket = createConnection(port, '127.0.0.1');
  try { socket.write(operation + '\n'); return await line(socket); }
  finally { socket.destroy(); }
}
function description(entry, target) {
  return { ...entry.description, identifier: new m.ExtensionIdentifier(entry.description.identifier.value),
    extensionLocation: m.URI.revive(entry.description.extensionLocation),
    extensionKey: entry.extension_key, installationTarget: target, installationProfileId: entry.profile_id };
}
const delta = (versionId, fields = {}) => ({ versionId, toAdd: [], toRemove: [],
  myToAdd: [], myToRemove: [], addActivationEvents: {}, ...fields });

async function connect(label) {
  const socket = createConnection(port, '127.0.0.1');
  socket.write(label + '\n');
  const binding = await line(socket), initialization = binding.initialization;
  const nodeSocket = new m.NodeSocket(socket, label);
  const protocol = new m.PersistentProtocol({ socket: nodeSocket, loadEstimator: { hasHighLoad: () => false } });
  const exit = once(socket, 'close');
  const host = { socket, protocol };
  hosts.push(host);
  protocol.sendResume();
  const next = () => new Promise(resolve => {
    const listener = protocol.onMessage(value => { listener.dispose(); resolve(value); });
  });
  assert.equal((await next()).readUInt8(0), 2);
  const extension = description(binding.extension, binding.target);
  const initialized = next();
  protocol.send(m.VSBuffer.fromString(JSON.stringify({
    version: metadata.version, quality: 'stable', commit: metadata.commit,
    isTargetExtensionHost: true, parentPid: initialization.parent_pid,
    environment: { isExtensionDevelopmentDebug: false, appName: 'Runtime references', appHost: 'web',
      appLanguage: 'en', appUriScheme: 'boxteam', isExtensionTelemetryLoggingOnly: true,
      appRoot: m.URI.parse(initialization.environment.app_root_uri),
      globalStorageHome: m.URI.parse(initialization.environment.global_storage_home_uri),
      workspaceStorageHome: m.URI.parse(initialization.environment.workspace_storage_home_uri) },
    workspace: { id: initialization.workspace.id, name: initialization.workspace.name },
    extensions: { versionId: 1, allExtensions: [extension], myExtensions: [extension.identifier],
      activationEvents: {} },
    telemetryInfo: { sessionId: label, machineId: 'probe', sqmId: 'probe', devDeviceId: 'probe', firstSessionDate: '2026-11-07' },
    logLevel: 3, loggers: [], logsLocation: m.URI.parse(initialization.logs_location_uri), autoStart: false,
    remote: { isRemote: false, connectionData: null }, consoleForward: { includeStack: false, logNative: false }, uiKind: 2,
  })));
  assert.equal((await initialized).readUInt8(0), 1);
  const rpc = new m.RPCProtocol(protocol);
  host.rpc = rpc;
  const register = (actor, methods) => rpc.set(m.MainContext[actor], methods);
  const notify = (actor, names) => register(actor, Object.fromEntries(names.map(name => [name, () => {}])));
  notify('MainThreadLogger', ['$registerLogger', '$deregisterLogger']);
  notify('MainThreadTelemetry', ['$publicLog2', '$publicLog']);
  notify('MainThreadConsole', ['$logExtensionHostMessage']);
  const terminalCalls = [];
  register('MainThreadTerminalService', Object.fromEntries(['$registerProcessSupport', '$createTerminal', '$startSendingDataEvents', '$setEnvironmentVariableCollection']
    .map(name => [name, (...args) => terminalCalls.push({ name, args })])));
  notify('MainThreadSearch', ['$registerTextSearchProvider']);
  const taskCalls = [], debugCalls = [];
  for (const [actor, names, calls] of [
    ['MainThreadTask', ['$registerSupportedExecutions', '$registerTaskSystem', '$registerTaskProvider', '$fetchTasks', '$executeTask'], taskCalls],
    ['MainThreadDebugService', ['$registerDebugTypes', '$startDebugging', '$stopDebugging', '$registerDebugConfigurationProvider', '$registerDebugAdapterDescriptorFactory'], debugCalls],
  ]) register(actor, Object.fromEntries(names.map(name => [name, (...args) => calls.push({ name, args })])));
  notify('MainThreadCommands', ['$registerCommand', '$unregisterCommand']);
  notify('MainThreadFileSystem', ['$ensureActivation']);
  register('MainThreadWindow', { $getInitialState: () => ({ isFocused: true, isActive: true }) });
  register('MainThreadLanguageModelTools', { $getTools: () => [] });
  register('MainThreadStorage', { $initializeExtensionStorage: () => undefined });
  register('MainThreadErrors', { $onUnexpectedError: (...args) => errors.push(args) });
  register('MainThreadExtensionService', {
    $onWillActivateExtension() {}, $onDidActivateExtension() {}, $setPerformanceMarks() {},
    $onExtensionActivationError: (...args) => errors.push(args), $onExtensionRuntimeError: (...args) => errors.push(args),
  });
  const empty = { contents: {}, keys: [], overrides: [] };
  await rpc.getProxy(m.ExtHostContext.ExtHostConfiguration).$initializeConfiguration({
    defaults: empty, policy: empty, application: empty, userLocal: empty, userRemote: empty,
    workspace: empty, folders: [], configurationScopes: [],
  });
  await rpc.getProxy(m.ExtHostContext.ExtHostWorkspace).$initializeWorkspace({
    id: initialization.workspace.id, name: initialization.workspace.name,
    folders: initialization.workspace.folders.map((folder, index) => ({ uri: m.URI.parse(folder.native_uri), name: folder.name, index })) }, true);
  const service = rpc.getProxy(m.ExtHostContext.ExtHostExtensionService);
  await service.$startExtensionHost(delta(1));
  assert.equal(await service.$activate(extension.identifier,
    { startup: false, extensionId: extension.identifier, activationEvent: '*' }), true);
  return { extension, target: binding.target, service, terminalCalls, taskCalls, debugCalls,
    inspect: () => rpc.getProxy(m.ExtHostContext.ExtHostCommands).$executeContributedCommand('runtime.inspect'),
    async stop() { protocol.send(m.VSBuffer.wrap(Uint8Array.of(3))); assert.deepEqual(await exit, [false]); },
  };
}
try {
  const first = await connect('first'), second = await connect('second');
  const firstState = await first.inspect(), secondState = await second.inspect();
  assert.equal(firstState.version, '1.0.0');
  assert.equal(secondState.version, '1.0.0');
  assert.equal(firstState.hasManagementToken, false);
  assert.equal(secondState.hasManagementToken, false);
  assert.equal(firstState.hasReferenceCapability, false);
  assert.equal(secondState.hasReferenceCapability, false);
  for (const [host, state] of [[first, firstState], [second, secondState]]) {
    assert.equal(state.terminalErrors.length, 8);
    assert.equal(state.terminalErrors.every(message => message?.includes('目标终端服务')), true);
    assert.deepEqual(host.terminalCalls, []);
    assert.equal(state.debugErrors.length, 20);
    assert.equal(state.debugErrors.every(message => message?.includes('目标调试服务')), true);
    assert.equal(state.taskErrors.length, 10);
    assert.equal(state.taskErrors.every(message => message?.includes('目标任务执行服务')), true);
    assert.match(state.deprecatedTaskError, /目标任务执行服务/);
    assert.deepEqual(host.debugCalls, []); assert.deepEqual(host.taskCalls, []);
  }
  receipts.push({ terminalErrors: firstState.terminalErrors, terminalCalls: first.terminalCalls,
    debugErrors: firstState.debugErrors, debugCalls: first.debugCalls, taskErrors: firstState.taskErrors,
    deprecatedTaskError: firstState.deprecatedTaskError, taskCalls: first.taskCalls });
  const updated = await control('update');
  const third = await connect('third');
  assert.equal((await third.inspect()).version, '2.0.0');
  const dynamic = description(updated.dynamic, first.target);
  for (const change of [
    { extensionKey: 'missing-1.0.0' },
    { installationTarget: { ...first.target, backendId: updated.foreign_backend } },
    { extensionLocation: m.URI.file('/wrong-package') },
  ]) {
    await assert.rejects(first.service.$deltaExtensions(delta(2, { toAdd: [{ ...dynamic, ...change }], myToAdd: [dynamic.identifier] })));
    assert.deepEqual((await first.inspect()).ids, ['boxteam-probe.runtime']);
  }
  await first.service.$deltaExtensions(delta(2, { toAdd: [dynamic], myToAdd: [dynamic.identifier] }));
  assert.deepEqual((await first.inspect()).ids, ['boxteam-probe.dynamic', 'boxteam-probe.runtime']);
  await first.service.$deltaExtensions(delta(3, { toRemove: [dynamic.identifier], myToRemove: [dynamic.identifier] }));
  assert.deepEqual((await first.inspect()).ids, ['boxteam-probe.runtime']);
  receipts.push(await control('handoff-and-uninstall'));
  assert.equal((await first.inspect()).version, '1.0.0');
  assert.equal((await second.inspect()).version, '1.0.0');
  assert.equal((await third.inspect()).version, '2.0.0');
  receipts.push(await control('assert-retained'));
  await first.stop();
  receipts.push(await control('assert-second'));
  assert.equal((await second.inspect()).version, '1.0.0');
  await second.stop();
  await third.stop();
  receipts.push(await control('assert-settled'));
  assert.deepEqual(errors, []);
} finally {
  for (const host of hosts) { host.rpc?.dispose(); host.protocol.dispose(); host.socket.destroy(); }
  await writeFile(join(artifacts, 'runtime-references.json'), JSON.stringify({ receipts, errors }, null, 2) + '\n');
}
