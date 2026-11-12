import { expect, test } from 'bun:test';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { build, analyzeMetafile } from '../../../../src/clients/workbench/node_modules/esbuild/lib/main.js';
import { writeBuildMetadata } from '../../../../src/clients/workbench/scripts/buildMetadata.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

test('流式 metadata 保留真实 esbuild 输入输出和分析能力，写入错误继续抛出', async () => {
  const run = await TestRunContext.fromTestFile('tests/integration/clients/web/workbench_build_metadata.test.mjs').prepare();
  const entry = join(run.artifactsDir, '入口.mjs');
  await writeFile(entry, 'export const message = "路径\\\"与 emoji 🧪";\n');
  const built = await build({ entryPoints: [entry], outdir: join(run.artifactsDir, 'dist'),
    bundle: true, format: 'esm', metafile: true, logLevel: 'silent' });
  const output = join(run.artifactsDir, 'build-meta.json');
  await writeBuildMetadata(output, built.metafile);
  const encoded = await readFile(output, 'utf8');
  expect(JSON.parse(encoded)).toEqual(built.metafile);
  expect(await analyzeMetafile(encoded)).toBe(await analyzeMetafile(built.metafile));
  await expect(writeBuildMetadata(join(run.artifactsDir, 'missing', 'build-meta.json'), built.metafile)).rejects.toMatchObject({ code: 'ENOENT' });
});
