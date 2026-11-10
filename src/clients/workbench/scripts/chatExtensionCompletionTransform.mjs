import { join } from 'node:path';
import ts from 'typescript';

export const chatExtensionCompletionSourcePaths = [
  'src/vs/workbench/contrib/chat/browser/widget/input/editor/chatInputCompletions.ts',
];

/** 移除会把 Gallery 标签搜索送入插件包管理器的聊天扩展安装建议。 */
export function transformChatExtensionCompletion(root, path, source) {
  if (path !== join(root, 'upstream', chatExtensionCompletionSourcePaths[0])) return source;

  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const statements = new Set();
  const visit = (node, ancestors) => {
    const parents = [...ancestors, node];
    if (ts.isPropertyAssignment(node) && node.name.getText(parsed) === '_debugDisplayName'
        && ts.isStringLiteral(node.initializer) && node.initializer.text === 'installChatExtensions') {
      const statement = [...parents].reverse().find(ts.isExpressionStatement);
      if (!statement) throw new Error('聊天扩展安装补全没有独立登记语句');
      statements.add(statement);
    }
    ts.forEachChild(node, child => visit(child, parents));
  };
  visit(parsed, []);
  if (statements.size !== 1) throw new Error(`聊天扩展安装补全登记数量不符：${statements.size}`);

  const [statement] = statements;
  return source.slice(0, statement.getFullStart()) + source.slice(statement.end);
}
