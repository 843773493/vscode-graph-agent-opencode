import assert from 'node:assert/strict';
import { copyFile, readFile, readdir } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { downloadArtifact } from '@electron/get';

const [source, destination] = process.argv.slice(2);
assert.ok(source && destination, '必须显式指定官方 ZIP 缓存来源和本次测试缓存目录');
const root = process.cwd();
const host = JSON.parse(await readFile(join(root, 'src/clients/electron/package.json'), 'utf8'));
const version = host.devDependencies.electron;
const checksums = JSON.parse(await readFile(join(root, 'src/clients/electron/node_modules/electron/checksums.json'), 'utf8'));
const filename = `electron-v${version}-${process.platform}-${process.arch}.zip`;
const entries = await readdir(source, { recursive: true, withFileTypes: true });
const archives = entries.filter(entry => entry.isFile() && entry.name === filename);
if (archives.length === 0) {
  console.log(JSON.stringify({ seeded: false, filename }));
} else {
  assert.equal(archives.length, 1, '官方运行时缓存出现重复 ZIP，必须先明确来源');
  const archive = join(archives[0].parentPath, filename);
  // 由当前下载库生成缓存键并校验官方摘要，不依赖旧缓存目录的散列算法。
  const cached = await downloadArtifact({
    version, platform: process.platform, arch: process.arch, artifactName: 'electron',
    checksums, cacheRoot: resolve(destination),
    downloader: { async download(_url, target) { await copyFile(archive, target); } },
  });
  console.log(JSON.stringify({ seeded: true, source: archive, cached, sha256: checksums[filename] }));
}
