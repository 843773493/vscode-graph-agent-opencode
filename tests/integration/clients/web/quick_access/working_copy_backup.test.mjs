import { afterAll, expect, spyOn, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/working_copy_backup.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'working-copy-backup.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { WorkingCopyBackupService } from '${upstream}workbench/services/workingCopy/common/workingCopyBackupService.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer, bufferToReadable, streamToBuffer } from '${upstream}base/common/buffer.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const receipt = { boundary: '真实转换后WorkingCopyBackupService／FileService／内存文件Provider，备份由原服务生成；仅坏元数据、截断与读取拒绝为故障注入，不代表真实Gateway页面或IndexedDB验收', checks: [] };

async function fixture(callback) {
  const log = new m.NullLogService(), files = new m.FileService(log), provider = new m.InMemoryFileSystemProvider();
  const registration = files.registerProvider('vscode-userdata', provider);
  const home = m.URI.parse('vscode-userdata:/Backups/workbench'), services = [];
  const open = () => { const service = new m.WorkingCopyBackupService(home, files, log); services.push(service); return service; };
  const identifier = gateway => ({ resource: m.URI.parse(`boxteam-file://${gateway}/workspace/root/same.txt`), typeId: '' });
  const a = identifier('gateway-a'), b = identifier('gateway-b');
  const content = value => m.bufferToReadable(m.VSBuffer.fromString(value));
  try { await callback({ files, home, open, a, b, content, log }); }
  finally { for (const service of services.reverse()) service.dispose(); registration.dispose(); files.dispose(); provider.dispose(); }
}

test('原备份跨服务重建保留正文、metadata及同路径owner，未创建备份仍为正常缺失', async () => {
  await fixture(async f => {
    const service = f.open(), meta = { mtime: 12, boxteamFileRevision: 'revision-a' };
    expect(await service.resolve(f.a)).toBeUndefined();
    await service.backup(f.a, f.content('A尚未保存'), 1, meta);
    await service.backup(f.b, f.content('B尚未保存'), 2, { mtime: 23 });
    service.dispose();
    const restored = f.open();
    const identifiers = await restored.getBackups();
    expect(identifiers.map(item => item.resource.toString()).sort()).toEqual([f.a, f.b].map(item => item.resource.toString()).sort());
    const a = await restored.resolve(f.a), b = await restored.resolve(f.b);
    expect(a.meta).toEqual(meta);
    expect((await m.streamToBuffer(a.value)).toString()).toBe('A尚未保存');
    expect((await m.streamToBuffer(b.value)).toString()).toBe('B尚未保存');
    receipt.checks.push('正常重建保留正文／metadata／完整owner，真正缺失不报错');
  });
});

for (const metadata of ['{"typeId":', 'null', '[]', '{"typeId":42}']) {
  test(`损坏metadata ${metadata} 明确拒绝枚举和恢复，原件及另一owner保留`, async () => {
    await fixture(async f => {
      const service = f.open();
      await service.backup(f.a, f.content('不可丢失的正文'), 1, { boxteamFileRevision: 'revision-a' });
      await service.backup(f.b, f.content('另一owner'), 1);
      const resource = service.toBackupResource(f.a), original = (await f.files.readFile(resource)).value;
      const broken = `${f.a.resource.toString()} ${metadata}\n不可丢失的正文`;
      await f.files.writeFile(resource, m.VSBuffer.fromString(broken)); service.dispose();
      const restored = f.open();
      await expect(restored.getBackups()).rejects.toThrow(resource.toString());
      const stream = f.files.readFileStream.bind(f.files), releases = [];
      const reads = spyOn(f.files, 'readFileStream').mockImplementation(async target => {
        const result = await stream(target);
        releases.push(spyOn(result.value, 'destroy'));
        return result;
      });
      try {
        await expect(restored.resolve(f.a)).rejects.toThrow(resource.toString());
        expect(releases).toHaveLength(1);
        expect(releases[0]).toHaveBeenCalledTimes(1);
      } finally { reads.mockRestore(); for (const release of releases) release.mockRestore(); }
      expect((await f.files.readFile(resource)).value.toString()).toBe(broken);
      expect((await m.streamToBuffer((await restored.resolve(f.b)).value)).toString()).toBe('另一owner');
      // 故障修正后沿同一个服务重新读取，不靠清空profile、重建索引或额外恢复账本。
      await f.files.writeFile(resource, original);
      const recovered = await restored.resolve(f.a);
      expect(recovered.meta.boxteamFileRevision).toBe('revision-a');
      expect((await m.streamToBuffer(recovered.value)).toString()).toBe('不可丢失的正文');
      receipt.checks.push(`metadata ${metadata} 拒绝／原件保持／另一owner可恢复／显式修正后原链重试`);
    });
  });
}

test('缺少preamble结束标记明确报错，不能当作没有备份回退读盘', async () => {
  await fixture(async f => {
    const service = f.open();
    await service.backup(f.a, f.content('尚未保存'), 1);
    const resource = service.toBackupResource(f.a), truncated = '截断的备份，没有换行分隔符';
    await f.files.writeFile(resource, m.VSBuffer.fromString(truncated)); service.dispose();
    const restored = f.open();
    await expect(restored.getBackups()).rejects.toThrow(resource.toString());
    await expect(restored.resolve(f.a)).rejects.toThrow(resource.toString());
    expect((await f.files.readFile(resource)).value.toString()).toBe(truncated);
    receipt.checks.push('截断的preamble拒绝枚举／恢复，原字节保持');
  });
});

test('目录读取失败不能成为空备份集，保留原错误及实际文件', async () => {
  await fixture(async f => {
    const service = f.open();
    await service.backup(f.a, f.content('原备份'), 1); service.dispose();
    const failure = new Error('备份目录读取被拒绝');
    const resolve = f.files.resolve.bind(f.files);
    const denied = spyOn(f.files, 'resolve').mockImplementation(resource => resource.toString() === f.home.toString()
      ? Promise.reject(failure) : resolve(resource));
    try { await expect(f.open().getBackups()).rejects.toBe(failure); }
    finally { denied.mockRestore(); }
    const restored = await f.open().resolve(f.a);
    expect((await m.streamToBuffer(restored.value)).toString()).toBe('原备份');
    receipt.checks.push('目录拒读保留同一错误，恢复访问后原备份保持');
  });
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
