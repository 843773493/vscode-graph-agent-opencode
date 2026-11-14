import { build } from 'esbuild';
import { existsSync } from 'node:fs';
import { mkdir, readFile, readdir, rm } from 'node:fs/promises';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import ts from 'typescript';
import { buildSourcePlugin } from './sourceTransform.mjs';

const sourceFileFor = path => {
  if (existsSync(path)) return path;
  const typescriptPath = path.replace(/\.m?js$/, '.ts').replace(/\.jsx$/, '.tsx');
  return existsSync(typescriptPath) ? typescriptPath : path;
};

const isWithin = (parent, path) => {
  const pathFromParent = relative(parent, path);
  return pathFromParent !== '' && pathFromParent !== '..' && !pathFromParent.startsWith('..' + sep) && !isAbsolute(pathFromParent);
};

function viteSourceUrl(root, path) {
  const upstreamSource = join(root, 'upstream/src');
  const workbenchSource = join(root, 'src');
  const webSource = resolve(root, '../web/src');
  const sharedSource = resolve(root, '../shared');
  if (isWithin(workbenchSource, path)) return '/' + relative(root, path).replaceAll('\\', '/');
  if (isWithin(upstreamSource, path) && path.endsWith('.css')) {
    return '/upstream/src/' + relative(upstreamSource, path).replaceAll('\\', '/');
  }
  if (isWithin(webSource, path) || isWithin(sharedSource, path)) {
    return '/@fs/' + path.replaceAll('\\', '/').replace(/^\//, '');
  }
}

async function collectSourceFiles(folder) {
  const files = [];
  for (const entry of await readdir(folder, { withFileTypes: true })) {
    const path = join(folder, entry.name);
    if (entry.isDirectory()) files.push(...await collectSourceFiles(path));
    else if (/\.tsx?$/.test(entry.name) && !entry.name.endsWith('.d.ts')) files.push(path);
  }
  return files;
}

/** 用一次 esbuild split build 固定上游模块身份，项目模块仍由 Vite 提供。 */
export async function buildWorkbenchDependencies(root, transformSource, outdir, productIdentity) {
  const upstreamSource = join(root, 'upstream/src');
  const appSource = join(root, 'src');
  const entries = new Set([join(upstreamSource, 'vs/workbench/workbench.web.main.internal.ts')]);
  for (const sourceFile of await collectSourceFiles(appSource)) {
    const source = await readFile(sourceFile, 'utf8');
    for (const { fileName } of ts.preProcessFile(source).importedFiles) {
      const path = sourceFileFor(resolve(dirname(sourceFile), fileName));
      if (isWithin(upstreamSource, path) && /\.tsx?$/.test(path) && !path.endsWith('.d.ts')) entries.add(path);
    }
  }

  const entryPoints = Object.fromEntries([...entries].sort().map(path => [
    relative(upstreamSource, path).replace(/\.tsx?$/, ''), path,
  ]));
  const developmentOutput = join(outdir, 'dev-upstream');
  await rm(developmentOutput, { recursive: true, force: true });
  await mkdir(outdir, { recursive: true });
  await build({
    absWorkingDir: root,
    entryPoints,
    bundle: true,
    splitting: true,
    format: 'esm',
    platform: 'browser',
    target: 'es2024',
    define: { __BOXTEAM_PRODUCT__: JSON.stringify(productIdentity) },
    outdir,
    entryNames: 'dev-upstream/[dir]/[name]',
    chunkNames: 'dev-upstream/chunks/[name]-[hash]',
    nodePaths: [join(root, 'node_modules')],
    tsconfig: join(root, 'tsconfig.jsonc'),
    loader: { '.ttf': 'file', '.svg': 'file', '.png': 'file', '.sh': 'file', '.wasm': 'file' },
    plugins: [
      {
        name: 'workbench-dev-source-ownership',
        setup(builder) {
          builder.onResolve({ filter: /^(react|react-dom)(\/.*)?$/ }, ({ path }) => ({ path, external: true }));
          builder.onResolve({ filter: /^(\.\.?\/|\/|[A-Za-z]:[\\/])/ }, args => {
            const [specifier, ...query] = args.path.split('?');
            const path = sourceFileFor(isAbsolute(specifier) ? specifier : resolve(args.resolveDir, specifier));
            if (isWithin(upstreamSource, path) && path.endsWith('.css') && query.length === 0) {
              return { path: '/upstream/src/' + relative(upstreamSource, path).replaceAll('\\', '/'), external: true };
            }
            const url = viteSourceUrl(root, path);
            return url ? { path: url + (query.length ? '?' + query.join('?') : ''), external: true } : undefined;
          });
        },
      },
      buildSourcePlugin(root, transformSource),
    ],
    logLevel: 'info',
  });

  return {
    transform(source, file) {
      const sourceFile = file.split('?', 1)[0];
      if (!isWithin(appSource, sourceFile) || !/\.tsx?$/.test(sourceFile)) return source;
      for (const { fileName } of ts.preProcessFile(source).importedFiles) {
        const path = sourceFileFor(resolve(dirname(sourceFile), fileName));
        if (!isWithin(upstreamSource, path)) continue;
        // 样式继续由 Vite 处理；脚本指向同一次 split build 的 entry，共享模块身份。
        const upstreamUrl = viteSourceUrl(root, path)
          ?? '/out/dev-upstream/' + relative(upstreamSource, path).replaceAll('\\', '/').replace(/\.tsx?$/, '.js');
        source = source.replaceAll("'" + fileName + "'", JSON.stringify(upstreamUrl))
          .replaceAll(JSON.stringify(fileName), JSON.stringify(upstreamUrl));
      }
      return source;
    },
  };
}
