import { join } from 'node:path';
import ts from 'typescript';

export const extensionLanguageFeaturesSourcePaths = [
  'src/vs/editor/common/languageFeatureRegistry.ts',
  'src/vs/editor/common/languageSelector.ts',
  'src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'src/vs/editor/contrib/wordHighlighter/browser/wordHighlighter.ts',
  'src/vs/editor/common/languages.ts',
  'src/vs/editor/contrib/wordHighlighter/browser/textualHighlightProvider.ts',
];

export function transformExtensionLanguageFeatures(root, path, source, replaceOnce) {
  const paths = extensionLanguageFeaturesSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = replaceOnce(source, 'readonly provider: T;', 'readonly provider: T;\n\treadonly selectorScore: typeof score;');
    contents = replaceOnce(contents, 'register(selector: LanguageSelector, provider: T): IDisposable {', 'register(selector: LanguageSelector, provider: T, selectorScore: typeof score = score): IDisposable {');
    contents = replaceOnce(contents, '\n\t\t\tprovider,\n', '\n\t\t\tprovider,\n\t\t\tselectorScore,\n');
    contents = replaceOnce(contents, '\n\tallNoModel(): T[] {', `
  /** 次要文档也消费同一注册项的评分，不能另存 provider.selector 绕过宿主归属。 */
  score(provider: T, model: ITextModel): number {
    const entry = this._entries.find(entry => entry.provider === provider);
    if (!entry) return 0;
    const notebook = this._notebookInfoResolver?.(model.uri);
    return entry.selectorScore(entry.selector, model.uri, model.getLanguageId(), shouldSynchronizeModel(model), notebook?.uri, notebook?.type);
  }

\tallNoModel(): T[] {`);
    return replaceOnce(contents, 'entry._score = score(entry.selector,', 'entry._score = entry.selectorScore(entry.selector,');
  }
  if (path === paths[1]) {
    let contents = replaceOnce(source, 'candidateNotebookType: string | undefined): number {', 'candidateNotebookType: string | undefined, patternMatcher?: (pattern: string | IRelativePattern, resource: URI) => boolean): number {');
    contents = replaceOnce(contents, 'candidateIsSynchronized, candidateNotebookUri, candidateNotebookType);', 'candidateIsSynchronized, candidateNotebookUri, candidateNotebookType, patternMatcher);');
    return replaceOnce(contents, '\n\t\tif (pattern) {', '\n\t\tif (pattern) {\n      if (patternMatcher) return patternMatcher(pattern, candidateUri) ? 10 : 0;');
  }
  if (path === paths[3]) {
    let contents = replaceOnce(source, "import { score } from '../../../common/languageSelector.js';\n", '');
    contents = replaceOnce(contents, 'score(provider.selector, otherModel.uri, otherModel.getLanguageId(), true, undefined, undefined)', 'registry.score(provider, otherModel)');
    return contents;
  }
  if (path === paths[4]) {
    let contents = replaceOnce(source, "import { LanguageSelector } from './languageSelector.js';\n", '');
    return replaceOnce(contents, '\n\treadonly selector: LanguageSelector;\n', '');
  }
  if (path === paths[5]) {
    let contents = replaceOnce(source, "import { LanguageFilter } from '../../../common/languageSelector.js';\n", '');
    return replaceOnce(contents, "\n\tselector: LanguageFilter = { language: '*' };\n", '');
  }
  if (path !== paths[2]) return source;
  let contents = `import { hostLanguageFeatureScore } from ${JSON.stringify(join(root, 'src/extensions/languageFeatures.js'))};
import type { score } from '../../../editor/common/languageSelector.js';\n` + source;
  contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
  contents = replaceOnce(contents, 'private readonly _selector: IDocumentFilterDto[],', 'private readonly _selector: IDocumentFilterDto[],\n\t\tprivate readonly _selectorScore: typeof score | undefined,');
  contents = replaceOnce(contents, '\n\t\t\tselector,\n\t\t\tthis._proxy,', '\n\t\t\tselector,\n\t\t\thostLanguageFeatureScore(this.extHostContext.extensionHost, selector),\n\t\t\tthis._proxy,');
  contents = replaceOnce(contents, '\n\t\t\tselector: selector,\n', '\n');
  // 用现有 TypeScript parser 定位原调用的末参，不复制各 Provider 或手写括号解析。
  const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
  const insertions = [];
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'register') {
      const first = node.arguments[0]?.getText(parsed);
      if (first === 'selector' || first === 'this._selector') {
        if (node.arguments.length !== 2) throw new Error('原语言 Provider 注册参数已变化');
        insertions.push({ position: node.arguments[1].end, text: first === 'selector'
          ? ', hostLanguageFeatureScore(this.extHostContext.extensionHost, selector)' : ', this._selectorScore' });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  if (insertions.length !== 33) throw new Error('原语言 Provider 注册清单已变化：' + insertions.length);
  for (const { position, text } of insertions.sort((a, b) => b.position - a.position)) contents = contents.slice(0, position) + text + contents.slice(position);
  return contents;
}
