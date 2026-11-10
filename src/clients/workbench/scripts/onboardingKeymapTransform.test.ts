import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import ts from 'typescript';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';
import { onboardingKeymapSourcePaths } from './onboardingKeymapTransform.mjs';

const root = resolve(process.cwd(), 'src/clients/workbench');

test('新手引导保留主题选择并移除外部键位包 Gallery 安装', async () => {
  expect(onboardingKeymapSourcePaths).toEqual([
    'src/vs/workbench/contrib/welcomeOnboarding/browser/onboardingVariationA.ts',
  ]);

  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, []);
  const path = join(root, 'upstream', onboardingKeymapSourcePaths[0]);
  const parsed = ts.createSourceFile(path, await transform(path, await readFile(path, 'utf8')), ts.ScriptTarget.Latest, true);
  const converted = parsed.getText();
  expect(parsed.parseDiagnostics).toEqual([]);
  expect(converted).not.toContain('IExtensionGalleryService');
  expect(converted).not.toContain('IExtensionManagementService');
  expect(converted).not.toContain('installFromGallery');
  expect(converted).not.toContain('getExtensions([{ id: keymap.extensionId }');
  expect(converted).not.toContain('_detectInstalledEditors');
  expect(converted).not.toContain('_applyStepSelections');
  expect(converted).not.toContain('selectedKeymapId');
  expect(converted).not.toContain('_detectedEditorIds');
  expect(converted).not.toContain('onboardingKeymaps');
  expect(converted).toContain('product.onboardingThemes');
  expect(converted).toContain('Choose a color theme');
  expect(converted).not.toContain('Extensions view.');
});

test('注入到 Workbench 的产品配置不包含第三方键位扩展标识', async () => {
  const transform = sourceTransform(root, {
    commit: pinnedUpstreamCommit,
    onboardingKeymaps: [{ id: 'sublime', extensionId: 'example.sublime-keybindings' }],
  }, []);
  const path = join(root, 'upstream/src/vs/platform/product/common/product.ts');
  const converted = await transform(path, await readFile(path, 'utf8'));
  expect(converted).not.toContain('onboardingKeymaps');
  expect(converted).not.toContain('example.sublime-keybindings');
});
