import { resolve, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { packagingSource } from './source-policy.mjs';
const projectRoot = resolve(process.env.BOXTEAM_PROJECT_ROOT || process.cwd());
const args = packagingSource(projectRoot);
for (const file of ['build.mjs', 'package.mjs']) {
  const result = spawnSync('bun', [join(projectRoot, 'packaging/electron', file), ...args], { cwd: projectRoot, stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Electron ${file} 失败：exit=${result.status}`);
}
