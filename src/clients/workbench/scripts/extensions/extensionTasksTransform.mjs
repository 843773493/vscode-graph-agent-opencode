import { join } from 'node:path';
import ts from 'typescript';

export const extensionTasksSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadTask.ts',
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  'src/vs/workbench/api/common/extHostTask.ts',
  'src/vs/workbench/api/node/extHostTask.ts',
];

const unavailable = '目标扩展暂不支持任务 API；尚未接入目标任务执行服务';

/** 目标任务尚未接入时，由原任务 owner 拒绝，不读取或改变窗口任务。 */
export function transformExtensionTasks(root, path, source, replaceOnce) {
  const index = extensionTasksSourcePaths.findIndex(relative => path === join(root, 'upstream', relative));
  if (index < 0) return source;
  // TODO(6.4)：沿原任务 owner 接入目标执行、变量解析和事件后，替换同一入口的拒绝。
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true), edits = [];
  const insert = (body, statement) => edits.push({ start: body.getStart(parsed) + 1, end: body.getStart(parsed) + 1, text: '\n' + statement });
  if (index === 1) {
    const members = new Map([
      ['tasks', new Set(['registerTaskProvider', 'fetchTasks', 'executeTask', 'taskExecutions', 'onDidStartTask', 'onDidEndTask',
        'onDidStartTaskProcess', 'onDidEndTaskProcess', 'onDidStartTaskProblemMatchers', 'onDidEndTaskProblemMatchers'])],
      ['workspace', new Set(['registerTaskProvider'])],
    ]);
    function visit(node) {
      if (ts.isVariableDeclaration(node) && members.has(node.name.getText(parsed)) && ts.isObjectLiteralExpression(node.initializer)) {
        const expected = members.get(node.name.getText(parsed));
        for (const member of node.initializer.properties) {
          const name = member.name?.getText(parsed);
          if (!expected.has(name)) continue;
          const body = ts.isPropertyAssignment(member) ? member.initializer.body : member.body;
          if (!body || !ts.isBlock(body)) throw new Error('任务公开 API 缺少原函数体：' + name);
          insert(body, `if (initData.isTargetExtensionHost) throw new errors.NotSupportedError(${JSON.stringify(unavailable + '：' + node.name.getText(parsed) + '.' + name)});`);
          expected.delete(name);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(parsed);
    if ([...members.values()].some(names => names.size)) throw new Error('任务公开 API 锚点不完整');
  } else if (index === 0) {
    const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'MainThreadTask');
    if (!owner) throw new Error('原 MainThreadTask owner 不存在');
    for (const member of owner.members) {
      if (ts.isMethodDeclaration(member) && member.name.getText(parsed).startsWith('$')) {
        insert(member.body, `if (this.hostContext.extensionHost.targetContext) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
      }
    }
    source = applyEdits(source, edits);
    source = replaceOnce(source, "import { ErrorNoTelemetry } from '../../../base/common/errors.js';",
      "import { ErrorNoTelemetry, NotSupportedError } from '../../../base/common/errors.js';");
    source = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly hostContext: IExtHostContext,');
    source = replaceOnce(source, 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostTask);',
      'this._proxy = hostContext.getProxy(ExtHostContext.ExtHostTask);');
    return replaceOnce(source, 'this._providers = new Map();',
      'this._providers = new Map();\n    if (hostContext.extensionHost.targetContext) return;');
  } else {
    const guarded = new Set(['registerTaskProvider', 'registerTaskSystem', 'fetchTasks', 'executeTask', 'taskExecutions', 'terminateTask',
      'onDidStartTask', 'onDidEndTask', 'onDidStartTaskProcess', 'onDidEndTaskProcess', 'onDidStartTaskProblemMatchers',
      'onDidEndTaskProblemMatchers', 'getTaskExecution', 'addCustomExecution']);
    for (const owner of parsed.statements.filter(node => ts.isClassDeclaration(node)
      && ['ExtHostTaskBase', 'WorkerExtHostTask', 'ExtHostTask'].includes(node.name?.text))) {
      for (const member of owner.members) {
        if (ts.isConstructorDeclaration(member)) {
          if (owner.name.text === 'ExtHostTaskBase') {
            const parameter = member.parameters.find(parameter => parameter.name.getText(parsed) === 'initData');
            if (!parameter) throw new Error('原任务服务缺少初始化参数');
            edits.push({ start: parameter.getStart(parsed), end: parameter.end,
              text: '@IExtHostInitDataService protected readonly _initData: IExtHostInitDataService' });
          } else {
            const superCall = member.body.statements.find(statement => ts.isExpressionStatement(statement)
              && ts.isCallExpression(statement.expression) && statement.expression.expression.kind === ts.SyntaxKind.SuperKeyword);
            if (!superCall) throw new Error('原任务宿主缺少 super 初始化');
            edits.push({ start: superCall.end, end: superCall.end, text: '\n    if (initData.isTargetExtensionHost) return;' });
          }
        } else if ((ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member)) && member.body
          && (guarded.has(member.name.getText(parsed)) || member.name.getText(parsed).startsWith('$'))) {
          insert(member.body, `if (this._initData.isTargetExtensionHost) throw new NotSupportedError(${JSON.stringify(unavailable)});`);
        }
      }
    }
    source = applyEdits(source, edits);
    if (index === 2) return replaceOnce(source, 'this._proxy.$registerSupportedExecutions(true);',
      'if (!this._initData.isTargetExtensionHost) this._proxy.$registerSupportedExecutions(true);');
    return "import { NotSupportedError } from '../../../base/common/errors.js';\n" + source;
  }
  return applyEdits(source, edits);
}

function applyEdits(source, edits) {
  for (const { start, end, text } of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, start) + text + source.slice(end);
  return source;
}
