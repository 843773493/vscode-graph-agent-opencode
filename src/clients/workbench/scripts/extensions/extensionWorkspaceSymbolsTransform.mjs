import { join } from 'node:path';

export const extensionWorkspaceSymbolsSourcePaths = [
  'src/vs/workbench/contrib/search/common/search.ts',
  'src/vs/workbench/contrib/search/browser/symbolsQuickAccess.ts',
];

/** 明确范围仍消费原 Registry、聚合与评分，不恢复旧目标宿主归属。 */
export function transformExtensionWorkspaceSymbols(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', extensionWorkspaceSymbolsSourcePaths[0])) {
    let contents = replaceOnce(source, 'token: CancellationToken = CancellationToken.None): Promise<WorkspaceSymbolItem[]> {', 'token: CancellationToken = CancellationToken.None, providers: readonly IWorkspaceSymbolProvider[] = WorkspaceSymbolProviderRegistry.all(), onProviderError?: (provider: IWorkspaceSymbolProvider, error: unknown) => void, onProviderResult?: (provider: IWorkspaceSymbolProvider, items: readonly WorkspaceSymbolItem[]) => void): Promise<WorkspaceSymbolItem[]> {');
    contents = replaceOnce(contents, 'const promises = WorkspaceSymbolProviderRegistry.all().map(async provider => {', 'const promises = providers.map(async provider => {');
    contents = replaceOnce(contents, `			if (!value) {
				return;
			}`, `			if (!value) {
        if (!token.isCancellationRequested) onProviderResult?.(provider, []);
				return;
			}`);
    contents = replaceOnce(contents, `			for (const symbol of value) {
				all.push(new WorkspaceSymbolItem(symbol, provider));
			}`, `      const items = value.map(symbol => new WorkspaceSymbolItem(symbol, provider));
      all.push(...items);
      if (!token.isCancellationRequested) onProviderResult?.(provider, items);`);
    contents = replaceOnce(contents, 'onUnexpectedExternalError(err);', 'if (onProviderError) { if (!token.isCancellationRequested) onProviderError(provider, err); }\n      else onUnexpectedExternalError(err);');
    return replaceOnce(contents, '\t// de-duplicate entries', `  return coalesceWorkspaceSymbols(all);
}

/** 增量快照与最终结果共用原排序、去重和 Provider 选择。 */
export function coalesceWorkspaceSymbols(all: readonly WorkspaceSymbolItem[]): WorkspaceSymbolItem[] {
	// de-duplicate entries`);
  }
  if (path === join(root, 'upstream', extensionWorkspaceSymbolsSourcePaths[1])) {
    let contents = "import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';\n" + source;
    contents = replaceOnce(contents, '@IChatWidgetService private readonly chatWidgetService: IChatWidgetService', '@IChatWidgetService private readonly chatWidgetService: IChatWidgetService,\n    @IWorkspaceContextService protected readonly workspaceContextService: IWorkspaceContextService');
    contents = replaceOnce(contents, 'private delayer =', 'protected delayer =');
    contents = replaceOnce(contents, 'protected _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken): Promise<Array<ISymbolQuickPickItem>> {', 'protected _getPicks(filter: string, disposables: DisposableStore, token: CancellationToken): Promise<Array<ISymbolQuickPickItem>> | Array<ISymbolQuickPickItem> {');
    contents = replaceOnce(contents, 'getWorkspaceSymbols, IWorkspaceSymbol, IWorkspaceSymbolProvider', 'getWorkspaceSymbols, IWorkspaceSymbol, IWorkspaceSymbolProvider, WorkspaceSymbolItem');
    contents = contents.replaceAll('skipLocal?: boolean; skipSorting?: boolean', 'skipLocal?: boolean; skipSorting?: boolean; editorGroup?: (side: boolean) => number; openToken?: CancellationToken; providers?: readonly IWorkspaceSymbolProvider[]; onProviderError?: (provider: IWorkspaceSymbolProvider, error: unknown) => void');
    contents = replaceOnce(contents, '\t\t// Split between symbol and container query', `    const symbolQuery = query.values && query.values.length > 1 ? pieceToQuery(query.values[0]) : query;
    const items = await getWorkspaceSymbols(symbolQuery.original, token, options?.providers, options?.onProviderError);
    return this.toSymbolPicks(query, options, token, items);
  }

  protected toSymbolPicks(query: IPreparedQuery, options: { skipLocal?: boolean; skipSorting?: boolean; editorGroup?: (side: boolean) => number; openToken?: CancellationToken } | undefined, token: CancellationToken, workspaceSymbols: readonly WorkspaceSymbolItem[]): ISymbolQuickPickItem[] {
		// Split between symbol and container query`);
    contents = contents.replaceAll('this.openSymbol(provider, symbol, token, { keyMods', 'this.openSymbol(provider, symbol, options?.openToken ?? token, { editorGroup: options?.editorGroup, keyMods');
    contents = replaceOnce(contents, 'options: { keyMods: IKeyMods;', 'options: { editorGroup?: (side: boolean) => number; keyMods: IKeyMods;');
    contents = replaceOnce(contents, 'options?.forceOpenSideBySide ? SIDE_GROUP : ACTIVE_GROUP', 'options?.forceOpenSideBySide ? (options.editorGroup?.(true) ?? SIDE_GROUP) : (options.editorGroup?.(false) ?? ACTIVE_GROUP)');
    return replaceOnce(contents, `		// Run the workspace symbol query
		const workspaceSymbols = await getWorkspaceSymbols(symbolQuery.original, token);
`, '');
  }
  return source;
}
