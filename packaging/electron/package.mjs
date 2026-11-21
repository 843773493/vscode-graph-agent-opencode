import { packager } from '@electron/packager';
import { readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { resolve, join, basename } from 'node:path';
import { create as createTar } from 'tar';
import { parseArgs } from 'node:util';
import { spawnSync } from 'node:child_process';

const { values } = parseArgs({ options: { 'project-root': { type: 'string' }, 'output-root': { type: 'string' } } });
const projectRoot = resolve(values['project-root'] || process.env.BOXTEAM_PROJECT_ROOT || process.cwd());
const output = resolve(values['output-root'] ?? join(projectRoot, 'out/packaging/electron'));
await rm(join(output, 'release.json'), { force: true });
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
  icon: join(appRoot, 'renderer/out/branding/icon'),
  win32metadata: { CompanyName: product.productName },
  platform: process.platform, arch: process.arch,
  asar: true, overwrite: true, prune: false,
  download: { checksums, cacheRoot: join(output, 'downloads') },
  tmpdir: join(output, 'tmp'),
  // 主进程已经打成单个 ESM bundle，没有需要重建的原生业务依赖。
  executableName: 'boxteam',
});
await mkdir(join(output, 'releases'), { recursive: true });
const archives = [];
async function writeChecksum(file) {
  const digest = createHash('sha256');
  for await (const chunk of createReadStream(file)) digest.update(chunk);
  const sha256 = digest.digest('hex');
  await writeFile(file + '.sha256', `${sha256}  ${basename(file)}\n`);
  return sha256;
}
const installers = [];
for (const bundle of bundles) {
  const archive = join(output, 'releases', `BoxTeam-${product.version}-${process.platform}-${process.arch}.tar.gz`);
  await createTar({ gzip: true, file: archive, cwd: resolve(bundle, '..'), strict: true }, [basename(bundle)]);
  const sha256 = await writeChecksum(archive);
  archives.push({ archive, sha256, bundle });
  if (process.platform === 'win32') {
    const installer = join(output, 'releases', `BoxTeam-${product.version}-win32-${process.arch}-setup.exe`);
    const result = spawnSync(process.env.BOXTEAM_NSIS_BIN ?? 'makensis', [
      `-DVERSION=${product.version}`, `-DINPUTDIR=${bundle}`, `-DOUTPUT=${installer}`,
      join(projectRoot, 'packaging/electron/installer.nsi'),
    ], { stdio: 'inherit', windowsHide: true, env: process.env });
    if (result.error) throw result.error;
    if (result.status !== 0 || result.signal) throw new Error(`Electron 安装器构建失败：exit=${result.status}, signal=${result.signal}`);
    installers.push({ installer, sha256: await writeChecksum(installer) });
  }
}
const manifest = { ...JSON.parse(await readFile(join(appRoot, 'build.json'), 'utf8')),
  electronVersion, platform: process.platform, arch: process.arch, archives,
  ...(installers.length ? { installers } : {}) };
await writeFile(join(output, 'release.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(JSON.stringify(manifest, null, 2));
