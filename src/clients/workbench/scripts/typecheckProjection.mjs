import { mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import { dirname, join, relative, sep } from 'node:path';
import { sourceTransform } from './sourceTransform.mjs';

/** 类型检查读取正式构建的转换结果，未变源码只建立链接。 */
export async function projectTypecheckSources({ repository, root, output, paths, commit }) {
  const overlay = join(output, 'repository');
  await rm(overlay, { recursive: true, force: true });
  const projectedRoot = join(overlay, relative(repository, root));
  const targets = paths.map(path => relative(repository, join(root, 'upstream', path)));
  async function project(directory = '') {
    await mkdir(join(overlay, directory), { recursive: true });
    for (const entry of await readdir(join(repository, directory), { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (path === relative(repository, join(root, 'dist')) || path === '.git' || path === 'out' || targets.includes(path)) continue;
      if (targets.some(target => target.startsWith(path + sep))) await project(path);
      else await symlink(join(repository, path), join(overlay, path), entry.isDirectory() ? 'junction' : 'file');
    }
  }
  await project();
  const product = JSON.parse(await readFile(join(root, 'upstream/product.json'), 'utf8'));
  const manifest = JSON.parse(await readFile(join(root, 'upstream/package.json'), 'utf8'));
  const transform = sourceTransform(projectedRoot, { ...product, version: manifest.version, commit, quality: 'stable' }, []);
  for (const path of paths) {
    const destination = join(projectedRoot, 'upstream', path);
    await mkdir(dirname(destination), { recursive: true });
    await writeFile(destination, await transform(destination, await readFile(join(root, 'upstream', path), 'utf8')));
  }
  return { projectedRoot, targets };
}
