import { expect, test } from 'bun:test';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from '../../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { buildSourcePlugin, pinnedUpstreamCommit, sourceTransform } from '../../../../../src/clients/workbench/scripts/sourceTransform.mjs';
import { TestRunContext } from '../../../../harness/js/run-context.mjs';

const run = await TestRunContext.fromTestFile('tests/integration/clients/web/quick_access/file_input.test.mjs').prepare();
const root = resolve('src/clients/workbench'), upstream = './src/clients/workbench/upstream/src/vs/';
const bundle = join(run.artifactsDir, 'file-input.fixture.mjs');
await build({ stdin: { contents: `
export { FileEditorInput } from '${upstream}workbench/contrib/files/browser/editors/fileEditorInput.js';
export { URI } from '${upstream}base/common/uri.js';
export { Event } from '${upstream}base/common/event.js';
`, resolveDir: resolve('.') }, outfile: bundle, bundle: true, format: 'esm', platform: 'browser', target: 'es2024',
  plugins: [buildSourcePlugin(root, sourceTransform(root, { commit: pinnedUpstreamCommit }, []))],
  tsconfig: join(root, 'tsconfig.jsonc'), logLevel: 'silent' });
const { FileEditorInput, URI, Event } = await import(pathToFileURL(bundle).href);

/** 真实 FileEditorInput；只替换服务边界并对每个取得的模型引用计数。 */
function fixture() {
  const release = Promise.withResolvers(), entered = Promise.withResolvers();
  const references = [], body = {}, state = { fail: false };
  const input = new FileEditorInput(URI.parse('boxteam-file://gateway/workspace/file.txt'),
    undefined, undefined, undefined, undefined, undefined, undefined,
    {}, { files: { get: () => undefined, onDidCreate: Event.None, resolve: async () => body } },
    { async createModelReference() {
      const reference = { object: body, disposals: 0, dispose() { this.disposals++; } };
      references.push(reference); entered.resolve(); await release.promise;
      if (state.fail) throw new Error('明确的引用获取失败');
      return reference;
    } },
    { onDidChangeFormatters: Event.None },
    { onDidChangeFileSystemProviderRegistrations: Event.None, onDidChangeFileSystemProviderCapabilities: Event.None },
    { onDidChangeReadonly: Event.None }, {}, {},
    { inspect: () => ({ value: undefined }) }, { onDidChange: Event.None });
  return { input, references, release, entered, body, state };
}

test('同一 input 并发 resolve 只持有一个引用，最后关闭全部释放', async () => {
  const f = fixture();
  try {
    const pending = [f.input.resolve(), f.input.resolve()];
    await f.entered.promise;
    f.release.resolve();
    expect(await Promise.all(pending)).toEqual([f.body, f.body]);
    expect(await f.input.resolve()).toBe(f.body);
    f.input.dispose();
    expect(f.references.map(reference => reference.disposals)).toEqual([1]);
  } finally { f.release.resolve(); f.input.dispose(); }
});

test('等待并发 resolve 时关闭，迟到引用仍只释放一次', async () => {
  const f = fixture();
  try {
    const pending = [f.input.resolve(), f.input.resolve()];
    await f.entered.promise; f.input.dispose(); f.release.resolve();
    expect(await Promise.all(pending)).toEqual([f.body, f.body]);
    expect(f.references.map(reference => reference.disposals)).toEqual([1]);
  } finally { f.release.resolve(); f.input.dispose(); }
});

test('引用获取失败明确传播，用户重试可重新获取并释放', async () => {
  const f = fixture();
  try {
    f.state.fail = true;
    const pending = f.input.resolve(); await f.entered.promise; f.release.resolve();
    await expect(pending).rejects.toThrow('明确的引用获取失败');
    f.state.fail = false;
    expect(await f.input.resolve()).toBe(f.body);
    f.input.dispose();
    expect(f.references.map(reference => reference.disposals)).toEqual([0, 1]);
  } finally { f.release.resolve(); f.input.dispose(); }
});
