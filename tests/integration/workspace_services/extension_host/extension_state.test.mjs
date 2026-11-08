import { afterAll, expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import ts from '../../../../src/clients/workbench/node_modules/typescript/lib/typescript.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_state.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'extension-state.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
const browserSourcePath = join(root, 'upstream/src/vs/workbench/services/extensions/browser/extensionService.ts');
const browserSource = ts.createSourceFile(browserSourcePath, await transform(browserSourcePath, await readFile(browserSourcePath, 'utf8')), ts.ScriptTarget.Latest, true);
const browserOwner = browserSource.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtensionService');
const restartHook = browserOwner.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(browserSource) === '_prepareExtensionHostRestart');
if (!restartHook) throw new Error('未找到真实 Browser 恢复 hook');
await build({ stdin: { contents: `
export { ExtensionHostExtensions } from '${upstream}workbench/services/extensions/common/extensions.js';
export { AbstractExtHostExtensionService } from '${upstream}workbench/api/common/extHostExtensionService.js';
export { AbstractExtensionService } from '${upstream}workbench/services/extensions/common/abstractExtensionService.js';
export { ExtensionHostManager } from '${upstream}workbench/services/extensions/common/extensionHostManager.js';
export { LazyCreateExtensionHostManager } from '${upstream}workbench/services/extensions/common/lazyCreateExtensionHostManager.js';
export { MainThreadExtensionService } from '${upstream}workbench/api/browser/mainThreadExtensionService.js';
export { RemoteRunningLocation, LocalWebWorkerRunningLocation } from '${upstream}workbench/services/extensions/common/extensionRunningLocation.js';
export { Emitter, Event } from '${upstream}base/common/event.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { ExtensionIdentifier, ExtensionIdentifierMap, extensionInstanceKey } from '${upstream}platform/extensions/common/extensions.js';
export { URI } from '${upstream}base/common/uri.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { RecoveryBrowserExtensionService } from 'recovery-browser-owner';
export { scanTargetExtensions } from './src/clients/workbench/src/extensions/inventory.js';
export { IconExtensionPoint } from '${upstream}workbench/services/themes/common/iconExtensionPoint.js';
export { getIconRegistry } from '${upstream}platform/theme/common/iconRegistry.js';
export { MenuRegistry } from '${upstream}platform/actions/common/actions.js';
import '${upstream}workbench/services/actions/common/menusExtensionPoint.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [{ name: 'explicit-window-timer-service-boundary', setup(builder) {
    // 运行产品实际转换后的 Browser hook；浏览器构造／页面服务不是本组 owner 边界。
    builder.onResolve({ filter: /^recovery-browser-owner$/ }, () => ({ path: 'recovery-browser-owner', namespace: 'recovery-browser-owner' }));
    builder.onLoad({ filter: /.*/, namespace: 'recovery-browser-owner' }, () => ({ contents: `
      import { AbstractExtensionService } from '${upstream}workbench/services/extensions/common/abstractExtensionService.js';
      import { ExtensionHostKind } from '${upstream}workbench/services/extensions/common/extensionHostKind.js';
      import { installationTargetKey } from '${upstream}platform/extensions/common/extensions.js';
      import { reopenTargetWebHost } from './src/clients/workbench/src/extensions/inventory.js';
      export class RecoveryBrowserExtensionService extends AbstractExtensionService { ${restartHook.getText(browserSource)} }
    `, loader: 'ts', resolveDir: resolve('.') }));
    builder.onLoad({ filter: /\/src\/agentApi\.ts$/ }, () => ({ contents: 'export const gateway = { request: (...args) => globalThis.__extensionRecoveryInventory(...args) }; export class GatewayRequestError extends Error {}', loader: 'ts' }));
    // 本片不运行窗口启动性能服务；只保留同一DI标识，避免在Node装具初始化DOM。
    builder.onLoad({ filter: /\/services\/timer\/browser\/timerService\.ts$/ }, args => ({
      contents: "import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js'; export const ITimerService = createDecorator('timerService');",
      loader: 'ts', resolveDir: dirname(args.path),
    }));
  } }, buildSourcePlugin(root, transform)],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const id = value => new m.ExtensionIdentifier(value);
const targetA = { kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' };
const locationA = () => new m.RemoteRunningLocation(targetA, 'default', ['workspace-a']);
const description = (name, overrides = {}) => ({ identifier: id(name), name, publisher: 'probe', version: '1.0.0',
  engines: { vscode: '*' }, main: './extension.js', extensionLocation: m.URI.file('/extensions/' + name),
  isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined', installationTarget: targetA, ...overrides });
const delta = (versionId, fields = {}) => ({ versionId, toAdd: [], toRemove: [], myToAdd: [], myToRemove: [], addActivationEvents: {}, ...fields });
const base = description('probe.base', { activationEvents: ['onBase'] });
const receipt = { boundary: '编译真实发送端描述类、接收端构造器／RPC方法、原Registry、Sequencer、路径索引、原owner／Manager、实际转换后的Browser恢复hook、产品Web资格类及原命令icon／icon font消费者。进程／Worker执行、RPC字节、主线程服务、eager activation与Gateway HTTP／WebSocket回执为具名边界；fsRealpath在具名边界阻塞或失败。真实Node／Worker及Gateway产品入口另验。', checks: [] };

function receiver(realpath = async path => path, { all = [base], mine = all.map(extension => extension.identifier), mainThread = {} } = {}) {
  // 不复制receiver状态机；仅替换本用例不执行的扩展激活和主线程服务。
  class Receiver extends m.AbstractExtHostExtensionService {
    _getEntryPoint(extension) { return extension.main; }
    _startExtensionHost() { this._started = true; return this.startup ?? Promise.resolve(); }
  }
  const service = new Receiver({ createChild: () => ({ dispose() {} }) }, { fsRealpath: realpath }, { getProxy: () => mainThread },
    {}, {}, new m.NullLogService(), { extensions: { versionId: 1, allExtensions: all, myExtensions: mine,
      activationEvents: { 'probe.base': ['onBase'] } }, remote: { connectionData: null } }, {}, {}, {}, {}, {}, {});
  const states = [];
  const read = () => ({ version: service._extensionStateVersion,
    all: service._globalRegistry.getAllExtensionDescriptions().map(extension => [extension.identifier.value, extension.version]),
    mine: service._myRegistry.getAllExtensionDescriptions().map(extension => [extension.identifier.value, extension.version]),
    oldEvent: service._globalRegistry.containsActivationEvent('onBase'),
    nextEvent: service._globalRegistry.containsActivationEvent('onNext') });
  const listeners = [service._globalRegistry.onDidChange(() => states.push(read())), service._myRegistry.onDidChange(() => states.push(read()))];
  return { service, states, read, close() { listeners.forEach(listener => listener.dispose()); service.dispose(); } };
}

test('完整快照比较位置、版本、入口及贡献，同版本只能表达相同状态', () => {
  for (const changes of [{ extensionLocation: m.URI.file('/other/base') }, { version: '2.0.0' }, { main: './new.js' },
    { contributes: { commands: [{ command: 'probe.next', title: 'Next' }] } }, { isBuiltin: true }]) {
    const host = new m.ExtensionHostExtensions(1, [base], [base.identifier]);
    expect(host.set(1, [{ ...base, extensionLocation: m.URI.parse(base.extensionLocation.toString()) }], [id('PROBE.BASE')]).toAdd).toEqual([]);
    const next = { ...base, ...changes };
    expect(() => host.set(1, [next], [base.identifier])).toThrow('conflicting snapshot');
    expect(host.versionId).toBe(1);
    const changed = host.set(2, [next], [base.identifier]);
    expect(changed.toAdd).toEqual([next]);
    expect(changed.toRemove).toEqual([base.identifier]);
    expect(host.versionId).toBe(2);
  }
  receipt.checks.push('等版本初始化／位置／版本／入口／声明差异');
  const other = description('probe.other');
  const host = new m.ExtensionHostExtensions(1, [base, other], [base.identifier, other.identifier]);
  host.set(1, [other, base], [other.identifier, base.identifier]);
  expect(host.allExtensions).toEqual([base, other]);
  expect(host.myExtensions).toEqual([base.identifier, other.identifier]);
});

test('重复ID和游离归属原子拒绝，成功delta推进版本并丢弃旧回放', () => {
  expect(() => new m.ExtensionHostExtensions(1, [base, { ...base, identifier: id('PROBE.BASE') }], [])).toThrow('Duplicate');
  expect(() => new m.ExtensionHostExtensions(1, [base], [id('absent')])).toThrow('absent');
  const host = new m.ExtensionHostExtensions(1, [base], [base.identifier]);
  expect(host.containsActivationEvent('onBase')).toBe(true);
  const next = description('probe.next');
  const snapshot = host.toSnapshot();
  for (const invalid of [delta(2, { toRemove: [base.identifier, base.identifier] }),
    delta(2, { toAdd: [next, next] }), delta(2, { toAdd: [next], myToAdd: [next.identifier, next.identifier] }),
    delta(2, { toRemove: [base.identifier] }), delta(2, { myToAdd: [id('absent')] })]) {
    expect(() => host.delta(invalid)).toThrow();
    expect(host.toSnapshot()).toEqual(snapshot);
  }
  const update = delta(2, { toRemove: [base.identifier], toAdd: [{ ...base, version: '2.0.0', activationEvents: [] }] });
  expect(host.delta(update)).toEqual(update);
  expect(host.versionId).toBe(2);
  expect(host.containsActivationEvent('onBase')).toBe(false);
  expect(host.delta(delta(2, { toRemove: [base.identifier] }))).toBeNull();
  expect(host.delta(delta(1, { myToRemove: [base.identifier] }))).toBeNull();
  expect(host.allExtensions[0].version).toBe('2.0.0');
  receipt.checks.push('非法候选不部分写入；已提交版本拒绝重复／旧delta');
});

test('接收端串行构建并原子发布两registry，慢索引不能被新delta越过', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const f = receiver(async path => { if (path === '/slow') { entered.resolve(); await release.promise; } return path; });
  try {
    const paths = await f.service.getExtensionPathIndex();
    const slow = description('probe.next', { extensionLocation: m.URI.file('/slow') });
    const fast = { ...slow, version: '2.0.0', extensionLocation: m.URI.file('/fast') };
    const first = f.service.$deltaExtensions(delta(2, { toAdd: [slow], myToAdd: [slow.identifier], addActivationEvents: { 'probe.next': ['onNext'] } }));
    await entered.promise;
    const second = f.service.$deltaExtensions(delta(3, { toRemove: [slow.identifier], toAdd: [fast] }));
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.read().version).toBe(1);
    expect(f.read().nextEvent).toBe(false);
    expect(f.states).toEqual([]);
    release.resolve(); await Promise.all([first, second]);
    expect(f.states.map(state => state.version)).toEqual([2, 2, 3, 3]);
    expect(f.states.every(state => JSON.stringify(state.all) === JSON.stringify(state.mine))).toBe(true);
    expect(f.read().nextEvent).toBe(false);
    expect(paths.findSubstr(m.URI.file('/fast/file.js')).version).toBe('2.0.0');
    expect(paths.findSubstr(m.URI.file('/slow/file.js'))).toBeUndefined();
    const final = f.read();
    await f.service.$deltaExtensions(delta(2, { toRemove: [base.identifier] }));
    await f.service.$deltaExtensions(delta(3, { toRemove: [base.identifier] }));
    expect(f.read()).toEqual(final); expect(f.states).toHaveLength(4);
    receipt.checks.push('慢路径索引串行；每条通知观察完整all/my/activation/version；旧delta无通知');
  } finally { release.resolve(); f.close(); }
});

test('索引失败保留原描述、激活事件和版本；同版本显式重试可恢复', async () => {
  let reject = true;
  const f = receiver(async path => { if (path === '/retry' && reject) { reject = false; throw new Error('目标realpath拒绝'); } return path; });
  try {
    const before = f.read(), paths = await f.service.getExtensionPathIndex();
    const next = { ...base, version: '2.0.0', extensionLocation: m.URI.file('/retry') };
    const update = delta(2, { toRemove: [base.identifier], toAdd: [next], addActivationEvents: { 'probe.base': ['onNext'] } });
    await expect(f.service.$deltaExtensions(update)).rejects.toThrow('目标realpath拒绝');
    expect(f.read()).toEqual(before); expect(f.states).toEqual([]);
    expect(paths.findSubstr(m.URI.joinPath(base.extensionLocation, 'file.js')).version).toBe('1.0.0');
    await f.service.$deltaExtensions(update);
    expect(f.read()).toMatchObject({ version: 2, oldEvent: false, nextEvent: true });
    expect(paths.findSubstr(m.URI.file('/retry/file.js')).version).toBe('2.0.0');
    await expect(f.service.$deltaExtensions(delta(3, { myToAdd: [id('absent')] }))).rejects.toThrow('absent');
    expect(f.read().version).toBe(2);
    await f.service.$deltaExtensions(delta(3));
    expect(f.read().version).toBe(3);
    receipt.checks.push('realpath真实失败不发布；失败缓存清除，同版本重试；非法归属拒绝后队列继续');
  } finally { f.close(); }
});

test('首次路径索引读取失败可显式重试，更新无需重建旧版本已失效路径', async () => {
  let failed = false;
  const f = receiver(async path => {
    if (!failed) { failed = true; throw new Error('初次索引失败'); }
    return path;
  });
  try {
    await expect(f.service.getExtensionPathIndex()).rejects.toThrow('初次索引失败');
    expect((await f.service.getExtensionPathIndex()).findSubstr(m.URI.joinPath(base.extensionLocation, 'file.js')).version).toBe('1.0.0');
    receipt.checks.push('首次索引失败不缓存永久失败，消费者显式重试可恢复');
  } finally { f.close(); }
  const g = receiver(async path => { if (path !== '/replacement') throw new Error('旧包已不可读'); return path; });
  try {
    await g.service.$deltaExtensions(delta(2, { toRemove: [base.identifier], toAdd: [{ ...base, extensionLocation: m.URI.file('/replacement') }] }));
    expect((await g.service.getExtensionPathIndex()).findSubstr(m.URI.file('/replacement/file.js')).version).toBe('1.0.0');
    receipt.checks.push('更新直接发布候选路径树，不为首次建立索引额外读取已失效旧包');
  } finally { g.close(); }
});

test('保留原依赖环及依赖闭包过滤，非循环扩展不受影响', async () => {
  const f = receiver();
  try {
    const a = description('probe.a', { extensionDependencies: ['probe.b'] });
    const b = description('probe.b', { extensionDependencies: ['probe.a'] });
    const c = description('probe.c', { extensionDependencies: ['probe.a'] });
    await f.service.$deltaExtensions(delta(2, { toAdd: [a, b, c], myToAdd: [a.identifier, b.identifier, c.identifier] }));
    expect(f.read().all).toEqual([['probe.base', '1.0.0']]);
    expect(f.read().mine).toEqual(f.read().all);
    receipt.checks.push('沿原registry过滤依赖环及依赖闭包');
  } finally { f.close(); }
});

test('首次等版本空start合法，重复／冲突start不写状态，eager等待不占更新队列', async () => {
  const f = receiver(), gate = Promise.withResolvers();
  try {
    const before = f.read();
    await expect(f.service.$startExtensionHost(delta(1, { toAdd: [description('probe.conflict')] }))).rejects.toThrow('conflicts');
    expect(f.read()).toEqual(before);
    f.service.startup = gate.promise;
    const start = f.service.$startExtensionHost(delta(1));
    await f.service.$deltaExtensions(delta(2));
    expect(f.read().version).toBe(2);
    gate.resolve(); await start;
    await expect(f.service.$startExtensionHost(delta(3))).rejects.toThrow('already started');
    expect(f.read().version).toBe(2);
    receipt.checks.push('等版本空启动；冲突／重复启动拒绝；激活等待不阻塞后续状态更新');
  } finally { gate.resolve(); f.close(); }
});

// 真实owner、collection、Manager和receiver；仅进程启动／RPC传输与主线程服务为替身。
function ownerFixture(f, { disconnectError, disposeError, sharedOwner, activate, disconnect,
  all = [base], mine = all.map(extension => extension.identifier), location = locationA(), registered = true } = {}) {
  const notifications = [], notified = Promise.withResolvers(), lifecycle = [], exit = new m.Emitter(), telemetry = [];
  const owner = sharedOwner ?? new m.AbstractExtensionService({ hasLocalProcess: false, allowRemoteExtensionsInLocalWebWorker: false },
    {}, {}, {}, {}, { error(error) { notifications.push(error); notified.resolve(); } }, {}, { publicLog2(...args) { telemetry.push(args); } },
    { onEnablementChanged: m.Event.None }, { onWillActivateFileSystemProvider: m.Event.None }, {},
    { onDidChangeProfile: m.Event.None, onDidEnableExtensions: m.Event.None,
      onDidInstallExtensions: m.Event.None, onDidUninstallExtension: m.Event.None },
    {}, {}, {}, new m.NullLogService(), {}, {}, { onWillShutdown: m.Event.None }, {}, {});
  const mirror = new m.ExtensionHostExtensions(1, all, mine);
  const proxy = { startExtensionHost: delta => f.service.$startExtensionHost(delta), deltaExtensions: delta => f.service.$deltaExtensions(delta),
    activate: activate ?? (async () => false) };
  class Manager extends m.ExtensionHostManager {
    _createExtensionHostCustomers() { return proxy; }
  }
  const host = { runningLocation: location, startup: 2, extensions: mirror, onExit: exit.event,
    start: async () => ({}),
    async disconnect() { lifecycle.push('disconnect'); exit.fire([0, null]); await disconnect?.(); if (disconnectError) throw disconnectError; },
    dispose() { lifecycle.push('dispose'); if (disposeError) throw disposeError; } };
  const internal = owner._acquireInternalAPI(host);
  const manager = new Manager(host, [], internal, {}, {}, { publicLog2() {} }, new m.NullLogService());
  let exits = 0;
  const listeners = new m.DisposableStore();
  listeners.add(manager.onDidExit(() => { exits++; }));
  owner._extensionHostManagers.add(manager, listeners);
  return { owner, manager, mirror, internal, telemetry, notifications, notified: notified.promise, lifecycle, exits: () => exits,
    async prepare() {
      await manager.ready();
      const lock = await owner._registry.acquireLock('test initial state');
      try { owner._registry.deltaExtensions(lock, all, []); } finally { lock.dispose(); }
      for (const extensionId of mine) owner._runningLocations.set(extensionId, host.runningLocation);
      if (registered) owner._installedExtensionsReady.open();
    },
    close() { owner.dispose(); exit.dispose(); } };
}

test('真实Manager更新被receiver拒绝后，原owner移除失败代际并保留另一宿主', async () => {
  const f = receiver(async path => { if (path === '/rejected') throw new Error('目标索引拒绝'); return path; });
  const o = ownerFixture(f);
  const peer = { startup: 0, disconnect: async () => {}, dispose() {} };
  try {
    await o.prepare();
    o.owner._extensionHostManagers.add(peer, new m.DisposableStore());
    const next = { ...base, extensionLocation: m.URI.file('/rejected'), version: '2.0.0' };
    const removed = new m.ExtensionIdentifierMap();
    removed.set(base.identifier, locationA());
    const lock = await o.owner._registry.acquireLock('publish replacement before host projection');
    try { o.owner._registry.deltaExtensions(lock, [next], [base]); } finally { lock.dispose(); }
    await expect(o.owner._updateExtensionsOnExtHost(o.manager, 2, [next], [base.identifier], removed)).rejects.toThrow('目标索引拒绝');
    // mirror已推送的版本不能继续使用；由原collection销毁整个失败代际。
    expect(o.mirror.versionId).toBe(2); expect(f.read().version).toBe(1);
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([peer]);
    expect(o.lifecycle).toEqual(['disconnect', 'dispose']);
    expect(o.exits()).toBe(0);
    expect(o.notifications).toHaveLength(1);
    expect(o.notifications[0]).toContain('目标索引拒绝');
    receipt.checks.push('真实Manager乐观mirror失败后由唯一owner移除／断连／释放；peer保留、退出订阅先撤销、原错误向调用者传播');
  } finally { o.close(); f.close(); }
});

test('原启动owner观察fire-and-forget失败，不把错误代际留在collection', async () => {
  const f = receiver(async () => { throw new Error('初始路径索引拒绝'); });
  const o = ownerFixture(f);
  try {
    await o.prepare();
    o.owner._startOnDemandExtensionHosts();
    await o.notified;
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([]);
    expect(o.lifecycle).toEqual(['disconnect', 'dispose']);
    expect(o.exits()).toBe(0);
    expect(o.notifications[0]).toContain('初始路径索引拒绝');
    receipt.checks.push('原启动owner明确观察start拒绝，移除原代际并通知，而非未处理rejection');
  } finally { o.close(); f.close(); }
});

test('原Manager的start等待eager时，owner仍能完成真实receiver状态更新', async () => {
  const f = receiver(), o = ownerFixture(f), gate = Promise.withResolvers();
  try {
    await o.prepare();
    f.service.startup = gate.promise;
    o.owner._startOnDemandExtensionHosts();
    await o.owner._updateExtensionsOnExtHost(o.manager, 2, [], [], new m.ExtensionIdentifierMap());
    expect(f.read().version).toBe(2);
    expect(o.mirror.versionId).toBe(2);
    expect(o.notifications).toEqual([]);
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([o.manager]);
    gate.resolve();
    receipt.checks.push('真实Manager/owner不持队列等待eager；启动期间状态更新可以完成');
  } finally { gate.resolve(); o.close(); f.close(); }
});

test('失败代际清理始终尝试disconnect和dispose，原更新错误与两项清理错误均保留', async () => {
  const f = receiver();
  const disconnected = new Error('断连失败'), disposed = new Error('释放失败');
  const o = ownerFixture(f, { disconnectError: disconnected, disposeError: disposed });
  try {
    await o.prepare();
    const original = new Error('状态更新失败');
    const error = await o.owner._stopFailedExtensionHost(o.manager, original);
    expect(error.errors[0]).toBe(original);
    expect(error.errors[1].errors).toEqual([disconnected, disposed]);
    expect(o.lifecycle).toEqual(['disconnect', 'dispose']);
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([]);
    expect(o.notifications[0]).toContain('状态更新失败');
    expect(() => o.internal._onWillActivateExtension(base.identifier)).toThrow('generation is closed');
    receipt.checks.push('原RPC错误／断连错误／dispose错误全部保留，退出订阅撤销，不剩失败Manager');
  } finally { o.owner.dispose(); o.manager._extensionHost.dispose = () => {}; o.manager.dispose(); f.close(); }
});

test('关闭原Manager在断连等待前撤销内部回调，不污染同位置新代际的状态', async () => {
  const f = receiver(), o = ownerFixture(f), nextReceiver = receiver();
  let next;
  try {
    await o.prepare();
    await o.owner._extensionHostManagers.stopOne(o.manager);
    next = ownerFixture(nextReceiver, { sharedOwner: o.owner });
    await next.manager.ready();
    next.internal._onWillActivateExtension(base.identifier);
    const liveError = new Error('新代际错误');
    next.internal._onExtensionRuntimeError(base.identifier, liveError);
    const reason = { startup: false, extensionId: base.identifier, activationEvent: 'api' };
    for (const callback of [
      () => o.internal._onWillActivateExtension(base.identifier),
      () => o.internal._onDidActivateExtension(base.identifier, 1, 2, 3, reason),
      () => o.internal._onDidActivateExtensionError(base.identifier, new Error('旧激活错误')),
      () => o.internal._onExtensionRuntimeError(base.identifier, new Error('旧运行错误')),
    ]) expect(callback).toThrow('generation is closed');
    await expect(o.internal._activateById(base.identifier, reason)).rejects.toThrow('generation is closed');
    expect(o.owner.getExtensionsStatus()[m.extensionInstanceKey(base)].runtimeErrors).toEqual([liveError]);
    expect(o.owner.getExtensionsStatus()[m.extensionInstanceKey(base)].activationTimes).toBeUndefined();
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([next.manager]);
    receipt.checks.push('原Manager关闭先撤销回调；同位置新代际保持状态和归属；旧激活及四类状态回调明确拒绝');
  } finally { o.close(); nextReceiver.close(); f.close(); }
});

test('实际断连尚未完成或直接dispose时，原内部回调均已撤销', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const f = receiver(), o = ownerFixture(f, { disconnect: async () => { entered.resolve(); await release.promise; } });
  let closing;
  try {
    await o.prepare();
    closing = o.owner._extensionHostManagers.stopOne(o.manager);
    await entered.promise;
    expect(o.lifecycle).toEqual(['disconnect']);
    expect(() => o.internal._onWillActivateExtension(base.identifier)).toThrow('generation is closed');
    release.resolve(); await closing;
    const direct = ownerFixture(f);
    try {
      await direct.manager.ready();
      direct.manager.dispose();
      expect(() => direct.internal._onExtensionRuntimeError(base.identifier, new Error('迟到'))).toThrow('generation is closed');
    } finally { direct.close(); }
    receipt.checks.push('真实断连在阻塞时已撤销回调；直接dispose也撤销，不依赖transport清理成功');
  } finally { release.resolve(); if (closing) await closing; o.close(); f.close(); }
});

test('旧代际已准入激活迟到完成明确失败，不转交或重放到替换宿主', async () => {
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const f = receiver(), nextReceiver = receiver();
  let attempts = 0, nextAttempts = 0;
  const o = ownerFixture(f, { activate: async () => { attempts++; entered.resolve(); await release.promise; return true; } });
  let activation, next;
  try {
    await o.prepare();
    activation = o.internal._activateById(base.identifier, { startup: false, extensionId: base.identifier, activationEvent: 'api' });
    await entered.promise;
    await o.owner._extensionHostManagers.stopOne(o.manager);
    next = ownerFixture(nextReceiver, { sharedOwner: o.owner, activate: async () => { nextAttempts++; return true; } });
    await next.manager.ready();
    release.resolve();
    await expect(activation).rejects.toThrow('generation is closed');
    expect(attempts).toBe(1); expect(nextAttempts).toBe(0);
    expect(o.owner._extensionHostManagers.map(manager => manager)).toEqual([next.manager]);
    receipt.checks.push('原service→真实Manager→受控RPC的激活结算被旧回调代际拒绝；没有重发至替换宿主');
  } finally { release.resolve(); if (activation) await activation.catch(() => {}); o.close(); nextReceiver.close(); f.close(); }
});

test('惰性Manager未创建实际进程时关闭也撤销原回调准入', async () => {
  const f = receiver(), o = ownerFixture(f);
  let disposed = 0;
  const host = { startup: 3, runningLocation: locationA(), onExit: m.Event.None, dispose() { disposed++; } };
  const internal = o.owner._acquireInternalAPI(host);
  const lazy = new m.LazyCreateExtensionHostManager(host, [], internal,
    { createInstance() { throw new Error('不应启动惰性宿主'); } }, new m.NullLogService());
  try {
    await lazy.disconnect();
    expect(() => internal._onWillActivateExtension(base.identifier)).toThrow('generation is closed');
    lazy.dispose();
    expect(disposed).toBe(1);
    receipt.checks.push('惰性Manager未启动即关闭也撤销内部API，无新进程或回调fallback');
  } finally { o.close(); f.close(); }
});

function mainThread(internal, { workbench = {}, notifications = [], kind = 3, enablement = {} } = {}) {
  return new m.MainThreadExtensionService({ extensionHostKind: kind, internalExtensionService: internal,
    getProxy: () => ({}), _setExtensionHostProxy() {}, _setAllMainProxyIdentifiers() {} },
    { error(error) { notifications.push(error); }, notify(notification) { notifications.push(notification); } },
    workbench, {}, enablement, {}, {}, { isBuilt: true, isExtensionDevelopment: false });
}

test('原MainThread查询读取发起host快照，同ID版本不借全局描述，等待注册时关闭拒绝迟到结果', async () => {
  const a = { ...base, version: '1.0.0' }, b = { ...base, version: '2.0.0' }, hidden = description('probe.hidden');
  const f = receiver(), o = ownerFixture(f, { all: [a], registered: false });
  const thread = mainThread(o.internal);
  try {
    await o.prepare();
    const lock = await o.owner._registry.acquireLock('window has another version');
    try { o.owner._registry.deltaExtensions(lock, [b, hidden], [base.identifier]); } finally { lock.dispose(); }
    const query = thread.$getExtension('PROBE.BASE');
    o.owner._installedExtensionsReady.open();
    expect(await query).toBe(a);
    expect(await thread.$getExtension(hidden.identifier.value)).toBeUndefined();
    o.mirror.delta(delta(2, { toRemove: [a.identifier], toAdd: [b] }));
    expect(await thread.$getExtension(base.identifier.value)).toBe(b);
    await o.owner._extensionHostManagers.stopOne(o.manager);
    await expect(thread.$getExtension(base.identifier.value)).rejects.toThrow('generation is closed');

    const waiting = ownerFixture(f, { registered: false });
    try {
      await waiting.prepare();
      const pending = mainThread(waiting.internal).$getExtension(base.identifier.value);
      await waiting.owner._extensionHostManagers.stopOne(waiting.manager);
      waiting.owner._installedExtensionsReady.open();
      await expect(pending).rejects.toThrow('generation is closed');
    } finally { waiting.close(); }
    receipt.checks.push('真实MainThread查询仅读host当前快照，大小写不改公开ID；全局同ID／不可见项不借用，注册等待后再核代际');
  } finally { o.close(); f.close(); }
});

test('缺失依赖修复使用来报错host的扩展描述，不借全局同ID的名称或版本', async () => {
  const original = { ...base, displayName: '来源扩展v1' }, other = { ...base, displayName: '另一目标v2', version: '2.0.0' };
  const f = receiver(), o = ownerFixture(f, { all: [original] }), notifications = [];
  try {
    await o.prepare();
    const lock = await o.owner._registry.acquireLock('another description');
    try { o.owner._registry.deltaExtensions(lock, [other], [original.identifier]); } finally { lock.dispose(); }
    const thread = mainThread(o.internal, { notifications, enablement: { isEnabled: () => true },
      workbench: { queryLocal: async () => [{ identifier: { id: 'probe.dependency' }, local: { manifest: { displayName: '缺失依赖' } } }] } });
    await thread.$onExtensionActivationError(original.identifier, { $isError: true, name: 'Error', message: '无法加载依赖', stack: '' },
      { dependency: 'probe.dependency' });
    expect(notifications).toHaveLength(1);
    expect(notifications[0].message).toContain('来源扩展v1');
    expect(notifications[0].message).not.toContain('另一目标v2');
    receipt.checks.push('真实MainThread missing-dependency分支取来报错host描述，原修复提示不借窗口全局同ID');
  } finally { o.close(); f.close(); }
});

test('原MainThread激活只调用实际持有者，普通外部API、不可见及歧义目标均不广播', async () => {
  const local = { ...base, api: 'none' }, foreign = description('probe.foreign'), invisible = description('probe.invisible');
  const declarative = description('probe.declarative', { main: undefined, api: 'none' });
  const f = receiver(), otherReceiver = receiver();
  const calls = [];
  const a = ownerFixture(f, { all: [local, foreign, declarative], mine: [local.identifier], location: new m.LocalWebWorkerRunningLocation(0),
    activate: async (id) => { calls.push(['a', id.value]); return true; } });
  const b = ownerFixture(otherReceiver, { sharedOwner: a.owner, all: [local, foreign, invisible],
    mine: [local.identifier, foreign.identifier, invisible.identifier],
    activate: async (id) => { calls.push(['b', id.value]); return true; } });
  const reason = { startup: false, extensionId: local.identifier, activationEvent: 'api' };
  try {
    await a.prepare(); await b.manager.ready();
    await mainThread(a.internal).$activateExtension(id('PROBE.BASE'), reason);
    expect(calls).toEqual([['a', 'PROBE.BASE']]);
    await expect(mainThread(a.internal).$activateExtension(foreign.identifier, reason)).rejects.toThrow('not activatable');
    await expect(mainThread(a.internal).$activateExtension(invisible.identifier, reason)).rejects.toThrow('not activatable');
    await expect(mainThread(a.internal).$activateExtension(declarative.identifier, reason)).rejects.toThrow('not activatable');
    await expect(a.owner.activateById(local.identifier, reason)).rejects.toThrow('Ambiguous');
    expect(calls).toHaveLength(1);
    await a.owner.activateById(foreign.identifier, reason);
    expect(calls).toEqual([['a', 'PROBE.BASE'], ['b', 'probe.foreign']]);
    await expect(a.owner.activateById(id('absent'), reason)).rejects.toThrow('Unknown');
    receipt.checks.push('实际MainThread／owner／双Manager：发起host优先；无上下文同ID歧义零调用；只有唯一持有者被调用，不向peer广播');
  } finally { a.close(); otherReceiver.close(); f.close(); }
});

test('api:none依赖成功沿真实ExtHost activator结算，不向发起host重复派发', async () => {
  const dependency = description('probe.dependency', { api: 'none' });
  const dependent = { ...base, main: undefined, extensionDependencies: [dependency.identifier.value] };
  let thread;
  const f = receiver(undefined, { all: [dependent, dependency], mine: [dependent.identifier], mainThread: {
    $activateExtension: (...args) => thread.$activateExtension(...args),
    $onWillActivateExtension: (...args) => thread.$onWillActivateExtension(...args),
    $onDidActivateExtension: (...args) => thread.$onDidActivateExtension(...args),
    $publicLog2() {},
  } });
  const peerReceiver = receiver(), calls = [];
  const a = ownerFixture(f, { all: [dependent, dependency], mine: [dependent.identifier],
    activate: async () => { throw new Error('不能将跨host依赖派给发起者'); } });
  const b = ownerFixture(peerReceiver, { sharedOwner: a.owner, all: [dependency], activate: async extensionId => {
    calls.push(extensionId.value); return true;
  } });
  try {
    await a.prepare(); await b.manager.ready(); thread = mainThread(a.internal);
    const reason = { startup: false, extensionId: base.identifier, activationEvent: 'api' };
    await f.service.activateByIdWithErrors(dependent.identifier, reason);
    expect(calls).toEqual(['probe.dependency']);
    expect(f.service._activator.getActivatedExtension(dependency.identifier).activationFailed).toBe(false);
    expect(f.service._activator.getActivatedExtension(dependency.identifier).exports).toBeUndefined();
    receipt.checks.push('真实activator的api:none成功结算及无exports语义保留，实际RPC只有唯一owner一次');
  } finally { a.close(); peerReceiver.close(); f.close(); }
});

test('api:none依赖沿原activator交唯一持有者，失败仍以原依赖及发起扩展报告', async () => {
  const dependency = description('probe.dependency', { api: 'none' });
  const dependent = { ...base, extensionDependencies: [dependency.identifier.value] };
  let thread;
  const errorReports = [], calls = [];
  const f = receiver(undefined, { all: [dependent, dependency], mine: [dependent.identifier], mainThread: {
    $activateExtension: (...args) => thread.$activateExtension(...args),
    $onExtensionActivationError: (...args) => { errorReports.push(thread.$onExtensionActivationError(...args).then(() => null, error => error)); },
  } });
  const peerReceiver = receiver();
  const a = ownerFixture(f, { all: [dependent, dependency], mine: [dependent.identifier], location: new m.LocalWebWorkerRunningLocation(0),
    activate: async () => { throw new Error('不应向发起host派发依赖'); } });
  const b = ownerFixture(peerReceiver, { sharedOwner: a.owner, all: [dependency], activate: async extensionId => {
    calls.push(extensionId.value); throw new Error('依赖宿主真实边界失败');
  } });
  try {
    await a.prepare(); await b.manager.ready();
    thread = mainThread(a.internal);
    const reason = { startup: false, extensionId: dependent.identifier, activationEvent: 'api' };
    await expect(f.service.activateByIdWithErrors(dependent.identifier, reason)).rejects.toThrow('dependency');
    expect(calls).toEqual(['probe.dependency']);
    expect(await Promise.all(errorReports)).toEqual([null, null]);
    expect(a.telemetry.map(event => event[1].extensionId)).toEqual(['probe.dependency', 'probe.base']);
    expect(a.owner.getExtensionsStatus()[m.extensionInstanceKey(dependency)]).toMatchObject({ activationStarted: false, runtimeErrors: [] });
    expect(() => a.internal._onWillActivateExtension(dependency.identifier)).toThrow('does not belong');
    expect(() => a.internal._onExtensionRuntimeError(dependency.identifier, new Error('不属本host'))).toThrow('does not belong');
    receipt.checks.push('真实ExtHost activator→MainThread→owner→唯一Manager保留api:none失败归属；合法依赖错误可报告，不能写依赖运行状态');
  } finally { a.close(); peerReceiver.close(); f.close(); }
});

test('可见api:none缺少持有者或持有者拒绝时明确失败，无替代派发', async () => {
  const dependency = description('probe.dependency', { api: 'none' });
  const f = receiver(), a = ownerFixture(f, { all: [base, dependency], mine: [base.identifier],
    activate: async () => { throw new Error('不能派给非持有者'); } });
  const peerReceiver = receiver(); let attempts = 0;
  const reason = { startup: false, extensionId: base.identifier, activationEvent: 'api' };
  try {
    await a.prepare();
    await expect(mainThread(a.internal).$activateExtension(dependency.identifier, reason)).rejects.toThrow('Unknown');
    const b = ownerFixture(peerReceiver, { sharedOwner: a.owner, all: [dependency], activate: async () => { attempts++; return false; } });
    await b.manager.ready();
    await expect(mainThread(a.internal).$activateExtension(dependency.identifier, reason)).rejects.toThrow('was refused');
    const duplicate = ownerFixture(peerReceiver, { sharedOwner: a.owner, all: [dependency],
      activate: async () => { throw new Error('歧义时不能派发'); } });
    await duplicate.manager.ready();
    await expect(mainThread(a.internal).$activateExtension(dependency.identifier, reason)).rejects.toThrow('Ambiguous');
    expect(attempts).toBe(1);
    receipt.checks.push('可见api:none没有实际owner／实际owner拒绝／多owner歧义均失败，不向发起或其它host改投');
  } finally { a.close(); peerReceiver.close(); f.close(); }
});

test('新安装自动激活迁入唯一持有者，三类状态回调拒绝外部项且不改变owner', async () => {
  const f = receiver(); let calls = 0;
  const o = ownerFixture(f, { activate: async () => { calls++; return true; } });
  const peer = { containsExtension: () => false, disconnect: async () => {}, dispose() {},
    extensionHost: { extensions: new m.ExtensionHostExtensions(1, [], []) },
    activate() { throw new Error('不应广播到peer'); } };
  try {
    await o.prepare(); o.owner._extensionHostManagers.add(peer, new m.DisposableStore());
    await o.owner._activateAddedExtensionIfNeeded({ ...base, activationEvents: ['*'] });
    expect(calls).toBe(1);
    const unknown = id('probe.unknown'), reason = { startup: false, extensionId: base.identifier, activationEvent: 'api' };
    for (const callback of [() => o.internal._onWillActivateExtension(unknown),
      () => o.internal._onDidActivateExtension(unknown, 1, 2, 3, reason),
      () => o.internal._onExtensionRuntimeError(unknown, new Error('外部项')),
      () => o.internal._onDidActivateExtensionError(unknown, new Error('不可见项'))]) expect(callback).toThrow();
    expect(o.owner.getExtensionsStatus()['probe.unknown']).toBeUndefined();
    expect(o.owner._runningLocations.getRunningLocation(unknown)).toBeNull();
    receipt.checks.push('新安装activation同一路由；未知／非所属回调零状态及位置写入');
  } finally { o.close(); f.close(); }
});

const targetB = { kind: 'backend', gatewayId: 'gateway-b', backendId: '22222222-2222-4222-8222-222222222222' };
const hostView = (target, workspaceIds = ['workspace-a']) => ({ installationTarget: target, viewId: 'default', workspaceIds,
  capabilities: { installation_manage: true, host_manage: true }, nodeRuntime: { channel_state: 'available' } });
async function until(predicate) {
  const deadline = performance.now() + 2000;
  while (!predicate()) {
    if (performance.now() >= deadline) throw new Error('等待真实恢复状态超时');
    await new Promise(resolve => setTimeout(resolve, 1));
  }
}

// 仅进程／RPC字节和Worker执行为具名边界；Factory→原owner→collection→Manager→receiver均运行生产方法。
async function recoveryFixture({ all = [base], views = [hostView(targetA)], browser = false, disconnect } = {}) {
  const notifications = [], statuses = [], generations = [], calls = [];
  let owner;
  const factory = { createExtensionHost(running, location) {
    if (!location.installationTarget) return null;
    const view = running.workspaceViews.find(view => JSON.stringify(view.installationTarget) === JSON.stringify(location.installationTarget));
    if (!view || location.kind === 2 && !view.webHost || location.kind === 3 && !view.nodeRuntime) return null;
    const snapshot = owner._registry.getSnapshot(), descriptions = running.getHostExtensions(snapshot.extensions, location);
    const mine = running.filterByRunningLocation(descriptions, location).map(extension => extension.identifier);
    const f = receiver(undefined, { all: descriptions, mine }), exit = new m.Emitter();
    const lifetime = new m.DisposableStore(), generation = generations.length + 1;
    const host = { runningLocation: location, startup: 1, extensions: new m.ExtensionHostExtensions(snapshot.versionId, descriptions, mine),
      onExit: exit.event, start: async () => ({}),
      async disconnect() { exit.fire([0, null]); await disconnect?.(generation); },
      dispose() { lifetime.dispose(); } };
    if (location.kind === 2) {
      lifetime.add(view.webHost);
      lifetime.add(view.webHost.onDidClose(reason => exit.fire([1, reason])));
    }
    generations.push({ host, f, exit, generation, view, lifetime });
    return host;
  } };
  const Owner = browser ? m.RecoveryBrowserExtensionService : m.AbstractExtensionService;
  owner = new Owner({ hasLocalProcess: false, allowRemoteExtensionsInLocalWebWorker: false }, {}, factory, {}, {},
    { error: error => notifications.push(String(error)), status: message => { statuses.push(message); return { dispose() {} }; } },
    { debugExtensionHost: {} }, { publicLog2() {} }, { onEnablementChanged: m.Event.None }, { onWillActivateFileSystemProvider: m.Event.None }, {},
    { onDidChangeProfile: m.Event.None, onDidEnableExtensions: m.Event.None, onDidInstallExtensions: m.Event.None, onDidUninstallExtension: m.Event.None },
    {}, {}, {}, new m.NullLogService(), {}, {}, { onWillShutdown: m.Event.None }, {}, {});
  owner._doCreateExtensionHostManager = function (host, initialEvents) {
    const internal = this._acquireInternalAPI(host), item = generations.find(item => item.host === host);
    item.internal = internal;
    const proxy = { startExtensionHost: delta => item.f.service.$startExtensionHost(delta),
      deltaExtensions: delta => item.f.service.$deltaExtensions(delta),
      activate: async (extension, reason) => {
        calls.push({ generation: item.generation, type: 'activate', id: extension.value, reason });
        internal._onWillActivateExtension(extension); internal._onDidActivateExtension(extension, 1, 2, 3, reason); return true;
      },
      activateByEvent: async event => { calls.push({ generation: item.generation, type: 'event', event }); },
    };
    class Manager extends m.ExtensionHostManager { _createExtensionHostCustomers() { return proxy; } }
    return item.manager = new Manager(host, initialEvents, internal, {}, {}, { publicLog2() {} }, new m.NullLogService());
  };
  owner._setExtensionWorkspaceViews(views);
  const lock = await owner._registry.acquireLock('recovery fixture inventory');
  try { owner._registry.deltaExtensions(lock, all, []); } finally { lock.dispose(); }
  for (const extension of all) {
    const view = views.find(view => JSON.stringify(view.installationTarget) === JSON.stringify(extension.installationTarget));
    const location = view.webHost && extension.browser
      ? new m.LocalWebWorkerRunningLocation(0, view.installationTarget, view.viewId, view.workspaceIds)
      : new m.RemoteRunningLocation(view.installationTarget, view.viewId, view.workspaceIds);
    owner._runningLocations.set(extension.identifier, location, extension.installationTarget);
  }
  owner._installedExtensionsReady.open();
  owner._startExtensionHostsIfNecessary(true, []);
  await Promise.all(generations.map(item => item.manager.ready()));
  return { owner, calls, generations, notifications, statuses,
    settle: () => until(() => owner._extensionHostRestarts._toDispose.size === 0),
    close() { owner.dispose(); for (const item of generations) { item.f.close(); item.exit.dispose(); } } };
}

test('真实退出重建精确位置，只恢复已完成激活，不重放命令事件或另一宿主', async () => {
  const sibling = description('probe.pending'), peer = { ...base, installationTarget: targetB, version: '2.0.0' };
  const f = await recoveryFixture({ all: [base, sibling, peer], views: [hostView(targetA), hostView(targetB, ['workspace-b'])] });
  try {
    const [a, b] = f.generations;
    const reason = { startup: false, extensionId: base.identifier, activationEvent: 'onCommand:probe.change' };
    await a.manager.activate(base.identifier, reason);
    a.internal._onWillActivateExtension(sibling.identifier);
    await f.owner.activateByEvent('onCommand:probe.change', 0, a.host);
    f.calls.length = 0;
    a.exit.fire([137, 'SIGKILL']);
    await f.settle();
    expect(f.generations).toHaveLength(3);
    const next = f.generations[2];
    expect(next.host.runningLocation.equals(a.host.runningLocation)).toBe(true);
    expect(f.owner.getExtensionHosts()).toEqual([b.host, next.host]);
    expect(f.calls).toEqual([{ generation: 3, type: 'activate', id: base.identifier.value, reason }]);
    expect(f.notifications).toEqual([]); expect(f.statuses).toHaveLength(1);
    expect(() => a.internal._onDidActivateExtension(base.identifier, 1, 2, 3, reason)).toThrow('generation is closed');
    a.exit.fire([137, 'late']); await f.settle(); expect(f.generations).toHaveLength(3);
    receipt.checks.push('原退出订阅→精确位置factory→原Manager；仅已完成激活事实，无命令事件／未完成激活／peer重放，旧代际迟到拒绝');
  } finally { f.close(); }
});

test('原五分钟tracker按精确位置计数，第三次停止，过期后首轮可恢复', async () => {
  const clock = Date.now; let now = clock(); Date.now = () => now;
  const f = await recoveryFixture({ all: [base, { ...base, installationTarget: targetB }], views: [hostView(targetA), hostView(targetB, ['workspace-b'])] });
  try {
    for (let index = 0; index < 3; index++) {
      const current = f.generations.filter(item => item.host.runningLocation.installationTarget === targetA).at(-1);
      current.exit.fire([1, null]); await f.settle();
    }
    expect(f.generations).toHaveLength(4);
    expect(f.notifications).toHaveLength(1); expect(f.notifications[0]).toContain('5 分钟内已崩溃 3 次');
    const peer = f.generations.find(item => item.host.runningLocation.installationTarget === targetB);
    peer.exit.fire([1, null]); await f.settle(); expect(f.generations).toHaveLength(5);
    now += 5 * 60 * 1000 + 1;
    f.owner._startExtensionHostsIfNecessary(false, [], locationA());
    await f.generations.at(-1).manager.ready();
    f.generations.at(-1).exit.fire([1, null]); await f.settle();
    expect(f.generations).toHaveLength(7); expect(f.notifications).toHaveLength(1);
    receipt.checks.push('原ExtensionHostCrashTracker五分钟第三次停止；按target/view/roots隔离计数，过期窗口恢复');
  } finally { Date.now = clock; f.close(); }
});

test('主动停止及崩溃清理等待中的主动停止均不重启', async () => {
  const a = await recoveryFixture();
  try {
    await a.owner._doStopExtensionHosts(); await a.settle();
    expect(a.generations).toHaveLength(1); expect(a.owner.getExtensionHosts()).toEqual([]);
    expect(a.notifications).toEqual([]); expect(a.statuses).toEqual([]);
  } finally { a.close(); }
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const b = await recoveryFixture({ disconnect: async () => { entered.resolve(); await release.promise; } });
  try {
    b.generations[0].exit.fire([1, null]); await entered.promise;
    await b.owner._doStopExtensionHosts(); release.resolve(); await b.settle(); await new Promise(resolve => setTimeout(resolve, 5));
    expect(b.generations).toHaveLength(1); expect(b.owner.getExtensionHosts()).toEqual([]);
    expect(b.notifications).toEqual([]); expect(b.statuses).toEqual([]);
    receipt.checks.push('原stopAll在disconnect前撤退出订阅；正常停机取消在途恢复，无虚假崩溃通知');
  } finally { release.resolve(); b.close(); }
});

test('原Browser hook重新核精确Web资格并换代，原命令图标和icon font消费新冻结URL', async () => {
  const originals = Object.fromEntries(['location', 'fetch', 'WebSocket'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  const opens = [], sockets = [], lifetime = new m.DisposableStore(); let rejectQualification = false, deferQualification = false;
  const native = description('probe.web', { name: 'web', main: undefined, browser: './browser.js', extensionKind: ['web'],
    contributes: { commands: [{ command: 'probe.recoveryAsset', title: '恢复资源', icon: './icon.svg' }],
      icons: { 'probe-recovery-icon': { description: '恢复字体', default: { fontPath: './icons.woff', fontCharacter: '\\E001' } } } } });
  const inventory = { profile_id: 'native-default', target_platform: 'linux-x64', extensions: [{ source: 'profile',
    extension_key: 'probe.web-1.0.0', is_valid: true, validations: [], description: JSON.parse(JSON.stringify(native)) }] };
  class QualificationSocket extends EventTarget {
    constructor() { super(); sockets.push(this); queueMicrotask(() => this.dispatchEvent(new Event('open'))); }
    send(data) {
      const request = JSON.parse(data); opens.push(request);
      if (deferQualification) return;
      if (rejectQualification) { queueMicrotask(() => this.remoteClose('原key已卸载')); return; }
      const value = { ...request, inventory, request_id: 'request-' + opens.length, client_id: 'client-' + opens.length,
        generation: 'generation-' + opens.length, context: { os: 'linux', remote_settings: { resource_uri: 'boxteam://profile/settings' },
          workspace: { id: 'empty', name: 'Empty', folders: [] } } };
      queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })));
    }
    remoteClose(reason = 'transport lost') { const event = new Event('close'); Object.assign(event, { code: 1008, reason }); this.dispatchEvent(event); }
    close() { this.remoteClose('client closed'); }
  }
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { href: 'http://qualification.test/' } });
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: QualificationSocket });
  globalThis.fetch = async () => new Response(JSON.stringify({ data: { token: 'recovery-boundary-token' } }));
  const inventoryRequests = [];
  globalThis.__extensionRecoveryInventory = async (path, options) => {
    inventoryRequests.push(path); const request = JSON.parse(options.body);
    if (path.endsWith('/extension-targets')) return { ...request, source: 'live', targets: [{ ...request, backend_id: targetA.backendId,
      source: 'live', capabilities: { installation_manage: true, host_manage: true } }] };
    if (path.endsWith('/inventory')) return { ...request, profile_scope: 'default', inventory };
    throw new Error('恢复不应重新扫描目标库存：' + path);
  };
  const iconOwner = new m.IconExtensionPoint(); let f;
  try {
    const initial = await m.scanTargetExtensions({ getCompleteWorkspace: async () => ({ folders: [] }) },
      extension => extension.extensionKind, lifetime, targetA.gatewayId, new m.NullLogService());
    f = await recoveryFixture({ all: initial.extensions, views: initial.views, browser: true });
    f.owner._doHandleExtensionPoints(initial.extensions, false);
    const beforeCommand = m.MenuRegistry.getCommand('probe.recoveryAsset').icon.dark.toString();
    const beforeIcon = m.getIconRegistry().getIcon('probe-recovery-icon').defaults.font.definition.src[0].location.toString();
    expect(beforeCommand).toContain('/generation-1/'); expect(beforeIcon).toContain('/generation-1/');
    await f.generations[0].manager.activate(initial.extensions[0].identifier,
      { startup: false, extensionId: initial.extensions[0].identifier, activationEvent: 'onCommand:probe.recoveryAsset' });
    f.calls.length = 0;
    sockets[0].remoteClose(); await f.settle();
    expect(f.notifications).toEqual([]); expect(f.generations).toHaveLength(2);
    expect(opens).toEqual([opens[0], opens[0]]); expect(opens[0].extension_keys).toEqual(['probe.web-1.0.0']);
    expect(inventoryRequests).toEqual(['/api/gateway/extension-targets', '/api/gateway/extension-installations/inventory']);
    expect(() => initial.views[0].webHost.connection).toThrow('关闭');
    const view = f.owner.getExtensionWorkspaceViews()[0], restored = f.owner.extensions[0];
    expect(view.webHost.connection.generation).toBe('generation-2');
    expect(restored.contributes).toBe(initial.extensions[0].contributes);
    expect(restored.extensionLocation.toString()).toContain('/generation-2/');
    expect(m.MenuRegistry.getCommand('probe.recoveryAsset').icon.dark.toString()).toContain('/generation-2/');
    expect(m.getIconRegistry().getIcon('probe-recovery-icon').defaults.font.definition.src[0].location.toString()).toContain('/generation-2/');
    expect(m.getIconRegistry().getIcons().filter(icon => icon.id === 'probe-recovery-icon')).toHaveLength(1);
    expect(f.calls.map(call => call.type)).toEqual(['activate']);
    rejectQualification = true; sockets[1].remoteClose(); await f.settle();
    expect(f.generations).toHaveLength(2); expect(f.owner.getExtensionHosts()).toEqual([]);
    expect(f.notifications[0]).toMatch(/Reload Window/); expect(f.notifications[0]).toContain('原key已卸载');
    // 等待资格时正常停机，必须撤销真实owner启动的connection等待；不能迟到创建Worker。
    rejectQualification = false;
    const again = await m.scanTargetExtensions({ getCompleteWorkspace: async () => ({ folders: [] }) },
      extension => extension.extensionKind, lifetime, targetA.gatewayId, new m.NullLogService());
    const waiting = await recoveryFixture({ all: again.extensions, views: again.views, browser: true });
    try {
      deferQualification = true; const opened = opens.length;
      sockets.at(-1).remoteClose(); await until(() => opens.length === opened + 1);
      await waiting.owner._doStopExtensionHosts(); await waiting.settle();
      expect(waiting.owner.getExtensionHosts()).toEqual([]); expect(waiting.generations).toHaveLength(1);
      expect(waiting.notifications).toEqual([]);
    } finally { waiting.close(); }
    receipt.checks.push('真实scanner→原Browser hook→新资格generation→同Registry／原命令icon／原icon font；精确key无重扫，closed资格不复用，卸载提示Reload，等待可取消');
  } finally {
    if (f) {
      const current = f.owner.extensions;
      const lock = await f.owner._registry.acquireLock('remove recovery contributions');
      try { f.owner._registry.deltaExtensions(lock, [], current); } finally { lock.dispose(); }
      f.owner._doHandleExtensionPoints(current, false); f.close();
    }
    void iconOwner; lifetime.dispose(); delete globalThis.__extensionRecoveryInventory;
    for (const [name, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor); else delete globalThis[name];
    }
  }
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'extension-state-checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
