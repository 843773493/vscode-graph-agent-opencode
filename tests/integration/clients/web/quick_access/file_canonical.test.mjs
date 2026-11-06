import { expect, test, spyOn } from 'bun:test';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/file_canonical.test.mjs').prepare();
const workbench = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'file-canonical.fixture.mjs');
await build({ stdin: { contents: `
export { canonicalWorkspaceFileResources, canonicalRestoredWorkspaceFiles, workspaceFileUri } from './src/clients/workbench/src/workspaceFiles.js';
export { gateway } from './src/clients/workbench/src/agentApi.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer, bufferToStream, streamToBuffer } from '${upstream}base/common/buffer.js';
export { dirname } from '${upstream}base/common/resources.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { WorkingCopyBackupService } from '${upstream}workbench/services/workingCopy/common/workingCopyBackupService.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(workbench, sourceTransform(workbench, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(workbench, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const owner = { gatewayId: 'canonical-gateway', workspaceId: 'canonical-workspace' };
const root = `boxteam://workspace/${owner.workspaceId}/resources/files/root`;
const qualified = root.replace('boxteam://workspace/', `boxteam://${owner.gatewayId}/workspace/`);
const uri = (name, old = false, target = owner) => m.workspaceFileUri(target, (old ? qualified : root) + '/' + name, old ? qualified : root);
const identifier = resource => ({ resource, typeId: '' });
const body = text => m.bufferToStream(m.VSBuffer.fromString(text));
const text = async backup => (await m.streamToBuffer(backup.value)).toString();

/** 真实文件／备份服务；仅 owner 元数据 HTTP 与编辑器组展示是明确边界。 */
async function fixture(callback, project = path => path.replace(qualified, root).replace('/mixed/doc.json', '/Mixed/Doc.json')) {
  const files = new m.FileService(new m.NullLogService());
  const providers = ['backup', 'boxteam-file'].map(scheme => {
    const provider = new m.InMemoryFileSystemProvider();
    files.registerProvider(scheme, provider); return provider;
  });
  const identity = new m.UriIdentityService(files), backupRoot = m.URI.parse('backup:/hot-exit');
  let backups = new m.WorkingCopyBackupService(backupRoot, files, new m.NullLogService());
  const metadata = [], routes = [];
  const route = spyOn(m.gateway, 'resolveWorkspace').mockImplementation(async source => {
    routes.push({ gatewayId: source.gatewayId, workspaceId: source.workspaceId });
    return JSON.stringify([source.gatewayId, source.workspaceId]);
  });
  const request = spyOn(m.gateway, 'request').mockImplementation(async (path, options, workspaceKey) => {
    expect(path).toBe('/api/v1/workspace/files/references'); expect(options.method).toBe('POST');
    const paths = JSON.parse(options.body).paths;
    metadata.push({ owner: JSON.parse(workspaceKey), paths });
    return { uris: paths.map(project) };
  });
  const write = async (resource, contents) => {
    await files.createFolder(m.dirname(resource)); await files.writeFile(resource, m.VSBuffer.fromString(contents));
  };
  const group = (resource, id = 1) => {
    const viewState = { cursorState: [{ position: { lineNumber: 2, column: 4 } }] };
    const input = { resource, forceFile: true, options: { viewState } };
    const editor = { toUntyped: options => { expect(options).toEqual({ preserveViewState: id }); return input; } };
    const replacements = [];
    const value = { id, editors: [editor], isPinned: item => item === editor, isSticky: item => item === editor,
      replaceEditors: async items => { replacements.push(...items); } };
    return { value, replacements, viewState };
  };
  const restore = group => m.canonicalRestoredWorkspaceFiles(group.value, input => ({ input }), files, backups, identity, () => undefined);
  try {
    await callback({ files, identity, metadata, routes, write, group, restore, get backups() { return backups; },
      restart() { backups.dispose(); backups = new m.WorkingCopyBackupService(backupRoot, files, new m.NullLogService()); } });
  } finally { route.mockRestore(); request.mockRestore(); backups.dispose(); identity.dispose(); files.dispose(); providers.forEach(provider => provider.dispose()); }
}

test('文件规范准入按完整 owner 每批至多100条，投影收敛且不合并逻辑链接入口', async () => {
  await fixture(async f => {
    const other = { ...owner, gatewayId: 'another-gateway' };
    const values = [...Array.from({ length: 205 }, () => uri('mixed/doc.json', true)), uri('Alias/Doc.json'), uri('Mixed/Doc.json', false, other)];
    const canonical = await m.canonicalWorkspaceFileResources(values);
    expect(canonical.slice(0, 205).every(value => value.toString() === uri('Mixed/Doc.json').toString())).toBe(true);
    expect(canonical[205]).toBe(values[205]); expect(canonical[206]).toBe(values[206]);
    expect(canonical[205].toString()).not.toBe(canonical[0].toString());
    expect(canonical[206].toString()).not.toBe(canonical[0].toString());
    expect(f.routes).toEqual([owner, other]);
    expect(f.metadata.filter(batch => batch.owner[0] === owner.gatewayId).map(batch => batch.paths.length)).toEqual([100, 100, 6]);
    expect(f.metadata.filter(batch => batch.owner[0] === other.gatewayId).map(batch => batch.paths.length)).toEqual([1]);
  });
});

test('两个恢复组串行迁移真实持久备份，新键写入完成前不删旧键；无标签备份同迁且保留metadata和视图态', async () => {
  await fixture(async f => {
    const source = identifier(uri('mixed/doc.json', true)), target = identifier(uri('Mixed/Doc.json'));
    const orphan = identifier(uri('orphan.json', true)), canonicalOrphan = identifier(uri('orphan.json'));
    const meta = { mtime: 17, ctime: 11, size: 20, etag: 'original-etag', orphaned: false };
    await f.write(target.resource, '{"disk":true}'); await f.write(canonicalOrphan.resource, '{"diskOrphan":true}');
    await f.backups.backup(source, body('{"dirty":true}'), 37, meta);
    await f.backups.backup(orphan, body('{"orphanDirty":true}'), 51, meta);
    f.restart();
    await f.backups.getBackups();
    expect(f.backups.hasBackupSync(source, 0)).toBe(true);
    const entered = Promise.withResolvers(), release = Promise.withResolvers();
    const originalWrite = f.files.writeFile.bind(f.files);
    const write = spyOn(f.files, 'writeFile').mockImplementation(async (resource, ...args) => {
      if (resource.scheme === 'backup') { entered.resolve(); await release.promise; }
      return originalWrite(resource, ...args);
    });
    const discard = spyOn(f.backups, 'discardBackup');
    const first = f.group(source.resource), second = f.group(source.resource, 2);
    try {
      const pending = [f.restore(first), f.restore(second)];
      await entered.promise;
      expect(discard).not.toHaveBeenCalled(); expect(f.backups.hasBackupSync(source)).toBe(true);
      expect(await text(await f.backups.resolve(source))).toBe('{"dirty":true}');
      release.resolve(); await Promise.all(pending);
      const restored = await f.backups.resolve(target);
      expect(await text(restored)).toBe('{"dirty":true}'); expect(restored.meta).toEqual(meta);
      expect(await text(await f.backups.resolve(canonicalOrphan))).toBe('{"orphanDirty":true}');
      expect(await f.backups.resolve(source)).toBeUndefined(); expect(await f.backups.resolve(orphan)).toBeUndefined();
      expect(discard).toHaveBeenCalledTimes(2);
      for (const group of [first, second]) {
        expect(group.replacements).toHaveLength(1);
        expect(group.replacements[0].replacement.input.resource.toString()).toBe(target.resource.toString());
        expect(group.replacements[0].options).toEqual({ viewState: group.viewState, pinned: true, sticky: true, preserveFocus: true });
      }
    } finally { release.resolve(); write.mockRestore(); discard.mockRestore(); }
  });
});

test('规范目标已有草稿或两份旧备份汇入同目标时在写入前拒绝，保留所有原正文', async () => {
  for (const targetExists of [true, false]) await fixture(async f => {
    const source = identifier(uri('mixed/doc.json', true));
    const other = identifier(targetExists ? uri('Mixed/Doc.json') : uri('Mixed/Doc.json', true));
    await f.backups.backup(source, body('dirty-one')); await f.backups.backup(other, body('dirty-two'));
    const group = f.group(source.resource), write = spyOn(f.files, 'writeFile');
    try {
      await expect(f.restore(group)).rejects.toThrow('已有草稿备份');
      expect(write).not.toHaveBeenCalled(); expect(group.replacements).toEqual([]);
      expect(await text(await f.backups.resolve(source))).toBe('dirty-one');
      expect(await text(await f.backups.resolve(other))).toBe('dirty-two');
    } finally { write.mockRestore(); }
  });
});

test('新键持久写入、源读取或真实目标核实失败均保留旧备份，且不替换恢复输入', async () => {
  for (const phase of ['write', 'source', 'target', 'owner']) await fixture(async f => {
    const source = identifier(uri('mixed/doc.json', true)), target = uri('Mixed/Doc.json');
    await f.backups.backup(source, body('keep-dirty'));
    if (phase !== 'target') await f.write(target, 'disk');
    const group = f.group(source.resource);
    const fault = phase === 'write' ? spyOn(f.files, 'writeFile').mockRejectedValue(new Error('明确的新键写入失败'))
      : phase === 'source' ? spyOn(f.backups, 'resolve').mockResolvedValue(undefined)
      : phase === 'owner' ? spyOn(m.gateway, 'request').mockRejectedValue(new Error('明确的owner归一失败')) : undefined;
    try {
      await expect(f.restore(group)).rejects.toThrow();
      expect(group.replacements).toEqual([]); expect(f.backups.hasBackupSync(source)).toBe(true);
    } finally { fault?.mockRestore(); }
    expect(await text(await f.backups.resolve(source))).toBe('keep-dirty');
  });
});

test('规范目标已有损坏前缀的持久备份也不能被迁移覆盖', async () => {
  await fixture(async f => {
    const source = identifier(uri('mixed/doc.json', true)), target = identifier(uri('Mixed/Doc.json'));
    await f.backups.backup(source, body('keep-source'));
    await f.backups.backup(target, body('original-target'));
    const targetBackup = f.backups.toBackupResource(target);
    await f.files.writeFile(targetBackup, m.VSBuffer.fromString('broken-preamble-preserve-me'));
    f.restart();
    const group = f.group(source.resource);
    await expect(f.restore(group)).rejects.toThrow('已有草稿备份');
    expect(group.replacements).toEqual([]);
    expect(await text(await f.backups.resolve(source))).toBe('keep-source');
    expect((await f.files.readFile(targetBackup)).value.toString()).toBe('broken-preamble-preserve-me');
  });
});
