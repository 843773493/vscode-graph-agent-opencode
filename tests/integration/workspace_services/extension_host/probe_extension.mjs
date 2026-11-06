import * as vscode from 'vscode';
import { mkdir, readFile, writeFile } from 'node:fs/promises';

export function activate(context) {
  const snapshot = () => vscode.extensions.all.map(extension => [extension.id, extension.packageJSON.version]);
  const changes = [];
  context.subscriptions.push(vscode.extensions.onDidChange(() => changes.push(snapshot())));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.extension-state', () => ({ current: snapshot(), changes })));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.crash', () => process.kill(process.pid, 'SIGKILL')));
  context.subscriptions.push(vscode.commands.registerCommand('host-probe.storage', async () => {
    const label = await readFile(vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'identity.txt').fsPath, 'utf8');
    const result = {};
    for (const [name, uri] of Object.entries({ globalStorageUri: context.globalStorageUri, storageUri: context.storageUri, logUri: context.logUri })) {
      if (!uri || uri.scheme !== 'file') throw new Error('扩展存储位置不是目标本机资源：' + name);
      await mkdir(uri.fsPath, { recursive: true });
      const file = vscode.Uri.joinPath(uri, label + '.txt');
      await writeFile(file.fsPath, label);
      result[name] = { uri: uri.toString(), file: file.toString(), value: await readFile(file.fsPath, 'utf8') };
    }
    let syncError;
    try { context.globalState.setKeysForSync(['probe']); } catch (error) { syncError = error.message; }
    if (!syncError) throw new Error('目标跨设备同步未明确拒绝');
    const secretErrors = await Promise.all([
      () => context.secrets.get('probe'), () => context.secrets.store('probe', 'target-only'),
      () => context.secrets.delete('probe'), () => context.secrets.keys(),
    ].map(async operation => {
      try { await operation(); } catch (error) { return error.message; }
      throw new Error('目标秘密存储未明确拒绝');
    }));
    return { extensionUri: context.extensionUri.toString(), locations: result, syncError, secretErrors };
  }));
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
