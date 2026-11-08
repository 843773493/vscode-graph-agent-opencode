import { join } from 'node:path';
import ts from 'typescript';

export const extensionDebugSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadDebugService.ts',
  'src/vs/workbench/api/common/extHostDebugService.ts',
  'src/vs/workbench/api/node/extHostDebugService.ts',
  'src/vs/workbench/api/common/extHost.api.impl.ts',
];

const unavailable = '目标扩展暂不支持调试 API；尚未接入目标调试服务';

/** 调试尚未绑定目标 owner 时，原协议不得借用窗口调试会话或启动替代进程。 */
export function transformExtensionDebug(root, path, source, replaceOnce) {
  const index = extensionDebugSourcePaths.findIndex(relative => path === join(root, 'upstream', relative));
  if (index < 0) return source;
  // TODO(6.4)：目标调试、适配器及 runInTerminal 接入原 owner 后，沿同一链替换拒绝。
  let contents = source;
  if (index === 0) {
    contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'private readonly _debugAdapters: Map<number, ExtensionHostDebugAdapter>;',
      'private readonly _debugAdapters = new Map<number, ExtensionHostDebugAdapter>();');
    contents = replaceOnce(contents, 'private readonly _extHostKnownSessions: Set<DebugSessionUUID>;',
      'private readonly _extHostKnownSessions = new Set<DebugSessionUUID>();');
    contents = replaceOnce(contents, '\t\tthis._debugAdapters = new Map();\n\t\tthis._extHostKnownSessions = new Set();\n', '');
    contents = replaceOnce(contents, 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostDebugService);',
      'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostDebugService);\n    if (extHostContext.extensionHost.targetContext) return;');
  } else if (index === 1 || index === 2) {
    contents = "import { NotSupportedError } from '../../../base/common/errors.js';\n"
      + `import { IExtHostInitDataService } from '${index === 1 ? './' : '../common/'}extHostInitDataService.js';\n` + contents;
    if (index === 1) {
      contents = replaceOnce(contents, '@IExtHostTesting private readonly _testing: IExtHostTesting,',
        '@IExtHostTesting private readonly _testing: IExtHostTesting,\n    @IExtHostInitDataService protected readonly _initData: IExtHostInitDataService,');
      contents = replaceOnce(contents, 'this._extensionService.getExtensionRegistry().then((extensionRegistry: ExtensionDescriptionRegistry) => {',
        'if (!this._initData.isTargetExtensionHost) this._extensionService.getExtensionRegistry().then((extensionRegistry: ExtensionDescriptionRegistry) => {');
      contents = replaceOnce(contents, '@IExtHostTesting testing: IExtHostTesting,',
        '@IExtHostTesting testing: IExtHostTesting,\n    @IExtHostInitDataService initData: IExtHostInitDataService,');
    } else {
      contents = replaceOnce(contents, '@ILogService private readonly _logService: ILogService,',
        '@ILogService private readonly _logService: ILogService,\n    @IExtHostInitDataService initData: IExtHostInitDataService,');
    }
    contents = replaceOnce(contents,
      'super(extHostRpcService, workspaceService, extensionService, configurationService, editorTabs, variableResolver, commands, testing);',
      'super(extHostRpcService, workspaceService, extensionService, configurationService, editorTabs, variableResolver, commands, testing, initData);');
  }
  const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true), edits = [];
  const insert = (body, check) => edits.push({ at: body.getStart(parsed) + 1, text: '\n' + check });
  const reject = index === 0 ? `if (this.extHostContext.extensionHost.targetContext) throw new NotSupportedError(${JSON.stringify(unavailable)});`
    : `if (this._initData.isTargetExtensionHost) throw new NotSupportedError(${JSON.stringify(unavailable)});`;
  if (index === 3) {
    function visit(node) {
      if (ts.isVariableDeclaration(node) && node.name.getText(parsed) === 'debug' && ts.isObjectLiteralExpression(node.initializer)) {
        for (const member of node.initializer.properties) {
          if (!member.body) throw new Error('原公开调试 API 缺少函数体');
          insert(member.body, `if (initData.isTargetExtensionHost) throw new errors.NotSupportedError(${JSON.stringify(unavailable)});`);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(parsed);
  } else {
    const name = index === 0 ? 'MainThreadDebugService' : index === 1 ? 'ExtHostDebugServiceBase' : 'ExtHostDebugService';
    const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === name);
    if (!owner) throw new Error('原调试 owner 不存在：' + name);
    for (const member of owner.members) {
      if (!member.body || !member.name || member.modifiers?.some(modifier =>
        [ts.SyntaxKind.PrivateKeyword, ts.SyntaxKind.ProtectedKeyword].includes(modifier.kind))) continue;
      const method = member.name.getText(parsed);
      if (method !== 'dispose' && (index !== 0 || method.startsWith('$') || ['createDebugAdapter', 'substituteVariables', 'runInTerminal'].includes(method))) {
        insert(member.body, reject);
      }
    }
  }
  if (!edits.length) throw new Error('原调试 API／协议入口未找到');
  for (const { at, text } of edits.sort((a, b) => b.at - a.at)) contents = contents.slice(0, at) + text + contents.slice(at);
  return index === 0 ? "import { NotSupportedError } from '../../../base/common/errors.js';\n" + contents : contents;
}
