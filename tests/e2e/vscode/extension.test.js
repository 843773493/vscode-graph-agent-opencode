import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import * as vscode from 'vscode';
import { OPEN_SIDEBAR_COMMAND } from '../../../src/shared/constants.js';

async function waitForPathExists(targetPath, timeoutMs = 15000) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    if (existsSync(targetPath)) {
      return true;
    }

    await new Promise((resolve) => setTimeout(resolve, 250));
  }

  return false;
}

suite('Graph Agent extension e2e tests', () => {
  test('激活后会在用户目录创建默认工作区目录', async () => {
    await vscode.commands.executeCommand('vscode-graph-agent.openSidebar');

    const workspaceRoot = process.env.USERPROFILE || process.env.HOME;
    assert.ok(workspaceRoot, '无法解析用户主目录');

    const defaultWorkspaceDir = join(workspaceRoot, '.BoxTeamWorkspace');
    const defaultWorkspaceBoxteamDir = join(defaultWorkspaceDir, '.boxteam');

    assert.equal(await waitForPathExists(defaultWorkspaceDir), true, `未创建默认工作区目录: ${defaultWorkspaceDir}`);
    assert.equal(await waitForPathExists(defaultWorkspaceBoxteamDir), true, `未创建工作区专属目录: ${defaultWorkspaceBoxteamDir}`);

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