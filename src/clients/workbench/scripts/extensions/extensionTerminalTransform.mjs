import { join } from 'node:path';
import ts from 'typescript';

export const extensionTerminalSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadTerminalService.ts',
  'src/vs/workbench/api/browser/mainThreadTerminalShellIntegration.ts',
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  'src/vs/workbench/api/common/extHostExtensionService.ts',
  'src/vs/workbench/api/common/extHostTerminalService.ts',
  'src/vs/workbench/api/node/extHostTerminalService.ts',
];

const unavailable = '目标扩展暂不支持终端 API；尚未接入目标终端服务';

/** 目标终端尚未接入时，在原 API／RPC owner 拒绝，不借用窗口终端。 */
export function transformExtensionTerminal(root, path, source, replaceOnce) {
  const index = extensionTerminalSourcePaths.findIndex(relative => path === join(root, 'upstream', relative));
  if (index < 0) return source;
  // TODO(6.4)：沿原终端 owner 接入目标执行、事件与环境集合后，替换同一入口的拒绝。
  if (index === 3) return replaceOnce(source,
    'get environmentVariableCollection() { return that._extHostTerminalService.getEnvironmentVariableCollection(extensionDescription); }',
    `get environmentVariableCollection() {
      if (that._initData.isTargetExtensionHost) throw new errors.NotSupportedError(${JSON.stringify(unavailable)});
      return that._extHostTerminalService.getEnvironmentVariableCollection(extensionDescription);
    }`);
  if (index === 4) {
    let contents = replaceOnce(source,
      '@IExtHostRpcService extHostRpc: IExtHostRpcService\n',
      '@IExtHostRpcService extHostRpc: IExtHostRpcService,\n    @IExtHostInitDataService protected readonly _initData: IExtHostInitDataService\n');
    // 目标终端未接入，初始化不向窗口宣告进程能力；不是一次用户终端操作。
    contents = replaceOnce(contents, 'this._proxy.$registerProcessSupport(supportsProcesses);',
      'if (!this._initData.isTargetExtensionHost) this._proxy.$registerProcessSupport(supportsProcesses);');
    contents = replaceOnce(contents, 'super(false, extHostCommands, extHostRpc);', 'super(false, extHostCommands, extHostRpc, initData);');
    contents = replaceOnce(contents, 'public createExtensionTerminal(options: vscode.ExtensionTerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {',
      `public createExtensionTerminal(options: vscode.ExtensionTerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {
      if (this._initData.isTargetExtensionHost) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
    return replaceOnce(contents, 'public createTerminalFromOptions(options: vscode.TerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {',
      `public createTerminalFromOptions(options: vscode.TerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {
      if (this._initData.isTargetExtensionHost) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
  }
  if (index === 5) {
    let contents = "import { NotSupportedError } from '../../../base/common/errors.js';\nimport { IExtHostInitDataService } from '../common/extHostInitDataService.js';\n" + source;
    contents = replaceOnce(contents, '@IExtHostRpcService extHostRpc: IExtHostRpcService\n',
      '@IExtHostRpcService extHostRpc: IExtHostRpcService,\n    @IExtHostInitDataService initData: IExtHostInitDataService\n');
    contents = replaceOnce(contents, 'super(true, extHostCommands, extHostRpc);', 'super(true, extHostCommands, extHostRpc, initData);');
    return replaceOnce(contents, 'public createTerminalFromOptions(options: vscode.TerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {',
      `public createTerminalFromOptions(options: vscode.TerminalOptions, internalOptions?: ITerminalInternalOptions): vscode.Terminal {
      if (this._initData.isTargetExtensionHost) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
  }
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true), edits = [];
  const insert = (body, statement) => edits.push({ at: body.getStart(parsed) + 1, text: '\n' + statement });
  if (index === 2) {
    const namespaces = new Map([
      ['window', new Set(['activeTerminal', 'terminals', 'createTerminal', 'onDidCloseTerminal', 'onDidOpenTerminal',
        'onDidChangeActiveTerminal', 'onDidChangeTerminalDimensions', 'onDidChangeTerminalState', 'onDidWriteTerminalData',
        'onDidExecuteTerminalCommand', 'onDidChangeTerminalShellIntegration', 'onDidStartTerminalShellExecution',
        'onDidEndTerminalShellExecution', 'registerTerminalLinkProvider', 'registerTerminalProfileProvider',
        'registerTerminalCompletionProvider', 'registerTerminalQuickFixProvider'])],
      ['env', new Set(['shell', 'onDidChangeShell'])],
    ]);
    function visit(node) {
      if (ts.isVariableDeclaration(node) && namespaces.has(node.name.getText(parsed)) && ts.isObjectLiteralExpression(node.initializer)) {
        const members = namespaces.get(node.name.getText(parsed));
        for (const member of node.initializer.properties) {
          const name = member.name?.getText(parsed);
          if (!members.has(name)) continue;
          if (!member.body) throw new Error('终端公开 API 缺少原函数体：' + name);
          insert(member.body, `if (initData.isTargetExtensionHost) throw new errors.NotSupportedError(${JSON.stringify(unavailable + '：' + node.name.getText(parsed) + '.' + name)});`);
          members.delete(name);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(parsed);
    if ([...namespaces.values()].some(members => members.size)) throw new Error('终端公开 API 锚点不完整');
  } else {
    const context = index === 0 ? '_extHostContext' : 'extHostContext';
    const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text ===
      (index === 0 ? 'MainThreadTerminalService' : 'MainThreadTerminalShellIntegration'));
    if (!owner) throw new Error('原终端 MainThread owner 不存在');
    for (const member of owner.members) {
      if (ts.isMethodDeclaration(member) && member.name.getText(parsed).startsWith('$')) {
        insert(member.body, `if (this.${context}.extensionHost.targetContext) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
      }
    }
    source = "import { NotSupportedError } from '../../../base/common/errors.js';\n" + applyEdits(source, edits);
    if (index === 1) source = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    return replaceOnce(source,
      `this._proxy = ${context}.getProxy(ExtHostContext.${index === 0 ? 'ExtHostTerminalService' : 'ExtHostTerminalShellIntegration'});`,
      `this._proxy = ${context}.getProxy(ExtHostContext.${index === 0 ? 'ExtHostTerminalService' : 'ExtHostTerminalShellIntegration'});
      if (${context}.extensionHost.targetContext) return;`);
  }
  return applyEdits(source, edits);
}

function applyEdits(source, edits) {
  for (const { at, text } of edits.sort((a, b) => b.at - a.at)) source = source.slice(0, at) + text + source.slice(at);
  return source;
}
