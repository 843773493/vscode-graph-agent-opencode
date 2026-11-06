import { afterAll, expect, spyOn, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/configuration_cache.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'configuration-cache.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
await build({ stdin: { contents: `
export { ConfigurationCache } from '${upstream}workbench/services/configuration/common/configurationCache.js';
export { FolderConfiguration } from '${upstream}workbench/services/configuration/browser/configuration.js';
export { DefaultConfiguration as RegistryDefaultConfiguration } from '${upstream}platform/configuration/common/configurations.js';
export { Registry } from '${upstream}platform/registry/common/platform.js';
export { ConfigurationScope, Extensions } from '${upstream}platform/configuration/common/configurationRegistry.js';
export { WorkbenchState } from '${upstream}platform/workspace/common/workspace.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { Event } from '${upstream}base/common/event.js';
export { workspaceFileUri } from './src/clients/workbench/src/workspaceFiles.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, transform)], tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const key = { type: 'folder', key: 'resource-A' };
const receipt = { boundary: '真实ConfigurationCache、FolderConfiguration、Registry/DefaultConfiguration、FileService与内存Provider；默认值事件到FolderConfiguration.reparse由具名测试回调连接，目录准备和失败为明确装具，不复制缓存实现；不代表完整WorkspaceService编排、真实HTTP、IndexedDB或页面验收', checks: [] };

async function fixture(callback) {
  const files = new m.FileService(new m.NullLogService());
  const provider = new m.InMemoryFileSystemProvider();
  files.registerProvider('vscode-userdata', provider);
  const home = m.URI.parse('vscode-userdata:/Cache');
  const cache = new m.ConfigurationCache(['file', 'vscode-userdata', 'tmp', 'workbench'], { cacheHome: home }, files);
  const entered = Promise.withResolvers(), release = Promise.withResolvers();
  const create = files.createFolder.bind(files);
  let first = true;
  const gate = spyOn(files, 'createFolder').mockImplementation(async resource => {
    if (first && resource.toString().endsWith('/folder/resource-A')) {
      first = false; entered.resolve(); await release.promise;
    }
    return create(resource);
  });
  try { await callback({ files, cache, home, gate, entered: entered.promise, release: () => release.resolve() }); }
  finally { release.resolve(); gate.mockRestore(); files.dispose(); provider.dispose(); }
}

test('首个目录准备延迟时，后发配置仍按调用顺序发布', async () => {
  await fixture(async f => {
    const old = f.cache.write(key, 'old');
    await f.entered;
    const latest = f.cache.write(key, 'latest');
    // 等待真实文件队列推进；不通过等待顺序替缓存实现排序。
    await new Promise(resolve => setTimeout(resolve, 20));
    f.release();
    await Promise.all([old, latest]);
    expect(await f.cache.read(key)).toBe('latest');
    receipt.checks.push('较早目录准备延迟不能使旧配置覆盖后发配置');
  });
});

test('写入后删除同一派生配置，迟到建目录不能复活已删除条目', async () => {
  await fixture(async f => {
    const pending = f.cache.write(key, 'retired');
    await f.entered;
    const removed = f.cache.remove(key);
    await new Promise(resolve => setTimeout(resolve, 20));
    f.release();
    await Promise.all([pending, removed]);
    expect(await f.cache.read(key)).toBe('');
    expect(await f.files.exists(m.URI.joinPath(f.home, 'CachedConfigurations', key.type, key.key))).toBe(false);
    await f.cache.write(key, 'replacement');
    expect(await f.cache.read(key)).toBe('replacement');
    receipt.checks.push('写入／删除按同一队列结算；删除后可正常重建');
  });
});

test('type／key／cacheHome隔离，A的迟到更新不阻塞或覆盖B', async () => {
  await fixture(async f => {
    const old = f.cache.write(key, 'A');
    await f.entered;
    const otherType = { type: 'workspaces', key: key.key };
    const otherKey = { ...key, key: 'resource-B' };
    const profile = new m.ConfigurationCache([], { cacheHome: m.URI.joinPath(f.home, 'profile-B') }, f.files);
    await Promise.all([f.cache.write(otherType, 'workspace'), f.cache.write(otherKey, 'B'), profile.write(key, 'profile-B')]);
    expect(await f.cache.read(otherType)).toBe('workspace');
    expect(await f.cache.read(otherKey)).toBe('B');
    expect(await profile.read(key)).toBe('profile-B');
    f.release(); await old;
    expect(await f.cache.read(key)).toBe('A');
    expect(await profile.read(key)).toBe('profile-B');
    receipt.checks.push('原type／key／cacheHome隔离；不建全局串行队列');
  });
});

test('本地持久设置不重复缓存，非本地资源仍沿原缓存资格判断', async () => {
  await fixture(async f => {
    for (const scheme of ['file', 'vscode-userdata', 'tmp', 'workbench']) {
      expect(f.cache.needsCaching(m.URI.from({ scheme, path: '/settings.json' }))).toBe(false);
    }
    expect(f.cache.needsCaching(m.URI.parse('boxteam-file://gateway-A/workspace-A/root/settings.json'))).toBe(true);
    receipt.checks.push('原needsCaching排除持久本地资源，业务资源仍可缓存；完整启动由真实页面核对');
  });
});

test('原文件写入失败向调用方传播，队列随后仍可重试', async () => {
  await fixture(async f => {
    f.release();
    const save = spyOn(f.files, 'writeFile').mockRejectedValueOnce(new Error('配置缓存写入拒绝'));
    try {
      await expect(f.cache.write(key, 'failed')).rejects.toThrow('配置缓存写入拒绝');
      await f.cache.write(key, 'retry');
      expect(await f.cache.read(key)).toBe('retry');
    } finally { save.mockRestore(); }
    receipt.checks.push('写入失败不伪报成功；后发写入可重试');
  });
});

test('目录准备失败不能伪报缓存保存成功，重试仍使用原条目', async () => {
  await fixture(async f => {
    f.release();
    f.gate.mockRejectedValueOnce(new Error('配置缓存目录拒绝'));
    await expect(f.cache.write(key, 'failed')).rejects.toThrow('配置缓存目录拒绝');
    await f.cache.write(key, 'retry');
    expect(await f.cache.read(key)).toBe('retry');
    receipt.checks.push('目录准备失败传播原原因；重试不创建第二缓存链');
  });
});

test('真实非本地FolderConfiguration消费原缓存：更新／删除及另一Gateway同路径隔离', async () => {
  await fixture(async f => {
    const provider = new m.InMemoryFileSystemProvider();
    f.files.registerProvider('boxteam-file', provider);
    const identity = new m.UriIdentityService(f.files), folders = [];
    const save = f.cache.write.bind(f.cache);
    let written = Promise.withResolvers(), acceptsWrite = () => true;
    const nextWrite = predicate => { written = Promise.withResolvers(); acceptsWrite = predicate; return written.promise; };
    const saves = spyOn(f.cache, 'write').mockImplementation((key, content) => {
      const promise = save(key, content);
      if (acceptsWrite(key, content)) written.resolve({ key, promise });
      return promise;
    });
    const removals = spyOn(f.cache, 'remove');
    try {
      const open = async (gatewayId, predicate = () => true) => {
        const pendingWrite = nextWrite(predicate);
        const root = `boxteam://${gatewayId}/workspace/workspace-A/resources/files/root`;
        const uri = m.workspaceFileUri({ gatewayId, workspaceId: 'workspace-A' }, root, root);
        const settings = m.URI.joinPath(uri, '.vscode/settings.json');
        await f.files.createFolder(m.URI.joinPath(uri, '.vscode'));
        await f.files.writeFile(settings, m.VSBuffer.fromString(JSON.stringify({ 'editor.fontSize': 17 })));
        const folder = new m.FolderConfiguration(true, { uri }, '.vscode', 2, true, f.files, identity, new m.NullLogService(), f.cache);
        folders.push(folder);
        const ready = m.Event.toPromise(folder.onDidChange);
        await folder.loadConfiguration(); await ready;
        const cached = await pendingWrite; await cached.promise;
        expect((await folder.loadConfiguration()).getValue('editor.fontSize')).toBe(17);
        expect(JSON.parse((JSON.parse(await f.cache.read(cached.key))).content.settings)['editor.fontSize']).toBe(17);
        return { settings, folder, key: cached.key };
      };
      const a = await open('gateway-A');
      const b = await open('gateway-B', key => key.key !== a.key.key);
      expect(a.key).not.toEqual(b.key);
      const updated = nextWrite((key, content) => key.key === a.key.key && JSON.parse(content).content.settings.includes('23'));
      await f.files.writeFile(a.settings, m.VSBuffer.fromString(JSON.stringify({ 'editor.fontSize': 23 })));
      const update = await updated; await update.promise;
      expect(update.key).toEqual(a.key);
      expect((await a.folder.loadConfiguration()).getValue('editor.fontSize')).toBe(23);
      expect(JSON.parse((JSON.parse(await f.cache.read(b.key))).content.settings)['editor.fontSize']).toBe(17);
      const deleted = nextWrite((key, content) => key.key === a.key.key && JSON.parse(content).content.settings === '{}');
      await f.files.del(a.settings);
      const deletion = await deleted; await deletion.promise;
      expect(deletion.key).toEqual(a.key);
      // 原消费者把缺失设置归一为{}并更新缓存，不把文件删除等同于缓存目录删除。
      expect(JSON.parse(await f.cache.read(a.key)).content.settings).toBe('{}');
      expect((await a.folder.loadConfiguration()).getValue('editor.fontSize')).toBeUndefined();
      expect((await b.folder.loadConfiguration()).getValue('editor.fontSize')).toBe(17);
      expect(JSON.parse((JSON.parse(await f.cache.read(b.key))).content.settings)['editor.fontSize']).toBe(17);
      expect(removals).not.toHaveBeenCalled();
      receipt.checks.push('真实FolderConfiguration读取／更新／删除后空设置；Gateway进入实际key与唯一文件投影，B正文保持');
    } finally {
      for (const folder of folders) folder.dispose();
      saves.mockRestore(); removals.mockRestore(); identity.dispose(); provider.dispose();
    }
  });
});

test('真实默认值／scope／restricted变化和信任重解析只消费已加载正文，不重复读取或写缓存', async () => {
  await fixture(async f => {
    f.release();
    const provider = new m.InMemoryFileSystemProvider(), registration = f.files.registerProvider('boxteam-file', provider);
    const identity = new m.UriIdentityService(f.files), registry = m.Registry.as(m.Extensions.Configuration), log = new m.NullLogService();
    const regular = 'configurationCacheProbe.regular', restricted = 'configurationCacheProbe.restricted', scoped = 'configurationCacheProbe.scoped';
    const schema = (defaultValue, scope, restrictRegular) => ({ id: 'configuration-cache-reparse-probe', properties: {
      [regular]: { type: 'number', default: defaultValue, scope: m.ConfigurationScope.RESOURCE, restricted: restrictRegular },
      [restricted]: { type: 'string', default: 'safe-default', scope: m.ConfigurationScope.RESOURCE, restricted: true },
      [scoped]: { type: 'string', default: 'scope-default', scope },
    } });
    let currentSchema = schema(1, m.ConfigurationScope.WINDOW, false), folder, defaults, defaultListener, reads, registered = false;
    const initialWrite = Promise.withResolvers(), cacheOperations = [], readOperations = [], reparsed = [], expected = [], defaultChanges = [];
    const save = f.cache.write.bind(f.cache);
    const writes = spyOn(f.cache, 'write').mockImplementation((key, content) => {
      const promise = save(key, content); cacheOperations.push(promise); initialWrite.resolve({ key, promise }); return promise;
    });
    const snapshot = model => ({ regular: model.getValue(regular), restricted: model.getValue(restricted), scoped: model.getValue(scoped), tasks: model.getValue('tasks.version') });
    try {
      registry.registerConfiguration(currentSchema); registered = true;
      const root = 'boxteam://gateway-reparse/workspace/workspace-A/resources/files/root';
      const uri = m.workspaceFileUri({ gatewayId: 'gateway-reparse', workspaceId: 'workspace-A' }, root, root), directory = m.URI.joinPath(uri, '.vscode');
      const settings = m.URI.joinPath(directory, 'settings.json'); await f.files.createFolder(directory);
      for (const name of ['tasks', 'launch', 'mcp']) await f.files.writeFile(m.URI.joinPath(directory, name + '.json'), m.VSBuffer.fromString(name === 'tasks' ? '{"version":"2.0.0","tasks":[]}' : '{}'));
      // 初始文件事件先经原 Provider 派发，避免把种子写入的迟到通知计为重解析读取。
      const seeded = m.Event.toPromise(m.Event.filter(provider.onDidChangeFile, changes => changes.some(change => change.resource.toString() === settings.toString())));
      await f.files.writeFile(settings, m.VSBuffer.fromString(JSON.stringify({ [regular]: 17, [restricted]: 'workspace-command', [scoped]: 'from-file' }))); await seeded;
      folder = new m.FolderConfiguration(true, { uri }, '.vscode', m.WorkbenchState.WORKSPACE, true, f.files, identity, log, f.cache);
      const ready = m.Event.toPromise(folder.onDidChange); await folder.loadConfiguration(); await ready;
      const cached = await initialWrite.promise; await cached.promise;
      expect(snapshot(await folder.loadConfiguration())).toEqual({ regular: 17, restricted: 'workspace-command', scoped: undefined, tasks: '2.0.0' });
      expect(JSON.parse(JSON.parse(await f.cache.read(cached.key)).content.settings)[restricted]).toBe('workspace-command');
      defaults = new m.RegistryDefaultConfiguration(log); await defaults.initialize();
      expect(defaults.configurationModel.getValue(regular)).toBe(1);
      // 只替换 WorkspaceService 的事件编排；默认模型、重新解析和信任过滤均执行原 owner。
      defaultListener = defaults.onDidChangeConfiguration(event => {
        if (event.properties.includes(regular)) defaultChanges.push({ value: event.defaults.getValue(regular), model: snapshot(folder.reparse()) });
      });
      const baselineWrites = writes.mock.calls.length, read = f.files.readFile.bind(f.files);
      reads = spyOn(f.files, 'readFile').mockImplementation((...args) => { const promise = read(...args); readOperations.push(promise); return promise; });
      const variants = [
        { value: 31, scope: m.ConfigurationScope.RESOURCE, restrictRegular: true, scoped: 'from-file', untrustedRegular: undefined },
        { value: 32, scope: m.ConfigurationScope.WINDOW, restrictRegular: false, scoped: undefined, untrustedRegular: 17 },
      ];
      for (const variant of variants) {
        const previous = currentSchema; currentSchema = schema(variant.value, variant.scope, variant.restrictRegular);
        registry.updateConfigurations({ remove: [previous], add: [currentSchema] });
        const trusted = { regular: 17, restricted: 'workspace-command', scoped: variant.scoped, tasks: '2.0.0' };
        const untrusted = { ...trusted, regular: variant.untrustedRegular, restricted: undefined };
        for (let index = 0; index < 3; index++) { reparsed.push(snapshot(folder.reparse())); expected.push(trusted); }
        reparsed.push(snapshot(folder.updateWorkspaceTrust(false))); expected.push(untrusted);
        for (let index = 0; index < 3; index++) { reparsed.push(snapshot(folder.reparse())); expected.push(untrusted); }
        reparsed.push(snapshot(folder.updateWorkspaceTrust(true))); expected.push(trusted);
      }
      // 若旧实现启动了异步重读，先让原读写链收尾再断言，失败时也不向下个测试遗留任务。
      await Promise.all(readOperations); await new Promise(resolve => setTimeout(resolve, 0)); await Promise.all(cacheOperations.slice(baselineWrites));
      expect(defaultChanges).toEqual(variants.map(variant => ({ value: variant.value,
        model: { regular: 17, restricted: 'workspace-command', scoped: variant.scoped, tasks: '2.0.0' } })));
      expect(reparsed).toEqual(expected); expect(baselineWrites).toBeGreaterThan(0);
      expect(reads).not.toHaveBeenCalled(); expect(writes.mock.calls).toHaveLength(baselineWrites);
      receipt.checks.push('真实Registry默认值/scope/restricted事件及反复信任切换消费原正文，受限设置实际消失/恢复，零新增readFile/cache.write；原文件更新/删除链另测保留');
    } finally {
      defaultListener?.dispose(); defaults?.dispose(); folder?.dispose(); reads?.mockRestore(); writes.mockRestore();
      if (registered) registry.deregisterConfigurations([currentSchema]);
      identity.dispose(); registration.dispose(); provider.dispose();
    }
  });
});

afterAll(async () => { await writeFile(join(run.artifactsDir, 'configuration-cache.json'), JSON.stringify(receipt, null, 2) + '\n'); });
