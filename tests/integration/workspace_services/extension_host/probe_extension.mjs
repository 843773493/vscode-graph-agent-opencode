import * as vscode from 'vscode';
import { readFile } from 'node:fs/promises';

export function activate(context) {
  const snapshot = () => vscode.extensions.all.map(extension => [extension.id, extension.packageJSON.version]);
  const changes = [];
  context.subscriptions.push(vscode.extensions.onDidChange(() => changes.push(snapshot())));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.extension-state', () => ({ current: snapshot(), changes })));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.crash', () => process.kill(process.pid, 'SIGKILL')));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.inspect', async () => {
    const folders = vscode.workspace.workspaceFolders;
    const file = vscode.Uri.joinPath(folders[0].uri, 'identity.txt');
    return {
      id: context.extension.id,
      version: context.extension.packageJSON.version,
      folders: folders.map(folder => folder.uri.toString()),
      native: await readFile(file.fsPath, 'utf8'),
      api: Buffer.from(await vscode.workspace.fs.readFile(file)).toString(),
      ownVersion: vscode.extensions.getExtension(context.extension.id).packageJSON.version,
      all: vscode.extensions.all.map(extension => extension.id),
    };
  }));
}
