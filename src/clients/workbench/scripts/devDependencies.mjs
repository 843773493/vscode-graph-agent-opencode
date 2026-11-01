import { readdir, readFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import ts from 'typescript';

/** 固定的 Code OSS 使用同一份 Vite 预构建，业务源码继续逐模块转换和热更新。 */
export async function workbenchDependencies(root) {
  const upstream = join(root, 'upstream/src');
  const application = join(root, 'src');
  const shared = resolve(root, '../web/src');
  const dependencyId = path => `@boxteam/code-oss/${relative(upstream, path).replaceAll('\\', '/')}`;
  const isUpstream = path => path.startsWith(`${upstream}/`) || path.startsWith(`${upstream}\\`);
  const imports = (source, file) => ts.preProcessFile(source).importedFiles
    .map(({ fileName }) => ({ specifier: fileName, path: resolve(dirname(file), fileName) }))
    .filter(({ path }) => isUpstream(path));
  const include = new Set();
  for (const entry of await readdir(application, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name)) continue;
    const file = join(entry.parentPath, entry.name);
    for (const { path } of imports(await readFile(file, 'utf8'), file)) include.add(dependencyId(path));
  }
  return {
    include: [...include].sort(),
    alias: { find: '@boxteam/code-oss', replacement: upstream },
    transform(source, file) {
      if (!file.startsWith(application) || !/\.tsx?$/.test(file)) return source;
      for (const { specifier, path } of imports(source, file)) {
        const id = JSON.stringify(dependencyId(path));
        source = source.replaceAll(`'${specifier}'`, id).replaceAll(JSON.stringify(specifier), id);
      }
      return source;
    },
    plugin: {
      name: 'keep-workbench-business-source',
      setup(build) {
        build.onResolve({ filter: /./ }, ({ path, resolveDir }) => {
          if (!resolveDir || (!path.startsWith('.') && !isAbsolute(path))) return;
          const file = resolve(resolveDir, path);
          if (file.startsWith(application) || file.startsWith(shared)) {
            // 上游文件系统注册会引用业务源码；不得把它固化进依赖包或复制类身份。
            const normalized = file.replaceAll('\\', '/');
            const url = file.startsWith(application) ? `/${relative(root, file)}` : `/@fs/${normalized.replace(/^\//, '')}`;
            return { path: url.replaceAll('\\', '/'), external: true };
          }
        });
      },
    },
  };
}
