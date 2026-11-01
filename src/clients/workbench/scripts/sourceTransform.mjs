import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

/** 构建与 Vite 共用上游源码定制，不改写固定子模块。 */
export function sourceTransform(root, productConfiguration, extensions) {
  const upstream = join(root, 'upstream');
  return async function transformSource(path, source) {
    let contents = source;
    const replacements = {
      '/*BUILD->INSERT_PRODUCT_CONFIGURATION*/': JSON.stringify(productConfiguration).slice(1, -1),
      '/*BUILD->INSERT_BUILTIN_EXTENSIONS*/': JSON.stringify(extensions).slice(1, -1),
    };
    for (const [marker, value] of Object.entries(replacements)) contents = contents.replace(marker, () => value);
    if (path === join(upstream, 'src/vs/workbench/browser/web.main.ts')) {
      const marker = 'await this.registerIndexedDBFileSystemProviders(environmentService, fileService, logService, loggerService, logsPath);';
      const cacheSchemes = '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp] /* Cache all non native resources */';
      const closeDatabase = 'this.onWillShutdownDisposables.add(toDisposable(() => indexedDB?.close()));';
      if (!contents.includes(marker)) throw new Error('上游文件系统初始化入口已变更');
      if (!contents.includes(cacheSchemes)) throw new Error('上游本地配置资源分类入口已变更');
      if (!contents.includes(closeDatabase)) throw new Error('上游 IndexedDB 关闭入口已变更');
      // 默认虚拟工作区与 vscode-userdata 一样由本地 IndexedDB 提供；额外配置缓存会留下关闭后的异步写入。
      contents = `import { installWorkspaceFileSystem, workspaceFolder } from ${JSON.stringify(join(root, 'src/filesystem.ts'))};\n`
        + contents.replace(marker, `${marker}\nawait installWorkspaceFileSystem(fileService);`)
          .replace(cacheSchemes, '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp, workspaceFolder.scheme]')
          // pagehide 的同步 shutdown 仍会产生延迟日志；数据库连接由浏览器随文档释放，不能提前关闭并中止事务。
          .replace(closeDatabase, '// IndexedDB 连接随浏览器文档释放，保留关闭期间日志和设置写入的机会。');
    }
    return contents;
  };
}

export function buildSourcePlugin(root, transformSource) {
  return {
    name: 'upstream-build-configuration',
    setup(builder) {
      builder.onResolve({ filter: /\.css\?text$/ }, ({ path, resolveDir }) => ({ path: resolve(resolveDir, path.slice(0, -5)), namespace: 'session-stylesheet' }));
      builder.onLoad({ filter: /.*/, namespace: 'session-stylesheet' }, async ({ path }) => ({ contents: await readFile(path, 'utf8'), loader: 'text' }));
      builder.onResolve({ filter: /^\.\/codicon\.ttf$/ }, () => ({ path: join(root, 'node_modules/@vscode/codicons/dist/codicon.ttf') }));
      builder.onLoad({ filter: /\.ts$/ }, async ({ path }) => {
        if (path.endsWith('.d.ts')) return;
        const source = await readFile(path, 'utf8');
        const contents = await transformSource(path, source);
        if (contents !== source) return { contents, loader: 'ts' };
      });
      builder.onResolve({ filter: /node_modules\// }, ({ path }) => ({ path: join(root, 'node_modules', path.split('node_modules/').at(-1)) }));
    },
  };
}
