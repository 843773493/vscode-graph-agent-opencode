import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';
import { workbenchDependencies } from './devDependencies.mjs';

test('上游 SettingsEditor 的 shared i18n 沿应用相同源码身份外置', async () => {
  const root = resolve('src/clients/workbench');
  const upstreamInput = join(root, 'upstream/src/vs/workbench/services/preferences/common/preferencesEditorInput.ts');
  const sharedModule = resolve(root, '../shared/core/i18n/index.ts');
  const applicationBootstrap = join(root, 'src/bootstrap.ts');
  expect(resolve(dirname(applicationBootstrap), '../../shared/core/i18n/index.ts')).toBe(sharedModule);
  const transformed = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
  const source = await transformed(upstreamInput, await readFile(upstreamInput, 'utf8'));
  const specifier = JSON.stringify(sharedModule.replace(/\.ts$/, '.js'));
  expect(source).toContain(`from ${specifier}`);

  const dependencies = await workbenchDependencies(root, transformed);
  const resolution = await dependencies.plugin.resolveId.call({
    async resolve(id, importer) {
      expect(id).toBe(JSON.parse(specifier));
      expect(importer).toBe(upstreamInput);
      return { id: sharedModule };
    },
  }, JSON.parse(specifier), upstreamInput);

  expect(resolution).toEqual({
    id: '/@fs/' + sharedModule.replaceAll('\\', '/').replace(/^\//, ''),
    external: true,
  });
});
