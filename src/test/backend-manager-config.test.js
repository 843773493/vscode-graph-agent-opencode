import assert from 'node:assert/strict';
import * as vscode from 'vscode';

import { DEFAULT_BACKEND_PORT } from '../shared/constants.js';

suite('Backend manager config tests', () => {
  test('vscodeGraphAgent 配置项有默认值', () => {
    const config = vscode.workspace.getConfiguration('vscodeGraphAgent');

    assert.equal(config.get('port', DEFAULT_BACKEND_PORT), DEFAULT_BACKEND_PORT);
    assert.equal(config.get('pythonPath', 'python'), 'python');
  });
});