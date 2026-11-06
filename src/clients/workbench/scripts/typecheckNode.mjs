import { execFileSync, spawn } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { isAbsolute, join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { pinnedUpstreamCommit } from './sourceTransform.mjs';

const { values } = parseArgs({ options: { runtime: { type: 'string' }, outdir: { type: 'string' } } });
if (!values.runtime || !isAbsolute(values.runtime)) throw new Error('Node 类型检查必须以 --runtime 指定已安装冻结依赖的绝对目录');
const root = process.cwd(), runtime = values.runtime;
const repository = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: root, encoding: 'utf8' }).trim();
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: join(root, 'upstream'), encoding: 'utf8' }).trim();
if (commit !== pinnedUpstreamCommit) throw new Error(`上游版本不匹配：${commit}`);
const json = async file => JSON.parse(await readFile(file, 'utf8'));
const [manifest, metadata, upstreamLock, workbench] = await Promise.all([
  json(join(runtime, 'package.json')), json(join(runtime, 'version.json')),
  json(join(root, 'upstream/package-lock.json')), json(join(root, 'package.json')),
]);
if (metadata.commit !== commit) throw new Error(`Node 类型检查的组件不属于固定上游：${metadata.commit}`);

// 解析实际组件的冻结依赖，不从仓库其它 node_modules 偶然捡取同名运行包。
const paths = {};
for (const [name, version] of Object.entries(manifest.dependencies)) {
  const installed = await json(join(runtime, 'node_modules', name, 'package.json'));
  if (version !== upstreamLock.packages[`node_modules/${name}`]?.version || installed.version !== version) {
    throw new Error(`Node 组件依赖不匹配固定上游：${name}，要求 ${version}，实际 ${installed.version}`);
  }
  paths[name] = [join(runtime, 'node_modules', name)];
  paths[`${name}/*`] = [join(runtime, 'node_modules', name, '*')];
  const typeName = `@types/${name.replace(/^@/, '').replace('/', '__')}`;
  if (workbench.devDependencies[typeName]) {
    const typeVersion = upstreamLock.packages[`node_modules/${typeName}`]?.version;
    const installedType = await json(join(root, 'node_modules', typeName, 'package.json'));
    if (workbench.devDependencies[typeName] !== typeVersion || installedType.version !== typeVersion) {
      throw new Error(`Node 类型依赖不匹配固定上游：${typeName}，要求 ${typeVersion}，实际 ${installedType.version}`);
    }
    paths[name] = [join(root, 'node_modules', typeName)];
  }
}
const output = values.outdir ? resolve(values.outdir) : join(repository, 'out/build/workbench-node-typecheck');
await mkdir(output, { recursive: true });
const configuration = join(output, 'tsconfig.jsonc');
await writeFile(configuration, JSON.stringify({ extends: join(root, 'node/tsconfig.jsonc'),
  compilerOptions: { typeRoots: [join(root, 'node_modules/@types')], paths } }, null, 2) + '\n');
console.log(`Node 类型检查使用 ${runtime} 的 ${Object.keys(manifest.dependencies).length} 个冻结依赖（${commit}）`);
const code = await new Promise((resolve, reject) => {
  const child = spawn(join(root, 'node_modules/.bin/tsgo'), ['--project', configuration, '--noEmit', '--singleThreaded'], { cwd: root, stdio: 'inherit' });
  child.on('error', reject);
  child.on('exit', (code, signal) => signal ? reject(new Error(`Node 类型检查被 ${signal} 终止`)) : resolve(code ?? 1));
});
process.exitCode = code;
