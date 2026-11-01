import { build } from 'esbuild';
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { resolve, join, relative } from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: { 'project-root': { type: 'string' }, 'output-root': { type: 'string' } } });
const projectRoot = resolve(values['project-root'] || process.env.BOXTEAM_PROJECT_ROOT || process.cwd());
const output = resolve(values['output-root'] ?? join(projectRoot, 'out/packaging/electron'));
const appRoot = join(output, 'app');
const renderer = join(appRoot, 'renderer');
const project = JSON.parse(await readFile(join(projectRoot, 'package.json'), 'utf8'));
await rm(appRoot, { recursive: true, force: true });
await mkdir(appRoot, { recursive: true });
const result = spawnSync('bun', [join(projectRoot, 'src/clients/workbench/scripts/build.mjs'),
  '--entry', join(projectRoot, 'src/clients/electron-web/main.ts'), '--outdir', join(output, 'renderer/dist')],
{ cwd: projectRoot, stdio: 'inherit' });
if (result.error) throw result.error;
if (result.status !== 0) throw new Error(`Electron renderer 构建失败：exit=${result.status}`);
await cp(join(output, 'renderer/dist'), renderer, { recursive: true });
const host = await build({
  absWorkingDir: projectRoot, entryPoints: ['src/clients/electron/main/main.mjs'],
  outfile: join(appRoot, 'main.mjs'), platform: 'node', format: 'esm', target: 'node22',
  bundle: true, external: ['electron'], metafile: true,
  // http-proxy 的 CommonJS 依赖只在主进程 bundle 内解析 Node 内置模块。
  banner: { js: "import { createRequire as createNativeRequire } from 'node:module'; const require = createNativeRequire(import.meta.url);" },
});
await writeFile(join(appRoot, 'package.json'), JSON.stringify({ name: 'boxteam-desktop', productName: 'BoxTeam',
  version: project.version, type: 'module', main: 'main.mjs', private: true }, null, 2) + '\n');
await cp(join(renderer, 'LICENSE.txt'), join(appRoot, 'LICENSE.txt'));
await cp(join(renderer, 'ThirdPartyNotices.txt'), join(appRoot, 'ThirdPartyNotices.txt'));
const rendererMeta = JSON.parse(await readFile(join(renderer, 'build-meta.json'), 'utf8'));
const digest = createHash('sha256');
const inputs = [...Object.keys(rendererMeta.inputs), ...Object.keys(host.metafile.inputs),
  'packaging/electron/build.mjs', 'package.json', 'src/clients/electron/package.json',
  'src/clients/workbench/scripts/build.mjs', 'src/clients/workbench/scripts/sourceTransform.mjs',
  'src/clients/workbench/index.html'];
for (const file of [...new Set(inputs.map(file => resolve(projectRoot, file.replace(/^session-stylesheet:/, ''))))].sort()) {
  digest.update(relative(projectRoot, file).replaceAll('\\', '/')).update('\0').update(await readFile(file)).update('\0');
}
const manifest = { version: project.version, sourceFingerprint: digest.digest('hex'),
  workbench: JSON.parse(await readFile(join(renderer, 'version.json'), 'utf8')) };
await writeFile(join(appRoot, 'build.json'), JSON.stringify(manifest, null, 2) + '\n');
// 构建路径只属于验证元数据，不把源码绝对路径表放进产品。
await rm(join(renderer, 'build-meta.json'));
await writeFile(join(output, 'build-meta.json'), JSON.stringify({ ...manifest, host: host.metafile, renderer: rendererMeta }));
console.log(`Electron 应用构建完成：${appRoot}`);
