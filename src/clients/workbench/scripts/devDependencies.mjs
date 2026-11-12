import { readdir, readFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import ts from 'typescript';

/** 固定上游参与 Vite 依赖预构建，业务源码继续按需加载与热更新。 */
export async function workbenchDependencies(root, transformSource) {
  const upstream = join(root, 'upstream/src');
  const application = join(root, 'src');
  const shared = resolve(root, '../web/src');
  const dependencyId = path => '@boxteam/code-oss/' + relative(upstream, path).replaceAll('\\', '/');
  const isUpstream = path => path.startsWith(upstream + '/') || path.startsWith(upstream + '\\');
  const imports = (source, file) => ts.preProcessFile(source).importedFiles
    .map(({ fileName }) => ({ specifier: fileName, path: resolve(dirname(file), fileName) }))
    .filter(({ path }) => isUpstream(path));
  const include = new Set();
  for (const entry of await readdir(application, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name)) continue;
    const file = join(entry.parentPath, entry.name);
    for (const { path } of imports(await readFile(file, 'utf8'), file)) include.add(dependencyId(path));
  }
  const transform = (source, file) => {
    if (!file.startsWith(application) || !/\.tsx?$/.test(file)) return source;
    for (const { specifier, path } of imports(source, file)) {
      const id = JSON.stringify(dependencyId(path));
      source = source.replaceAll("'" + specifier + "'", id).replaceAll(JSON.stringify(specifier), id);
    }
    return source;
  };
  return {
    include: [...include].sort(),
    alias: { find: '@boxteam/code-oss', replacement: upstream },
    transform,
    plugin: {
      name: 'workbench-optimized-source',
      async resolveId(id, importer) {
        if (!importer) return;
        if (id.endsWith('.css?text')) return resolve(dirname(importer), id.slice(0, -5)) + '?workbench-style';
        if (id.includes('node_modules/')) return join(root, 'node_modules', id.split('node_modules/').at(-1));
        if (!isUpstream(importer) || (!id.startsWith('.') && !isAbsolute(id))) return;
        const file = resolve(dirname(importer), id);
        // 上游反向引用的业务模块保留真实源码身份，不固化到依赖包。
        if (file.startsWith(application) || file.startsWith(shared)) {
          const resolved = await this.resolve(id, importer, { skipSelf: true });
          if (!resolved) this.error('业务源码无法解析：' + id);
          const target = resolved.id;
          const url = target.startsWith(application) ? '/' + relative(root, target) : '/@fs/' + target.replaceAll('\\', '/').replace(/^\//, '');
          return { id: url.replaceAll('\\', '/'), external: true };
        }
      },
      async load(id) {
        if (id.endsWith('?workbench-style')) return 'export default ' + JSON.stringify(await readFile(id.slice(0, -16), 'utf8'));
      },
      transform(code, id) {
        if (id.startsWith(application)) return transform(code, id);
        if (id.endsWith('.ts')) return transformSource(id, code);
      },
    },
  };
}
