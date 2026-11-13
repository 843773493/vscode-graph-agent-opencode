import { afterAll, expect, spyOn, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/working_copy_history.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'working-copy-history.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { WorkingCopyHistoryService } from '${upstream}workbench/services/workingCopy/common/workingCopyHistoryService.js';
export { FileService } from '${upstream}platform/files/common/fileService.js';
export { InMemoryFileSystemProvider } from '${upstream}platform/files/common/inMemoryFilesystemProvider.js';
export { UriIdentityService } from '${upstream}platform/uriIdentity/common/uriIdentityService.js';
export { NullLogService } from '${upstream}platform/log/common/log.js';
export { CancellationToken } from '${upstream}base/common/cancellation.js';
export { URI } from '${upstream}base/common/uri.js';
export { VSBuffer } from '${upstream}base/common/buffer.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const token = m.CancellationToken.None;
const receipt = { boundary: '真实转换后的WorkingCopyHistoryService、FileService、URI identity和内存Provider；历史由原addEntry生成后注入格式损坏或文件读取拒绝，不代表IndexedDB、Gateway页面或异常退出验收', checks: [] };

async function fixture(callback) {
  const log = new m.NullLogService(), files = new m.FileService(log), provider = new m.InMemoryFileSystemProvider();
  const registrations = ['vscode-userdata', 'boxteam-file'].map(scheme => files.registerProvider(scheme, provider));
  const identity = new m.UriIdentityService(files), services = [];
  const home = m.URI.parse('vscode-userdata:/History');
  class Service extends m.WorkingCopyHistoryService { getModelOptions() { return { flushOnChange: true }; } }
  const open = () => {
    const service = new Service(files, { getEnvironment: async () => null }, { localHistoryHome: home }, identity,
      { getUriBasenameLabel: resource => resource.path.split('/').at(-1), getUriLabel: resource => resource.toString() }, log,
      { getValue: key => key === 'workbench.localHistory.maxFileEntries' ? 50 : 0 });
    services.push(service); return service;
  };
  const a = m.URI.parse('boxteam-file://gateway-a/workspace/root/same.txt');
  const b = m.URI.parse('boxteam-file://gateway-b/workspace/root/same.txt');
  const write = (resource, text) => files.writeFile(resource, m.VSBuffer.fromString(text));
  try {
    const service = open();
    await write(a, 'A历史正文');
    const first = await service.addEntry({ resource: a, timestamp: 1000 }, token);
    await write(b, 'B历史正文');
    await service.addEntry({ resource: b, timestamp: 2000 }, token);
    const listing = m.URI.joinPath(first.location, '../entries.json');
    const original = (await files.readFile(listing)).value.toString();
    service.dispose();
    await callback({ files, open, a, b, first, listing, original, write, home, log });
  } finally {
    for (const service of services.reverse()) service.dispose();
    identity.dispose(); for (const registration of registrations.reverse()) registration.dispose(); files.dispose(); provider.dispose();
  }
}

test('正常重建保留历史正文、时间、完整owner及原保存入口，缺失为正常空历史', async () => {
  await fixture(async f => {
    const service = f.open();
    const entries = await service.getEntries(f.a, token);
    expect(entries).toHaveLength(1); expect(entries[0].timestamp).toBe(1000);
    expect(entries[0].workingCopy.resource.toString()).toBe(f.a.toString());
    expect((await f.files.readFile(entries[0].location)).value.toString()).toBe('A历史正文');
    expect((await service.getAll(token)).map(uri => uri.toString()).sort()).toEqual([f.a, f.b].map(uri => uri.toString()).sort());
    expect(await service.getEntries(m.URI.parse('boxteam-file://gateway-c/workspace/root/missing.txt'), token)).toEqual([]);
    await f.write(f.a, 'A新历史');
    await service.addEntry({ resource: f.a, timestamp: 4000 }, token);
    expect(await f.open().getEntries(f.a, token)).toHaveLength(2);
    receipt.checks.push('正常跨服务重建、保存及完整owner保持，真实缺失为空');
  });
});

const corruptions = [
  ['坏JSON', () => '{"version":'],
  ['未知版本', value => JSON.stringify({ ...value, version: 2 })],
  ['owner不符', (value, f) => JSON.stringify({ ...value, resource: f.b.toString() })],
  ['空记录', () => 'null'],
  ['entries格式不符', value => JSON.stringify({ ...value, entries: null })],
  ['entry格式不符', value => JSON.stringify({ ...value, entries: [{ ...value.entries[0], timestamp: '旧时间' }] })],
];
for (const [name, corrupt] of corruptions) {
  test(`${name}拒绝读取和追加，不覆盖原件或快照，另一owner保持，修正后原链恢复`, async () => {
    await fixture(async f => {
      const broken = corrupt(JSON.parse(f.original), f);
      await f.write(f.listing, broken);
      const service = f.open();
      await expect(service.getEntries(f.a, token)).rejects.toThrow(f.listing.toString());
      const writes = spyOn(f.files, 'cloneFile');
      try {
        await expect(service.addEntry({ resource: f.a, timestamp: 4000 }, token)).rejects.toThrow(f.listing.toString());
        expect(writes).not.toHaveBeenCalled();
      } finally { writes.mockRestore(); }
      expect((await f.files.readFile(f.listing)).value.toString()).toBe(broken);
      expect((await f.files.readFile(f.first.location)).value.toString()).toBe('A历史正文');
      const b = await service.getEntries(f.b, token);
      expect((await f.files.readFile(b[0].location)).value.toString()).toBe('B历史正文');
      await f.write(f.listing, f.original);
      expect(await f.open().getEntries(f.a, token)).toHaveLength(1);
      receipt.checks.push(`${name}拒绝读/写、零clone、原件与另一owner保持，显式修复后原链读取`);
    });
  });
}

test('列出全部历史不能把未知版本吞成部分成功', async () => {
  await fixture(async f => {
    await f.write(f.listing, JSON.stringify({ ...JSON.parse(f.original), version: 2 }));
    await expect(f.open().getAll(token)).rejects.toThrow(f.listing.toString());
    expect(JSON.parse((await f.files.readFile(f.listing)).value.toString()).version).toBe(2);
    receipt.checks.push('getAll未知版本明确失败并保留原件');
  });
});

test('实际历史目录拒读保留同一错误，不能变为空历史后写回', async () => {
  await fixture(async f => {
    const failure = new Error('历史目录读取被拒绝'), resolve = f.files.resolve.bind(f.files);
    const denied = spyOn(f.files, 'resolve').mockImplementation(resource => resource.toString().startsWith(f.home.toString())
      ? Promise.reject(failure) : resolve(resource));
    try {
      await expect(f.open().getEntries(f.a, token)).rejects.toBe(failure);
      await expect(f.open().getAll(token)).rejects.toBe(failure);
    } finally { denied.mockRestore(); }
    expect((await f.files.readFile(f.listing)).value.toString()).toBe(f.original);
    expect(await f.open().getEntries(f.a, token)).toHaveLength(1);
    receipt.checks.push('目录拒读原错传播，原件与恢复能力保持');
  });
});

test('移动历史先读取目标格式，目标损坏不能先移动或覆盖任一方原件', async () => {
  await fixture(async f => {
    const service = f.open(), b = await service.getEntries(f.b, token);
    const listingB = m.URI.joinPath(b[0].location, '../entries.json');
    const broken = JSON.stringify({ ...JSON.parse((await f.files.readFile(listingB)).value.toString()), version: 2 });
    await f.write(listingB, broken);
    const fresh = f.open(); await fresh.getEntries(f.a, token);
    const moves = spyOn(f.files, 'move');
    try {
      await expect(fresh.moveEntries(f.a, f.b)).rejects.toThrow(listingB.toString());
      expect(moves).not.toHaveBeenCalled();
    } finally { moves.mockRestore(); }
    expect((await f.files.readFile(f.listing)).value.toString()).toBe(f.original);
    expect((await f.files.readFile(listingB)).value.toString()).toBe(broken);
    expect((await f.files.readFile(f.first.location)).value.toString()).toBe('A历史正文');
    expect((await f.files.readFile(b[0].location)).value.toString()).toBe('B历史正文');
    receipt.checks.push('移动前核源/目标，目标坏格式拒绝且零move，双方原件与正文保持');
  });
});

test('正常移动沿原服务合并双方历史并登记目标owner', async () => {
  await fixture(async f => {
    const service = f.open(); await service.getEntries(f.a, token);
    expect((await service.moveEntries(f.a, f.b)).map(uri => uri.toString())).toEqual([f.b.toString()]);
    const entries = await f.open().getEntries(f.b, token);
    expect(entries).toHaveLength(3);
    expect(entries.every(entry => entry.workingCopy.resource.toString() === f.b.toString())).toBe(true);
    const bodies = await Promise.all(entries.map(async entry => (await f.files.readFile(entry.location)).value.toString()));
    expect(bodies.sort()).toEqual(['A历史正文', 'B历史正文', 'B历史正文']);
    receipt.checks.push('正常移动合并源/目标和移动快照，正文与目标owner保持');
  });
});

test('实际列表文件拒读保留同一错误及原件', async () => {
  await fixture(async f => {
    const failure = new Error('历史列表读取被拒绝'), read = f.files.readFile.bind(f.files);
    const denied = spyOn(f.files, 'readFile').mockImplementation(resource => resource.toString() === f.listing.toString()
      ? Promise.reject(failure) : read(resource));
    try {
      await expect(f.open().getEntries(f.a, token)).rejects.toBe(failure);
      await expect(f.open().getAll(token)).rejects.toBe(failure);
    } finally { denied.mockRestore(); }
    expect((await f.files.readFile(f.listing)).value.toString()).toBe(f.original);
    expect(await f.open().getEntries(f.a, token)).toHaveLength(1);
    receipt.checks.push('列表文件拒读保留原错，原件及显式重新读取保持');
  });
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
