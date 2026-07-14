import { defineConfig } from '@vscode/test-cli';
import { mkdirSync } from 'node:fs';
import path from 'node:path';

const vscodeE2eWorkspace = path.resolve(
  process.cwd(),
  'out',
  'tests',
  'e2e',
  'vscode',
  'workspace',
);
mkdirSync(vscodeE2eWorkspace, { recursive: true });

export default defineConfig({
  files: './tests/e2e/vscode/**/*.test.js',
  version: 'stable',
  launchArgs: [
    '--disable-extensions',
    '--new-window',
    '--profile-temp',
    vscodeE2eWorkspace,
  ],
  mocha: {
    ui: 'tdd',
    color: true,
    timeout: 60000,
  },
});
