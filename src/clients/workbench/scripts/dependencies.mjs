import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const json = async path => JSON.parse(await readFile(path, 'utf8'));

export async function extensionDependencies(root) {
  const entries = [];
  for (const name of await readdir(join(root, 'upstream/extensions'))) {
    if (name.includes('test')) continue;
    const folder = join(root, 'upstream/extensions', name);
    const manifest = await json(join(folder, 'package.json')).catch(error => {
      if (['ENOENT', 'ENOTDIR'].includes(error.code)) return undefined;
      throw error;
    });
    if (!manifest || (manifest.main && !manifest.browser)) continue;
    for (const scope of ['', 'client', 'server']) {
      const path = join(folder, scope);
      const pkg = scope ? await json(join(path, 'package.json')).catch(error => {
        if (error.code === 'ENOENT') return undefined;
        throw error;
      }) : manifest;
      if (!pkg) continue;
      const lock = await json(join(path, 'package-lock.json')).catch(error => {
        if (error.code === 'ENOENT') return { packages: {} };
        throw error;
      });
      const buildDependencies = Object.fromEntries(Object.entries(pkg.devDependencies ?? {}).filter(([dependency]) => ['lodash.throttle', '@vscode/hubrpc'].includes(dependency)));
      for (const [dependency, range] of Object.entries({ ...pkg.dependencies, ...buildDependencies })) {
        const version = lock.packages[`node_modules/${dependency}`]?.version ?? range;
        if (version.startsWith('file:')) throw new Error(`尚未处理本地依赖：${name}/${dependency}`);
        const alias = `oss-${dependency.replaceAll(/[@/]/g, '-')}-${version.replaceAll(/[^a-zA-Z0-9.-]/g, '-')}`;
        entries.push({ name, scope, dependency, alias, version });
      }
    }
  }
  return entries;
}

async function sync(root) {
  const pkg = await json(join(root, 'package.json'));
  const web = await json(join(root, 'upstream/remote/web/package.json'));
  const lock = await json(join(root, 'upstream/remote/web/package-lock.json'));
  const deps = Object.fromEntries(Object.entries(web.dependencies).map(([name, range]) => [name, lock.packages[`node_modules/${name}`]?.version ?? range]));
  // Workbench 中按需动态加载的依赖，以及官方扩展脚本直接使用的构建依赖。
  for (const name of ['@hpke/core', '@vscode/diff', '@xterm/headless', 'libsodium-wrappers', 'path-browserify', 'buffer', '@vscode/hubrpc', '@microsoft/applicationinsights-common']) deps[name] = pkg.dependencies[name] ?? (name === '@vscode/hubrpc' ? '0.0.2-20' : undefined);
  for (const entry of await extensionDependencies(root)) deps[entry.alias] = `npm:${entry.dependency}@${entry.version}`;
  pkg.dependencies = deps;
  await writeFile(join(root, 'package.json'), JSON.stringify(pkg, null, 2) + '\n');
}
if (import.meta.url === pathToFileURL(resolve(process.argv[1])).href) await sync(resolve(import.meta.dirname, '..'));
