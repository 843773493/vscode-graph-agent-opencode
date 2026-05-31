import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import * as vscode from 'vscode';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const EXTENSION_ROOT = path.resolve(__dirname, '..', '..', '..');

function getWorkspaceRoot() {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length > 0) {
    return folders[0].uri.fsPath;
  }

  const homeDir = process.env.HOME || process.env.USERPROFILE;
  if (!homeDir) {
    throw new Error('无法解析用户主目录，无法创建默认用户级工作区');
  }

  return path.join(homeDir, '.BoxTeamWorkspace');
}

function ensureWorkspaceLayout(workspaceRoot) {
  if (!fs.existsSync(workspaceRoot)) {
    fs.mkdirSync(workspaceRoot, { recursive: true });
  }

  const boxteamDir = path.join(workspaceRoot, '.boxteam');
  if (!fs.existsSync(boxteamDir)) {
    fs.mkdirSync(boxteamDir, { recursive: true });
  }
}

async function waitFor(predicate, timeoutMs = 60000, intervalMs = 500) {
  const startedAt = Date.now();
  let lastError = null;

  while (Date.now() - startedAt < timeoutMs) {
    try {
      const result = await predicate();
      if (result) {
        return result;
      }
    } catch (error) {
      lastError = error;
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  throw new Error(`等待条件超时（${timeoutMs}ms）${lastError ? `: ${String(lastError)}` : ''}`);
}

suite('Graph Agent extension runtime e2e tests', () => {
  test('激活扩展后会注册命令并启动后端', async () => {
    assert.ok(fs.existsSync(path.join(EXTENSION_ROOT, 'package.json')), '找不到扩展根目录 package.json');

    const workspaceRoot = getWorkspaceRoot();
    ensureWorkspaceLayout(workspaceRoot);

    await vscode.commands.executeCommand('vscode-graph-agent.openSidebar');

    const backendApi = await waitFor(async () => {
      const backendManager = globalThis.__graphAgentBackendManager;
      if (!backendManager) {
        return null;
      }

      const result = await backendManager.ensureStarted();
      return result ? backendManager : null;
    }, 120000, 1000);

    assert.ok(backendApi, '后端没有成功启动');
    assert.ok(typeof backendApi.port === 'number' && backendApi.port > 0, '后端端口无效');

    const workspaceDir = path.join(workspaceRoot, '.boxteam');
    assert.ok(fs.existsSync(workspaceRoot), '工作区根目录未创建');
    assert.ok(fs.existsSync(workspaceDir), '.boxteam 目录未创建');

    const registered = await vscode.commands.getCommands(true);
    assert.ok(registered.includes('vscode-graph-agent.openSidebar'));
    assert.ok(registered.includes('graph-agent.showHistory'));
    assert.ok(registered.includes('graph-agent.showStatus'));
  });
});
