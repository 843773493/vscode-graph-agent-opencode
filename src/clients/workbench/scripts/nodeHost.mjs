import { build } from 'esbuild';
import { execFileSync } from 'node:child_process';
import { builtinModules } from 'node:module';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

/** 构建目标侧原 Node Extension Host；依赖安装和目标进程归后端基础设施 owner。 */
export async function buildNodeExtensionHost(root, output) {
  root = resolve(root);
  output = resolve(output);
  const upstream = join(root, 'upstream');
  const fromUpstream = relative(upstream, output);
  if (!isAbsolute(fromUpstream) && fromUpstream !== '..' && !fromUpstream.startsWith(`..${sep}`)) {
    throw new Error('Node 宿主构建不能写入固定上游源码');
  }
  const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: upstream, encoding: 'utf8' }).trim();
  if (commit !== pinnedUpstreamCommit) throw new Error(`上游版本不匹配：${commit}`);
  const json = async file => JSON.parse(await readFile(join(upstream, file), 'utf8'));
  const [product, pkg, lock, nodeVersion] = await Promise.all([
    json('product.json'), json('package.json'), json('package-lock.json'),
    readFile(join(upstream, '.nvmrc'), 'utf8').then(value => value.trim()),
  ]);
  await mkdir(dirname(output), { recursive: true });
  // 每次构建占用全新的专用目录；拒绝覆盖项目文件、链接或混入旧 generation。
  await mkdir(output);
  const result = await build({
    entryPoints: [join(upstream, 'src/vs/workbench/api/node/extensionHostProcess.ts')],
    outfile: join(output, 'extensionHostProcess.mjs'),
    bundle: true, format: 'esm', platform: 'node', target: `node${nodeVersion}`,
    packages: 'external', metafile: true, tsconfig: join(root, 'tsconfig.jsonc'),
    plugins: [buildSourcePlugin(root, sourceTransform(root,
      { ...product, version: pkg.version, commit, quality: 'stable' }, [], { runtime: 'node' }))],
    banner: { js: `import { createRequire as boxteamCreateRequire } from 'node:module';
const require = boxteamCreateRequire(import.meta.url);
if (process.versions.node !== ${JSON.stringify(nodeVersion)}) throw new Error('Node Extension Host 要求 Node ${nodeVersion}，实际为 ' + process.versions.node);
globalThis._VSCODE_FILE_ROOT = import.meta.dirname;` },
  });
  // 直接和动态 import 复用固定上游的精确版本，不维护另一份逐包版本表。
  const builtins = new Set(builtinModules.flatMap(name => [name, `node:${name}`]));
  const dependencies = new Set(Object.values(result.metafile.outputs).flatMap(value =>
    value.imports.filter(entry => entry.external && !builtins.has(entry.path)).map(entry =>
      entry.path.split('/').slice(0, entry.path.startsWith('@') ? 2 : 1).join('/'))));
  // 原宿主用 createRequire 在 parent 存活监测时按需加载，不在 esbuild import 图中。
  dependencies.add('@vscode/native-watchdog');
  const versions = Object.fromEntries([...dependencies].sort().map(name => {
    const version = lock.packages[`node_modules/${name}`]?.version;
    if (!version) throw new Error(`固定上游缺少 Node 宿主依赖：${name}`);
    return [name, version];
  }));
  const metadata = { commit, version: pkg.version, nodeVersion };
  // TODO: 目标安装切片须锁定完整依赖树、验证各平台原生模块，并发布匹配 Node 的不可变运行时。
  await Promise.all([
    writeFile(join(output, 'package.json'), JSON.stringify({
      name: '@boxteam/node-extension-host', private: true, type: 'module',
      engines: { node: nodeVersion }, dependencies: versions,
    }, null, 2) + '\n'),
    writeFile(join(output, 'version.json'), JSON.stringify(metadata, null, 2) + '\n'),
    writeFile(join(output, 'build-meta.json'), JSON.stringify(result.metafile)),
    ...['LICENSE.txt', 'ThirdPartyNotices.txt'].map(async name =>
      writeFile(join(output, name), await readFile(join(upstream, name)))),
  ]);
  return { ...metadata, dependencies: versions, inputs: Object.keys(result.metafile.inputs) };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const { values } = parseArgs({ options: { outdir: { type: 'string' } } });
  if (!values.outdir) throw new Error('Node 宿主构建必须明确指定 --outdir');
  const result = await buildNodeExtensionHost(resolve('src/clients/workbench'), resolve(values.outdir));
  console.log(`Node Extension Host 构建完成：${result.commit}，Node ${result.nodeVersion}`);
}
