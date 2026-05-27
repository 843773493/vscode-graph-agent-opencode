import * as vscode from 'vscode';

import { BackendManager } from './backend/backendManager.js';
import { OPEN_SIDEBAR_COMMAND, OPEN_UI_SHELL_DEBUG_COMMAND, SIDEBAR_VIEW_ID } from './shared/constants.js';
import { SidebarProvider } from './webview/sidebarProvider.js';

let sidebarViewProviderRegistered = false;

export function activate(context) {
  const outputChannel = vscode.window.createOutputChannel('Graph Agent');
  const backendManager = new BackendManager(outputChannel);
  const sidebarProvider = new SidebarProvider(context, backendManager);
  const shellModeEnabled = process.env.GRAPH_AGENT_UI_SHELL_DEBUG === '1';

  context.subscriptions.push(outputChannel);
  outputChannel.show(true);
  outputChannel.appendLine('[graph-agent] 扩展已激活');

  void backendManager.ensureStarted().then(() => {
    outputChannel.appendLine('[graph-agent] 后端已在扩展激活阶段初始化完成');
  }).catch((error) => {
    outputChannel.appendLine(`[graph-agent] 后端初始化失败: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
    throw error;
  });

  if (!sidebarViewProviderRegistered) {
    context.subscriptions.push(
      vscode.window.registerWebviewViewProvider(SIDEBAR_VIEW_ID, sidebarProvider, {
        webviewOptions: {
          retainContextWhenHidden: true,
        },
      }),
    );
    sidebarViewProviderRegistered = true;
  }
  context.subscriptions.push(sidebarProvider);

  context.subscriptions.push(
    vscode.commands.registerCommand(OPEN_SIDEBAR_COMMAND, async () => {
      await vscode.commands.executeCommand('workbench.view.extension.vscode-graph-agent');
    }),
  );

  context.subscriptions.push(
    vscode.commands.registerCommand(OPEN_UI_SHELL_DEBUG_COMMAND, async () => {
      const shellDebugProvider = new SidebarProvider(context, backendManager, { shellMode: true });
      await shellDebugProvider.createShellDebugPanel();
    }),
  );

  if (shellModeEnabled) {
    void vscode.commands.executeCommand(OPEN_UI_SHELL_DEBUG_COMMAND).catch((error) => {
      outputChannel.appendLine(`[graph-agent] 纯壳调试面板自动打开失败: ${error instanceof Error ? error.stack ?? error.message : String(error)}`);
      throw error;
    });
  }

  // 1. Copilot状态按钮
  const copilotStatusButton = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  copilotStatusButton.text = '$(copilot)';
  copilotStatusButton.tooltip = 'Graph Agent 状态';
  copilotStatusButton.command = 'graph-agent.showStatus';
  copilotStatusButton.show();

  // 2. 聊天快捷入口按钮
  const chatEntryButton = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 99);
  chatEntryButton.text = '$(comment-discussion)';
  chatEntryButton.tooltip = '打开聊天面板';
  chatEntryButton.command = OPEN_SIDEBAR_COMMAND;
  chatEntryButton.show();

  // 3. 代理状态指示器按钮
  const agentStatusIndicator = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 98);
  agentStatusIndicator.text = '$(circle-filled)';
  agentStatusIndicator.tooltip = '代理运行状态: 空闲';
  agentStatusIndicator.command = 'graph-agent.toggleAgent';
  agentStatusIndicator.show();

  context.subscriptions.push(copilotStatusButton, chatEntryButton, agentStatusIndicator);

  // 历史记录按钮点击事件
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.showHistory', async () => {
      outputChannel.appendLine('[graph-agent] 历史记录按钮被点击');
      await vscode.commands.executeCommand(OPEN_SIDEBAR_COMMAND);
    }),
  );

  context.subscriptions.push({
    dispose: () => backendManager.dispose(),
  });
}

export function deactivate() {
  return undefined;
}
