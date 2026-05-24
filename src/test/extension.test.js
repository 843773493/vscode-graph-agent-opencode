import assert from 'node:assert/strict';
import * as vscode from 'vscode';
import { OPEN_SIDEBAR_COMMAND } from '../shared/constants.js';

suite('Graph Agent extension smoke tests', () => {
  test('激活后注册核心命令', async () => {
    await vscode.commands.executeCommand('vscode-graph-agent.openSidebar');

    const openSidebarCommand = await vscode.commands.getCommands(true).then((commands) =>
      commands.includes(OPEN_SIDEBAR_COMMAND),
    );
    const showHistoryCommand = await vscode.commands.getCommands(true).then((commands) =>
      commands.includes('graph-agent.showHistory'),
    );
    const contextCommand = await vscode.commands.getCommands(true).then((commands) =>
      commands.includes('graph-agent.context.explainCode'),
    );

    assert.equal(openSidebarCommand, true);
    assert.equal(showHistoryCommand, true);
    assert.equal(contextCommand, true);
  });
});
