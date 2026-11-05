import { execFileSync, spawn } from 'node:child_process';
import { mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from './sourceTransform.mjs';
import { extensionDependencies } from './dependencies.mjs';

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
await writeFile(configuration, JSON.stringify({ extends: join(projectedRoot, 'tsconfig.jsonc'), compilerOptions: { preserveSymlinks: true },
  files: transformedSourcePaths.filter(path => path.startsWith('src/')).map(path => join(projectedRoot, 'upstream', path)) }));
const configurations = [{ path: configuration, compiler: 'tsgo' }];
// 扩展沿自己的 Node／vscode 声明检查，不能混入 Workbench 的全局计时器和 bundler 模块语义。
const extensionNames = [...new Set(transformedSourcePaths.filter(path => path.startsWith('extensions/')).map(path => path.split('/')[1]))];
const dependencies = await extensionDependencies(root);
for (const name of extensionNames) {
  const extensionConfiguration = join(projectedRoot, `tsconfig_typecheck_${name}.jsonc`);
  await writeFile(extensionConfiguration, JSON.stringify({
    extends: join(projectedRoot, 'upstream/extensions', name, 'tsconfig.json'),
    compilerOptions: { noEmit: true, rootDir: repository, preserveSymlinks: false,
      module: 'CommonJS', moduleResolution: 'Node10', ignoreDeprecations: '6.0', types: ['node'],
      typeRoots: [join(projectedRoot, 'node_modules/@types')],
      // 与实际扩展构建共用精确别名，不能误读根 node_modules 中其它扩展的同名依赖。
      paths: Object.fromEntries(dependencies.filter(entry => entry.name === name && entry.scope === '')
        .flatMap(entry => [[entry.dependency, [join(root, 'node_modules', entry.alias)]],
          [`${entry.dependency}/*`, [join(root, 'node_modules', entry.alias, '*')]]])) },
    files: transformedSourcePaths.filter(path => path.startsWith(`extensions/${name}/`)).map(path => join(projectedRoot, 'upstream', path)),
    include: [join(projectedRoot, 'upstream/src/vscode-dts/*.d.ts')],
  }));
  // 原扩展依赖 Node10 类型解析；tsgo 7 已移除此选项，使用产品既有 TypeScript 6 检查该宿主。
  configurations.push({ path: extensionConfiguration, compiler: 'tsc' });
}
await writeFile(join(output, 'sources.json'), JSON.stringify({ commit, targets, configurations }, null, 2));
console.log(`类型检查包含 ${targets.length} 份实际 sourceTransform 源码（${commit}）`);
// 全工作台并行检查会放大 checker 内存；使用编译器原生单线程选项，外部内存保护保持不变。
for (const { path: configuration, compiler } of configurations) {
  const code = await new Promise((resolve, reject) => {
    const child = spawn(join(root, 'node_modules/.bin', compiler), ['--project', configuration, '--noEmit',
      ...(compiler === 'tsgo' ? ['--singleThreaded'] : []), ...process.argv.slice(2)], { cwd: root, stdio: 'inherit' });
    child.on('error', reject);
    child.on('exit', (code, signal) => signal ? reject(new Error(`类型检查被 ${signal} 终止`)) : resolve(code ?? 1));
  });
  if (code !== 0) process.exitCode = code;
}
