import { cp, mkdir, readdir, readFile, writeFile, symlink, rm, lstat } from 'node:fs/promises';
import { join, relative, resolve, sep } from 'node:path';
import { spawn } from 'node:child_process';
import { extensionDependencies } from './dependencies.mjs';

// 在构建目录运行官方扩展脚本，绝不向固定上游子模块写入生成文件。
export async function buildExtensions(root, dist, transformSource) {
  const stage = join(dist, '.build/upstream');
  const source = join(root, 'upstream');
  const sourceExtensions = join(source, 'extensions');
  await cp(sourceExtensions, join(stage, 'extensions'), {
    recursive: true,
    filter: path => !relative(sourceExtensions, path).split(sep)
      .some(part => ['node_modules', 'test', 'tests'].includes(part)),
  });
  const commonPath = join(stage, 'extensions/esbuild-extension-common.mts');
  const commonSource = await readFile(commonPath, 'utf8');
  if (!commonSource.includes("options.alias = {")) throw new Error('上游扩展别名入口已变化');
  await writeFile(commonPath, commonSource.replace('options.alias = {', `options.alias = { util: ${JSON.stringify(join(root, 'scripts/browserEncoding.mjs'))},`));
  await mkdir(join(stage, 'build/lib'), { recursive: true });
  await cp(join(source, 'build/lib/buildEvents.ts'), join(stage, 'build/lib/buildEvents.ts'));
  await mkdir(join(stage, 'src'), { recursive: true });
  for (const name of ['tsconfig.json', 'tsconfig.base.json']) await cp(join(source, 'src', name), join(stage, 'src', name));
  await symlink(join(root, 'node_modules'), join(stage, 'node_modules'), 'dir');
  await symlink(join(root, 'node_modules'), join(stage, 'extensions/node_modules'), 'dir');
  const extensions = [];
  const dependencies = await extensionDependencies(root);
  const webDependencies = Object.keys(JSON.parse(await readFile(join(root, 'upstream/remote/web/package.json'), 'utf8')).dependencies);
  for (const name of await readdir(join(stage, 'extensions'))) {
    if (name.includes('test') || name === 'node_modules') continue;
    const folder = join(stage, 'extensions', name);
    const items = await readdir(folder, { withFileTypes: true }).catch(error => {
      if (error.code === 'ENOTDIR') return [];
      throw error;
    });
    if (!items.some(item => item.name === 'package.json')) continue;
    if (name === 'search-result') {
      const path = join(folder, 'src/extension.ts');
      await writeFile(path, await transformSource(join(source, 'extensions/search-result/src/extension.ts'), await readFile(path, 'utf8')));
    }
    let manifest = JSON.parse(await readFile(join(folder, 'package.json'), 'utf8'));
    if (manifest.main && !manifest.browser) continue;
    for (const entry of dependencies.filter(entry => entry.name === name)) {
      const target = join(folder, entry.scope, 'node_modules', entry.dependency);
      await mkdir(resolve(target, '..'), { recursive: true });
      await symlink(join(root, 'node_modules', entry.alias), target, 'dir');
    }
    for (const dependency of webDependencies) {
      const target = join(folder, 'node_modules', dependency);
      const exists = await lstat(target).then(() => true, error => {
        if (error.code === 'ENOENT') return false;
        throw error;
      });
      if (!exists) {
        await mkdir(resolve(target, '..'), { recursive: true });
        await symlink(join(root, 'node_modules', dependency), target, 'dir');
      }
    }
    const scripts = items.filter(item => /^esbuild\.(browser|webview|notebook|markdownEditor)\.mts$/.test(item.name));
    for (const script of scripts) {
      console.log(`[extension] ${name}/${script.name}`);
      await new Promise((fulfill, reject) => {
        const process = spawn('bun', [join(folder, script.name)], { cwd: root, stdio: 'inherit' });
        process.on('error', reject);
        process.on('exit', code => code === 0 ? fulfill() : reject(new Error(`扩展编译失败：${name}/${script.name} (${code})`)));
      });
    }
    manifest = JSON.parse(await readFile(join(folder, 'package.json'), 'utf8'));
    if (manifest.browser) {
      const entry = resolve(folder, manifest.browser);
      // Bun.file 不用于这里，构建工具保持标准 ESM 文件 API。
      const outputNames = await readdir(join(entry, '..'));
      if (!outputNames.includes(entry.split('/').at(-1)) && !outputNames.includes(entry.split('/').at(-1) + '.js')) {
        throw new Error(`扩展入口未产出：${name}/${manifest.browser}`);
      }
    }
    const packageNLS = items.some(item => item.name === 'package.nls.json')
      ? JSON.parse(await readFile(join(folder, 'package.nls.json'), 'utf8')) : undefined;
    extensions.push({ extensionPath: name, packageJSON: manifest, packageNLS });
    await cp(folder, join(dist, 'extensions', name), {
      recursive: true,
      filter: path => !relative(folder, path).split(sep).includes('node_modules'),
    });
  }
  await rm(join(dist, '.build'), { recursive: true });
  return extensions;
}
