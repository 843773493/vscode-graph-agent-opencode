export const mcpGallerySourcePaths = [
  'src/vs/platform/mcp/common/mcpManagement.ts',
  'src/vs/platform/mcp/common/mcpGalleryManifestService.ts',
  'src/vs/platform/mcp/common/mcpGalleryService.ts',
  'src/vs/workbench/services/mcp/browser/mcpGalleryManifestService.ts',
  'src/vs/workbench/services/mcp/electron-browser/mcpGalleryManifestService.ts',
  'src/vs/workbench/contrib/chat/browser/chat.shared.contribution.ts',
  'src/vs/platform/customizationMarketplace/common/mcpGalleryMarketplaceProvider.ts',
  'src/vs/platform/customizationMarketplace/common/customizationMarketplaceSources.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsViewlet.ts',
  'src/vs/workbench/services/progress/browser/progressService.ts',
  'src/vs/base/browser/ui/list/listPaging.ts',
];

/** MCP 目录沿原服务、模型和分页协议接线；不修改固定子模块，也不另建目录 client。 */
export function transformMcpGallery(path, source, replaceOnce, replaceSection) {
  let contents = source;
  if (path.endsWith('/mcpManagement.ts')) {
    contents = replaceOnce(contents, "export const mcpGalleryServiceUrlConfig = 'chat.mcp.gallery.serviceUrl';", "export const mcpGalleryServiceUrlConfig = 'chat.mcp.gallery.serviceUrl';\nexport const mcpGalleryServiceVersionConfig = 'chat.mcp.gallery.version';");
  }
  if (path === 'src/vs/platform/mcp/common/mcpGalleryManifestService.ts') {
    contents = replaceOnce(contents, "import { CancellationToken } from '../../../base/common/cancellation.js';\n", '');
    contents = replaceOnce(contents, "import { IRequestService, isSuccess } from '../../request/common/request.js';\n", '');
    contents = replaceOnce(contents, '\tprivate readonly versionByUrl = new Map<string, Promise<string>>();\n', '');
    contents = replaceOnce(contents, '\t\t@IRequestService private readonly requestService: IRequestService,\n', '');
    contents = replaceSection(contents, '\tprotected async createMcpGalleryManifest(', '\n\t\tconst isProductGalleryUrl', `\tprotected createMcpGalleryManifest(url: string, version = SUPPORTED_VERSIONS[0]): IMcpGalleryManifest {
\t\turl = url.trim().replace(/\\/+$/, '');
`, replaceOnce);
    contents = replaceSection(contents, '\tprivate async getVersion(', '\n}', '', replaceOnce);
  }
  if (path === 'src/vs/workbench/services/mcp/browser/mcpGalleryManifestService.ts') {
    contents = replaceOnce(contents, "import { IRequestService } from '../../../../platform/request/common/request.js';\n", '');
    contents = replaceOnce(contents, 'IMcpGalleryConfig, mcpGalleryServiceUrlConfig', 'IMcpGalleryConfig, mcpGalleryServiceUrlConfig, mcpGalleryServiceVersionConfig');
    contents = replaceOnce(contents, '\t\t@IRequestService requestService: IRequestService,\n', '');
    contents = replaceOnce(contents, '\t\tsuper(productService, requestService, logService);', `\t\tsuper(productService, logService);
\t\tthis._register(this.configurationService.onDidChangeConfiguration(e => {
\t\t\tif (e.affectsConfiguration(mcpGalleryServiceUrlConfig) || e.affectsConfiguration(mcpGalleryServiceVersionConfig)) {
\t\t\t\tthis.getAndUpdateMcpGalleryManifest();
\t\t\t}
\t\t}));
\t\tthis.getAndUpdateMcpGalleryManifest();`);
    contents = replaceOnce(contents, `\t\t\tthis.getMcpGalleryManifest().then(manifest => {
\t\t\t\tchannel.call('setMcpGalleryManifest', [manifest]);
\t\t\t\tthis._register(this.onDidChangeMcpGalleryManifest(manifest => channel.call('setMcpGalleryManifest', [manifest])));
\t\t\t});`, `\t\t\tthis._register(this.onDidChangeMcpGalleryManifest(manifest => channel.call('setMcpGalleryManifest', [manifest])));
\t\t\tvoid channel.call('setMcpGalleryManifest', [this.mcpGalleryManifest]);`);
    contents = replaceSection(contents, '\tprivate initPromise:', '\n\tprivate update(', `\toverride async getMcpGalleryManifest(): Promise<IMcpGalleryManifest | null> {
\t\treturn this.mcpGalleryManifest;
\t}

\tprivate getAndUpdateMcpGalleryManifest(): void {
\t\tconst config = this.configurationService.getValue<IMcpGalleryConfig | undefined>('chat.mcp.gallery');
\t\tconst url = config?.serviceUrl?.trim();
\t\tthis.update(url ? this.createMcpGalleryManifest(url, config?.version) : null);
\t}
`, replaceOnce);
  }
  if (path === 'src/vs/workbench/services/mcp/electron-browser/mcpGalleryManifestService.ts') {
    contents = replaceOnce(contents, "import { IRequestService } from '../../../../platform/request/common/request.js';\n", '');
    contents = replaceOnce(contents, '\t\t@IRequestService requestService: IRequestService,\n', '');
    contents = replaceOnce(contents, 'super(productService, remoteAgentService, requestService, logService, configurationService);', 'super(productService, remoteAgentService, logService, configurationService);');
  }
  if (path.endsWith('/chat.shared.contribution.ts')) {
    contents = replaceOnce(contents, 'mcpGalleryServiceEnablementConfig, mcpGalleryServiceUrlConfig }', 'mcpGalleryServiceEnablementConfig, mcpGalleryServiceUrlConfig, mcpGalleryServiceVersionConfig }');
    contents = replaceOnce(contents, '\t\t[mcpGalleryServiceUrlConfig]: {', `\t\t[mcpGalleryServiceVersionConfig]: {
\t\t\ttype: 'string', enum: ['v0.1', 'v0'], default: 'v0.1',
\t\t\tscope: ConfigurationScope.APPLICATION,
\t\t\tdescription: nls.localize('mcp.gallery.version', "MCP registry protocol version. Defaults to v0.1; registries using v0 require an explicit v0 setting. No version probing or fallback is performed."),
\t\t},
\t\t[mcpGalleryServiceUrlConfig]: {`);
    contents = replaceOnce(contents, "description: nls.localize('mcp.gallery.serviceUrl', \"Configure the MCP Gallery service URL to connect to\")", "description: nls.localize('mcp.gallery.serviceUrl', \"MCP registry base URL. Only this source is queried; an empty value leaves the gallery unconfigured.\")");
    contents = replaceOnce(contents, "\t\t\tincluded: false,\n\t\t\tpolicy: {\n\t\t\t\tname: 'McpGalleryServiceUrl',", "\t\t\tpolicy: {\n\t\t\t\tname: 'McpGalleryServiceUrl',");
  }
  if (path.endsWith('/mcpGalleryService.ts')) {
    contents = replaceOnce(contents, "import { CancellationToken } from '../../../base/common/cancellation.js';", "import { CancellationToken, CancellationTokenSource } from '../../../base/common/cancellation.js';");
    contents = replaceOnce(contents, "import { Disposable } from '../../../base/common/lifecycle.js';", "import { Disposable, toDisposable } from '../../../base/common/lifecycle.js';");
    contents = replaceOnce(contents, '\tprivate galleryMcpServerDataSerializers:', '\tprivate queryCancellation = new CancellationTokenSource();\n\tprivate galleryMcpServerDataSerializers:');
    contents = replaceOnce(contents, '\t\tthis.galleryMcpServerDataSerializers = new Map();', `\t\tthis._register(toDisposable(() => this.queryCancellation.dispose(true)));
\t\tthis._register(this.mcpGalleryManifestService.onDidChangeMcpGalleryManifest(() => {
\t\t\tthis.queryCancellation.dispose(true);
\t\t\tthis.queryCancellation = new CancellationTokenSource();
\t\t}));
\t\tthis.galleryMcpServerDataSerializers = new Map();`);
    contents = replaceSection(contents, '\tasync queryPage(', '\n\tasync getMcpServersFromGallery(', `\tasync queryPage(options: IMcpGalleryQueryPageOptions, token: CancellationToken, galleryManifest?: IMcpGalleryManifest): Promise<IMcpGalleryQueryPage> {
\t\tconst cancellation = new CancellationTokenSource(token);
\t\tconst changed = this.queryCancellation.token.onCancellationRequested(() => cancellation.cancel());
\t\ttry {
\t\t\tif (cancellation.token.isCancellationRequested) throw new CancellationError();
\t\t\tconst manifest = galleryManifest ?? await raceCancellationError(this.mcpGalleryManifestService.getMcpGalleryManifest(), cancellation.token);
\t\t\tif (!manifest) throw new Error(localize('mcpGalleryNotConfigured', "The MCP gallery is not configured. Configure 'chat.mcp.gallery.serviceUrl'."));
\t\t\tif (!this.getSerializer(manifest)) throw new Error(localize('mcpGalleryUnsupportedVersion', "Unsupported MCP registry protocol version: {0}. Use v0.1 or explicitly configure v0.", manifest.version));
\t\t\tif (!Number.isSafeInteger(options.pageSize) || options.pageSize <= 0) throw new Error('MCP gallery pageSize must be a positive integer.');
\t\t\tconst query = new Query().withPage(options.cursor ?? '', options.pageSize).withSearchText(options.cursor ? undefined : options.text?.trim());
\t\t\tconst { servers, metadata } = await raceCancellationError(this.queryGalleryMcpServers(query, manifest, cancellation.token), cancellation.token);
\t\t\tconst total = metadata.total;
\t\t\treturn { items: servers, total: total !== undefined && Number.isSafeInteger(total) && total >= servers.length ? total : undefined, nextCursor: metadata.nextCursor };
\t\t} finally {
\t\t\tchanged.dispose();
\t\t\tcancellation.dispose();
\t\t}
\t}

\tasync query(options?: IQueryOptions, token: CancellationToken = CancellationToken.None, manifest?: IMcpGalleryManifest): Promise<IIterativePager<IGalleryMcpServer>> {
\t\tconst sourceToken = this.queryCancellation.token;
\t\tconst active = manifest ?? await raceCancellationError(this.mcpGalleryManifestService.getMcpGalleryManifest(), token);
\t\tif (sourceToken.isCancellationRequested) throw new CancellationError();
\t\tif (!active) throw new Error(localize('mcpGalleryNotConfigured', "The MCP gallery is not configured. Configure 'chat.mcp.gallery.serviceUrl'."));
\t\tconst first = await this.queryPage({ text: options?.text, pageSize: DefaultPageSize }, token, active);
\t\tlet cursor = first.nextCursor;
\t\treturn {
\t\t\tfirstPage: { items: [...first.items], hasMore: !!cursor },
\t\t\tgetNextPage: async (ct: CancellationToken): Promise<IIterativePage<IGalleryMcpServer>> => {
\t\t\t\tif (token.isCancellationRequested || ct.isCancellationRequested || sourceToken.isCancellationRequested) throw new CancellationError();
\t\t\t\tif (!cursor) return { items: [], hasMore: false };
\t\t\t\tconst cancellation = new CancellationTokenSource(token);
\t\t\t\tconst cancelled = ct.onCancellationRequested(() => cancellation.cancel());
\t\t\t\ttry {
\t\t\t\t\tconst page = await this.queryPage({ cursor, pageSize: DefaultPageSize }, cancellation.token, active);
\t\t\t\t\tif (page.nextCursor === cursor) throw new McpGalleryInvalidCursorError();
\t\t\t\t\tcursor = page.nextCursor;
\t\t\t\t\treturn { items: [...page.items], hasMore: !!cursor };
\t\t\t\t} finally { cancelled.dispose(); cancellation.dispose(); }
\t\t\t}
\t\t};
\t}
`, replaceOnce);
    contents = replaceOnce(contents, 'token: CancellationToken, strict = false): Promise<IGalleryMcpServersResult>', 'token: CancellationToken): Promise<IGalleryMcpServersResult>');
    contents = replaceOnce(contents, 'this.queryRawGalleryMcpServers(query, mcpGalleryManifest, token, strict)', 'this.queryRawGalleryMcpServers(query, mcpGalleryManifest, token)');
    contents = replaceOnce(contents, 'token: CancellationToken, strict = false): Promise<IRawGalleryMcpServersResult>', 'token: CancellationToken): Promise<IRawGalleryMcpServersResult>');
    // 原 strict 的每一条失败分支成为唯一行为；不再把失败折叠为空页。
    contents = contents.replace(/\n([\t]+)if \(strict\) \{\n\1\t(throw [^\n]+;)\n\1\}/g, '\n$1$2');
    contents = contents.replace(/(\n[\t]+throw [^\n]+;)\n[\t]+return \{ servers: \[\], metadata: \{ count: 0 \} \};/g, '$1');
    if (contents.includes('strict')) throw new Error('原 MCP strict 分支未完整收敛');
    contents = replaceOnce(contents, 'const rateLimit = this.rateLimits.get(mcpGalleryUrl);', 'if (token.isCancellationRequested) throw new CancellationError();\n\t\tconst rateLimit = this.rateLimits.get(mcpGalleryUrl);');
    contents = replaceOnce(contents, 'const data = await asJson(context);', 'const data = await asJson(context);\n\t\tif (token.isCancellationRequested) throw new CancellationError();');
  }
  if (path.endsWith('/mcpGalleryMarketplaceProvider.ts')) {
    contents = replaceOnce(contents, "import { CancellationToken } from '../../../base/common/cancellation.js';", "import { CancellationToken, CancellationTokenSource } from '../../../base/common/cancellation.js';\nimport { raceCancellationError } from '../../../base/common/async.js';\nimport { Disposable, toDisposable } from '../../../base/common/lifecycle.js';\nimport { CancellationError } from '../../../base/common/errors.js';");
    contents = replaceOnce(contents, "import { IConfigurationService } from '../../configuration/common/configuration.js';\n", '');
    contents = replaceOnce(contents, 'IGalleryMcpServer, IMcpGalleryService, mcpGalleryServiceUrlConfig', 'IGalleryMcpServer, IMcpGalleryService');
    contents = replaceOnce(contents, "import { IProductService } from '../../product/common/productService.js';\n", '');
    contents = replaceOnce(contents, '\treadonly registryUrl: string;\n', '\treadonly registryUrl: string;\n\treadonly registryVersion: string;\n');
    contents = replaceSection(contents, 'function getConfiguredCustomMcpGalleryUrl(', 'export function getCustomizationMarketplaceSourceInfos(', '', replaceOnce);
    contents = replaceOnce(contents, 'status: McpGalleryManifestStatus', '_status: McpGalleryManifestStatus');
    contents = replaceOnce(contents, 'return status === McpGalleryManifestStatus.Available ? getAllMcpGalleryMarketplaceSourceInfos() : [];', 'return getAllMcpGalleryMarketplaceSourceInfos();');
    contents = replaceSection(contents, '\treturn ([\'custom\', \'default\'] as const).map(', '\n}\n\nfunction safeWebUri', `\treturn [createLazyCustomizationMarketplaceProvider(
\t\tCustomizationMarketplaceSources.McpGallery.id + '.custom',
\t\t() => instantiationService.createInstance(McpGalleryMarketplaceProvider),
\t\tCustomizationMarketplaceSources.McpGallery.id,
\t)];`, replaceOnce);
    contents = replaceOnce(contents, 'export class McpGalleryMarketplaceProvider implements', 'export class McpGalleryMarketplaceProvider extends Disposable implements');
    contents = replaceOnce(contents, '\treadonly id: string;', '\treadonly id = `${CustomizationMarketplaceSources.McpGallery.id}.custom`;\n\tprivate sourceCancellation = new CancellationTokenSource();');
    contents = replaceSection(contents, '\tconstructor(', '\n\tasync query(', `\tconstructor(
\t\t@IMcpGalleryService private readonly galleryService: IMcpGalleryService,
\t\t@IMcpGalleryManifestService private readonly manifestService: IMcpGalleryManifestService,
\t) {
\t\tsuper();
\t\tthis._register(toDisposable(() => this.sourceCancellation.dispose(true)));
\t\tthis._register(manifestService.onDidChangeMcpGalleryManifest(() => {
\t\t\tthis.sourceCancellation.dispose(true);
\t\t\tthis.sourceCancellation = new CancellationTokenSource();
\t\t\tthis.continuations.clear();
\t\t}));
\t}
`, replaceOnce);
    contents = replaceSection(contents, '\t\tconst configuredUrl =', '\n\t\tconst pageSize', `\t\tconst cacheToken = this.sourceCancellation.token;
\t\tconst cancellation = new CancellationTokenSource(token);
\t\tconst changed = cacheToken.onCancellationRequested(() => cancellation.cancel());
\t\ttry {
\t\tif (cancellation.token.isCancellationRequested) throw new CancellationError();
\t\tconst manifest = await raceCancellationError(this.manifestService.getMcpGalleryManifest(), cancellation.token);
\t\tconst registryUrl = normalizeMcpGalleryUrl(manifest?.url);
\t\tif (!manifest || !registryUrl) throw new Error(localize('mcpGalleryNotConfigured', "The MCP gallery is not configured. Configure 'chat.mcp.gallery.serviceUrl'."));`, replaceOnce);
    contents = replaceOnce(contents, 'continuation.registryUrl !== registryUrl', 'continuation.registryUrl !== registryUrl || continuation.registryVersion !== manifest.version');
    contents = replaceOnce(contents, '\t\t\tregistryUrl,\n', '\t\t\tregistryUrl,\n\t\t\tregistryVersion: manifest.version,\n');
    contents = replaceOnce(contents, '}, token, manifest);', '}, cancellation.token, manifest);\n\t\t\t\tif (cancellation.token.isCancellationRequested) throw new CancellationError();');
    contents = replaceOnce(contents, 'toMarketplaceEntry(server, this.registry, registryUrl, manifest)', "toMarketplaceEntry(server, 'custom', registryUrl, manifest)");
    contents = replaceOnce(contents, '\t\t\titems: servers.map(', '\t\t\tcacheToken,\n\t\t\titems: servers.map(');
    contents = replaceOnce(contents, '\t\t};\n\t}\n}', '\t\t};\n\t\t} finally { changed.dispose(); cancellation.dispose(); }\n\t}\n}');
  }
  if (path.endsWith('/customizationMarketplaceSources.ts')) {
    contents = replaceOnce(contents, 'import { mcpGalleryServiceUrlConfig }', 'import { mcpGalleryServiceUrlConfig, mcpGalleryServiceVersionConfig }');
    contents = replaceOnce(contents, 'configurationDependencies: [mcpGalleryServiceUrlConfig]', 'configurationDependencies: [mcpGalleryServiceUrlConfig, mcpGalleryServiceVersionConfig]');
  }
  if (path.endsWith('/mcpListWidget.ts')) {
    contents = replaceOnce(contents, '@IMcpGalleryManifestService private readonly mcpGalleryManifestService:', '@IMcpGalleryManifestService mcpGalleryManifestService:');
    contents = replaceSection(contents, '\tprivate async queryLegacyGallery(', '\n\tprivate async queryGallerySnapshot(', `\tprivate async queryLegacyGallery(options: IQueryOptions | undefined, token: CancellationToken): Promise<IWorkbenchMcpServer[]> {
\t\treturn (await this.mcpWorkbenchService.queryGallery(options, token)).firstPage.items;
\t}
`, replaceOnce);
    contents = replaceOnce(contents, '\tprivate gallerySnapshotFailed = false;', '\tprivate gallerySnapshotFailed = false;\n\tprivate galleryQueryError: string | undefined;');
    contents = replaceOnce(contents, '\t\t\tthis.gallerySnapshotServers = [];\n\t\t\tthis.galleryServers = [];', '\t\t\tthis.delayedGallerySearch.cancel();\n\t\t\tthis.gallerySearchLoading = false;\n\t\t\tthis.galleryQueryError = undefined;\n\t\t\tthis.searchInput.hideMessage();\n\t\t\tthis.gallerySnapshotServers = [];\n\t\t\tthis.galleryServers = [];');
    contents = replaceOnce(contents, '\t\t\tthis.gallerySnapshotServers = servers;', '\t\t\tthis.galleryQueryError = undefined;\n\t\t\tthis.searchInput.hideMessage();\n\t\t\tthis.gallerySnapshotServers = servers;');
    const start = contents.indexOf('\tprivate async queryGallerySnapshot(');
    const end = contents.indexOf('\tprivate addSurfaceActivation(', start);
    let queries = contents.slice(start, end);
    queries = queries.replaceAll('} catch {', '} catch (error) {');
    queries = replaceOnce(queries, '\t\t\t\tthis.gallerySnapshotFailed = true;', '\t\t\t\tthis.galleryQueryError = getErrorMessage(error);\n\t\t\t\tthis.searchInput.showMessage({ content: this.galleryQueryError, type: MessageType.ERROR });\n\t\t\t\tthis.gallerySnapshotFailed = true;');
    queries = replaceOnce(queries, '\t\t\tthis.galleryServers = servers;', '\t\t\tthis.galleryQueryError = undefined;\n\t\t\tthis.galleryServers = servers;');
    queries = replaceSection(queries, '\t\t\t\tthis.galleryServers = this.gallerySnapshotServers.filter(', '\n\t\t\t}\n\t\t} finally', '\t\t\t\tthis.galleryServers = [];\n\t\t\t\tthis.galleryQueryError = getErrorMessage(error);\n\t\t\t\tthis.searchInput.showMessage({ content: this.galleryQueryError, type: MessageType.ERROR });', replaceOnce);
    contents = replaceOnce(contents, contents.slice(start, end), queries);
    contents = replaceOnce(contents, 'this.emptyText.textContent = this.gallerySearchLoading', 'this.emptyText.textContent = this.galleryQueryError ?? (this.gallerySearchLoading');
    contents = replaceOnce(contents, 'localize(\'noMcpServersInGroup\', "No MCP servers in this group");', 'localize(\'noMcpServersInGroup\', "No MCP servers in this group"));');
    contents = replaceOnce(contents, "this.emptySubtext.textContent = this.gallerySearchLoading ||", "this.emptySubtext.textContent = this.galleryQueryError || this.gallerySearchLoading ||");
  }
  if (path.endsWith('/customizationMarketplaceInstallService.ts')) {
    contents = replaceOnce(contents, "return source.registry === 'default'\n\t\t\t? true\n\t\t\t: normalizeMcpGalleryUrl", 'return normalizeMcpGalleryUrl');
    contents = replaceSection(contents, '\t\tconst manifest = source.registry === \'default\'', '\n\t\tif (!manifest || !sourceRegistryUrl', `\t\tconst manifest = await this.mcpGalleryManifestService.getMcpGalleryManifest();
\t\tconst sourceRegistryUrl = normalizeMcpGalleryUrl(source.registryUrl);
\t\tconst configuredRegistryUrl = normalizeMcpGalleryUrl(this.configurationService.getValue<string>(mcpGalleryServiceUrlConfig));`, replaceOnce);
    contents = replaceOnce(contents, "resource.installation.registry === 'custom' &&\n\t\t\t\t\tevent.affectsConfiguration(mcpGalleryServiceUrlConfig)", "(event.affectsConfiguration(mcpGalleryServiceUrlConfig) || event.affectsConfiguration('chat.mcp.gallery.version'))");
  }
  if (path.endsWith('/mcpWorkbenchService.ts')) {
    contents = replaceOnce(contents, 'class McpWorkbenchServer implements', 'export class McpWorkbenchServer implements');
  }
  if (path.endsWith('/extensionsViewlet.ts')) {
    contents = replaceOnce(contents, '\t\tthis.showExtensionsViews(addedViews);', '\t\tvoid this.showExtensionsViews(addedViews).catch(error => this.onError(error));');
    contents = replaceOnce(contents, '\t\tthis.doSearch(true);', '\t\tvoid this.doSearch(true).catch(error => this.onError(error));');
  }
  if (path.endsWith('/progressService.ts')) {
    // 活动栏只观察任务以清理进度；清理链不应复制一个无人接收的任务拒绝。
    contents = replaceOnce(contents, `\t\tpromise.finally(() => {
\t\t\tclearTimeout(delayHandle);
\t\t\tdispose(activityProgress);
\t\t});`, `\t\tconst clearProgress = () => {
\t\t\tclearTimeout(delayHandle);
\t\t\tdispose(activityProgress);
\t\t};
\t\tvoid promise.then(clearProgress, clearProgress);`);
  }
  if (path.endsWith('/listPaging.ts')) {
    contents = replaceOnce(contents, "import { Event } from '../../../common/event.js';", "import { Event } from '../../../common/event.js';\nimport { isCancellationError, onUnexpectedError } from '../../../common/errors.js';");
    contents = replaceOnce(contents, '\t\tprivate modelProvider: () => IPagedModel<TElement>\n', '\t\tprivate modelProvider: () => IPagedModel<TElement>,\n\t\tprivate readonly onResolveError: (error: unknown) => void,\n');
    contents = replaceOnce(contents, '\t\tpromise.then(entry => this.renderer.renderElement(entry, index, data.data!, details));', `\t\tvoid promise.then(entry => {
\t\t\tif (!cts.token.isCancellationRequested && model === this.modelProvider()) this.renderer.renderElement(entry, index, data.data!, details);
\t\t}, error => {
\t\t\tif (!cts.token.isCancellationRequested && model === this.modelProvider() && !isCancellationError(error)) this.onResolveError(error);
\t\t});`);
    contents = replaceOnce(contents, 'export interface IPagedListOptions<T> {', 'export interface IPagedListOptions<T> {\n\treadonly onResolveError?: (error: unknown) => void;');
    contents = replaceOnce(contents, 'new PagedRenderer<T, ITemplateData<T>>(r, modelProvider)', 'new PagedRenderer<T, ITemplateData<T>>(r, modelProvider, options.onResolveError ?? onUnexpectedError)');
  }
  if (path.endsWith('/mcpServerEditor.ts')) {
    contents = replaceOnce(contents, '\t\tif (manifest) {\n\t\t\tconst supportUri', '\t\tif (manifest && (!extension.gallery || manifest.url === extension.gallery.galleryUrl)) {\n\t\t\tconst supportUri');
    contents = replaceOnce(contents, '\t\tif (gallery) {\n\t\t\tif (!extension.local)', `\t\tif (gallery) {
\t\t\tappend(moreInfo, $('.more-info-entry', undefined,
\t\t\t\t$('div.more-info-entry-name', undefined, localize('mcpGallerySource', "Registry source")),
\t\t\t\t$('code', undefined, gallery.galleryUrl ?? ''),
\t\t\t));
\t\t\tif (!extension.local)`);
  }
  if (path.endsWith('/mcpServersView.ts')) {
    for (const line of [
      "import { MarkdownString } from '../../../../base/common/htmlContent.js';\n",
      "import { CustomizationMarketplaceConfiguration } from '../../../../platform/customizationMarketplace/common/customizationMarketplaceSources.js';\n",
      "import { AICustomizationManagementSection } from '../../chat/common/aiCustomizationWorkspaceService.js';\n",
      "import { CustomizationMarketplaceWelcome } from '../../chat/browser/aiCustomization/customizationMarketplaceWelcome.js';\n",
      '\tshowWelcomeContent?: boolean;\n',
      '\tprivate listContainer: HTMLElement | null = null;\n',
      '\tprivate welcomeContainer: HTMLElement | null = null;\n',
      '\t\tprivate readonly mpcViewOptions: McpServerListViewOptions,\n',
      "\t\t// Create welcome container\n\t\tthis.welcomeContainer = dom.append(container, dom.$('.customizations-marketplace-welcome-container.hide'));\n\t\tthis.createWelcomeContent(this.welcomeContainer);\n\n",
    ]) contents = replaceOnce(contents, line, '');
    contents = replaceSection(contents, 'export interface McpServerListViewOptions {', 'interface IQueryResult {', '', replaceOnce);
    contents = replaceOnce(contents, '\tdisposables: DisposableStore;\n', '\tdisposables: DisposableStore;\n\tmessage?: Message;\n');
    contents = replaceSection(contents, '\tprivate showWelcomeContent(', '\tprivate updateBody(', '', replaceOnce);
    contents = replaceOnce(contents, '\t\tthis.showWelcomeContent(!!this.input.showWelcomeContent);\n\t\tif (!this.input.showWelcomeContent) {\n\t\t\tthis.updateBody();\n\t\t}', '\t\tthis.updateBody(this.input.message);');
    contents = replaceOnce(contents, '\t\tthis.listContainer = dom.append(container, mcpServersList);', '\t\tconst listContainer = dom.append(container, mcpServersList);');
    contents = replaceOnce(contents, '\t\t\tthis.listContainer,', '\t\t\tlistContainer,');
    contents = `import { CancellationToken, CancellationTokenSource } from '../../../../base/common/cancellation.js';\nimport { CancellationError, getErrorMessage, isCancellationError } from '../../../../base/common/errors.js';\nimport { IMcpGalleryManifestService } from '../../../../platform/mcp/common/mcpGalleryManifest.js';\n` + contents;
    contents = replaceOnce(contents, 'isDisposable, MutableDisposable }', 'isDisposable, MutableDisposable, toDisposable }');
    contents = replaceOnce(contents, "private currentQuery = '@mcp';", "private currentQuery = '@mcp';\nprivate queryCancellation: CancellationTokenSource | undefined;");
    contents = replaceOnce(contents, '\t\t@IConfigurationService private readonly marketplaceConfigurationService:', '\t\t@IMcpGalleryManifestService private readonly mcpGalleryManifestService: IMcpGalleryManifestService,\n\t\t@IConfigurationService private readonly marketplaceConfigurationService:');
    contents = replaceOnce(contents, '@IConfigurationService private readonly marketplaceConfigurationService:', '@IConfigurationService configurationService:');
    contents = replaceOnce(contents, '\t\t@ILogService private readonly logService: ILogService,', '\t\t@INotificationService private readonly notificationService: INotificationService,\n\t\t@ILogService private readonly logService: ILogService,');
    contents = replaceOnce(contents, 'contextMenuService, marketplaceConfigurationService, contextKeyService', 'contextMenuService, configurationService, contextKeyService');
    contents = replaceOnce(contents, '\t\tthis._register(this.marketplaceConfigurationService.onDidChangeConfiguration(event => {\n\t\t\tif (event.affectsConfiguration(CustomizationMarketplaceConfiguration.MarketplaceEnabled) && this.isBodyVisible()) {\n\t\t\t\tvoid this.show(this.currentQuery);\n\t\t\t}\n\t\t}));\n', '');
    contents = replaceOnce(contents, '\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));', `\t\tthis._register(toDisposable(() => this.queryCancellation?.dispose(true)));
this._register(mcpGalleryManifestService.onDidChangeMcpGalleryManifest(() => {
  if (this.currentQuery && this.isBodyVisible()) void this.show(this.currentQuery).catch(error => {
    if (!isCancellationError(error)) this.updateBody({ text: getErrorMessage(error), severity: Severity.Error });
  });
}));
\t\tthis._register(observeWorkspaceMcpView(this, this.mcpWorkbenchService));`);
    contents = replaceSection(contents, '\tasync show(query: string):', '\n\tprivate renderInput()', `\tasync show(query: string): Promise<IPagedModel<IWorkbenchMcpServer>> {
\t\tthis.queryCancellation?.dispose(true);
\t\tconst cancellation = this.queryCancellation = new CancellationTokenSource();
\t\tthis.currentQuery = query;
\t\tthis.workspaceMcpQueryFailed = false;
\t\tthis.input?.disposables.dispose();
\t\tthis.input = undefined;
\t\ttry {
\t\t\tconst input = await this.query(query.trim(), cancellation.token);
\t\t\tif (this.queryCancellation !== cancellation || cancellation.token.isCancellationRequested) {
\t\t\t\tinput.disposables.dispose();
\t\t\t\tthrow new CancellationError();
\t\t\t}
\t\t\tthis.input = input;
\t\t\tthis.renderInput();
\t\t\tif (input.onDidChangeModel) input.disposables.add(input.onDidChangeModel(model => {
\t\t\t\tif (this.input !== input) return;
\t\t\t\tinput.model = model;
\t\t\t\tthis.renderInput();
\t\t\t}));
\t\t\treturn input.model;
\t\t} catch (error) {
\t\t\tif (this.queryCancellation !== cancellation || cancellation.token.isCancellationRequested) throw new CancellationError();
\t\t\tif (!isCancellationError(error)) {
\t\t\t\tthis.workspaceMcpQueryFailed = true;
\t\t\t\tthis.updateBody({ text: getErrorMessage(error), severity: Severity.Error });
\t\t\t}
\t\t\tthrow error;
\t\t}
\t}
`, replaceOnce);
    contents = replaceOnce(contents, 'private async query(query: string):', 'private async query(query: string, token: CancellationToken):');
    contents = replaceOnce(contents, '\t\tif (query) {\n\t\t\tconst servers', `\t\tif (query) {
\t\t\tconst manifest = await this.mcpGalleryManifestService.getMcpGalleryManifest();
\t\t\tif (token.isCancellationRequested) throw new CancellationError();
\t\t\tthis.updateTitleDescription(manifest ? manifest.url + ' (' + manifest.version + ')' : localize('mcpGalleryUnconfigured', "MCP gallery not configured"));
\t\t\tif (!manifest) return { model: new PagedModel([]), disposables, message: {
\t\t\t\ttext: localize('mcpGalleryNotConfigured', "The MCP gallery is not configured. Configure 'chat.mcp.gallery.serviceUrl'."), severity: Severity.Info,
\t\t\t} };
\t\t\tconst servers`);
    contents = replaceOnce(contents, "this.mcpWorkbenchService.queryGallery({ text: query.replace('@mcp', '') })", "this.mcpWorkbenchService.queryGallery({ text: query.replace('@mcp', '') }, token)");
    contents = replaceOnce(contents, 'new SyncDescriptor(McpServersListView, [{ showMarketplaceWelcome: true }])', 'new SyncDescriptor(McpServersListView)');
    contents = replaceOnce(contents, 'new SyncDescriptor(McpServersListView, [{}])', 'new SyncDescriptor(McpServersListView)');
    contents = replaceOnce(contents, '\t\t\t\tmultipleSelectionSupport: false,', `\t\t\t\tonResolveError: error => {
\t\t\t\t\tthis.updateBody({ text: getErrorMessage(error), severity: Severity.Error });
\t\t\t\t\tthis.notificationService.error(error instanceof Error ? error : getErrorMessage(error));
\t\t\t\t},
\t\t\t\tmultipleSelectionSupport: false,`);
  }
  return contents;
}
