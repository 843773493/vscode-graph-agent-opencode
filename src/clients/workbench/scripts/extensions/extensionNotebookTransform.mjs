import { join } from 'node:path';
import ts from 'typescript';

export const extensionNotebookSourcePaths = [
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  ...['mainThreadNotebookDocumentsAndEditors', 'mainThreadNotebookDocuments', 'mainThreadNotebookEditors',
    'mainThreadNotebook', 'mainThreadNotebookKernels', 'mainThreadNotebookRenderers', 'mainThreadNotebookSaveParticipant']
    .map(name => `src/vs/workbench/api/browser/${name}.ts`),
];

const unavailable = '目标扩展暂不支持 Notebook API；文档、Cell URI、serializer 与执行服务尚未完成目标绑定';

/** 原公开 API 同时进入 Node 和 Web，必须在运行面分流前处理。 */
export function transformExtensionNotebookApi(root, path, source) {
  if (path !== join(root, 'upstream', extensionNotebookSourcePaths[0])) return source;
  const namespaces = new Map([
    ['window', new Set(['activeNotebookEditor', 'onDidChangeActiveNotebookEditor', 'visibleNotebookEditors',
      'onDidChangeVisibleNotebookEditors', 'onDidChangeNotebookEditorSelection', 'onDidChangeNotebookEditorVisibleRanges', 'showNotebookDocument'])],
    ['workspace', new Set(['notebookDocuments', 'openNotebookDocument', 'onDidSaveNotebookDocument',
      'onDidChangeNotebookDocument', 'onWillSaveNotebookDocument', 'onDidOpenNotebookDocument', 'onDidCloseNotebookDocument', 'registerNotebookSerializer'])],
    ['notebooks', new Set(['createNotebookController', 'registerNotebookCellStatusBarItemProvider', 'createRendererMessaging',
      'createNotebookControllerDetectionTask', 'registerKernelSourceActionProvider'])],
  ]);
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true), edits = [];
  function visit(node) {
    if (ts.isVariableDeclaration(node) && namespaces.has(node.name.getText(parsed)) && ts.isObjectLiteralExpression(node.initializer)) {
      const expected = namespaces.get(node.name.getText(parsed));
      for (const member of node.initializer.properties) {
        const name = member.name?.getText(parsed);
        if (!expected.has(name)) continue;
        const body = ts.isPropertyAssignment(member) ? member.initializer.body : member.body;
        if (!body || !ts.isBlock(body)) throw new Error('Notebook 公开 API 缺少原函数体：' + name);
        edits.push({ at: body.getStart(parsed) + 1,
          text: `\nif (initData.isTargetExtensionHost) throw new errors.NotSupportedError(${JSON.stringify(unavailable)});` });
        expected.delete(name);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  if ([...namespaces.values()].some(names => names.size)) throw new Error('Notebook 公开 API 锚点不完整');
  return insert(source, edits);
}

/** Notebook 仍为窗口共享服务；接通完整目标链前，不向目标宿主发送窗口正文或接受操作。 */
export function transformExtensionNotebooks(root, path, source, replaceOnce) {
  const index = extensionNotebookSourcePaths.findIndex(relative => path === join(root, 'upstream', relative));
  if (index < 1) return source;
  // TODO(6.2)：同批迁齐原 Notebook owner、serializer、Cell URI 和执行归属后，替换这些入口的拒绝。
  let contents = source;
  if (index === 1) {
    return replaceOnce(contents, 'extHostContext.set(MainContext.MainThreadNotebookEditors, this._mainThreadEditors);',
      'extHostContext.set(MainContext.MainThreadNotebookEditors, this._mainThreadEditors);\n    if (extHostContext.extensionHost.targetContext) return;');
  }
  if (index === 7) {
    contents = replaceOnce(contents, "import { IDisposable } from '../../../base/common/lifecycle.js';",
      "import { Disposable, IDisposable } from '../../../base/common/lifecycle.js';");
    contents = replaceOnce(contents, 'private _saveParticipantDisposable: IDisposable;',
      'private _saveParticipantDisposable: IDisposable = Disposable.None;');
    contents = replaceOnce(contents,
      'this._saveParticipantDisposable = this.workingCopyFileService.addSaveParticipant(',
      'if (extHostContext.extensionHost.targetContext) return;\n    this._saveParticipantDisposable = this.workingCopyFileService.addSaveParticipant(');
    // 原保存参与者即使被内部直接调用，也不能向未支持的目标 Notebook 发 RPC。
    const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
    const owner = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'ExtHostNotebookDocumentSaveParticipant');
    const method = owner?.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(parsed) === 'participate');
    if (!method?.body) throw new Error('原 Notebook 保存参与者入口不存在');
    contents = insert(contents, [{ at: method.body.getStart(parsed) + 1,
      text: `\nif (this._host.targetContext) throw new NotSupportedError(${JSON.stringify(unavailable)});` }]);
  } else {
    contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    const barriers = {
      2: 'this._modelReferenceCollection = new BoundModelReferenceCollection(this._uriIdentityService.extUri);',
      3: 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostNotebookEditors);',
      5: 'this._proxy = extHostContext.getProxy(ExtHostContext.ExtHostNotebookKernels);',
      6: 'this.proxy = extHostContext.getProxy(ExtHostContext.ExtHostNotebookRenderers);',
    };
    if (barriers[index]) contents = replaceOnce(contents, barriers[index], barriers[index] + '\n    if (extHostContext.extensionHost.targetContext) return;');
    const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true), edits = [];
    for (const owner of parsed.statements.filter(node => ts.isClassDeclaration(node) && node.name?.text.startsWith('MainThreadNotebook'))) {
      for (const member of owner.members) {
        if (!ts.isMethodDeclaration(member) || !member.body) continue;
        const name = member.name.getText(parsed);
        if (!name.startsWith('$') && !['handleNotebooksAdded', 'handleNotebooksRemoved', 'handleEditorsAdded', 'handleEditorsRemoved'].includes(name)) continue;
        edits.push({ at: member.body.getStart(parsed) + 1,
          text: `\nif (this.extHostContext.extensionHost.targetContext) throw new NotSupportedError(${JSON.stringify(unavailable)});` });
      }
    }
    if (!edits.length) throw new Error('原 Notebook 协议入口不存在：' + path);
    contents = insert(contents, edits);
  }
  return "import { NotSupportedError } from '../../../base/common/errors.js';\n" + contents;
}

function insert(source, edits) {
  for (const { at, text } of edits.sort((a, b) => b.at - a.at)) source = source.slice(0, at) + text + source.slice(at);
  return source;
}
