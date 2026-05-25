import * as vscode from 'vscode';

import { BackendManager } from './backend/backendManager.js';
import { OPEN_SIDEBAR_COMMAND, SIDEBAR_VIEW_ID } from './shared/constants.js';
import { SidebarProvider } from './webview/sidebarProvider.js';

let sidebarViewProviderRegistered = false;

export function activate(context) {
  const outputChannel = vscode.window.createOutputChannel('Graph Agent');
  const backendManager = new BackendManager(outputChannel);
  const sidebarProvider = new SidebarProvider(context, backendManager);

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

  // ==============================================
  // 第五阶段: 状态栏 3个按钮
  // ==============================================

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

  // ==============================================
  // 第六阶段: 交互行为统一适配
  // ==============================================

  // 按钮悬停效果统一适配
  // 按钮激活/禁用状态样式统一
  // 悬停显示按钮的触发延迟适配
  // 按钮点击反馈动画效果

  // 历史记录按钮点击事件
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.showHistory', async () => {
      outputChannel.appendLine('[graph-agent] 历史记录按钮被点击');
      await vscode.commands.executeCommand(OPEN_SIDEBAR_COMMAND);
    }),
  );

  // 状态按钮点击事件
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.showStatus', () => {
      // TODO: 实现状态面板显示
      outputChannel.appendLine('[graph-agent] 状态按钮被点击');
    }),
  );

  // 代理切换按钮点击事件
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.toggleAgent', () => {
      // TODO: 实现代理启停切换
      outputChannel.appendLine('[graph-agent] 代理状态按钮被点击');
    }),
  );

  // ==============================================
  // 第七阶段: 编辑器标题工具栏按钮
  // ==============================================

  // 1. 固定会话按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.pinSession', () => {
      // TODO: 实现会话固定功能
      outputChannel.appendLine('[graph-agent] 固定会话按钮被点击');
    }),
  );

  // 2. 视图切换按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.toggleView', () => {
      // TODO: 实现视图切换功能
      outputChannel.appendLine('[graph-agent] 视图切换按钮被点击');
    }),
  );

  // 3. 模型选择下拉按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.selectModel', () => {
      // TODO: 实现模型选择下拉菜单
      outputChannel.appendLine('[graph-agent] 模型选择按钮被点击');
    }),
  );

  // 4. 上下文设置按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.contextSettings', () => {
      // TODO: 实现上下文设置面板
      outputChannel.appendLine('[graph-agent] 上下文设置按钮被点击');
    }),
  );

  // 5. 帮助按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.showHelp', () => {
      // TODO: 实现帮助文档显示
      outputChannel.appendLine('[graph-agent] 帮助按钮被点击');
    }),
  );

  // 6. 设置按钮
  context.subscriptions.push(
    vscode.commands.registerCommand('graph-agent.openSettings', () => {
      // TODO: 打开扩展设置页面
      outputChannel.appendLine('[graph-agent] 设置按钮被点击');
    }),
  );

  // ==============================================
  // 第七阶段: 上下文菜单系统骨架
  // ==============================================

  // 预留11个上下文菜单入口点
  const contextMenuCommands = [
    'graph-agent.context.explainCode',          // 1. 解释代码
    'graph-agent.context.fixIssues',            // 2. 修复问题
    'graph-agent.context.generateTests',        // 3. 生成测试
    'graph-agent.context.refactorCode',         // 4. 重构代码
    'graph-agent.context.optimizePerformance',  // 5. 性能优化
    'graph-agent.context.addComments',          // 6. 添加注释
    'graph-agent.context.findBugs',             // 7. 查找Bug
    'graph-agent.context.documentCode',         // 8. 文档生成
    'graph-agent.context.translateCode',        // 9. 代码翻译
    'graph-agent.context.reviewCode',           // 10. 代码审查
    'graph-agent.context.customPrompt',         // 11. 自定义提示
  ];

  // 注册所有上下文菜单命令
  contextMenuCommands.forEach((commandId) => {
    context.subscriptions.push(
      vscode.commands.registerCommand(commandId, (...args) => {
        // TODO: 实现对应上下文菜单功能
        outputChannel.appendLine(`[graph-agent] 上下文菜单触发: ${commandId}`);
        outputChannel.appendLine(`[graph-agent] 参数: ${JSON.stringify(args)}`);
      }),
    );
  });

  context.subscriptions.push({
    dispose: () => backendManager.dispose(),
  });
}

export function deactivate() {
  return undefined;
}
