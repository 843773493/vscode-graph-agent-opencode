import { afterAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_state.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'extension-state.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { ExtensionHostExtensions } from '${upstream}workbench/services/extensions/common/extensions.js';
export { AbstractExtHostExtensionService } from '${upstream}workbench/api/common/extHostExtensionService.js';
export { AbstractExtensionService } from '${upstream}workbench/services/extensions/common/abstractExtensionService.js';
export { ExtensionHostManager } from '${upstream}workbench/services/extensions/common/extensionHostManager.js';
export { LazyCreateExtensionHostManager } from '${upstream}workbench/services/extensions/common/lazyCreateExtensionHostManager.js';
export { RemoteRunningLocation } from '${upstream}workbench/services/extensions/common/extensionRunningLocation.js';
export { Emitter, Event } from '${upstream}base/common/event.js';
export { DisposableStore } from '${upstream}base/common/lifecycle.js';
export { ExtensionIdentifier, ExtensionIdentifierMap } from '${upstream}platform/extensions/common/extensions.js';
export { URI } from '${upstream}base/common/uri.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'node', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const id = value => new m.ExtensionIdentifier(value);
const description = (name, overrides = {}) => ({ identifier: id(name), name, publisher: 'probe', version: '1.0.0',
  engines: { vscode: '*' }, main: './extension.js', extensionLocation: m.URI.file('/extensions/' + name),
  isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined', ...overrides });
const delta = (versionId, fields = {}) => ({ versionId, toAdd: [], toRemove: [], myToAdd: [], myToRemove: [], addActivationEvents: {}, ...fields });
const base = description('probe.base', { activationEvents: ['onBase'] });
const receipt = { boundary: '编译真实发送端描述类、接收端构造器／RPC方法、原Registry、Sequencer与路径索引；主线程服务和eager activation为明确替身，仅在目标fsRealpath边界阻塞或失败。真实Node/RPC扩展另验。', checks: [] };

function receiver(realpath = async path => path) {
  // 不复制receiver状态机；仅替换本用例不执行的扩展激活和主线程服务。
  class Receiver extends m.AbstractExtHostExtensionService {
    _getEntryPoint(extension) { return extension.main; }
    _startExtensionHost() { this._started = true; return this.startup ?? Promise.resolve(); }
  }
  const service = new Receiver({ createChild: () => ({ dispose() {} }) }, { fsRealpath: realpath }, { getProxy: () => ({}) },
    {}, {}, new m.NullLogService(), { extensions: { versionId: 1, allExtensions: [base], myExtensions: [base.identifier],
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
function ownerFixture(f, { disconnectError, disposeError, sharedOwner, activate, disconnect } = {}) {
  const notifications = [], notified = Promise.withResolvers(), lifecycle = [], exit = new m.Emitter();
  const owner = sharedOwner ?? new m.AbstractExtensionService({ hasLocalProcess: false, allowRemoteExtensionsInLocalWebWorker: false },
    {}, {}, {}, {}, { error(error) { notifications.push(error); notified.resolve(); } }, {}, {},
    { onEnablementChanged: m.Event.None }, { onWillActivateFileSystemProvider: m.Event.None }, {},
    { onDidChangeProfile: m.Event.None, onDidEnableExtensions: m.Event.None,
      onDidInstallExtensions: m.Event.None, onDidUninstallExtension: m.Event.None },
    {}, {}, {}, new m.NullLogService(), {}, {}, { onWillShutdown: m.Event.None }, {}, {});
  const mirror = new m.ExtensionHostExtensions(1, [base], [base.identifier]);
  const proxy = { startExtensionHost: delta => f.service.$startExtensionHost(delta), deltaExtensions: delta => f.service.$deltaExtensions(delta),
    activate: activate ?? (async () => false) };
  class Manager extends m.ExtensionHostManager {
    _createExtensionHostCustomers() { return proxy; }
  }
  const host = { runningLocation: new m.RemoteRunningLocation(), startup: 2, extensions: mirror, onExit: exit.event,
    start: async () => ({}),
    async disconnect() { lifecycle.push('disconnect'); exit.fire([0, null]); await disconnect?.(); if (disconnectError) throw disconnectError; },
    dispose() { lifecycle.push('dispose'); if (disposeError) throw disposeError; } };
  const internal = owner._acquireInternalAPI(host);
  const manager = new Manager(host, [], internal, {}, {}, { publicLog2() {} }, new m.NullLogService());
  let exits = 0;
  const listeners = new m.DisposableStore();
  listeners.add(manager.onDidExit(() => { exits++; }));
  owner._extensionHostManagers.add(manager, listeners);
  return { owner, manager, mirror, internal, notifications, notified: notified.promise, lifecycle, exits: () => exits,
    async prepare() {
      await manager.ready();
      const lock = await owner._registry.acquireLock('test initial state');
      try { owner._registry.deltaExtensions(lock, [base], []); } finally { lock.dispose(); }
      owner._runningLocations.set(base.identifier, host.runningLocation);
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
    removed.set(base.identifier, new m.RemoteRunningLocation());
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
    expect(o.owner.getExtensionsStatus()['probe.base'].runtimeErrors).toEqual([liveError]);
    expect(o.owner.getExtensionsStatus()['probe.base'].activationTimes).toBeUndefined();
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
  const host = { startup: 3, runningLocation: new m.RemoteRunningLocation(), onExit: m.Event.None, dispose() { disposed++; } };
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

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'extension-state-checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
