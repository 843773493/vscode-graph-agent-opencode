import { join } from 'node:path';

export const extensionWorkspaceSymbolsSourcePaths = [
  'src/vs/workbench/contrib/search/common/search.ts',
  'src/vs/workbench/api/browser/mainThreadLanguageFeatures.ts',
  'src/vs/workbench/contrib/search/browser/search.contribution.ts',
  'src/vs/workbench/contrib/search/browser/symbolsQuickAccess.ts',
];

export function transformExtensionWorkspaceSymbols(root, path, source, replaceOnce) {
  const paths = extensionWorkspaceSymbolsSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = `import type { IExtensionHost } from '../../../services/extensions/common/extensions.js';\n` + source;
    contents = replaceOnce(contents, 'export interface IWorkspaceSymbolProvider {', 'export interface IWorkspaceSymbolProvider {\n  readonly extensionHost?: IExtensionHost;');
    contents = replaceOnce(contents, 'token: CancellationToken = CancellationToken.None): Promise<WorkspaceSymbolItem[]> {', 'token: CancellationToken = CancellationToken.None, providers: readonly IWorkspaceSymbolProvider[] = WorkspaceSymbolProviderRegistry.all()): Promise<WorkspaceSymbolItem[]> {');
    return replaceOnce(contents, 'const promises = WorkspaceSymbolProviderRegistry.all().map(async provider => {', 'const promises = providers.map(async provider => {');
  }
  if (path === paths[1]) {
    let contents = `import { hostOwnsWorkspaceResource } from ${JSON.stringify(join(root, 'src/extensions/targetUris.js'))};\n` + source;
    contents = replaceOnce(contents, 'let lastResultId: number | undefined;', `let lastResultId: number | undefined;
    let active = true;
    const assertActive = (token: CancellationToken) => {
      if (!active || token.isCancellationRequested) throw new CancellationError();
    };
    const assertSymbol = (symbol: search.IWorkspaceSymbol) => {
      if (!hostOwnsWorkspaceResource(this.extHostContext.extensionHost, symbol.location.uri)) {
        throw new Error('工作区符号位置不属于发起宿主的明确视图：' + symbol.location.uri.toString());
      }
    };`);
    contents = replaceOnce(contents, 'const provider: search.IWorkspaceSymbolProvider = {', 'const provider: search.IWorkspaceSymbolProvider = {\n      extensionHost: this.extHostContext.extensionHost,');
    contents = replaceOnce(contents, 'const result = await this._proxy.$provideWorkspaceSymbols(handle, search, token);', `assertActive(token);
        const result = await this._proxy.$provideWorkspaceSymbols(handle, search, token);
        const symbols = MainThreadLanguageFeatures._reviveWorkspaceSymbolDto(result.symbols);
        try {
          assertActive(token);
          symbols.forEach(assertSymbol);
        } catch (error) {
          if (result.cacheId !== undefined) this._proxy.$releaseWorkspaceSymbols(handle, result.cacheId);
          throw error;
        }`);
    contents = replaceOnce(contents, 'return MainThreadLanguageFeatures._reviveWorkspaceSymbolDto(result.symbols);', 'return symbols;');
    contents = replaceOnce(contents, 'const resolvedItem = await this._proxy.$resolveWorkspaceSymbol(handle, item, token);\n\t\t\t\treturn resolvedItem && MainThreadLanguageFeatures._reviveWorkspaceSymbolDto(resolvedItem);', `assertActive(token);
        assertSymbol(item);
        const resolvedItem = await this._proxy.$resolveWorkspaceSymbol(handle, item, token);
        assertActive(token);
        const symbol = resolvedItem && MainThreadLanguageFeatures._reviveWorkspaceSymbolDto(resolvedItem);
        if (symbol) assertSymbol(symbol);
        return symbol;`);
    return replaceOnce(contents, 'this._registrations.set(handle, search.WorkspaceSymbolProviderRegistry.register(provider));', `this._registrations.set(handle, combinedDisposable(search.WorkspaceSymbolProviderRegistry.register(provider), toDisposable(() => {
      active = false;
      if (lastResultId !== undefined) this._proxy.$releaseWorkspaceSymbols(handle, lastResultId);
    })));`);
  }
  if (path === paths[2]) {
    let contents = `import { unpackCommandSource } from ${JSON.stringify(join(root, 'src/extensions/commands.js'))};
import { workspaceSymbolProvidersForHost } from ${JSON.stringify(join(root, 'src/extensions/workspaceSymbols.js'))};\n` + source;
    contents = replaceOnce(contents, 'const [query] = args;\n\tassertType(typeof query', 'const [query, source] = args;\n\tassertType(typeof query');
    return replaceOnce(contents, 'const result = await getWorkspaceSymbols(query);', 'const result = await getWorkspaceSymbols(query, undefined, workspaceSymbolProvidersForHost(unpackCommandSource([source]).source));');
  }
  if (path === paths[3]) {
    let contents = source.replaceAll('skipLocal?: boolean; skipSorting?: boolean', 'skipLocal?: boolean; skipSorting?: boolean; providers?: readonly IWorkspaceSymbolProvider[]');
    return replaceOnce(contents, 'getWorkspaceSymbols(symbolQuery.original, token)', 'getWorkspaceSymbols(symbolQuery.original, token, options?.providers)');
  }
  return source;
}
