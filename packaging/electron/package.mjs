import { packager } from '@electron/packager';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve, join, basename } from 'node:path';
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: { 'project-root': { type: 'string' }, 'output-root': { type: 'string' } } });
const projectRoot = resolve(values['project-root'] || process.env.BOXTEAM_PROJECT_ROOT || process.cwd());
const output = resolve(values['output-root'] ?? join(projectRoot, 'out/packaging/electron'));
const appRoot = join(output, 'app');
const product = JSON.parse(await readFile(join(appRoot, 'package.json'), 'utf8'));
const host = JSON.parse(await readFile(join(projectRoot, 'src/clients/electron/package.json'), 'utf8'));
const electronVersion = host.devDependencies.electron;
if (!/^\d+\.\d+\.\d+$/.test(electronVersion)) throw new Error('Electron 发行版本必须固定到精确版本');
const checksums = JSON.parse(await readFile(join(projectRoot, 'src/clients/electron/node_modules/electron/checksums.json'), 'utf8'));
if (!['linux', 'darwin', 'win32'].includes(process.platform)) throw new Error(`不支持的桌面平台：${process.platform}`);
await mkdir(join(output, 'tmp'), { recursive: true });
const bundles = await packager({
  dir: appRoot, out: join(output, 'bundles'), name: 'BoxTeam',
  appVersion: product.version, electronVersion,
  platform: process.platform, arch: process.arch,
  asar: true, overwrite: true, prune: false,
  download: { checksums, cacheRoot: join(output, 'downloads') },
  tmpdir: join(output, 'tmp'),
  // 主进程已经打成单个 ESM bundle，没有需要重建的原生业务依赖。
  executableName: 'boxteam',
});
await mkdir(join(output, 'releases'), { recursive: true });
const archives = [];
for (const bundle of bundles) {
  const archive = join(output, 'releases', `BoxTeam-${product.version}-${process.platform}-${process.arch}.tar.gz`);
  const result = spawnSync('tar', ['-czf', archive, '-C', resolve(bundle, '..'), basename(bundle)], { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Electron 归档失败：exit=${result.status}`);
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(archive)) digest.update(chunk);
  const sha256 = digest.digest('hex');
  await writeFile(archive + '.sha256', `${sha256}  ${basename(archive)}\n`);
  archives.push({ archive, sha256, bundle });
}
const manifest = { ...JSON.parse(await readFile(join(appRoot, 'build.json'), 'utf8')),
  electronVersion, platform: process.platform, arch: process.arch, archives };
await writeFile(join(output, 'release.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify(manifest, null, 2));
