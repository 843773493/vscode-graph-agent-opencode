import * as vscode from 'vscode';

import { BackendManager } from './backend/backendManager.js';
import { OPEN_SIDEBAR_COMMAND, SIDEBAR_VIEW_ID } from './shared/constants.js';
import { SidebarProvider } from './webview/sidebarProvider.js';

export function activate(context) {
  const outputChannel = vscode.window.createOutputChannel('Graph Agent');
  const backendManager = new BackendManager(outputChannel);
  const sidebarProvider = new SidebarProvider(context, backendManager);

  context.subscriptions.push(outputChannel);
  outputChannel.show(true);
  outputChannel.appendLine('[graph-agent] 扩展已激活');
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(SIDEBAR_VIEW_ID, sidebarProvider, {
      webviewOptions: {
        retainContextWhenHidden: true,
      },
    }),
  );
  context.subscriptions.push(sidebarProvider);

  context.subscriptions.push(
    vscode.commands.registerCommand(OPEN_SIDEBAR_COMMAND, async () => {
      await vscode.commands.executeCommand('workbench.view.extension.vscode-graph-agent');
    }),
  );

  context.subscriptions.push({
    dispose: () => backendManager.dispose(),
  });
}

export function deactivate() {
  return undefined;
}
