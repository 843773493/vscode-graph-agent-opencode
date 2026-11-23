import { expect, test } from 'bun:test';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { buildWorkbenchDependencies } from '../../../../src/clients/workbench/scripts/devDependencies.mjs';
import { TestRunContext } from '../../../harness/js/run-context.mjs';

test('开发预构建发现新增上游入口，普通修改不重建，重建后保留共享模块身份', async () => {
  const run = await TestRunContext.fromTestFile('tests/integration/clients/web/workbench_development_dependencies.test.mjs').prepare();
  const root = join(run.workspaceRoot, 'workbench');
  const upstream = join(root, 'upstream/src/vs');
  await Promise.all([mkdir(join(root, 'src'), { recursive: true }), mkdir(join(upstream, 'base/common'), { recursive: true }),
    mkdir(join(upstream, 'workbench'), { recursive: true })]);
  await Promise.all([
    writeFile(join(root, 'tsconfig.jsonc'), '{"compilerOptions":{"target":"ES2024","module":"ESNext"}}'),
    writeFile(join(upstream, 'base/common/shared.ts'), 'export const shared = {};'),
    writeFile(join(upstream, 'base/common/first.ts'), "export { shared } from './shared.js';"),
    writeFile(join(upstream, 'base/common/second.ts'), "export { shared } from './shared.js';"),
    writeFile(join(upstream, 'base/common/javascript.js'), "export { shared } from './shared.js';"),
    writeFile(join(upstream, 'base/common/types.d.ts'), 'export interface OnlyType {}'),
    writeFile(join(upstream, 'base/common/style.css'), '.fixture { color: red; }'),
    writeFile(join(upstream, 'workbench/workbench.web.main.internal.ts'), "export { shared } from '../base/common/shared.js';"),
  ]);
  const file = join(root, 'src/entry.ts');
  const original = "import { shared } from '../upstream/src/vs/base/common/first.js';\nexport { shared };";
  await writeFile(file, original);
  const output = join(run.artifactsDir, 'dist/out');
  const build = () => buildWorkbenchDependencies(root, async (_path, source) => source, output, {});
  const dependencies = await build();
  expect(dependencies.needsRebuild(original + '\n// 普通源码修改', file)).toBe(false);
  expect(dependencies.needsRebuild("import '../upstream/src/vs/base/common/style.css?text';", file)).toBe(false);
  expect(dependencies.needsRebuild("import type { OnlyType } from '../upstream/src/vs/base/common/types.d.ts';", file)).toBe(false);
  const updated = original.replace('first.js', 'second.js');
  expect(dependencies.needsRebuild(updated, file)).toBe(true);
  expect(dependencies.transform(updated, file)).toContain('/out/dev-upstream/vs/base/common/second.js');
  await writeFile(file, updated);
  const rebuilt = await build();
  expect(rebuilt.needsRebuild(updated, file)).toBe(false);
  const main = await import(pathToFileURL(join(output, 'dev-upstream/vs/workbench/workbench.web.main.internal.js')).href);
  const second = await import(pathToFileURL(join(output, 'dev-upstream/vs/base/common/second.js')).href);
  expect(main.shared).toBe(second.shared);
  const javascriptImport = original.replace('first.js', 'javascript.js');
  expect(rebuilt.needsRebuild(javascriptImport, file)).toBe(true);
  await writeFile(file, javascriptImport);
  const javascriptDependencies = await build();
  expect(javascriptDependencies.needsRebuild(javascriptImport, file)).toBe(false);
  expect(javascriptDependencies.transform(javascriptImport, file)).toContain('/out/dev-upstream/vs/base/common/javascript.js');
  const javascript = await import(pathToFileURL(join(output, 'dev-upstream/vs/base/common/javascript.js')).href);
  expect(javascript.shared).toBe(main.shared);
});
