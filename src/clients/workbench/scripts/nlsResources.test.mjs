import { expect, test } from 'bun:test';
import { loadWorkbenchNlsMessages } from '../src/nlsResources.ts';

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
