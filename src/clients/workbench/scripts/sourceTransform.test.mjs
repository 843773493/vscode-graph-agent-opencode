import { expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

test('同一 sourceTransform 闭包只序列化一次内置扩展清单', async () => {
  let serializations = 0;
  const extensions = {
    toJSON() {
      serializations++;
      return [{ name: 'sample' }];
    },
  };
  const root = resolve('src/clients/workbench');
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, extensions);
  const path = join(root, 'upstream/src/vs/workbench/not-transformed.ts');
  const source = 'const extensions = [/*BUILD->INSERT_BUILTIN_EXTENSIONS*/];';

  expect(serializations).toBe(1);
  expect(await transform(path, source)).toContain('[{"name":"sample"}]');
  expect(await transform(path, source)).toContain('[{"name":"sample"}]');
  expect(serializations).toBe(1);
});
