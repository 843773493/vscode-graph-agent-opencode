import { expect, test } from 'bun:test';
import { randomUUID } from 'node:crypto';
import { access, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { buildExtensions } from './extensions.mjs';

const fixtures = resolve('out/tests/src/clients/workbench/scripts/extensions/workspace', randomUUID());

for (const ancestor of ['plain', 'tests', 'test', 'node_modules']) {
  test(`扩展构建仅过滤复制源内部目录，允许祖先目录 ${ancestor}`, async () => {
    const root = join(fixtures, ancestor, 'source');
    const files = {
      'upstream/extensions/esbuild-extension-common.mts': 'options.alias = { };',
      'upstream/extensions/sample/package.json': JSON.stringify({ name: 'sample', browser: './browser.js' }),
      'upstream/extensions/sample/browser.js': 'export const message = "实际扩展内容";',
      'upstream/extensions/sample/test/private.js': '不得复制',
      'upstream/extensions/sample/tests/private.js': '不得复制',
      'upstream/extensions/sample/node_modules/private/index.js': '不得发布依赖目录',
      'upstream/build/lib/buildEvents.ts': 'export {};',
      'upstream/src/tsconfig.json': '{}',
      'upstream/src/tsconfig.base.json': '{}',
      'upstream/remote/web/package.json': '{"dependencies":{}}',
    };
    for (const [name, body] of Object.entries(files)) {
      const target = join(root, name);
      await mkdir(resolve(target, '..'), { recursive: true });
      await writeFile(target, body);
    }
    await mkdir(join(root, 'node_modules'), { recursive: true });
    const dist = join(root, 'dist');
    await mkdir(dist);
    const extensions = await buildExtensions(root, dist);
    expect(extensions).toEqual([{ extensionPath: 'sample', packageJSON: { name: 'sample', browser: './browser.js' },
      packageNLS: undefined }]);
    expect(await readFile(join(dist, 'extensions/sample/browser.js'), 'utf8'))
      .toBe(files['upstream/extensions/sample/browser.js']);
    for (const name of ['test', 'tests', 'node_modules']) {
      await expect(access(join(dist, 'extensions/sample', name))).rejects.toMatchObject({ code: 'ENOENT' });
    }
    await expect(access(join(dist, '.build'))).rejects.toMatchObject({ code: 'ENOENT' });
    expect(await readFile(join(root, 'upstream/extensions/esbuild-extension-common.mts'), 'utf8'))
      .toBe(files['upstream/extensions/esbuild-extension-common.mts']);
  });
}
