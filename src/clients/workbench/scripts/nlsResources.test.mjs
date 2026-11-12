import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { loadWorkbenchNlsMessages } from '../src/nlsResources.ts';
import { sourceTransform, pinnedUpstreamCommit } from './sourceTransform.mjs';

test('NLS 资源必须与同批次 manifest 匹配并提供命名字段', async () => {
  const buildId = 'a'.repeat(64);
  const responses = [
    Response.json({ buildId, assets: { en: '/out/nls-en-hash.json', 'zh-cn': '/out/nls-zh-hash.json' } }),
    Response.json({ buildId, contents: { 'vs/workbench': { title: '标题' } }, extensions: {} }),
  ];
  const paths = [];
  const messages = await loadWorkbenchNlsMessages('zh-cn', (async input => {
    paths.push(String(input));
    return responses.shift();
  }), `/out/nls-assets-${buildId}.json`);
  expect(paths).toEqual([`/out/nls-assets-${buildId}.json`, '/out/nls-zh-hash.json']);
  expect(messages).toEqual({ 'vs/workbench': { title: '标题' }, extensions: {} });
});

test('NLS 资源批次错误或缺少 contents 时拒绝启动', async () => {
  const buildId = 'a'.repeat(64);
  const fetcher = resource => {
    const responses = [
      Response.json({ buildId, assets: { en: '/out/nls-en-hash.json', 'zh-cn': '/out/nls-zh-hash.json' } }),
      Response.json(resource),
    ];
    return async () => responses.shift();
  };
  await expect(loadWorkbenchNlsMessages('zh-cn', fetcher({ buildId: 'b'.repeat(64), contents: {}, extensions: {} }), `/out/nls-assets-${buildId}.json`))
    .rejects.toThrow('build mismatch');
  await expect(loadWorkbenchNlsMessages('zh-cn', fetcher({ buildId, assets: {} }), `/out/nls-assets-${'b'.repeat(64)}.json`))
    .rejects.toThrow('identity mismatch');
  await expect(loadWorkbenchNlsMessages('zh-cn', fetcher({ buildId, extensions: {} }), `/out/nls-assets-${buildId}.json`))
    .rejects.toThrow('invalid contents');
});

test('核心上游补译对应真实调用，并沿原 NLS formatter 保留参数及英文 original', async () => {
  const root = resolve('src/clients/workbench');
  const { contents } = JSON.parse(await readFile(join(root, 'resources/localization/zh-cn-project.json'), 'utf8'));
  const collector = new Map();
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], collector);
  const placeholders = message => [...message.matchAll(/\{(\d+)\}/g)].map(match => match[1]).sort();
  for (const [module, messages] of Object.entries(contents)) {
    const path = join(root, 'upstream/src', `${module}.ts`);
    await transform(path, await readFile(path, 'utf8'));
    for (const [key, translation] of Object.entries(messages)) {
      const original = collector.get(`${module}#${key}`);
      expect(original).toBeString();
      expect(placeholders(translation)).toEqual(placeholders(original));
    }
  }
  const nlsPath = join(root, 'upstream/src/vs/nls.ts');
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(await transform(nlsPath, await readFile(nlsPath, 'utf8')))
    .replace(/^export /gm, '');
  const { localize, localize2 } = new Function('globalThis', `${javascript}\nreturn { localize, localize2 };`)({ _VSCODE_NLS_MESSAGES: contents });
  const media = 'vs/workbench/contrib/mediaPreview/browser/';
  expect(localize(`${media}imagePreviewEditor#imagePreview.readFailed`, 'Unable to read image: {0}', '/home/alice/图片.png'))
    .toBe('无法读取图片：/home/alice/图片.png');
  expect(localize2(`${media}mediaPreviewActions#imagePreview.zoomIn`, 'Zoom In'))
    .toEqual({ value: '放大', original: 'Zoom In' });
  expect(localize('vs/workbench/contrib/search/browser/anythingQuickAccess#openLinkToSide', 'Open to the Side ({0})', 'Ctrl+Enter'))
    .toBe('在侧边打开 (Ctrl+Enter)');
});

test('项目主动改变的上游提示使用独立产品 key 与双语 original', async () => {
  const root = resolve('src/clients/workbench');
  const module = 'vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagement.contribution';
  const titleKey = 'workbench.chatCustomization.pluginItemDeleteBlocked';
  const detailKey = 'workbench.chatCustomization.pluginItemDeleteBlockedDetail';
  const path = join(root, 'upstream/src', `${module}.ts`);
  const collector = new Map();
  const transform = sourceTransform(root, { commit: pinnedUpstreamCommit }, [], collector);
  const output = await transform(path, await readFile(path, 'utf8'));
  const english = JSON.parse(await readFile(join(root, '../shared/core/i18n/messages/en.json'), 'utf8'));
  const chinese = JSON.parse(await readFile(join(root, '../shared/core/i18n/messages/zh-cn.json'), 'utf8'));
  expect(output).toContain(JSON.stringify(`${module}#${titleKey}`));
  expect(output).toContain(JSON.stringify(`${module}#${detailKey}`));
  expect(collector.get(`${module}#${titleKey}`)).toBe(english[titleKey]);
  expect(collector.get(`${module}#${detailKey}`)).toBe(english[detailKey]);
  expect(collector.has(`${module}#cannotDeletePluginItem`)).toBe(false);
  expect(collector.has(`${module}#cannotDeletePluginItemDetail`)).toBe(false);

  const nlsPath = join(root, 'upstream/src/vs/nls.ts');
  const javascript = new Bun.Transpiler({ loader: 'ts' }).transformSync(await transform(nlsPath, await readFile(nlsPath, 'utf8')))
    .replace(/^export /gm, '');
  const { localize } = new Function('globalThis', `${javascript}\nreturn { localize };`)({
    _VSCODE_NLS_MESSAGES: { [module]: { [titleKey]: chinese[titleKey], [detailKey]: chinese[detailKey] } },
  });
  expect(localize(`${module}#${titleKey}`, english[titleKey])).toBe(chinese[titleKey]);
  expect(localize(`${module}#${detailKey}`, english[detailKey])).toBe(chinese[detailKey]);
});
