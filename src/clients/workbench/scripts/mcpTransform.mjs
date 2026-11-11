import { join } from 'node:path';
import ts from 'typescript';
import { mcpWorkbenchViewSourcePaths, transformMcpWorkbenchView } from './mcpWorkbenchViewTransform.mjs';
import { pruneUnusedImportsAndConstants } from './extensions/extensionManagementTransform.mjs';

export const mcpSourcePaths = [
  'src/vs/workbench/api/common/extHost.api.impl.ts',
  'src/vs/workbench/contrib/mcp/browser/mcp.contribution.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpCommands.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpServerActions.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpServerEditor.ts',
  'src/vs/workbench/contrib/mcp/browser/mcpWorkbenchService.ts',
  'src/vs/workbench/contrib/mcp/common/mcpTypes.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceWorkbenchService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagementEditor.ts',
  'src/vs/workbench/contrib/chat/browser/chat.shared.contribution.ts',
  'src/vs/workbench/contrib/chat/common/customizationMarketplaceInstallTelemetry.ts',
  'src/vs/workbench/contrib/chat/browser/agentSessions/agentHost/agentHostCustomizationMarketplaceInstallProvider.ts',
  'src/vs/platform/customizationMarketplace/common/customizationMarketplaceSources.ts',
  'src/vs/platform/customizationMarketplace/electron-browser/customizationMarketplaceService.ts',
  ...mcpWorkbenchViewSourcePaths,
];

function replaceSection(source, from, to, replacement, replaceOnce) {
  const begin = source.indexOf(from), end = source.indexOf(to, begin + from.length);
  if (begin < 0 || end < 0) throw new Error(`上游 MCP 转换边界缺失：${from} / ${to}`);
  return replaceOnce(source, source.slice(begin, end), replacement);
}

function removeNodes(path, source, predicate) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const visit = node => {
    if (predicate(node, file)) {
      edits.push({ start: node.getFullStart(), end: ts.isEnumMember(node) && source[node.end] === ',' ? node.end + 1 : node.end });
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + source.slice(edit.end);
  }
  return source;
}

function removeClassMembers(path, source, className, memberNames) {
  return removeNodes(path, source, (node, file) => ts.isMethodDeclaration(node)
    && ts.isClassDeclaration(node.parent)
    && node.parent.name?.text === className
    && memberNames.includes(node.name.getText(file)));
}

function transformRetiredMcpGallery(relative, source, replaceOnce) {
  let contents = source;
  if (relative.endsWith('/common/mcpTypes.ts')) {
    contents = removeNodes(relative, contents, (node, file) =>
      ts.isMethodSignature(node)
        && ts.isInterfaceDeclaration(node.parent)
        && node.parent.name.text === 'IMcpWorkbenchService'
        && ['queryGallery', 'getMcpServerFromGallery', 'openSearch'].includes(node.name.getText(file))
      || ts.isEnumMember(node)
        && ts.isEnumDeclaration(node.parent)
        && node.parent.name.text === 'McpServerEditorTab'
        && node.name.getText(file) === 'Manifest'
      || ts.isVariableStatement(node)
        && node.declarationList.declarations.some(declaration =>
          ts.isIdentifier(declaration.name) && declaration.name.text === 'McpServersGalleryStatusContext'));
    return pruneUnusedImportsAndConstants(relative, contents);
  }
  if (relative.endsWith('/mcpWorkbenchService.ts')) {
    const classStart = contents.indexOf('export class MCPContextsInitialisation');
    if (classStart < 0) throw new Error('上游 MCP 初始上下文贡献缺失');
    const header = contents.slice(0, contents.indexOf('import '));
    contents = header + `import { Disposable } from '../../../../base/common/lifecycle.js';
import { IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { IWorkbenchContribution } from '../../../common/contributions.js';
import { HasInstalledMcpServersContext, IMcpWorkbenchService } from '../common/mcpTypes.js';

export class MCPContextsInitialisation extends Disposable implements IWorkbenchContribution {
	static ID = 'workbench.mcp.contexts.initialisation';

	constructor(
		@IMcpWorkbenchService mcpWorkbenchService: IMcpWorkbenchService,
		@IContextKeyService contextKeyService: IContextKeyService,
	) {
		super();
		const hasInstalledMcpServersContextKey = HasInstalledMcpServersContext.bindTo(contextKeyService);
		mcpWorkbenchService.whenInitialLocalMcpServersLoaded.then(() => {
			hasInstalledMcpServersContextKey.set(mcpWorkbenchService.local.length > 0);
			this._register(mcpWorkbenchService.onChange(() => hasInstalledMcpServersContextKey.set(mcpWorkbenchService.local.length > 0)));
		});
	}
}
`;
    return contents;
  }
  if (relative.endsWith('/aiCustomization/mcpListWidget.ts')) {
    contents = removeNodes(relative, contents, (node, file) =>
      ts.isExpressionStatement(node)
        && ts.isCallExpression(node.expression)
        && node.expression.expression.getText(file) === 'this._register'
        && node.expression.arguments[0]?.getText(file).startsWith('mcpGalleryManifestService.onDidChangeMcpGalleryManifest(')
      || ts.isIfStatement(node)
        && node.expression.getText(file) === '!this.isGalleryDiscoveryEnabled()'
        && node.thenStatement.getText(file).includes('mcpGalleryManifestService.getMcpGalleryManifest()')
      || ts.isFunctionDeclaration(node)
        && ['shouldLoadMcpGallerySnapshot', 'shouldShowLegacyMcpGallery'].includes(node.name?.text));
    contents = replaceOnce(contents, "import { IMcpGalleryManifestService } from '../../../../../platform/mcp/common/mcpGalleryManifest.js';\n", '');
    contents = replaceOnce(contents, '\t\t@IMcpGalleryManifestService private readonly mcpGalleryManifestService: IMcpGalleryManifestService,\n', '');
    contents = contents.replaceAll('this.delayedGallerySearch.cancel();\n', '');
    contents = contents.replaceAll('\t\t\tthis.galleryCts?.dispose(true);\n\t\t\tthis.galleryCts = undefined;\n', '');
    contents = contents.replaceAll('\t\t\tthis.galleryCts?.dispose(true);\n', '');
    contents = contents.replaceAll('\t\t\tthis.galleryCts = undefined;\n', '');
    contents = contents.replaceAll('\t\t\tthis.gallerySnapshotLoading = false;\n', '');
    contents = contents.replaceAll('\t\t\tthis.gallerySearchLoading = false;\n', '');
    contents = replaceSection(contents,
      '\t\tthis._register(this.searchInput.onDidChange(() => {',
      '\n\n\t\t// Empty state',
      `\t\tthis._register(this.searchInput.onDidChange(() => {
			this.searchQuery = this.searchInput.value;
			this.searchInput.hideMessage();
			this.delayedFilter.trigger(() => this.filterServers());
		}));`, replaceOnce);
    contents = replaceSection(contents,
      '\tprivate async refresh(): Promise<void> {',
      '\n\tprivate updateDiscoveryMessage(): void {',
      `\tprivate async refresh(): Promise<void> {
		this.updateDiscoveryMessage();
		this.filterServers();
	}
`, replaceOnce);
    contents = replaceOnce(contents, '\t\t\tif (this.searchQuery.trim() && !this.isGalleryDiscoveryEnabled()) {\n\t\t\t\tvoid this.queryMcpSearch();\n\t\t\t} else {\n\t\t\t\tvoid this.refresh();\n\t\t\t}', '\t\t\tvoid this.refresh();');
    contents = replaceSection(contents,
      '\tprivate isGalleryDiscoveryEnabled(): boolean {',
      '\n\tsetVisible(visible: boolean): void {', '', replaceOnce);
    contents = replaceSection(contents,
      '\tpublic showBrowseMarketplace(): void {',
      '\n\tprivate addSurfaceActivation(', '', replaceOnce);
    contents = replaceSection(contents,
      '\tprivate getAvailableGalleryServers(): IWorkbenchMcpServer[] {',
      '\n\tprivate renderMcpTree(): void {', '', replaceOnce);
    contents = contents.replace(/^\s*this\.(?:gallerySnapshotServers|galleryServers|gallerySnapshotFailed|gallerySnapshotLoading|gallerySearchLoading)\s*=.*\n/gm, '');
    contents = replaceOnce(contents, '\t\tconst showGallery = !this.isGalleryDiscoveryEnabled();\n', '');
    contents = replaceOnce(contents, "\t\t\t['available', showGallery ? this.getAvailableGalleryServers().map(server => ({ type: 'marketplace-item', server })) : []],\n", '');
    contents = replaceOnce(contents,
      "\t\t\t{ id: 'available', label: localize('availableMcpServersSection', \"Available\"), description: localize('availableMcpServersSectionDescription', \"Browse and install MCP servers from the marketplace.\"), icon: Codicon.globe },\n", '');
    contents = replaceOnce(contents,
      `\t\t].filter(group => group.id === 'available'
			? showGallery
			: group.id === 'user' || group.id === 'workspace' || isAICustomizationWorkspaceGroupKey(group.id) || grouped.get(group.id)!.length > 0);`,
      `\t\t].filter(group => group.id === 'user' || group.id === 'workspace' || isAICustomizationWorkspaceGroupKey(group.id) || grouped.get(group.id)!.length > 0);`);
    contents = replaceOnce(contents,
      `\t\t\tthis.emptyText.textContent = this.gallerySearchLoading
				? localize('searchingMcpMarketplace', "Searching the MCP marketplace...")
				: this.searchQuery.trim()
					? localize('noMatchingServers', "No servers match '{0}'", this.searchQuery)
					: localize('noMcpServersInGroup', "No MCP servers in this group");
			this.emptySubtext.textContent = this.gallerySearchLoading || !this.searchQuery.trim() ? '' : localize('tryDifferentSearch', "Try a different search term");`,
      `\t\t\tthis.emptyText.textContent = this.searchQuery.trim()
				? localize('noMatchingServers', "No servers match '{0}'", this.searchQuery)
				: localize('noMcpServersInGroup', "No MCP servers in this group");
			this.emptySubtext.textContent = this.searchQuery.trim() ? localize('tryDifferentSearch', "Try a different search term") : '';`);
    contents = replaceOnce(contents,
      "\t\treturn entry.type === 'marketplace-item' ? `marketplace:${entry.server.id}` : getMcpRowKey(entry);",
      '\t\treturn getMcpRowKey(entry);');
    contents = replaceSection(contents,
      '\tprotected appendMarketplaceServerRow(parent: HTMLElement, server: IWorkbenchMcpServer): void {',
      '\n\tprivate getInstalledEntryDescription(entry: IMcpInstalledEntry): string {', '', replaceOnce);
    contents = contents.replaceAll("\t\t\t\t\tcase 'marketplace-item':\n", '');
    contents = contents.replaceAll("\t\t\t\tcase 'marketplace-item':\n", '');
    contents = replaceOnce(contents, '\t\tconst marketplaceRenderer = new McpMarketplaceItemRenderer((server, button) => this.installMarketplaceServer(server, button));\n', '');
    contents = replaceOnce(contents, '\t\t\t\tasTreeRenderer(marketplaceRenderer),\n', '');
    contents = replaceOnce(contents, "\t\t\t\t\t\t\t: entry.type === 'marketplace-item'\n\t\t\t\t\t\t\t\t? localize('marketplaceMcpServerRowAriaLabel', \"{0}. Available to install from the MCP marketplace.\", entry.server.label)\n\t\t\t\t\t\t\t\t: this.getMcpEntryAriaLabel(entry);", '\t\t\t\t\t\t\t: this.getMcpEntryAriaLabel(entry);');
    contents = replaceOnce(contents, "\t\t\tif (entry.type !== 'marketplace-item' && isConnectorMcpEntry(entry)) {", '\t\t\tif (isConnectorMcpEntry(entry)) {');
    contents = replaceOnce(contents,
      "\t\t\tthis._onDidSelectServer.fire(entry.type === 'marketplace-item'\n\t\t\t\t? createWorkbenchMcpServerDetailInput(entry.server)\n\t\t\t\t: this.createInstalledMcpServerDetailInput(entry));",
      '\t\t\tthis._onDidSelectServer.fire(this.createInstalledMcpServerDetailInput(entry));');
    contents = replaceOnce(contents,
      '\t\t// Marketplace (gallery) entries are always clickable so users can install/inspect them,\n\t\t// even when no description is returned by the gallery. Installed rows only opt-in to the\n\t\t// detail view when there is something extra to show.\n\t\tconst isGallery = !element.server.local;\n\t\tconst hasDetail = !!description || isGallery;',
      '\t\tconst hasDetail = !!description;');
    contents = replaceOnce(contents, "\t\t\tif (event.element && event.element.type !== 'group-header' && event.element.type !== 'marketplace-item') {", "\t\t\tif (event.element && event.element.type !== 'group-header') {");
    contents = replaceOnce(contents, "\t\t\titemRenderer.setFocusedRowKey(entry && entry.type !== 'group-header' && entry.type !== 'marketplace-item' ? getMcpRowKey(entry) : undefined);\n\t\t\tmarketplaceRenderer.setFocusedServerId(entry?.type === 'marketplace-item' ? entry.server.id : undefined);", "\t\t\titemRenderer.setFocusedRowKey(entry && entry.type !== 'group-header' ? getMcpRowKey(entry) : undefined);");
    contents = replaceSection(contents,
      'interface IMcpMarketplaceItemTemplateData {',
      '\nfunction createMcpStartButton(', '', replaceOnce);
    contents = replaceOnce(contents,
      "interface IMcpMarketplaceEntry {\n\treadonly type: 'marketplace-item';\n\treadonly server: IWorkbenchMcpServer;\n}\n\n", '');
    contents = replaceOnce(contents, 'type IMcpSectionEntry = IMcpGroupHeaderEntry | IMcpInstalledEntry | IMcpMarketplaceEntry;', 'type IMcpSectionEntry = IMcpGroupHeaderEntry | IMcpInstalledEntry;');
    contents = replaceOnce(contents, "\t\treturn element.type === 'marketplace-item' ? MCP_MARKETPLACE_ITEM_HEIGHT : this.getInstalledHeight(element);", '\t\treturn this.getInstalledHeight(element);');
    contents = replaceOnce(contents, "\t\treturn element.type === 'marketplace-item' ? 'mcpMarketplaceItem' : 'mcpServerItem';", "\t\treturn 'mcpServerItem';");
    contents = replaceOnce(contents, 'const MCP_MARKETPLACE_ITEM_HEIGHT = 58;\n', '');
    contents = replaceSection(contents,
      '\t/**\n\t * Whether this entry originates from a marketplace browse result.',
      '\treadonly marketplaceRecord?: IRecordedCustomizationMarketplaceResource;', '', replaceOnce);
    contents = replaceOnce(contents,
      '\t\t\treturn `server:${entry.server.id}:${entry.marketplace ? 1 : 0}`;',
      '\t\t\treturn `server:${entry.server.id}`;');
    contents = replaceOnce(contents,
      '\t\t@ITelemetryService private readonly telemetryService: ITelemetryService,\n', '');
    contents = contents.replaceAll('\tprivate gallerySnapshotServers: IWorkbenchMcpServer[] = [];\n', '');
    contents = contents.replaceAll('\tprivate galleryServers: IWorkbenchMcpServer[] = [];\n', '');
    contents = contents.replaceAll('\tprivate gallerySnapshotFailed = false;\n', '');
    contents = contents.replaceAll('\tprivate gallerySnapshotLoading = false;\n', '');
    contents = contents.replaceAll('\tprivate gallerySearchLoading = false;\n', '');
    contents = contents.replaceAll('\tprivate galleryCts: CancellationTokenSource | undefined;\n', '');
    contents = contents.replaceAll('\tprivate readonly delayedGallerySearch = new Delayer<void>(400);\n', '');
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/customizationMarketplaceSources.ts')) {
    contents = replaceOnce(contents, "import { mcpGalleryServiceUrlConfig } from '../../mcp/common/mcpManagement.js';\n", '');
    contents = replaceSection(contents,
      '\tMcpGallery: {',
      '\tAgentFinderPublicFeed: {', '', replaceOnce);
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/customizationMarketplaceWorkbenchService.ts')) {
    contents = replaceOnce(contents, "type CustomizationMarketplaceBrowseSourceCategory = 'mcpGallery' | ", "type CustomizationMarketplaceBrowseSourceCategory = ");
    contents = replaceSection(contents,
      "\tif (sourceId === CustomizationMarketplaceSources.McpGallery.id) {",
      "\tif (sourceId === CustomizationMarketplaceSources.CopilotConnectors.id) {", '', replaceOnce);
    contents = replaceSection(contents,
      'export class PlatformCustomizationMarketplaceWorkbenchService implements ICustomizationMarketplaceService {',
      '\nexport class CustomizationMarketplaceWorkbenchService',
      `export class PlatformCustomizationMarketplaceWorkbenchService implements ICustomizationMarketplaceService {
	declare readonly _serviceBrand: undefined;
	readonly allSources: readonly ICustomizationMarketplaceSourceInfo[] = [];
	readonly sources = this.allSources;
	private readonly service: Lazy<CustomizationMarketplaceService>;

	constructor(@IConfigurationService private readonly configurationService: IConfigurationService) {
		this.service = new Lazy(() => new CustomizationMarketplaceService([]));
	}

	query(options: ICustomizationMarketplaceQuery, token: CancellationToken, onProgress?: CustomizationMarketplaceQueryProgressCallback): Promise<ICustomizationMarketplacePage> {
		return queryEnabledCustomizationMarketplaceSources(
			this.configurationService, this.sources, options, token,
			(request, token, onProgress) => this.service.value.query(request, token, onProgress),
			onProgress,
		);
  }
}
`, replaceOnce);
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/electron-browser/customizationMarketplaceService.ts')) {
    contents = replaceSection(contents,
      'export class NativeCustomizationMarketplaceService implements ICustomizationMarketplaceService {',
      '\nregisterSingleton(IPlatformCustomizationMarketplaceService',
      `export class NativeCustomizationMarketplaceService implements ICustomizationMarketplaceService {
	declare readonly _serviceBrand: undefined;
	readonly allSources: readonly ICustomizationMarketplaceSourceInfo[] = [];
	readonly sources = this.allSources;
	private readonly service: Lazy<CustomizationMarketplaceService>;

	constructor(@IConfigurationService private readonly configurationService: IConfigurationService) {
		this.service = new Lazy(() => new CustomizationMarketplaceService([]));
	}

	query(options: ICustomizationMarketplaceQuery, token: CancellationToken, onProgress?: CustomizationMarketplaceQueryProgressCallback): Promise<ICustomizationMarketplacePage> {
		return queryEnabledCustomizationMarketplaceSources(
			this.configurationService, this.sources, options, token,
			(request, token, onProgress) => this.service.value.query(request, token, onProgress),
			onProgress,
		);
	}
}
`, replaceOnce);
    contents = replaceOnce(contents,
      'CustomizationMarketplaceQueryProgressCallback, CustomizationMarketplaceService, ICustomizationMarketplacePage, ICustomizationMarketplaceQuery, ICustomizationMarketplaceService',
      'CustomizationMarketplaceQueryProgressCallback, CustomizationMarketplaceService, ICustomizationMarketplacePage, ICustomizationMarketplaceQuery, ICustomizationMarketplaceService, ICustomizationMarketplaceSourceInfo');
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/customizationMarketplaceInstallService.ts')) {
    contents = replaceOnce(contents,
      "import { IMcpGalleryManifest, IMcpGalleryManifestService } from '../../../../../platform/mcp/common/mcpGalleryManifest.js';\n", '');
    contents = replaceOnce(contents,
      '\t\t@IMcpGalleryManifestService private readonly mcpGalleryManifestService: IMcpGalleryManifestService,\n', '');
    for (const snippet of [
      "\t\tif (!installedSource && resourceSource?.kind === 'mcpGallery' && installed.mediaType === CustomizationMarketplaceMediaType.McpServer) {\n\t\t\treturn installed.identifier === resourceSource.name;\n\t\t}\n",
      "\t\tif (source?.kind === 'mcpGallery') {\n\t\t\treturn source.cardUrl;\n\t\t}\n",
    ]) {
      if (contents.includes(snippet)) contents = replaceOnce(contents, snippet, '');
    }
    contents = replaceOnce(contents,
      "\t\tconst itemUrl = installation?.kind === 'providerCatalog'\n\t\t\t? installation.itemUrl\n\t\t\t: installation?.kind === 'mcpGallery' ? installation.cardUrl : undefined;",
      "\t\tconst itemUrl = installation?.kind === 'providerCatalog' ? installation.itemUrl : undefined;");
    contents = replaceOnce(contents,
      "\t\t\t\tconst installed = this.mcpWorkbenchService.local.find(server => server.id === target.id && server.installState === McpServerInstallState.Installed)\n\t\t\t\t\t?? (record.installation.kind === 'mcpGallery' ? this.getInstalledGalleryMcpServer(record.installation) : undefined);",
      "\t\t\t\tconst installed = this.mcpWorkbenchService.local.find(server => server.id === target.id && server.installState === McpServerInstallState.Installed);");
    contents = replaceOnce(contents,
      "\t\tif (association.target.kind === 'mcp') {\n\t\t\treturn source?.kind === 'mcpGallery' && !!this.getInstalledGalleryMcpServer(source);\n\t\t}\n",
      "\t\tif (association.target.kind === 'mcp') {\n\t\t\treturn false;\n\t\t}\n");
    contents = replaceOnce(contents, "kind === 'skill' || kind === 'mcp' || kind === 'mcpGallery' ||", "kind === 'skill' || kind === 'mcp' ||");
    contents = replaceOnce(contents, "installation.kind === 'skill' || installation.kind === 'mcp' || installation.kind === 'mcpGallery' ||", "installation.kind === 'skill' || installation.kind === 'mcp' ||");
    contents = replaceSection(contents,
      '\t\tconst ownerAssociation = this.findRecord(resource);',
      '\t\tconst recorded = providerBinding ? undefined : this.installations.get().findByResource(resource);', '', replaceOnce);
    contents = replaceSection(contents,
      '\t\tconst record = this.findRecord(resource);\n\t\tif (!record) {',
      '\n\t}\n\n\tprivate async doUninstall',
      `\t\tthrow new Error(localize('customizationMarketplace.uninstallUnavailable', "This customization has no supported uninstall owner."));\n`, replaceOnce);
    contents = removeClassMembers(relative, contents, 'CustomizationMarketplaceInstallService', [
      'doUninstall', 'getInstalledGalleryMcpServer', 'isMcpGallerySourceCurrent', 'getMcpGalleryManifest',
      'isOwnerAssociationCurrent', 'associateInstalledResource',
    ]);
    contents = removeNodes(relative, contents, (node, file) =>
      ts.isIfStatement(node) && node.expression.getText(file) === "source.kind === 'mcpGallery'"
      || ts.isTypeAliasDeclaration(node) && node.name.text === 'McpGalleryInstallation');
    contents = replaceSection(contents,
      '\t\toperationDisposables.add(this.configurationService.onDidChangeConfiguration(event => {',
      '\n\t\tconst operation = runCustomizationMarketplaceInstallWithTelemetry(',
      `\t\toperationDisposables.add(this.configurationService.onDidChangeConfiguration(event => {
\t\t\tif (!this.isSourceEnabled(resource.sourceId)) {
\t\t\t\toperationDisposables.dispose();
\t\t\t}
\t\t}));
`, replaceOnce);
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/aiCustomizationManagementEditor.ts')) {
    contents = replaceOnce(contents,
      "import { normalizeMcpGalleryUrl } from '../../../../../platform/customizationMarketplace/common/mcpGalleryMarketplaceProvider.js';\n", '');
    contents = replaceOnce(contents,
      "\t\tconst sourceIds = (githubRegistry\n\t\t\t? [CustomizationMarketplaceSources.AgentFinderPublicFeed.id, CustomizationMarketplaceSources.McpGallery.id]\n\t\t\t: [CustomizationMarketplaceSources.McpGallery.id])\n\t\t\t.filter(id => sources.some(source => source.id === id));",
      "\t\tconst sourceIds = githubRegistry && sources.some(source => source.id === CustomizationMarketplaceSources.AgentFinderPublicFeed.id)\n\t\t\t? [CustomizationMarketplaceSources.AgentFinderPublicFeed.id]\n\t\t\t: [];");
    contents = replaceSection(contents,
      "\t\t\t\t\tif (sourceId === CustomizationMarketplaceSources.McpGallery.id) {",
      '\n\t\t\t\t});',
      `\t\t\t\t\treturn (getGitHubMcpRegistryResourceIdentity(item.identifier)
						?? getGitHubMcpRegistryIdentity(installation?.kind === 'providerCatalog' ? installation.itemUrl : undefined)) === name;`, replaceOnce);
    contents = replaceOnce(contents,
      '\t\t\t\tcursor = sourceId === CustomizationMarketplaceSources.McpGallery.id ? page.nextCursor : undefined;',
      '\t\t\t\tcursor = undefined;');
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/chat.shared.contribution.ts')) {
    contents = replaceSection(contents,
      '\t\t[mcpGalleryServiceEnablementConfig]: {',
      '\t\t[PromptsConfig.INSTRUCTIONS_LOCATION_KEY]: {', '', replaceOnce);
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/customizationMarketplaceInstallTelemetry.ts')) {
    contents = replaceOnce(contents, "export type AvailableCustomizationMarketplaceInstallType = 'mcpServer' | 'plugin';", "export type AvailableCustomizationMarketplaceInstallType = 'plugin';");
    contents = replaceOnce(contents,
      "\treturn customizationType === 'mcpServer'\n\t\t? { surface: 'available', customizationType, installKind: 'mcpGallery', origin: 'search' }\n\t\t: { surface: 'available', customizationType, installKind: 'configuredPlugin', origin: 'search' };",
      "\treturn { surface: 'available', customizationType, installKind: 'configuredPlugin', origin: 'search' };");
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  if (relative.endsWith('/agentHostCustomizationMarketplaceInstallProvider.ts')) {
    contents = replaceOnce(contents,
      "\t\tif (installation?.kind === 'mcpGallery' && !installation.cardUrl) {\n\t\t\treturn localize('agentHost.customizationInstall.mcpGalleryIdentityUnavailable', \"This MCP Gallery entry does not provide the server card URL required for Copilot installation.\");\n\t\t}\n", '');
    contents = replaceSection(contents,
      "\t\tif (installation?.kind === 'mcpGallery') {",
      "\t\tif (installation?.kind !== 'skill' && installation?.kind !== 'mcp') {", '', replaceOnce);
    contents = replaceOnce(contents,
      "\t\t\tif (installation?.kind !== 'providerCatalog' && installation?.kind !== 'mcpGallery') {",
      "\t\t\tif (installation?.kind !== 'providerCatalog') {");
    return pruneUnusedImportsAndConstants(relative, contents);
  }

  return contents;
}

/** 固定上游保留原 UI／命令；从注册点移除其项目 MCP 的第二 client／配置发现链。 */
export function transformMcp(root, path, source, replaceOnce) {
  const relative = mcpSourcePaths.find(candidate => path === join(root, 'upstream', candidate));
  if (!relative) return source;
  const serverImport = `import { WorkspaceMcpServer } from ${JSON.stringify(join(root, 'src/mcp/server.js'))};\n`;
  let contents = source;
  if (relative.endsWith('/api/common/extHost.api.impl.ts')) {
    contents = replaceOnce(contents,
      '\t\t\tregisterMcpServerDefinitionProvider(id, provider) {\n\t\t\t\treturn extHostMcp.registerMcpConfigurationProvider(extension, id, provider);\n\t\t\t},',
      '\t\t\tregisterMcpServerDefinitionProvider(id, _provider) {\n\t\t\t\tthrow new Error(`MCP Provider "${id}" 已停用，请通过目标插件包管理器配置服务`);\n\t\t\t},');
  }
  if (relative.endsWith('/mcp.contribution.ts')) {
    contents = replaceOnce(contents, "import { McpService } from '../common/mcpService.js';", `import { WorkspaceMcpService, WorkspaceMcpWorkbenchService } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};`);
    contents = replaceOnce(contents, "import { MCPContextsInitialisation, McpWorkbenchService } from './mcpWorkbenchService.js';", "import { MCPContextsInitialisation } from './mcpWorkbenchService.js';");
    contents = replaceOnce(contents, 'registerSingleton(IMcpService, McpService, InstantiationType.Delayed);', 'registerSingleton(IMcpService, WorkspaceMcpService, InstantiationType.Delayed);');
    contents = replaceOnce(contents, 'registerSingleton(IMcpWorkbenchService, McpWorkbenchService, InstantiationType.Eager);', 'registerSingleton(IMcpWorkbenchService, WorkspaceMcpWorkbenchService, InstantiationType.Eager);');
    contents = replaceSection(contents, 'mcpDiscoveryRegistry.register(new SyncDescriptor(RemoteNativeMpcDiscovery));', "registerWorkbenchContribution2('mcpContextKeys'", '', replaceOnce);
    for (const line of [
      "registerWorkbenchContribution2('mcpResourceFilesystem', McpResourceFilesystem, WorkbenchPhase.BlockRestore);",
      'registerWorkbenchContribution2(McpLanguageModelToolContribution.ID, McpLanguageModelToolContribution, WorkbenchPhase.AfterRestored);',
      "registerWorkbenchContribution2('mcpGatewayToolBrokerRemote', McpGatewayToolBrokerContribution, WorkbenchPhase.AfterRestored);",
      "registerWorkbenchContribution2('mcpActionRendering', MCPServerActionRendering, WorkbenchPhase.BlockRestore);",
      "registerWorkbenchContribution2('mcpAddContext', McpAddContextContribution, WorkbenchPhase.Eventually);",
      'registerWorkbenchContribution2(McpConfigMigrationContribution.ID, McpConfigMigrationContribution, WorkbenchPhase.Eventually);',
    ]) contents = replaceOnce(contents, line, '');
    for (const module of [
      '../common/discovery/extensionMcpDiscovery.js', '../common/discovery/installedMcpServersDiscovery.js',
      '../common/discovery/mcpDiscovery.js', '../common/discovery/nativeMcpRemoteDiscovery.js',
      '../common/discovery/pluginMcpDiscovery.js', '../common/discovery/workspaceMcpDiscoveryAdapter.js',
      '../common/discovery/workspaceDotMcpDiscovery.js', '../common/mcpLanguageModelToolContribution.js',
      '../common/mcpResourceFilesystem.js', './mcpGatewayToolBrokerContribution.js', './mcpAddContextContribution.js',
      './mcpDiscovery.js', './mcpMigration.js',
    ]) {
      const line = contents.split('\n').find(line => line.startsWith('import ') && line.endsWith(`from '${module}';`));
      if (!line) throw new Error(`上游 MCP import 锚点缺失：${module}`);
      contents = replaceOnce(contents, line + '\n', '');
    }
    contents = replaceOnce(contents, 'McpConfirmationServerOptionsCommand, MCPServerActionRendering, McpServerOptionsCommand', 'McpConfirmationServerOptionsCommand, McpServerOptionsCommand');
  }
  if (relative.endsWith('/mcpCommands.ts')) {
    // 原命令 ID 保留；尚未接入的写行为在命令 seam 拒绝，不能绕到原配置／凭据 owner。
    for (const [name, next] of [
      ['McpAgentHostServerOptionsCommand', 'McpConfirmationServerOptionsCommand'],
      ['McpConfirmationServerOptionsCommand', 'McpServerOptionsCommand'],
      ['InstallFromManifestAction', 'RemoveStoredInput'],
      ['RemoveStoredInput', 'EditStoredInput'], ['EditStoredInput', 'SetOAuthClientSecret'],
      ['SetOAuthClientSecret', 'ShowConfiguration'], ['ShowConfiguration', 'ShowOutput'],
      ['OpenUserMcpResourceCommand', 'OpenRemoteUserMcpResourceCommand'],
      ['OpenRemoteUserMcpResourceCommand', 'OpenWorkspaceFolderMcpResourceCommand'],
      ['OpenWorkspaceFolderMcpResourceCommand', 'OpenWorkspaceMcpResourceCommand'],
      ['OpenWorkspaceMcpResourceCommand', 'McpBrowseResourcesCommand'],
      ['McpConfigureSamplingModels', 'McpStartPromptingServerCommand'],
      ['McpStartPromptingServerCommand', 'McpSkipCurrentAutostartCommand'],
    ]) {
      const begin = contents.indexOf(`export class ${name} extends Action2 {`), end = contents.indexOf(`export class ${next} extends Action2 {`, begin);
      const original = contents.slice(begin, end);
      const run = original.search(/\n\t(?:override )?(?:async )?run\(/);
      if (begin < 0 || end < 0 || run < 0) throw new Error(`上游 MCP 命令 seam 缺失：${name}`);
      contents = replaceOnce(contents, original, original.slice(0, run) + '\n\toverride async run(): Promise<never> { throw unavailableWorkspaceMcpWrite(); }\n}\n\n');
    }
    const addBegin = contents.indexOf('export class AddConfigurationAction extends Action2 {');
    const addEnd = contents.indexOf('export class InstallFromManifestAction extends Action2 {', addBegin);
    const originalAdd = contents.slice(addBegin, addEnd);
    if (addBegin < 0 || addEnd < 0 || !originalAdd.includes('\n\tasync run(')) throw new Error('上游 MCP Add 命令 seam 缺失');
    // 项目 MCP 不依赖原 Chat setup；旧文件菜单尚无精确 writer，不提供换来源的假动作。
    contents = replaceOnce(contents, originalAdd, `export class AddConfigurationAction extends Action2 {
  constructor() { super({ id: McpCommandIds.AddConfiguration, title: localize2('mcp.addConfiguration', 'Add Server...'), category, f1: true,
    metadata: { description: localize2('mcp.addConfiguration.description', 'Adds a plugin service or external HTTP server to the selected workspace configuration') } }); }
  override run(accessor: ServicesAccessor, configUri?: URI | string): Promise<void> { return addWorkspaceMcpConfiguration(accessor, configUri); }
}

`);
    const listBegin = contents.indexOf('export class ListMcpServerCommand extends Action2 {');
    const listRun = contents.indexOf('\n\toverride async run(', listBegin);
    if (listBegin < 0 || listRun < 0) throw new Error('上游 MCP List 注册 seam 缺失');
    const listRegistration = contents.slice(listBegin, listRun);
    contents = replaceOnce(contents, listRegistration, replaceOnce(listRegistration,
      '\t\t\tprecondition: ContextKeyExpr.and(ChatContextKeys.Setup.hidden.negate(), ChatContextKeys.Setup.disabledInWorkspace.negate()),\n', ''));
    contents = replaceSection(contents, '\tprivate async _runWithMode(', '\tprivate async _runLocal(', '', replaceOnce);
    contents = replaceSection(contents, '\tprivate async _runAgentHost(', 'interface IListMcpServerServices', '}\n\n', replaceOnce);
    contents = replaceSection(contents, "type AgentHostMcpServerLifecycleAction = 'start' | 'stop';", 'export class McpAgentHostServerOptionsCommand', '', replaceOnce);
    contents = replaceOnce(contents, '\t\t\tchatWidgetService: accessor.get(IChatWidgetService),\n\t\t\tagentHostCustomizations: accessor.get(IAgentHostCustomizationService),\n', '');
    contents = replaceOnce(contents, '\treadonly chatWidgetService: IChatWidgetService;\n\treadonly agentHostCustomizations: IAgentHostCustomizationService;\n', '');
    contents = replaceOnce(contents, 'return this._runWithMode(services, undefined);', 'return this._runLocal(services);');
    contents = replaceOnce(contents, '\t\tconst store = new DisposableStore();\n\t\tconst pick = quickInput.createQuickPick<ItemType>', '\t\tawait mcpService.activateCollections();\n\t\tconst store = new DisposableStore();\n\t\tif (mcpService instanceof WorkspaceMcpService) store.add(mcpService.observe());\n\t\tconst pick = quickInput.createQuickPick<ItemType>');
    contents = replaceOnce(contents, '\t\tmcpService.activateCollections();\n\n\t\tstore.add(pick);', '\t\tstore.add(pick);');
    contents = replaceOnce(contents, 'mcpService.servers.read(reader).slice().sort(', 'mcpService.servers.read(reader).filter(server => !(mcpService instanceof WorkspaceMcpService) || mcpService.local.includes(server as WorkspaceMcpServer)).sort(');
    contents = replaceOnce(contents, 'if (firstRun && pick.items.length > 3)', 'if (firstRun && pick.items.length > 2)');
    contents = replaceOnce(contents, 'commandService.executeCommand(McpCommandIds.AddConfiguration);', 'await commandService.executeCommand(McpCommandIds.AddConfiguration);');
    contents = replaceOnce(contents, 'commandService.executeCommand(McpCommandIds.ServerOptions, picked.id);', 'await commandService.executeCommand(McpCommandIds.ServerOptions, picked.id);');
    contents = replaceSection(contents, 'export class McpServerOptionsCommand extends Action2 {', 'export class MCPServerActionRendering', `export class McpServerOptionsCommand extends Action2 {
  constructor() { super({ id: McpCommandIds.ServerOptions, title: localize2('mcp.options', 'Server Options'), category, f1: false }); }
  override run(accessor: ServicesAccessor, id: string): Promise<void> { return showWorkspaceMcpOptions(accessor, id); }
}

`, replaceOnce);
    contents = replaceSection(contents, 'export class MCPServerActionRendering', 'export class ResetMcpTrustCommand', '', replaceOnce);
    contents = replaceSection(contents, 'interface IAgentHostMcpServerCommandArg {', 'export class RestartServer', '', replaceOnce);
    contents = replaceSection(contents, 'async function openMcpConfigurationFile(', '/** Options accepted by', '', replaceOnce);
    for (const [name, next, action] of [
      ['ShowOutput', 'RestartServer', 'output'], ['RestartServer', 'StartServer', 'restart'],
      ['StartServer', 'StopServer', 'start'], ['StopServer', 'McpBrowseCommand', 'stop'],
    ]) {
      const begin = contents.indexOf(`export class ${name} extends Action2 {`), end = contents.indexOf(`export class ${next} extends Action2 {`, begin);
      const original = contents.slice(begin, end), run = original.search(/\n\t(?:override )?(?:async )?run\(/);
      if (begin < 0 || end < 0 || run < 0) throw new Error(`上游 MCP 生命周期命令 seam 缺失：${name}`);
      contents = replaceOnce(contents, original, original.slice(0, run) + `\n\toverride run(accessor: ServicesAccessor, serverId: unknown, options?: { waitForLiveTools?: boolean }): Promise<void> { return runWorkspaceMcpCommand(accessor, serverId, '${action}', options); }\n}\n\n`);
    }
    contents = replaceOnce(contents, "\t\tconst extensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);\n\t\tconst viewsService = accessor.get(IViewsService);\n\t\tawait extensionsWorkbenchService.openSearch('');\n\t\tawait viewsService.openView(InstalledMcpServersViewId, true);", "\t\tawait accessor.get(IExtensionsWorkbenchService).openSearch('@mcp');");
    contents = replaceOnce(contents, '\t\tawait accessor.get(ICommandService).executeCommand(\n\t\t\tAICustomizationManagementCommands.OpenMarketplace,\n\t\t\tAICustomizationManagementSection.McpServers,\n\t\t);', "\t\tawait accessor.get(IMcpWorkbenchService).openSearch('');");
    contents = contents.replaceAll("await accessor.get(IMcpWorkbenchService).openSearch('');", "await accessor.get(IExtensionsWorkbenchService).openSearch('@mcp');");
    contents = replaceOnce(contents, "McpConnectionState.toString(server.connectionState.read(reader)),", "server instanceof WorkspaceMcpServer ? server.statusLabel : McpConnectionState.toString(server.connectionState.read(reader)),");
    // 下线的写链与第二 client 依赖不进入本运行面；保留原列表和仍开放命令需要的导入。
    const commandImports = new Set([
      'Codicon', 'ThemeIcon', 'URI', 'groupBy', 'DisposableStore', 'autorun', 'ILocalizedString', 'localize', 'localize2',
      'Action2', 'MenuId', 'MenuRegistry', 'ICommandService', 'IExtensionsWorkbenchService', 'ContextKeyExpr', 'IInstantiationService',
      'ServicesAccessor', 'ILogService', 'mcpAutoStartConfig', 'McpAutoStartValue', 'INotificationService',
      'IQuickInputService', 'IQuickPickItem', 'IQuickPickSeparator', 'RemoteNameContext',
      'WorkbenchStateContext', 'WorkspaceFolderCountContext',
      'ChatContextKeys', 'ChatModeKind',
      'isContributionDisabled', 'extensionsFilterSubMenu', 'McpCommandIds',
      'McpContextKeys', 'HasInstalledMcpServersContext', 'IMcpServer', 'IMcpService',
      'InstalledMcpServersViewId', 'McpConnectionState', 'McpResourceQuickAccess', 'McpResourceQuickPick',
    ]);
    contents = contents.replace(/^import \{ ([^}]+) \} from ([^\n]+);$/gm, (_line, imports, module) => {
      const retained = imports.split(', ').filter(name => commandImports.has(name));
      return retained.length ? `import { ${retained.join(', ')} } from ${module};` : '';
    });
    contents = `import { WorkspaceMcpService, unavailableWorkspaceMcpWrite } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};\nimport { addWorkspaceMcpConfiguration, showWorkspaceMcpOptions, runWorkspaceMcpCommand } from ${JSON.stringify(join(root, 'src/mcp/commands.js'))};\n` + serverImport + contents;
  }
  if (relative.endsWith('/mcpServerActions.ts')) {
    const installBegin = contents.indexOf('export class InstallAction extends McpServerAction {');
    const installEnd = contents.indexOf('export class InstallInWorkspaceAction', installBegin);
    if (installBegin < 0 || installEnd < 0) throw new Error('上游 MCP InstallAction 边界缺失');
    const originalInstall = contents.slice(installBegin, installEnd);
    let install = replaceOnce(originalInstall, '\t\t@IMcpService private readonly mcpService: IMcpService,\n', '');
    install = replaceOnce(install, `\t\tconst installed = await this.mcpWorkbenchService.install(this.mcpServer);

\t\tawait startServerByFilter(this.mcpService, s => {
\t\t\treturn s.definition.id === installed.id;
\t\t});`, '\t\tawait this.mcpWorkbenchService.install(this.mcpServer);');
    contents = replaceOnce(contents, originalInstall, install);
    contents = replaceOnce(contents, "import { IAuthenticationService } from '../../../services/authentication/common/authentication.js';", '');
    contents = replaceOnce(contents, "import { IAccountQuery, IAuthenticationQueryService } from '../../../services/authentication/common/authenticationQuery.js';", '');
    contents = replaceSection(contents, 'export class AuthServerAction extends McpServerAction {', 'export class ShowServerOutputAction', `export class AuthServerAction extends McpServerAction {
  constructor() {
    super('mcp.authorize', localize('mcp.authorize', 'Authorize'), 'extension-action label prominent account', false);
    this.update();
  }
  update(): void {
    const server = this.mcpServer instanceof WorkspaceMcpServer ? this.mcpServer : undefined;
    this.enabled = server instanceof WorkspaceMcpServer && server.canAuthorize;
    this.class = 'extension-action label prominent account' + (this.enabled ? '' : ' hide');
  }
  override async run(): Promise<void> {
    const server = this.mcpServer instanceof WorkspaceMcpServer ? this.mcpServer : undefined;
    if (!(server instanceof WorkspaceMcpServer)) throw new Error('原 MCP 目标已移除');
    await server.authorize();
  }
}

`, replaceOnce);
    // 只迁移已接入 owner 的生命周期动作，其余配置／安装动作保留原 local 收窄。
    for (const [name, next, condition] of [
      ['StartServerAction', 'StopServerAction', '!McpConnectionState.canBeStarted(serverState.state)'],
      ['StopServerAction', 'RestartServerAction', 'McpConnectionState.canBeStarted(serverState.state)'],
    ]) {
      const begin = contents.indexOf(`export class ${name}`), end = contents.indexOf(`export class ${next}`, begin);
      let action = contents.slice(begin, end);
      action = replaceOnce(action, '\t\tif (!this.mcpServer.local) {\n\t\t\treturn;\n\t\t}\n', '');
      action = replaceOnce(action, 'if (!this.mcpServer) {', 'if (!(this.mcpServer instanceof WorkspaceMcpServer)) {');
      action = replaceOnce(action, `if (${condition}) {`, `if (server instanceof WorkspaceMcpServer ? !server.${name === 'StartServerAction' ? 'canConnect' : 'canStop'} : ${condition}) {`);
      if (name === 'StopServerAction') action = replaceOnce(action, "this.label = localize('stop', \"Stop Server\");", "this.label = server instanceof WorkspaceMcpServer && server.isPending ? localize('mcp.cancelOperation', 'Cancel Connection Operation') : localize('stop', 'Stop Server');");
      contents = replaceOnce(contents, contents.slice(begin, end), action);
    }
    contents = serverImport + contents;
  }
  if (relative.endsWith('/mcpServerEditor.ts')) {
    const actionImports = contents.split('\n').find(line => line.startsWith('import { ButtonWithDropDownExtensionAction,') && line.endsWith("from './mcpServerActions.js';"));
    if (!actionImports) throw new Error('上游 MCP 详情 action import 锚点缺失');
    contents = replaceOnce(contents, actionImports, "import { InstallAction, StartServerAction, StopServerAction, AuthServerAction, ButtonWithDropDownExtensionAction, ButtonWithDropdownExtensionActionViewItem, DropDownAction, McpServerStatusAction } from './mcpServerActions.js';");
    contents = replaceSection(contents, '\t\tconst actions = [', '\n\t\tconst actionsAndStatusContainer', `\t\tconst actions = [
      this.instantiationService.createInstance(InstallAction, false),
      this.instantiationService.createInstance(StartServerAction),
      this.instantiationService.createInstance(StopServerAction),
      this.instantiationService.createInstance(AuthServerAction),
    ];
`, replaceOnce);
    contents = replaceOnce(contents, 'if (extension.readmeUrl || extension.gallery?.readme) {', 'if (extension instanceof WorkspaceMcpServer || extension.readmeUrl) {');
    contents = replaceOnce(contents, 'private mcpServerManifest: Cache<IGalleryMcpServerConfiguration> | null;', `private mcpServerManifest: Cache<IGalleryMcpServerConfiguration> | null;
private readonly workspaceMcpObservation = this._register(new MutableDisposable());
protected override setEditorVisible(visible: boolean): void {
  super.setEditorVisible(visible);
  const server = this.input instanceof McpServerEditorInput ? this.input.mcpServer : undefined;
  this.workspaceMcpObservation.value = visible && server instanceof WorkspaceMcpServer ? server.observe() : undefined;
}`);
    contents = replaceOnce(contents, '\t\tthis.mcpServerReadme = new Cache(() => mcpServer.getReadme(token));', `\t\tthis.mcpServerReadme = new Cache(() => mcpServer.getReadme(token));
this.workspaceMcpObservation.value = this.isVisible() && mcpServer instanceof WorkspaceMcpServer ? mcpServer.observe() : undefined;`);
    contents = replaceOnce(contents, 'this.renderNavbar(mcpServer, template, preserveFocus);', 'this.renderNavbar(mcpServer, template, preserveFocus, token);');
    contents = replaceOnce(contents, 'private renderNavbar(extension: IWorkbenchMcpServer, template: IExtensionEditorTemplate, preserveFocus: boolean): void {', 'private renderNavbar(extension: IWorkbenchMcpServer, template: IExtensionEditorTemplate, preserveFocus: boolean, token: CancellationToken): void {');
    contents = replaceOnce(contents, '\toverride clearInput(): void {', '\toverride clearInput(): void {\nthis.workspaceMcpObservation.clear();');
    contents = replaceOnce(contents, 'if (e === extension) {', `if (e === extension) {
if (extension instanceof WorkspaceMcpServer) {
  template.description.textContent = extension.description;
  this.mcpServerReadme = new Cache(() => extension.getReadme(token));
  if (template.navbar.currentId === McpServerEditorTab.Readme) this.onNavbarChange(extension, { id: McpServerEditorTab.Readme, focus: false }, template);
}`);
    contents = serverImport + contents;
    contents = removeClassMembers(relative, contents, 'McpServerEditor', [
      'openManifestWithAdditionalDetails', 'openManifest', 'renderManifestDetails', 'renderAdditionalDetails',
    ]);
    contents = removeNodes(relative, contents, (node, file) =>
      ts.isClassDeclaration(node) && node.name?.text === 'AdditionalDetailsWidget'
      || ts.isPropertyDeclaration(node)
        && ts.isClassDeclaration(node.parent)
        && node.parent.name?.text === 'McpServerEditor'
        && node.name.getText(file) === 'mcpServerManifest'
      || ts.isExpressionStatement(node)
        && node.expression.getText(file) === 'this.mcpServerManifest = new Cache(() => mcpServer.getManifest(token))'
      || ts.isExpressionStatement(node)
        && node.expression.getText(file) === 'this.mcpServerManifest = null'
      || ts.isExpressionStatement(node)
        && node.expression.getText(file) === "template.name.classList.toggle('clickable', !!mcpServer.gallery?.webUrl)"
      || ts.isIfStatement(node)
        && node.expression.getText(file) === 'mcpServer.gallery?.webUrl'
      || ts.isIfStatement(node)
        && node.expression.getText(file) === 'extension.gallery || extension.local?.manifest'
      || ts.isCaseClause(node)
        && node.expression.getText(file) === 'McpServerEditorTab.Manifest'
      || ts.isExpressionStatement(node)
        && node.expression.getText(file) === 'this.renderAdditionalDetails(additionalDetailsContainer, extension)'
      || ts.isVariableStatement(node)
        && node.declarationList.declarations.some(declaration =>
          ts.isIdentifier(declaration.name) && declaration.name.text === 'additionalDetailsContainer')
      || ts.isEnumMember(node)
        && ts.isEnumDeclaration(node.parent)
        && node.parent.name.text === 'McpServerEditorTab'
        && node.name.getText(file) === 'Manifest');
    return pruneUnusedImportsAndConstants(relative, contents);
  }
  if (relative.endsWith('/mcpServersView.ts')) {
    // 原列表的 hidden 规则归 Extensions 容器；项目面板在可移动视图自身接回原样式作用域。
    contents = replaceOnce(contents, '\t\tsuper.renderBody(container);', "\t\tsuper.renderBody(container);\nif (this.id === 'boxteam.mcp.view') this.element.classList.add('extensions-viewlet');");
    contents = replaceOnce(contents, "private currentQuery = '@mcp';", "private currentQuery = '@mcp';\nprivate workspaceMcpQueryFailed = false;");
    contents = replaceOnce(contents, '\t\tthis.currentQuery = query;', '\t\tthis.workspaceMcpQueryFailed = false;\n\t\ttry {\n\t\tthis.currentQuery = query;');
    contents = replaceOnce(contents, '\t\treturn this.input.model;\n\t}\n\n\tprivate renderInput()', '\t\treturn this.input.model;\n\t\t} catch (error) { this.workspaceMcpQueryFailed = true; throw error; }\n\t}\n\n\tprivate renderInput()');
    contents = replaceOnce(contents, '\t\tthis._register(this.list.onContextMenu(e => this.onContextMenu(e), this));', `\t\tthis._register(this.list.onContextMenu(e => this.onContextMenu(e), this));
if (this.id === 'boxteam.mcp.view') void this.show('').catch(error => {
  if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
});`);
    contents = replaceOnce(contents, '"MCP Servers - Installed"', '"MCP Servers - Configured"');
    contents = replaceOnce(contents, '\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {', `\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));
this._register(this.mcpWorkbenchService.onChange(() => {
  if (this.workspaceMcpQueryFailed && this.currentQuery === '' && this.isBodyVisible()
    && this.mcpWorkbenchService instanceof WorkspaceMcpWorkbenchService && this.mcpWorkbenchService.isLocalCatalogAvailable) {
    void this.show('').catch(error => {
      if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
    });
  }
}));
this._register(this.mcpWorkbenchService.onReset(() => {
  if (this.currentQuery === '' && this.isBodyVisible()) void this.show('').catch(error => {
    if (!isCancellationError(error)) this.updateBody({ text: String(error), severity: Severity.Error });
  });
}));
\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {`);
    contents = replaceOnce(contents, '\t\t\t\tupdateEnablement();', '\t\t\t\tdata.description.textContent = mcpServer.description;\n\t\t\t\tupdateEnablement();');
    contents = replaceOnce(contents, 'if (newMcpServers.every(r => r.id !== oldMcpServer.id) && newMcpServers.some(r => r.name === oldMcpServer.name)) {', 'if (newMcpServers.every(r => r.id !== oldMcpServer.id)) {');
    contents = replaceOnce(contents, 'disposables.add(this.mcpWorkbenchService.onReset(() => onDidChangeModel.fire(new PagedModel([...this.mcpWorkbenchService.local]))));', 'disposables.add(this.mcpWorkbenchService.onReset(() => { servers = [...this.mcpWorkbenchService.local]; onDidChangeModel.fire(new PagedModel(servers)); }));');
    contents = `import { observeWorkspaceMcpView, WorkspaceMcpWorkbenchService } from ${JSON.stringify(join(root, 'src/mcp/service.js'))};\n` + contents;
  }
  contents = transformRetiredMcpGallery(relative, contents, replaceOnce);
  return transformMcpWorkbenchView(relative, contents, replaceOnce, replaceSection);
}
