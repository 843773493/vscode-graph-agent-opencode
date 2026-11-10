import { join } from 'node:path';

export const extensionWorkspaceSymbolsSourcePaths = [
  'src/vs/workbench/contrib/search/common/search.ts',
  'src/vs/workbench/contrib/search/browser/symbolsQuickAccess.ts',
];

/** 明确范围仍消费原 Registry、聚合与评分，不恢复旧目标宿主归属。 */
export function transformExtensionWorkspaceSymbols(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', extensionWorkspaceSymbolsSourcePaths[0])) {
    let contents = replaceOnce(source, 'token: CancellationToken = CancellationToken.None): Promise<WorkspaceSymbolItem[]> {', 'token: CancellationToken = CancellationToken.None, providers: readonly IWorkspaceSymbolProvider[] = WorkspaceSymbolProviderRegistry.all(), onProviderError?: (provider: IWorkspaceSymbolProvider, error: unknown) => void): Promise<WorkspaceSymbolItem[]> {');
    contents = replaceOnce(contents, 'const promises = WorkspaceSymbolProviderRegistry.all().map(async provider => {', 'const promises = providers.map(async provider => {');
    return replaceOnce(contents, 'onUnexpectedExternalError(err);', 'if (onProviderError) onProviderError(provider, err);\n      else onUnexpectedExternalError(err);');
  }
  if (path === join(root, 'upstream', extensionWorkspaceSymbolsSourcePaths[1])) {
    let contents = "import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';\n" + source;
    contents = replaceOnce(contents, '@IChatWidgetService private readonly chatWidgetService: IChatWidgetService', '@IChatWidgetService private readonly chatWidgetService: IChatWidgetService,\n    @IWorkspaceContextService protected readonly workspaceContextService: IWorkspaceContextService');
    contents = contents.replaceAll('skipLocal?: boolean; skipSorting?: boolean', 'skipLocal?: boolean; skipSorting?: boolean; providers?: readonly IWorkspaceSymbolProvider[]; onProviderError?: (provider: IWorkspaceSymbolProvider, error: unknown) => void');
    return replaceOnce(contents, 'getWorkspaceSymbols(symbolQuery.original, token)', 'getWorkspaceSymbols(symbolQuery.original, token, options?.providers, options?.onProviderError)');
  }
  return source;
}
