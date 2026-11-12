import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform, transformedSourcePaths } from './sourceTransform.mjs';
import { pruneUnusedImportsAndConstants } from './extensions/extensionManagementTransform.mjs';
import { parseUniqueJson } from './actionTitles.mjs';

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

test('转换清理用一次拼接保留未用 import 与常量的既有删除语义', () => {
  const source = `// 保留 import 前注释
import { used, unused } from 'first';
import { removed } from 'second';
const unusedValue = 1;
const retained = used;
export { retained };
`;
  const output = pruneUnusedImportsAndConstants('fixture.ts', source);
  expect(output).toContain('// 保留 import 前注释');
  expect(output).toContain("import { used } from 'first';");
  expect(output).toContain('const retained = used;');
  expect(output).not.toContain('unused');
  expect(output).not.toContain('removed');
  expect(output).not.toContain('unusedValue');
});

test('catalog JSON 校验拒绝重复 key 与占位符不匹配仍走 build 合同', () => {
  expect(() => parseUniqueJson('fixture.json', '{"same":1,"same":2}')).toThrow('Duplicate JSON key same');
  expect(parseUniqueJson('fixture.json', '{"message":"{0}"}').message).toBe('{0}');
});

test('Web 语言列表固定为本地简中与 English，移除 Gallery 供应', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/platform/languagePacks/browser/languagePacks.ts');
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output).toContain("this.createQuickPickItem('zh-cn', Language.value() === 'en' ? 'Chinese (Simplified)' : '简体中文')");
  expect(output).toContain("this.createQuickPickItem('en', 'English')");
  expect(output).not.toContain('extensionGalleryService.query');
  expect(output).not.toContain('getExtensionGalleryResourceURL');
});

test('原显示语言动作只列出两种内置语言', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/workbench/contrib/localization/common/localizationsActions.ts');
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output).toContain("{ id: 'zh-cn', label: (Language.value() === 'en' ? 'Chinese (Simplified)' : '简体中文')");
  expect(output).toContain("{ id: 'en', label: 'English'");
  expect(output).toContain('vs/workbench/contrib/localization/common/localizationsActions#chooseLocale');
  expect(output).toContain('vs/workbench/contrib/localization/common/localizationsActions#currentDisplayLanguage');
  expect(output).toContain("'Select Display Language'");
  expect(output).toContain("'Current'");
  expect(output).toContain('localizationsActions#boxteamConfigureLocaleDescription');
  expect(output).toContain('Choose Simplified Chinese or English as the Workbench display language.');
  expect(output).toContain('notificationService.error(error)');
  expect(output).toContain('await localeService.setLocale(selectedLanguage)');
  expect(output).not.toContain('installed language packs');
  expect(output).not.toContain('extensionWorkbenchService');
  expect(output).not.toContain('getAvailableLanguages');
});

test('原本地化命令贡献在恢复 UI 前通过原 Registry 注册', async () => {
  const root = resolve('src/clients/workbench');
  const relativePath = 'src/vs/workbench/contrib/localization/browser/localization.contribution.ts';
  const path = join(root, 'upstream', relativePath);
  expect(transformedSourcePaths).toContain(relativePath);
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output).toContain('BaseLocalizationWorkbenchContribution');
  expect(output).toContain("registerWorkbenchContribution2('boxteam.localization.web', WebLocalizationWorkbenchContribution, WorkbenchPhase.BlockRestore);");
  expect(output).not.toContain('registerWorkbenchContribution(');
  expect(output).not.toContain('LifecyclePhase.Eventually');
});

test('清除语言偏好固定恢复中文且不查询浏览器语言', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/workbench/services/localization/browser/localeService.ts');
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output).toContain("Press the reload button to use Simplified Chinese.");
  expect(output).not.toContain('navigator.language');
  expect(output).not.toContain('galleryService.query');
});

test('语言重载先提交 Workbench 展示状态', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/workbench/services/host/browser/browserHostService.ts');
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output.indexOf('prepareToClose()')).toBeLessThan(output.indexOf('handleExpectedShutdown(ShutdownReason.RELOAD)'));
});

test('NLS source 转换按模块和 key 登记，同时保留 localize2 英文 original', async () => {
  const root = resolve('src/clients/workbench');
  const collector = new Map();
  const path = join(root, 'upstream/src/vs/example/sample.ts');
  const source = `import { localize, localize2 } from '../nls.js';
const first = localize('sample', 'English {0}', value);
const second = localize2({ key: 'sample.help', comment: [] }, 'Original English');`;
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [], collector)(path, source);
  expect(output).toContain('"vs/example/sample#sample"');
  expect(output).toContain('localize2("vs/example/sample#sample.help", \'Original English\')');
  expect(output).toContain("'Original English'");
  expect(collector.get('vs/example/sample#sample')).toBe('English {0}');
});

test('NLS 转换复用上游分析器识别别名和 namespace import', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/example/nlsBindings.ts');
  const source = `import { unrelated } from './unrelated.js';
import { localize as label } from '../nls.js';
import * as nls from '../nls.js';
label('alias', 'Alias message');
nls.localize('namespace', 'Namespace message');`;
  const collector = new Map();
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [], collector)(path, source);
  expect(output).toContain('vs/example/nlsBindings#alias');
  expect(output).toContain('vs/example/nlsBindings#namespace');
  expect(collector.get('vs/example/nlsBindings#alias')).toBe('Alias message');
  expect(collector.get('vs/example/nlsBindings#namespace')).toBe('Namespace message');
});

test('原 Quick Input 的晚置 NLS import 仍提取确定按钮', async () => {
  const root = resolve('src/clients/workbench');
  const collector = new Map();
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], collector);
  for (const module of ['quickInput', 'quickInputController']) {
    const path = join(root, `upstream/src/vs/platform/quickinput/browser/${module}.ts`);
    const output = await transform(path, await readFile(path, 'utf8'));
    const identity = `vs/platform/quickinput/browser/${module}#ok`;
    expect(output).toContain(JSON.stringify(identity));
    expect(collector.get(identity)).toBe('OK');
  }
});

test('NLS 同模块重复 key 的冲突明确拒绝', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/example/nlsCollision.ts');
  const source = `import { localize } from '../nls.js';
localize('same', 'First');
localize('same', 'Second');`;
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], new Map());
  await expect(transform(path, source)).rejects.toThrow('NLS key collision: vs/example/nlsCollision#same');
});

test('上游 NLS formatter 支持稳定模块 key 并保留英文 fallback', async () => {
  const root = resolve('src/clients/workbench');
  const path = join(root, 'upstream/src/vs/nls.ts');
  const output = await sourceTransform(root, { commit: pinnedUpstreamCommit }, [])(path, await readFile(path, 'utf8'));
  expect(output).toContain('lookupMessage(key, message)');
  expect(output).toContain('messageMap?.[index.slice(0, separator)]?.[index.slice(separator + 1)]');
  expect(output).toContain('const message = lookupMessage(key, originalMessage);');
});
