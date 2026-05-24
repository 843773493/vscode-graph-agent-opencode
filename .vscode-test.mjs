import { defineConfig } from '@vscode/test-cli';

export default defineConfig({
  files: './src/test/**/*.test.js',
  version: 'stable',
  launchArgs: [
    '--disable-extensions',
    '--profile-temp',
  ],
  mocha: {
    ui: 'tdd',
    color: true,
    timeout: 10000,
  },
});
