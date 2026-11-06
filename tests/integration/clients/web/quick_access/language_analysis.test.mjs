import { afterAll, expect, test } from 'bun:test';
import { writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/language_analysis.test.mjs').prepare();
const root = resolve('src/clients/workbench'), bundle = join(run.artifactsDir, 'language-analysis.fixture.mjs');
const upstream = './src/clients/workbench/upstream/src/vs/';
await build({ stdin: { contents: `
export { OutlineModelService } from '${upstream}editor/contrib/documentSymbols/browser/outlineModel.js';
export { LanguageFeatureRegistry } from '${upstream}editor/common/languageFeatureRegistry.js';
export { Emitter } from '${upstream}base/common/event.js';
export { CancellationToken, CancellationTokenSource } from '${upstream}base/common/cancellation.js';
export { URI } from '${upstream}base/common/uri.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const m = await import(pathToFileURL(bundle).href);
const receipt = { boundary: '真实OutlineModelService、LRU、语言Provider registry和取消令牌；仅文本模型与异步符号提供者为受控依赖，不替代真实Web Worker验收', checks: [] };

const symbols = name => [{ name, detail: '', kind: 6, tags: [],
  range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 5 },
  selectionRange: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 5 } }];
const names = outline => [...outline.children.values()].map(child => child.symbol.name);

async function fixture(callback) {
  const registry = new m.LanguageFeatureRegistry(), removed = new m.Emitter(), calls = [], pending = [];
  const service = new m.OutlineModelService({ documentSymbolProvider: registry },
    { for: () => ({ get: () => 0, update: () => 0 }) }, { onModelRemoved: removed.event });
  const registration = registry.register({ language: 'json', scheme: 'boxteam-file' }, {
    displayName: '受控异步符号源', provideDocumentSymbols(model, token) {
      const gate = Promise.withResolvers();
      calls.push({ gate, token, modelId: model.id, version: model.getVersionId(), uri: model.uri.toString() });
      // 故意模拟不能及时响应取消的外部语言提供者，以核原owner拒绝旧代际的写入／删除。
      return gate.promise;
    },
  });
  const model = (id, gateway) => ({ id, version: 1, language: 'json',
    uri: m.URI.parse(`boxteam-file://${gateway}/workspace/root/same.json`),
    getVersionId() { return this.version; }, getLanguageId() { return this.language; }, isTooLargeForSyncing: () => false });
  const get = (model, token = m.CancellationToken.None) => {
    const promise = service.getOrCreate(model, token); pending.push(promise); return promise;
  };
  try { await callback({ calls, removed, service, get, model }); }
  finally {
    registration.dispose();
    for (const call of calls) call.gate.resolve([]);
    await Promise.allSettled(pending);
    service.dispose(); removed.dispose();
  }
}

test('旧版本请求最后取消时，不能删除已完成的新版本符号缓存或重新分析', async () => {
  await fixture(async f => {
    const model = f.model('model-A', 'gateway-A'), cancelled = new m.CancellationTokenSource();
    try {
      const old = f.get(model, cancelled.token);
      model.version++;
      const latest = f.get(model);
      expect(f.calls).toHaveLength(2);
      f.calls[1].gate.resolve(symbols('latest'));
      const current = await latest;
      cancelled.cancel();
      expect(f.calls[0].token.isCancellationRequested).toBe(true);
      const cached = f.get(model);
      expect(f.calls).toHaveLength(2);
      expect(await cached).toBe(current);
      f.calls[0].gate.resolve(symbols('old'));
      await old;
      expect(await f.get(model)).toBe(current);
      expect(names(current)).toEqual(['latest']);
      receipt.checks.push('旧请求取消和迟到完成均不能删除或替换新版本缓存；新查询零重复Provider调用');
    } finally { cancelled.dispose(); }
  });
});

test('同一版本并发复用在途分析，单个消费者取消不影响另一个', async () => {
  await fixture(async f => {
    const model = f.model('model-A', 'gateway-A'), cancelled = new m.CancellationTokenSource();
    try {
      const first = f.get(model, cancelled.token), second = f.get(model);
      expect(f.calls).toHaveLength(1);
      cancelled.cancel();
      expect(f.calls[0].token.isCancellationRequested).toBe(false);
      f.calls[0].gate.resolve(symbols('shared'));
      const [a, b] = await Promise.all([first, second]);
      expect(a).toBe(b); expect(names(b)).toEqual(['shared']);
      expect(await f.get(model)).toBe(b);
      expect(f.calls).toHaveLength(1);
      receipt.checks.push('同版本在途分析按原引用共享；一个消费者取消不终止其它消费者');
    } finally { cancelled.dispose(); }
  });
});

test('模型移除后旧结果迟到，同路径另一owner与重建模型保持独立', async () => {
  await fixture(async f => {
    const a = f.model('model-A', 'gateway-A'), b = f.model('model-B', 'gateway-B');
    const old = f.get(a), other = f.get(b);
    f.removed.fire(a);
    const replacement = f.model('model-A-recreated', 'gateway-A'), fresh = f.get(replacement);
    expect(f.calls).toHaveLength(3);
    f.calls[1].gate.resolve(symbols('owner-B'));
    f.calls[2].gate.resolve(symbols('owner-A-new'));
    const [otherModel, freshModel] = await Promise.all([other, fresh]);
    f.calls[0].gate.resolve(symbols('owner-A-old')); await old;
    expect(await f.get(b)).toBe(otherModel);
    expect(await f.get(replacement)).toBe(freshModel);
    expect(names(otherModel)).toEqual(['owner-B']); expect(names(freshModel)).toEqual(['owner-A-new']);
    expect([...f.service.getCachedModels()]).toEqual(expect.arrayContaining([otherModel, freshModel]));
    expect([...f.service.getCachedModels()]).toHaveLength(2);
    expect(f.calls).toHaveLength(3);
    receipt.checks.push('移除原模型释放缓存；同路径owner和新模型不被旧结果复活或串用');
  });
});

afterAll(async () => {
  await writeFile(join(run.artifactsDir, 'language-analysis-checks.json'), JSON.stringify(receipt, null, 2) + '\n');
  await run.close();
});
