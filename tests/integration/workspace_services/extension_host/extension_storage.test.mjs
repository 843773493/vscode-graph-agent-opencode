import { afterAll, beforeAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { chromium } from 'playwright';
import { build } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/workspace_services/extension_host/extension_storage.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'extension-storage.fixture.mjs');
const nodeSecretsBundle = join(run.artifactsDir, 'node-secrets.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { MainThreadStorage } from '${upstream}workbench/api/browser/mainThreadStorage.js';
export { ExtHostStorage } from '${upstream}workbench/api/common/extHostStorage.js';
export { MainThreadSecretState } from '${upstream}workbench/api/browser/mainThreadSecretState.js';
export { ExtHostSecretState } from '${upstream}workbench/api/common/extHostSecretState.js';
export { ExtensionMemento, ExtensionGlobalMemento } from '${upstream}workbench/api/common/extHostMemento.js';
export { ExtensionStorageService } from '${upstream}platform/extensionManagement/common/extensionStorage.js';
export { BrowserStorageService } from '${upstream}workbench/services/storage/browser/storageService.js';
export { ExtensionHostExtensions, toExtension } from '${upstream}workbench/services/extensions/common/extensions.js';
export { ExtensionService as BrowserExtensionService } from '${upstream}workbench/services/extensions/browser/extensionService.js';
export { ExtensionHostKind } from '${upstream}workbench/services/extensions/common/extensionHostKind.js';
export { ExtensionIdentifier } from '${upstream}platform/extensions/common/extensions.js';
export { StorageScope, StorageTarget } from '${upstream}platform/storage/common/storage.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { Event, Emitter } from '${upstream}base/common/event.js';
export { URI } from '${upstream}base/common/uri.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
await build({ stdin: { contents: `export { ExtHostSecretState } from '${upstream}workbench/api/common/extHostSecretState.js';`, resolveDir: resolve('.') },
  outfile: nodeSecretsBundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, [], { runtime: 'node' }))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });

const receipt = { boundary: '真实Chromium原BrowserStorageService/IndexedDB/BroadcastChannel、ExtensionStorageService、MainThreadStorage、ExtHostStorage和Memento；认证host binding与RPC传输为具名装具，不装配Gateway或真实Node，不代表原生文件存储验收。', checks: [] };
let browser, server;
beforeAll(async () => {
  server = Bun.serve({ hostname: '127.0.0.1', port: 0, fetch(request) {
    const script = { '/fixture.mjs': bundle, '/node-secrets.fixture.mjs': nodeSecretsBundle }[new URL(request.url).pathname];
    return script
      ? new Response(Bun.file(script), { headers: { 'Content-Type': 'text/javascript' } })
      : new Response('<!doctype html><title>扩展状态存储集成装具</title>', { headers: { 'Content-Type': 'text/html' } });
  } });
  run.addCleanup('组件HTTP来源', () => server.stop(true));
  browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ?? Bun.which('chromium'),
    headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  run.addCleanup('Chromium', () => browser.close());
});
afterAll(async () => {
  try { await run.close(); }
  finally { await writeFile(join(run.artifactsDir, 'extension-storage-checks.json'), JSON.stringify(receipt, null, 2) + '\n'); }
});

async function openClient(context, options = {}) {
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:${server.port}`);
  await page.evaluate(async options => {
    globalThis._VSCODE_FILE_ROOT = location.origin + '/';
    const m = await import('/fixture.mjs'), log = new m.NullLogService();
    const storage = new m.BrowserStorageService({ id: options.windowId ?? 'window-default' }, {
      currentProfile: { id: 'default-profile', isDefault: true }, onDidChangeCurrentProfile: m.Event.None,
    }, log);
    await storage.initialize();
    const owner = new m.ExtensionStorageService(storage, {}, log), clients = new Map();
    const make = async (name, backend = 'backend-a', workspaceId = 'stable-view-a', { web = false, generation = 'generation-a', clientId = 'client-a', apiNone = false } = {}) => {
      const description = { identifier: new m.ExtensionIdentifier('probe.state'), name: 'state', publisher: 'probe', version: '1.0.0', main: './extension.js',
        ...(apiNone ? { api: 'none' } : {}) };
      const host = { runningLocation: { kind: web ? m.ExtensionHostKind.LocalWebWorker : m.ExtensionHostKind.Remote },
        extensions: new m.ExtensionHostExtensions(1, [description], apiNone ? [] : [description.identifier]),
        ...(web ? {} : { binding: { gateway_id: 'gateway_a', backend_id: backend, client_id: clientId, generation,
          initialization: { workspace: { id: workspaceId } } } }) };
      let main;
      const ext = new m.ExtHostStorage({ getProxy: () => ({
        $initializeExtensionStorage: (...args) => main.$initializeExtensionStorage(...args),
        $setValue: (...args) => main.$setValue(...args),
        $registerExtensionStorageKeysToSync: (...args) => main.$registerExtensionStorageKeysToSync(...args),
      }) }, log);
      main = new m.MainThreadStorage({ getProxy: () => ext, extensionHost: host }, owner, storage, {}, log);
      const global = new m.ExtensionGlobalMemento(description, ext), workspace = new m.ExtensionMemento('probe.state', false, ext);
      await Promise.all([global.whenReady, workspace.whenReady]);
      const client = { main, ext, global, workspace, host };
      clients.set(name, client);
      return client;
    };
    globalThis.probe = { m, storage, owner, clients, make,
      read(name) { const c = clients.get(name); return { global: c.global.get('value'), workspace: c.workspace.get('value') }; },
      async write(name, scope, value) { await clients.get(name)[scope].update('value', value); await storage.flush(); },
      close(name) { const c = clients.get(name); c.main.dispose(); c.global.dispose(); c.workspace.dispose(); },
    };
  }, options);
  return page;
}

test('原 Browser 扩展服务增删预检统一消费管理对象，由原 profile owner 绑定Web身份', async () => {
  const context = await browser.newContext();
  try {
    const page = await openClient(context);
    const result = await page.evaluate(async () => {
      const m = await import('/fixture.mjs'), web = { kind: 'web', profileId: 'web-profile' };
      const service = new m.BrowserExtensionService({ createInstance: () => ({}) }, {}, {}, {}, { onEnablementChanged: m.Event.None },
        { onWillActivateFileSystemProvider: m.Event.None, registerProvider: () => ({ dispose() {} }) }, {},
        { onDidChangeProfile: m.Event.None, onDidEnableExtensions: m.Event.None, onDidInstallExtensions: m.Event.None, onDidUninstallExtension: m.Event.None },
        {}, {}, { getExtensionKind: () => ['web'] }, {}, new m.NullLogService(), {}, {},
        { onWillShutdown: m.Event.None, when: () => new Promise(() => {}) }, {}, {}, { currentProfile: { id: web.profileId } }, {});
      const description = target => ({ identifier: new m.ExtensionIdentifier('probe.shared'), name: 'shared', publisher: 'probe', version: '1.0.0',
        engines: { vscode: '*' }, ...(target.kind === 'web' ? { browser: './web.js' } : { main: './extension.js' }),
        extensionLocation: m.URI.file('/extensions/shared'), installationTarget: target,
        isBuiltin: false, isUserBuiltin: false, isUnderDevelopment: false, preRelease: false, targetPlatform: 'undefined' });
      const node = description({ kind: 'backend', gatewayId: 'gateway-a', backendId: '11111111-1111-4111-8111-111111111111' }), browser = description(web);
      const raw = { ...m.toExtension(browser), installationTarget: undefined }, managedNode = m.toExtension(node), checks = [];
      try {
        const first = await service._registry.acquireLock('native instance before Web preflight');
        try { service._registry.deltaExtensions(first, [node], []); } finally { first.dispose(); }
        checks.push(service.canAddExtension(raw), service.canRemoveExtension(raw), service.canAddExtension(managedNode), service.canRemoveExtension(managedNode));
        const second = await service._registry.acquireLock('Web owner publishes its instance');
        try { service._registry.deltaExtensions(second, [browser], []); } finally { second.dispose(); }
        checks.push(service.canAddExtension(raw), service.canRemoveExtension(raw));
        const third = await service._registry.acquireLock('remove only the Web instance');
        try { service._registry.deltaExtensions(third, [], [browser]); } finally { third.dispose(); }
        checks.push(service.canRemoveExtension(raw), service.canRemoveExtension(managedNode));
        return checks;
      } finally { service.dispose(); }
    });
    expect(result).toEqual([true, false, false, true, false, true, false, true]);
    receipt.checks.push('真实Chromium中原BrowserExtensionService完整构造及公开canAdd/canRemove入口：原profile绑定管理对象，Web和同ID Backend预检互不借实例；宿主传输未启动');
  } finally { await context.close(); }
});

test('原存储owner及Memento按目标/稳定视图区分正文和通知，保留Web原键', async () => {
  const context = await browser.newContext();
  try {
    const page = await openClient(context);
    const result = await page.evaluate(async () => {
      const p = globalThis.probe;
      await p.make('a'); await p.make('a-peer', 'backend-a', 'stable-view-a', { generation: 'new-generation', clientId: 'another-client' });
      await p.make('a-other-view', 'backend-a', 'stable-view-two'); await p.make('b', 'backend-b'); await p.make('web', undefined, undefined, { web: true });
      await p.write('a', 'global', 'A-global'); await p.write('a', 'workspace', 'A-view');
      await p.write('b', 'global', 'B-global'); await p.write('b', 'workspace', 'B-view'); await p.write('web', 'global', 'Web');
      const first = Object.fromEntries([...p.clients.keys()].map(name => [name, p.read(name)]));
      const c = p.clients.get('a'), scope = { installationTarget: { kind: 'backend', gatewayId: c.host.binding.gateway_id, backendId: c.host.binding.backend_id }, workspaceId: 'stable-view-a' };
      const raw = p.owner.getExtensionState('probe.state', false, scope);
      const sync = { id: 'probe.state', version: '1.0.0' };
      let syncError;
      try { c.global.setKeysForSync(['value']); } catch (error) { syncError = error.message; }
      if (!syncError) throw new Error('目标同步声明未明确拒绝');
      const webKeys = p.owner.getKeysForSync(sync);
      p.owner.setKeysForSync({ ...sync, version: '0.9.0' }, ['obsolete-web']);
      p.clients.get('web').global.setKeysForSync(['current-web']);
      await p.m.ExtensionStorageService.removeOutdatedExtensionVersions({ getInstalled: async () => [{ identifier: { id: sync.id }, manifest: { version: sync.version } }] }, p.storage);
      const cleanup = { currentWeb: p.owner.getKeysForSync(sync), obsoleteWeb: p.owner.getKeysForSync({ ...sync, version: '0.9.0' }) };
      p.close('a'); await p.write('a-peer', 'workspace', 'peer-still-open');
      return { first, raw, syncError, webKeys, cleanup, peer: p.read('a-peer'), webRaw: p.owner.getExtensionState('probe.state', true) };
    });
    expect(result.first).toEqual({
      a: { global: 'A-global', workspace: 'A-view' }, 'a-peer': { global: 'A-global', workspace: 'A-view' },
      'a-other-view': { global: 'A-global' }, b: { global: 'B-global', workspace: 'B-view' }, web: { global: 'Web' },
    });
    expect(result.raw).toEqual({ value: 'A-view' });
    expect(result.syncError).toBe('目标 Node 扩展暂不支持 setKeysForSync 跨设备状态同步'); expect(result.webKeys).toBeUndefined();
    expect(result.cleanup).toEqual({ currentWeb: ['current-web'], obsoleteWeb: undefined });
    expect(result.peer).toEqual({ global: 'A-global', workspace: 'peer-still-open' });
    expect(result.webRaw).toEqual({ value: 'Web' });
    receipt.checks.push('同ID双目标/双视图/新client及generation/原owner读写通知/sync keys隔离/Web原键/关闭不影响peer');
  } finally { await context.close(); }
});

test('不同窗口沿原IndexedDB广播同步，两边并发整值写入及重载保持稳定身份', async () => {
  const context = await browser.newContext();
  try {
    const first = await openClient(context, { windowId: 'window-one' }), second = await openClient(context, { windowId: 'window-two' });
    await first.evaluate(() => globalThis.probe.make('b', 'backend-b', 'stable-b'));
    await second.evaluate(() => globalThis.probe.make('b', 'backend-b', 'stable-b', { clientId: 'other-client', generation: 'other-generation' }));
    await first.evaluate(() => globalThis.probe.write('b', 'workspace', { source: 'one', value: 1 }));
    await second.waitForFunction(() => globalThis.probe.read('b').workspace?.source === 'one');
    await second.evaluate(() => globalThis.probe.write('b', 'global', 'shared-profile'));
    await first.waitForFunction(() => globalThis.probe.read('b').global === 'shared-profile');
    await Promise.all([
      first.evaluate(() => globalThis.probe.write('b', 'workspace', { source: 'one', value: 2 })),
      second.evaluate(() => globalThis.probe.write('b', 'workspace', { source: 'two', value: 3 })),
    ]);
    // 原写入语义是完整对象覆盖；只要求最终一份原值，不虚构逐字段事务合并。
    const restored = await openClient(context, { windowId: 'window-after-restart' });
    await restored.evaluate(() => globalThis.probe.make('b', 'backend-b', 'stable-b', { clientId: 'restarted-client', generation: 'restarted-generation' }));
    const final = await restored.evaluate(() => globalThis.probe.read('b'));
    expect(final.global).toBe('shared-profile');
    expect([{ source: 'one', value: 2 }, { source: 'two', value: 3 }]).toContainEqual(final.workspace);
    await first.close(); await second.close();
    await restored.evaluate(() => globalThis.probe.write('b', 'workspace', 'after-peer-close'));
    expect(await restored.evaluate(() => globalThis.probe.read('b').workspace)).toBe('after-peer-close');
    receipt.checks.push('真实双窗口IndexedDB/BroadcastChannel/整值并发发布/新窗口稳定恢复/peer关闭后可写');
  } finally { await context.close(); }
});

test('关闭代际、缺失认证及非所属扩展不能读写目标状态，保留api:none合法依赖', async () => {
  const context = await browser.newContext();
  try {
    const page = await openClient(context);
    const errors = await page.evaluate(async () => {
      const p = globalThis.probe, c = await p.make('a');
      const failures = [];
      async function rejected(operation) { try { await operation(); } catch (error) { failures.push(error.message); return; } throw new Error('应拒绝的状态操作意外成功'); }
      await rejected(() => c.main.$initializeExtensionStorage(true, 'another.extension'));
      await rejected(() => c.main.$setValue(false, 'another.extension', { value: 'wrong' }));
      delete c.host.binding;
      await rejected(() => c.main.$setValue(true, 'probe.state', { value: 'unbound' }));
      p.close('a');
      await rejected(() => c.main.$setValue(true, 'probe.state', { value: 'closed' }));
      const dependency = await p.make('api-none', 'backend-a', 'stable-view-a', { apiNone: true });
      await p.write('api-none', 'global', 'legal-dependency');
      if (dependency.global.get('value') !== 'legal-dependency') throw new Error('合法api:none依赖状态未发布');
      return failures;
    });
    expect(errors).toEqual(['扩展状态不属于发起宿主：another.extension', '扩展状态不属于发起宿主：another.extension', '目标扩展状态缺少认证绑定', '扩展状态的宿主代际已经关闭']);
    receipt.checks.push('旧代际/非所属/无认证绑定拒绝，api:none合法依赖仍用原链');
  } finally { await context.close(); }
});

test('目标秘密在Node公开入口及MainThread拒绝，不读取或广播Web秘密，原Web API保持', async () => {
  const context = await browser.newContext();
  try {
    const page = await openClient(context);
    const result = await page.evaluate(async () => {
      const m = await import('/fixture.mjs'), node = await import('/node-secrets.fixture.mjs');
      const changed = new m.Emitter(), body = new Map(), calls = [], notices = { web: [], target: [] };
      const secrets = {
        onDidChangeSecret: changed.event,
        async get(key) { calls.push('get'); return body.get(key); },
        async set(key, value) { calls.push('set'); body.set(key, value); changed.fire(key); },
        async delete(key) { calls.push('delete'); body.delete(key); changed.fire(key); },
        async keys() { calls.push('keys'); return [...body.keys()]; },
      };
      const make = (name, kind) => {
        let main;
        const ext = new m.ExtHostSecretState({ getProxy: () => ({
          $getPassword: (...args) => main.$getPassword(...args), $setPassword: (...args) => main.$setPassword(...args),
          $deletePassword: (...args) => main.$deletePassword(...args), $getKeys: (...args) => main.$getKeys(...args),
        }) });
        ext.onDidChangePassword(event => notices[name].push(event));
        main = new m.MainThreadSecretState({ extensionHost: { runningLocation: { kind } }, getProxy: () => ext }, secrets, new m.NullLogService(), {});
        return { ext, main };
      };
      const web = make('web', m.ExtensionHostKind.LocalWebWorker), target = make('target', m.ExtensionHostKind.Remote);
      let rpcCalls = 0;
      const nodeState = new node.ExtHostSecretState({ getProxy() { rpcCalls++; throw new Error('目标秘密不应进入浏览器RPC'); } });
      async function rejectAll(owner) {
        const results = await Promise.allSettled([owner.get('probe.state', 'token'), owner.store('probe.state', 'token', 'target-value'), owner.delete('probe.state', 'token'), owner.keys('probe.state')]);
        return results.map(result => result.status === 'rejected' ? result.reason.message : 'unexpected-success');
      }
      try {
        await web.ext.store('probe.state', 'token', 'web-value');
        const webValue = await web.ext.get('probe.state', 'token'), webKeys = await web.ext.keys('probe.state');
        const before = [...calls], nodeErrors = await rejectAll(nodeState), mainErrors = await rejectAll(target.ext);
        const after = [...calls], afterValue = await web.ext.get('probe.state', 'token');
        await web.ext.delete('probe.state', 'token');
        return { webValue, webKeys, before, after, afterValue, nodeErrors, mainErrors, rpcCalls, notices, remaining: await web.ext.keys('probe.state') };
      } finally { target.main.dispose(); web.main.dispose(); changed.dispose(); }
    });
    const unsupported = '目标 Node 扩展暂不支持 SecretStorage；不能访问工作台的秘密存储';
    expect(result.nodeErrors).toEqual(Array(4).fill(unsupported));
    expect(result.mainErrors).toEqual(Array(4).fill(unsupported));
    expect(result.rpcCalls).toBe(0); expect(result.after).toEqual(result.before);
    expect(result.webValue).toBe('web-value'); expect(result.afterValue).toBe('web-value');
    expect(result.webKeys).toEqual(['token']); expect(result.remaining).toEqual([]);
    expect(result.notices.web).toEqual([{ extensionId: 'probe.state', key: 'token' }, { extensionId: 'probe.state', key: 'token' }]);
    expect(result.notices.target).toEqual([]);
    receipt.checks.push('原SecretState四种API：Node在RPC前拒绝，MainThread不访问/订阅Web秘密，Web原读写删除/枚举/通知保持；秘密服务存储为具名装具');
  } finally { await context.close(); }
});
