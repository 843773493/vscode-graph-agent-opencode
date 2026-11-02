import { execFileSync, spawn } from 'node:child_process';
import { mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from './sourceTransform.mjs';

const root = process.cwd();
const repository = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: root, encoding: 'utf8' }).trim();
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: join(root, 'upstream'), encoding: 'utf8' }).trim();
if (commit !== pinnedUpstreamCommit) throw new Error(`上游版本不匹配：${commit}`);
// 放在统一构建产物目录，避免 Bun 发现镜像中的测试，也不与生产 build 清理 dist 竞态。
const output = join(repository, 'out/build/workbench-typecheck');
await rm(output, { recursive: true, force: true });
const overlay = join(output, 'repository');
const projectedRoot = join(overlay, relative(repository, root));
const targets = transformedSourcePaths.map(path => relative(repository, join(root, 'upstream', path)));

// 未改源码以链接复用；只有转换文件及其祖先目录实际落盘，保持所有相对导入指向同一模块身份。
async function project(directory = '') {
  await mkdir(join(overlay, directory), { recursive: true });
  for (const entry of await readdir(join(repository, directory), { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (path === relative(repository, join(root, 'dist')) || path === '.git' || path === 'out') continue;
    if (targets.includes(path)) continue;
    if (targets.some(target => target.startsWith(path + sep))) await project(path);
    else await symlink(join(repository, path), join(overlay, path), entry.isDirectory() ? 'junction' : 'file');
  }
}
await project();
const product = JSON.parse(await readFile(join(root, 'upstream/product.json'), 'utf8'));
const upstreamPackage = JSON.parse(await readFile(join(root, 'upstream/package.json'), 'utf8'));
const transform = sourceTransform(projectedRoot, { ...product, version: upstreamPackage.version, commit, quality: 'stable' }, []);
for (const path of transformedSourcePaths) {
  const destination = join(projectedRoot, 'upstream', path);
  await mkdir(dirname(destination), { recursive: true });
  await writeFile(destination, await transform(destination, await readFile(join(root, 'upstream', path), 'utf8')));
}
const configuration = join(projectedRoot, 'tsconfig_typecheck.jsonc');
// 配置位于投影的 Workbench 根，保留原 include、外部客户端与 node_modules 解析关系。
await writeFile(configuration, JSON.stringify({ extends: join(projectedRoot, 'tsconfig.jsonc'), compilerOptions: { preserveSymlinks: true } }));
await writeFile(join(output, 'sources.json'), JSON.stringify({ commit, targets, configuration }, null, 2));
console.log(`类型检查包含 ${targets.length} 份实际 sourceTransform 源码（${commit}）`);
const child = spawn(join(root, 'node_modules/.bin/tsgo'), ['--project', configuration, '--noEmit', ...process.argv.slice(2)], { cwd: root, stdio: 'inherit' });
child.on('error', error => { throw error; });
child.on('exit', (code, signal) => { if (signal) throw new Error(`类型检查被 ${signal} 终止`); process.exitCode = code ?? 1; });
