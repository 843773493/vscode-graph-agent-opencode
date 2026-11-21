import { resolve, join } from 'node:path';
import { parseArgs } from 'node:util';
import { spawnSync } from 'node:child_process';
import { packagingSource } from './source-policy.mjs';
const projectRoot = resolve(process.env.BOXTEAM_PROJECT_ROOT || process.cwd());
const args = packagingSource(projectRoot);
if (process.platform === 'win32') {
  const { values } = parseArgs({ args, options: { 'output-root': { type: 'string' }, 'project-root': { type: 'string' } } });
  const output = resolve(values['output-root'] ?? join(projectRoot, 'out/packaging/electron'));
  const runtime = spawnSync('bun', [join(projectRoot, 'packaging/runtime/build-windows-x64.mjs'), '--output-root', join(output, 'runtime')], { cwd: projectRoot, stdio: 'inherit' });
  if (runtime.error) throw runtime.error;
  if (runtime.status !== 0) throw new Error(`Windows 运行时构建失败：exit=${runtime.status}`);
}
for (const file of ['build.mjs', 'package.mjs']) {
  const result = spawnSync('bun', [join(projectRoot, 'packaging/electron', file), ...args], { cwd: projectRoot, stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Electron ${file} 失败：exit=${result.status}`);
}
