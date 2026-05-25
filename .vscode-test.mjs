import { defineConfig } from '@vscode/test-cli';

export default defineConfig({
  files: './tests/e2e/vscode/**/*.test.js',
  version: 'stable',
  launchArgs: [
    '--disable-extensions',
    '--new-window',
    '--profile-temp',
  ],
  mocha: {
    ui: 'tdd',
    color: true,
    timeout: 60000,
  },
});
