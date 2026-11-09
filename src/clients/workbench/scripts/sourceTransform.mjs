import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { searchEditorSourcePaths, transformSearchEditor } from './searchEditorTransform.mjs';
import { outlineSourcePaths, transformOutline } from './outlineTransform.mjs';
import { fileDialogSourcePaths, transformFileDialog } from './fileDialogTransform.mjs';
import { mcpSourcePaths, transformMcp } from './mcpTransform.mjs';
import { mcpProviderSourcePaths, transformMcpProviders } from './mcpProviderTransform.mjs';
import { extensionHostStateSourcePaths, transformExtensionHostState } from './extensionHostStateTransform.mjs';
import { extensionInstanceSourcePaths, transformExtensionInstances } from './extensions/extensionInstanceTransform.mjs';
import { extensionRunningSourcePaths, transformExtensionRunning } from './extensions/extensionRunningTransform.mjs';
import { extensionBrowserSourcePaths, transformExtensionBrowser } from './extensions/extensionBrowserTransform.mjs';
import { extensionManagementSourcePaths, transformExtensionManagement } from './extensions/extensionManagementTransform.mjs';
import { extensionOwnerSourcePaths, transformExtensionOwner } from './extensions/extensionOwnerTransform.mjs';
import { extensionStatusSourcePaths, transformExtensionStatus } from './extensions/extensionStatusTransform.mjs';
import { extensionEnablementSourcePaths, transformExtensionEnablement } from './extensions/extensionEnablementTransform.mjs';
import { extensionDocumentSourcePaths, transformExtensionDocuments } from './extensions/extensionDocumentTransform.mjs';
import { extensionTabsSourcePaths, transformExtensionTabs } from './extensions/extensionTabsTransform.mjs';
import { extensionLanguageFeaturesSourcePaths, transformExtensionLanguageFeatures } from './extensions/extensionLanguageFeaturesTransform.mjs';
import { extensionWorkspaceSymbolsSourcePaths, transformExtensionWorkspaceSymbols } from './extensions/extensionWorkspaceSymbolsTransform.mjs';
import { extensionStorageSourcePaths, transformExtensionStorage } from './extensions/extensionStorageTransform.mjs';
import { extensionConfigurationSourcePaths, transformExtensionConfiguration } from './extensions/extensionConfigurationTransform.mjs';
import { configurationContributionSourcePaths, transformConfigurationContributions } from './extensions/configurationContributionTransform.mjs';
import { configurationRegistrySourcePaths, transformConfigurationRegistry, transformConfigurationSourceTypes } from './extensions/configurationRegistryTransform.mjs';
import { extensionFileEventsSourcePaths, transformExtensionFileEvents } from './extensions/extensionFileEventsTransform.mjs';
import { nativeManagementSourcePaths, transformNativeManagement } from './nativeManagementTransform.mjs';
import { settingsEditorScopeSourcePaths, transformSettingsEditorScope } from './settingsEditorScopeTransform.mjs';
import { settingsEditorSourcePaths, transformSettingsEditor } from './settingsEditorTransform.mjs';
import { settingsTreeScopeSourcePaths, transformSettingsTreeScope } from './settingsTreeScopeTransform.mjs';
import { extensionTerminalSourcePaths, transformExtensionTerminal } from './extensions/extensionTerminalTransform.mjs';
import { extensionDebugSourcePaths, transformExtensionDebug } from './extensions/extensionDebugTransform.mjs';
import { pluginDebugSourcePaths, transformPluginDebug } from './pluginDebugTransform.mjs';
import { extensionTasksSourcePaths, transformExtensionTasks } from './extensions/extensionTasksTransform.mjs';
import { extensionNotebookSourcePaths, transformExtensionNotebookApi, transformExtensionNotebooks } from './extensions/extensionNotebookTransform.mjs';

export const pinnedUpstreamCommit = 'fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca';
export const transformedSourcePaths = [...new Set([
  'src/vs/workbench/browser/web.main.ts',
  'src/vs/workbench/browser/layout.ts',
  'src/vs/workbench/browser/quickaccess.ts',
  'src/vs/platform/quickinput/browser/quickAccess.ts',
  'src/vs/platform/quickinput/common/quickAccess.ts',
  'src/vs/platform/quickinput/browser/helpQuickAccess.ts',
  'src/vs/workbench/services/search/common/queryBuilder.ts',
  'src/vs/workbench/contrib/search/browser/search.contribution.ts',
  'src/vs/workbench/contrib/search/browser/searchQuickAccess.contribution.ts',
  'src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.ts',
  'src/vs/workbench/contrib/search/browser/searchTreeModel/searchModel.ts',
  'src/vs/workbench/contrib/search/browser/searchTreeModel/match.ts',
  'src/vs/workbench/contrib/search/browser/searchView.ts',
  'src/vs/workbench/browser/parts/views/viewPaneContainer.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts',
  'src/vs/workbench/services/textfile/common/textEditorService.ts',
  'src/vs/workbench/services/textfile/common/textFileEditorModel.ts',
  'src/vs/workbench/services/editor/browser/editorResolverService.ts',
  'src/vs/workbench/services/textmodelResolver/common/textModelResolverService.ts',
  'src/vs/editor/contrib/documentSymbols/browser/outlineModel.ts',
  'src/vs/workbench/services/extensionManagement/browser/webExtensionsScannerService.ts',
  'src/vs/platform/extensionManagement/common/abstractExtensionManagementService.ts',
  'src/vs/platform/extensionManagement/common/extensionsScannerService.ts',
  'src/vs/workbench/browser/parts/editor/editorGroupView.ts',
  'src/vs/workbench/browser/parts/editor/editorPanes.ts',
  'src/vs/workbench/browser/parts/editor/editorWithViewState.ts',
  'src/vs/workbench/services/configuration/common/configurationCache.ts',
  'src/vs/platform/workspaces/common/workspaces.ts',
  'src/vs/workbench/contrib/mediaPreview/browser/imagePreviewEditor.ts',
  'src/vs/workbench/contrib/webview/common/webview.ts',
  'src/vs/workbench/api/common/extHostWebview.ts',
  'src/vs/workbench/contrib/webview/browser/resourceLoading.ts',
  'src/vs/platform/webview/common/mimeTypes.ts',
  'extensions/markdown-language-features/src/preview/preview.ts',
  ...searchEditorSourcePaths,
  ...outlineSourcePaths,
  ...fileDialogSourcePaths,
  ...mcpSourcePaths,
  ...mcpProviderSourcePaths,
  ...extensionHostStateSourcePaths,
  ...extensionInstanceSourcePaths,
  ...extensionRunningSourcePaths,
  ...extensionBrowserSourcePaths,
  ...extensionManagementSourcePaths,
  ...extensionOwnerSourcePaths,
  ...extensionStatusSourcePaths,
  ...extensionEnablementSourcePaths,
  ...extensionDocumentSourcePaths,
  ...extensionNotebookSourcePaths,
  ...extensionTabsSourcePaths,
  ...extensionLanguageFeaturesSourcePaths,
  ...extensionWorkspaceSymbolsSourcePaths,
  ...extensionStorageSourcePaths,
  ...extensionTerminalSourcePaths,
  // 带原生进程依赖的入口由下面的 Node 清单及其真实 tsconfig 检查。
  ...extensionDebugSourcePaths.filter(path => !path.includes('/node/')),
  ...pluginDebugSourcePaths,
  ...extensionTasksSourcePaths.filter(path => !path.includes('/node/')),
  ...extensionConfigurationSourcePaths, ...configurationContributionSourcePaths, ...configurationRegistrySourcePaths,
  ...extensionFileEventsSourcePaths,
  ...settingsEditorScopeSourcePaths,
  ...settingsEditorSourcePaths,
  ...settingsTreeScopeSourcePaths,
])];

export const nodeTransformedSourcePaths = [...new Set([
  ...transformedSourcePaths, ...extensionDebugSourcePaths, ...extensionTasksSourcePaths, ...nativeManagementSourcePaths,
  'src/vs/platform/log/node/spdlogLog.ts',
])];

function replaceOnce(source, marker, replacement) {
  if (source.split(marker).length !== 2) throw new Error(`上游转换锚点必须恰好一次：${marker}`);
  return source.replace(marker, () => replacement);
}

/** 构建与 Vite 共用上游源码定制，不改写固定子模块。 */
export function sourceTransform(root, productConfiguration, extensions, { runtime = 'browser' } = {}) {
  if (productConfiguration.commit !== pinnedUpstreamCommit) throw new Error(`上游版本不匹配：要求 ${pinnedUpstreamCommit}`);
  const upstream = join(root, 'upstream');
  const product = {
    ...productConfiguration,
    // 官方 1.145.0 资源匹配当前 Webview v6 协议；保留原 UUID 子域隔离，替换上游陈旧的 v4 地址。
    webviewContentExternalBaseUrlTemplate: 'https://{{uuid}}.vscode-cdn.net/stable/a717861c07586a53d83b80d742cd3f59b20a6273/out/vs/workbench/contrib/webview/browser/pre/',
  };
  return async function transformSource(path, source) {
    // 严格文本锚点使用统一行尾，不改写固定上游的检出原件。
    let contents = source.replace(/\r\n/g, '\n');
    const replacements = {
      '/*BUILD->INSERT_PRODUCT_CONFIGURATION*/': JSON.stringify(product).slice(1, -1),
      '/*BUILD->INSERT_BUILTIN_EXTENSIONS*/': JSON.stringify(extensions).slice(1, -1),
    };
    for (const [marker, value] of Object.entries(replacements)) contents = contents.replace(marker, () => value);
    contents = transformExtensionInstances(root, path, contents, replaceOnce);
    contents = transformExtensionRunning(root, path, contents, replaceOnce);
    contents = transformExtensionBrowser(root, path, contents, replaceOnce);
    contents = transformExtensionManagement(root, path, contents, replaceOnce);
    contents = transformConfigurationSourceTypes(root, path, contents, replaceOnce);
    contents = transformExtensionHostState(root, path, contents, replaceOnce);
    contents = transformExtensionOwner(root, path, contents, replaceOnce);
    contents = transformExtensionStatus(root, path, contents, replaceOnce);
    contents = transformExtensionEnablement(root, path, contents, replaceOnce);
    contents = transformExtensionDocuments(root, path, contents, replaceOnce);
    contents = transformExtensionStorage(root, path, contents, replaceOnce, runtime);
    contents = transformExtensionTerminal(root, path, contents, replaceOnce);
    contents = transformExtensionDebug(root, path, contents, replaceOnce);
    contents = transformPluginDebug(root, path, contents, replaceOnce);
    contents = transformExtensionTasks(root, path, contents, replaceOnce);
    contents = transformExtensionNotebookApi(root, path, contents);
    contents = transformMcpProviders(root, path, contents, replaceOnce);
    if (path === join(upstream, 'src/vs/platform/extensionManagement/common/abstractExtensionManagementService.ts')) {
      // 尚未创建任何安装任务时没有可结算的结果；原 catch 会丢掉真正前置错误并返回空结果。
      contents = replaceOnce(contents, '} catch (error) {\n\t\t\tconst getAllDepsAndPacks =',
        `} catch (error) {
      if (!installingExtensionsMap.size && !alreadyRequestedInstallations.length) throw error;
      const getAllDepsAndPacks =`);
    }
    if (path === join(upstream, 'src/vs/platform/extensionManagement/common/extensionsScannerService.ts')) {
      contents = replaceOnce(contents, "import { getErrorMessage } from '../../../base/common/errors.js';",
        "import { getErrorMessage, onUnexpectedError } from '../../../base/common/errors.js';");
      // 原缓存校验任务由 scanner 持有；dispose 的预期取消不能成为未处理 rejection。
      contents = replaceOnce(contents, 'this.cacheValidatorThrottler.trigger(() => this.validatePendingCaches());',
        'this.cacheValidatorThrottler.trigger(() => this.validatePendingCaches()).catch(onUnexpectedError);');
    }
    contents = transformExtensionLanguageFeatures(root, path, contents, replaceOnce, runtime);
    if (runtime === 'node' && path === join(upstream, 'src/vs/platform/extensionManagement/node/extensionManagementService.ts')) {
      contents = replaceOnce(contents, "import { isEngineValid } from '../../extensions/common/extensionValidator.js';",
        "import { isEngineValid, validateExtensionManifest } from '../../extensions/common/extensionValidator.js';\nimport Severity from '../../../base/common/severity.js';");
      // 沿原 ExtensionKey 和 scanner 的包根语义预检，先拒绝原扫描器也会标 invalid 的候选，不能发布后才失败。
      contents = replaceOnce(contents, 'const allowedToInstall = this.allowedExtensionsService.isAllowed({ id: extensionId, version: manifest.version, publisherDisplayName: undefined });',
        `const extensionLocation = joinPath(this.extensionsScannerService.userExtensionsLocation, new ExtensionKey({ id: extensionId }, manifest.version).toString().toLowerCase());
      const errors = validateExtensionManifest(this.productService.version, this.productService.date, extensionLocation, manifest, false)
        .filter(([severity]) => severity === Severity.Error);
      if (errors.length) throw new ExtensionManagementError(errors.map(([, message]) => message).join('\\n'), ExtensionManagementErrorCode.Invalid);
      const allowedToInstall = this.allowedExtensionsService.isAllowed({ id: extensionId, version: manifest.version, publisherDisplayName: undefined });`);
    }
    if (runtime === 'node' && path === join(upstream, 'src/vs/platform/log/node/spdlogLog.ts')) {
      // 只在 native addon 边界使用 Windows 长路径；日志资源 URI 和目录归属保持原样。
      contents = "import { toNamespacedPath } from 'node:path';\n" + contents;
      contents = replaceOnce(contents, 'createAsyncRotatingLogger(name, logfilePath, filesize, filecount)',
        'createAsyncRotatingLogger(name, toNamespacedPath(logfilePath), filesize, filecount)');
    }
    if (runtime === 'node' && path === join(upstream, 'src/vs/workbench/api/common/extHostExtensionService.ts')) {
      contents = `import { retainNodeExtensionVersions } from ${JSON.stringify(join(root, 'node/extensionHostReferences.js'))};\n` + contents;
      // 先沿原流程注册 API actors，再在运行 barrier 打开前取得实际版本引用。
      contents = replaceOnce(contents, 'this._almostReadyToRunExtensions.open();',
        `if (this._initData.isTargetExtensionHost) {
        try { await retainNodeExtensionVersions(this._myRegistry.getAllExtensionDescriptions()); }
        catch (error) { this.terminate('运行版本准入失败：' + String(error), 1); throw error; }
      }
      this._almostReadyToRunExtensions.open();`);
      contents = replaceOnce(contents, 'const searchTree = await this._createExtensionPathIndex(myRegistry.getAllExtensionDescriptions());',
        'if (this._initData.isTargetExtensionHost) await retainNodeExtensionVersions(myRegistry.getAllExtensionDescriptions());\n      const searchTree = await this._createExtensionPathIndex(myRegistry.getAllExtensionDescriptions());');
    }
    // Node 宿主复用共同状态与编辑版本合同，不能带入只属于 renderer 的 FileService 和 UI 定制。
    if (runtime === 'node') return transformNativeManagement(root, path, contents, replaceOnce);
    contents = transformFileDialog(root, path, contents, replaceOnce);
    contents = transformExtensionConfiguration(root, path, contents, replaceOnce);
    contents = transformConfigurationContributions(root, path, contents, replaceOnce);
    contents = transformConfigurationRegistry(root, path, contents, replaceOnce);
    contents = transformExtensionFileEvents(root, path, contents, replaceOnce);
    contents = transformExtensionNotebooks(root, path, contents, replaceOnce);
    contents = transformSettingsEditor(root, path, contents, replaceOnce);
    contents = transformSettingsTreeScope(root, path, contents, replaceOnce);
    contents = transformExtensionTabs(root, path, contents, replaceOnce);
    contents = transformExtensionWorkspaceSymbols(root, path, contents, replaceOnce);
    if (path === join(upstream, 'src/vs/platform/workspaces/common/workspaces.ts')) {
      // 不透明文件根已在 URI.path 内编码；原 skipEncoding 会让重读时的 URI.parse 折叠路径段。
      contents = replaceOnce(contents, 'export function getStoredWorkspaceFolder(folderURI: URI, forceAbsolute: boolean, folderName: string | undefined, targetConfigFolderURI: URI, extUri: IExtUri): IStoredWorkspaceFolder {',
        `export function getStoredWorkspaceFolder(folderURI: URI, forceAbsolute: boolean, folderName: string | undefined, targetConfigFolderURI: URI, extUri: IExtUri): IStoredWorkspaceFolder {
      if (folderURI.scheme === 'boxteam-file') return { name: folderName, uri: folderURI.toString() };`);
    }
    if (path === join(upstream, 'src/vs/workbench/browser/layout.ts')) {
      // 自动恢复关闭面板时不得抢走 Quick Input 等外部控件的焦点；只交回原本属于面板的焦点。
      const start = 'private setPanelHidden(hidden: boolean, skipLayout?: boolean): void {';
      const end = '\n\tprivate inMaximizedAuxiliaryBarTransition = false;';
      const method = contents.slice(contents.indexOf(start), contents.indexOf(end));
      let restored = replaceOnce(method, 'const wasHidden = !this.isVisible(Parts.PANEL_PART);',
        'const hadPanelFocus = this.hasFocus(Parts.PANEL_PART);\n\t\tconst wasHidden = !this.isVisible(Parts.PANEL_PART);');
      restored = replaceOnce(restored, 'focusEditor = true;', 'focusEditor = hadPanelFocus;');
      contents = replaceOnce(contents, method, restored);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/webview/common/webview.ts')) {
      // 允许根中的 HTTP 扩展文件也走原隔离 Worker；第三方 URL 保留直接加载语义。
      contents = replaceOnce(contents, 'export function asWebviewUri(resource: URI, remoteInfo?: WebviewRemoteInfo): URI {',
        'export function asWebviewUri(resource: URI, remoteInfo?: WebviewRemoteInfo, localResourceRoots: readonly URI[] = []): URI {');
      contents = replaceOnce(contents, 'if (resource.scheme === Schemas.http || resource.scheme === Schemas.https) {',
        "if ((resource.scheme === Schemas.http || resource.scheme === Schemas.https) && !localResourceRoots.some(root => isEqualOrParent(resource.with({ query: '' }), root, true))) {");
      contents = "import { isEqualOrParent } from '../../../../base/common/resources.js';\n" + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/api/common/extHostWebview.ts')) {
      contents = replaceOnce(contents, 'return asWebviewUri(resource, this.#remoteInfo);',
        'return asWebviewUri(resource, this.#remoteInfo, this.#options.localResourceRoots ?? getDefaultLocalResourceRoots(this.#extension, this.#workspace));');
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/webview/browser/resourceLoading.ts')) {
      // 原 Fetch Provider 的 stat 是零值占位，不能据此让 Worker 永久命中；HTTP 文件沿 fetch 自身再验证。
      contents = replaceOnce(contents, 'const readOptions: { etag?: string; position?: number; length?: number } = { etag: options.ifNoneMatch };',
        'const isHttpResource = resourceToLoad.scheme === Schemas.http || resourceToLoad.scheme === Schemas.https;\nconst readOptions: { etag?: string; position?: number; length?: number } = { etag: isHttpResource ? undefined : options.ifNoneMatch };');
      contents = replaceOnce(contents, 'new WebviewResourceResponse.StreamSuccess(result.value, result.etag, result.mtime, mime, result.size)',
        'new WebviewResourceResponse.StreamSuccess(result.value, isHttpResource ? undefined : result.etag, isHttpResource ? undefined : result.mtime, mime, result.size)');
    }
    if (path === join(upstream, 'src/vs/platform/webview/common/mimeTypes.ts')) {
      contents = replaceOnce(contents, "['.oft', 'font/otf'],",
        "['.otf', 'font/otf'],\n['.ttf', 'font/ttf'],\n['.woff', 'font/woff'],\n['.woff2', 'font/woff2'],");
    }
    if (path === join(upstream, 'extensions/markdown-language-features/src/preview/preview.ts')) {
      // 首次生成贡献资源 URL 前发布同一允许根，避免首帧继续直连本机 HTTP。
      const options = 'this.#webviewPanel.webview.options = this.#getWebviewOptions();';
      contents = replaceOnce(contents, options, '');
      contents = replaceOnce(contents, 'const content = await (shouldReloadPage', `${options}\nconst content = await (shouldReloadPage`);
    }
    if (path === join(upstream, 'src/vs/workbench/browser/web.main.ts')) {
      const marker = 'await this.registerIndexedDBFileSystemProviders(environmentService, fileService, logService, loggerService, logsPath);';
      const cacheSchemes = '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp] /* Cache all non native resources */';
      const closeDatabase = 'this.onWillShutdownDisposables.add(toDisposable(() => indexedDB?.close()));';
      // 默认虚拟工作区与 vscode-userdata 一样由本地 IndexedDB 提供；额外配置缓存会留下关闭后的异步写入。
      contents = replaceOnce(contents, marker, `${marker}\nawait installWorkspaceFileSystem(fileService);`);
      const identity = 'const uriIdentityService = new UriIdentityService(fileService);';
      contents = replaceOnce(contents, identity, `${identity}\ninstallBackendFiles(fileService, uriIdentityService);`);
      contents = replaceOnce(contents, cacheSchemes, '[Schemas.file, Schemas.vscodeUserData, Schemas.tmp, workspaceFolder.scheme]');
      // pagehide 的同步 shutdown 仍会产生延迟日志；数据库连接由浏览器随文档释放，不能提前关闭并中止事务。
      contents = replaceOnce(contents, closeDatabase, '// IndexedDB 连接随浏览器文档释放，保留关闭期间日志和设置写入的机会。');
      contents = `import { installBackendFiles } from ${JSON.stringify(join(root, 'src/workspaceFiles.js'))};\nimport { installWorkspaceFileSystem, workspaceFolder } from ${JSON.stringify(join(root, 'src/filesystem.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/services/editor/browser/editorResolverService.ts')) {
      const entry = 'async resolveEditor(editor: IUntypedEditorInput, preferredGroup: PreferredGroup | undefined): Promise<ResolvedEditor> {';
      contents = replaceOnce(contents, entry, `${entry}\n[editor] = await canonicalWorkspaceFileInputs([editor]);`);
      contents = `import { canonicalWorkspaceFileInputs } from ${JSON.stringify(join(root, 'src/workspaceFiles.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/services/textfile/common/textFileEditorModel.ts')) {
      contents = replaceOnce(contents, 'interface IBackupMetaData extends IWorkingCopyBackupMeta {',
        'interface IBackupMetaData extends IWorkingCopyBackupMeta {\nboxteamFileRevision?: string;');
      contents = replaceOnce(contents, 'return { meta, content };',
        'const revision = workspaceFileRevisionForBackup(this.fileService, this.resource);\nif (meta && revision !== undefined) meta.boxteamFileRevision = revision;\nreturn { meta, content };');
      const restore = 'private async doResolveFromBackup(backup: IResolvedWorkingCopyBackup<IBackupMetaData>, encoding: string, options?: ITextFileResolveOptions): Promise<void> {';
      contents = replaceOnce(contents, restore, `${restore}\nrestoreWorkspaceFileRevision(this.fileService, this.resource, backup.meta?.boxteamFileRevision);`);
      contents = `import { workspaceFileRevisionForBackup, restoreWorkspaceFileRevision } from ${JSON.stringify(join(root, 'src/workspaceFiles.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/services/textmodelResolver/common/textModelResolverService.ts')) {
      const entry = 'async createModelReference(resource: URI): Promise<IReference<IResolvedTextEditorModel>> {';
      // FileEditorInput 先通过原 files.resolve 建立真实模型；沿该模型直接复用，不再向 owner 重复请求。
      contents = replaceOnce(contents, entry, `${entry}\nif (!this.modelService.getModel(resource)) [resource] = await canonicalWorkspaceFileResources([resource]);`);
      contents = `import { canonicalWorkspaceFileResources } from ${JSON.stringify(join(root, 'src/workspaceFiles.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/parts/editor/editorGroupView.ts')) {
      const entry = 'private restoreEditors(from: IEditorGroupView | ISerializedEditorGroupModel | null, groupViewOptions?: IEditorGroupViewOptions): Promise<void> | undefined {';
      contents = replaceOnce(contents, entry, `private async restoreEditors(from: IEditorGroupView | ISerializedEditorGroupModel | null, groupViewOptions?: IEditorGroupViewOptions): Promise<void> {
try {
  await this.instantiationService.invokeFunction(accessor => {
    const textEditors = accessor.get(ITextEditorService);
    return canonicalRestoredWorkspaceFiles(this, input => textEditors.createTextEditor(input),
      this.fileService, accessor.get(IWorkingCopyBackupService), this.uriIdentityService,
      (source, target) => this.editorPane.restoreEditorViewState(source, target, this.uriIdentityService.extUri));
  });
} catch (error) {
  this.titleControl.openEditors(this.editors);
  this.instantiationService.invokeFunction(accessor => accessor.get(INotificationService).error(error instanceof Error ? error : String(error)));
  return;
}`);
      contents = `import { canonicalRestoredWorkspaceFiles } from ${JSON.stringify(join(root, 'src/workspaceFiles.js'))};\nimport { ITextEditorService } from '../../../services/textfile/common/textEditorService.js';\nimport { IWorkingCopyBackupService } from '../../../services/workingCopy/common/workingCopyBackup.js';\nimport { INotificationService } from '../../../../platform/notification/common/notification.js';\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/parts/editor/editorPanes.ts')) {
      const entry = 'private doInstantiateEditorPane(descriptor: IEditorPaneDescriptor): EditorPane {';
      contents = replaceOnce(contents, entry, `restoreEditorViewState(source: EditorInput, target: EditorInput, comparer: IExtUri): object | undefined {
  // 复用原 Pane 和 Memento；冷恢复此时只构造，不创建 DOM 或读取模型。
  const pane = this.doInstantiateEditorPane(this.getEditorPaneDescriptor(source));
  return pane instanceof AbstractEditorWithViewState ? pane.restoreEditorViewState(source, target, comparer) : undefined;
}

${entry}`);
      contents = "import { AbstractEditorWithViewState } from './editorWithViewState.js';\nimport { IExtUri } from '../../../../base/common/resources.js';\n" + contents;
    }
    contents = transformSettingsEditorScope(root, path, contents, replaceOnce);
    if (path === join(upstream, 'src/vs/workbench/browser/parts/editor/editorWithViewState.ts')) {
      const entry = 'protected moveEditorViewState(source: URI, target: URI, comparer: IExtUri): void {';
      contents = replaceOnce(contents, entry, `restoreEditorViewState(source: EditorInput, target: EditorInput, comparer: IExtUri): T | undefined {
  const sourceResource = this.toEditorViewStateResource(source), targetResource = this.toEditorViewStateResource(target);
  if (sourceResource && targetResource) this.moveEditorViewState(sourceResource, targetResource, comparer);
  return this.loadEditorViewState(target);
}

${entry}`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/mediaPreview/browser/imagePreviewEditor.ts')) {
      contents = `import { onGatewayUserSessionInvalidated } from ${JSON.stringify(resolve(root, '../web/src/api/http.js'))};\n` + contents;
      // 身份撤销沿原 input 释放 watcher、解码与 URL；旧 input 不能自动重读或恢复旧图。
      const watch = 'this._inputDisposables.add(this._fileService.onDidFilesChange(onDidFilesChange));';
      contents = replaceOnce(contents, watch, `${watch}
if (input.resource.scheme === 'boxteam-file') {
  this._inputDisposables.add(toDisposable(onGatewayUserSessionInvalidated(0, () => {
    this._inputDisposables.clear();
    this._loadId++;
    this._viewer?.clear();
    this._objectUrl.clear();
    this._fileSize = undefined;
    this._setMessage(localize('imagePreview.identityChanged', "Gateway 或用户身份已改变，请重新打开图片"));
    this._onDidChangeImageInfo.fire();
  })));
}`);
      const read = 'const content = await this._fileService.readFile(input.resource, undefined, token);';
      contents = replaceOnce(contents, read, `if (!this._isCurrentLoad(loadId, input) || token.isCancellationRequested) return;
${read}`);
      // 待解码 URL 也归原 input；成功显示后再转交原 MutableDisposable，不另建引用账本。
      const createUrl = 'const url = URL.createObjectURL(blob);';
      contents = replaceOnce(contents, createUrl, `${createUrl}
const objectUrl = this._inputDisposables.add(toDisposable(() => URL.revokeObjectURL(url)));`);
      const decoded = `if (!this._isCurrentLoad(loadId, input)) {
\t\t\tURL.revokeObjectURL(url);`;
      contents = replaceOnce(contents, decoded, `this._inputDisposables.deleteAndLeak(objectUrl);
if (!this._isCurrentLoad(loadId, input)) {
  objectUrl.dispose();`);
      contents = replaceOnce(contents, 'this._objectUrl.value = toDisposable(() => URL.revokeObjectURL(url));',
        'this._objectUrl.value = objectUrl;');
      // 业务文件删除及读取失败不能继续把旧图当作当前成功；其余原生文件沿原版行为。
      contents = replaceOnce(contents, 'if (e.contains(input.resource, FileChangeType.UPDATED, FileChangeType.ADDED)) {',
        "if (e.contains(input.resource, FileChangeType.UPDATED, FileChangeType.ADDED) || input.resource.scheme === 'boxteam-file' && e.contains(input.resource, FileChangeType.DELETED)) {");
      const swallowed = `} catch {
\t\t\t// Keep showing the previous image, e.g. when the file is in the middle of being written
\t\t\treturn;
\t\t}`;
      contents = replaceOnce(contents, swallowed, `} catch (error) {
if (input.resource.scheme === 'boxteam-file' && this._isCurrentLoad(loadId, input)) {
  this._viewer?.clear();
  this._objectUrl.clear();
  this._fileSize = undefined;
  this._setMessage(localize('imagePreview.readFailed', "Unable to read image: {0}", error instanceof Error ? error.message : String(error)));
  this._onDidChangeImageInfo.fire();
}
return;
}`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts')) {
      const reference = 'private cachedTextFileModelReference: IReference<ITextFileEditorModel> | undefined = undefined;';
      contents = replaceOnce(contents, reference, `${reference}
private pendingTextFileModelReference: Promise<IReference<ITextFileEditorModel>> | undefined;`);
      const acquire = `if (!this.cachedTextFileModelReference) {
\t\t\t\tthis.cachedTextFileModelReference = await this.textModelService.createModelReference(this.resource) as IReference<ITextFileEditorModel>;
\t\t\t}

\t\t\tconst model = this.cachedTextFileModelReference.object;`;
      // 分屏并发解析共用同一在途引用，只有取得引用的 Promise 写入缓存；关闭时仍由原 input 释放。
      contents = replaceOnce(contents, acquire, `const reference = this.cachedTextFileModelReference ?? await (this.pendingTextFileModelReference ??=
this.textModelService.createModelReference(this.resource).then(reference => {
  this.cachedTextFileModelReference = reference as IReference<ITextFileEditorModel>;
  return this.cachedTextFileModelReference;
}).finally(() => { this.pendingTextFileModelReference = undefined; }));
const model = reference.object;`);
    }
    if (path === join(upstream, 'src/vs/editor/contrib/documentSymbols/browser/outlineModel.ts')) {
      // 旧版本的最后一个消费者取消时，只释放原分析条目，不能删掉后来建立的新版本缓存。
      const cancel = `data.source.cancel();
\t\t\t\tthis._cache.delete(textModel.id);`;
      contents = replaceOnce(contents, cancel, `data.source.cancel();
if (this._cache.peek(textModel.id) === data) this._cache.delete(textModel.id);`);
    }
    if (path === join(upstream, 'src/vs/workbench/services/textfile/common/textEditorService.ts')) {
      contents = replaceOnce(contents, 'const textResourceEditorInput = input as IUntypedFileEditorInput;',
        'const textResourceEditorInput = normalizeReadonlyInput(input as IUntypedFileEditorInput);');
      contents = `import { normalizeReadonlyInput } from ${JSON.stringify(join(root, 'src/readonlyDocuments.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/services/configuration/common/configurationCache.ts')) {
      const save = `\tasync save(content: string): Promise<void> {
\t\tconst created = await this.createCachedFolder();
\t\tif (created) {
\t\t\tawait this.queue.queue(async () => {
\t\t\t\tawait this.fileService.writeFile(this.cachedConfigurationFileResource, VSBuffer.fromString(content));
\t\t\t});
\t\t}
\t}`;
      // 目录准备和写入共用原条目队列，避免迟到保存越过更新／删除；FileService 已负责幂等递归建目录。
      contents = replaceOnce(contents, save, `\tasync save(content: string): Promise<void> {
\t\tawait this.queue.queue(async () => {
\t\t\tawait this.fileService.createFolder(this.cachedConfigurationFolderResource);
\t\t\tawait this.fileService.writeFile(this.cachedConfigurationFileResource, VSBuffer.fromString(content));
\t\t});
\t}`);
      const createFolder = `\n\tprivate async createCachedFolder(): Promise<boolean> {
\t\tif (await this.fileService.exists(this.cachedConfigurationFolderResource)) {
\t\t\treturn true;
\t\t}
\t\ttry {
\t\t\tawait this.fileService.createFolder(this.cachedConfigurationFolderResource);
\t\t\treturn true;
\t\t} catch (error) {
\t\t\treturn false;
\t\t}
\t}\n`;
      contents = replaceOnce(contents, createFolder, '');
    }
    if (path === join(upstream, 'src/vs/workbench/services/search/common/queryBuilder.ts')) {
      const commonQuery = 'private commonQuery(folderResources: (IWorkspaceFolderData | URI)[] = [], options: ICommonQueryBuilderOptions = {}): ICommonQueryProps<uri> {';
      contents = replaceOnce(contents, commonQuery, `${commonQuery}\noptions = scopeQueryOptions(this.workspaceContextService, folderResources, options);`);
      const queryProps = 'const queryProps: ICommonQueryProps<uri> = {';
      contents = replaceOnce(contents, queryProps, `assertSearchFolders(this.workspaceContextService, folderQueries);\n${queryProps}`);
      // 业务 Provider 按完整 URI 选精确已打开文件，避免原 fsPath 分支重建为其它 authority 的根。
      contents = replaceOnce(contents, 'if (options.onlyOpenEditors) {', 'if (options.onlyOpenEditors && !scopedSearch(this.workspaceContextService)) {');
      contents = `import { scopeQueryOptions, assertSearchFolders, scopedSearch } from ${JSON.stringify(join(root, 'src/search/scope.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/quickaccess.ts')) {
      // hide 会释放 Provider；异步关闭前先消费本次状态，不能在 await 后读取已 reset 的字段。
      contents = replaceOnce(contents, '\tasync restore(): Promise<void> {\n\t\tif (this.editorViewState) {\n\t\t\tfor (const editor of this.openedTransientEditors) {',
        '\tasync restore(): Promise<void> {\n\t\tconst editorViewState = this.editorViewState;\n\t\tconst openedTransientEditors = [...this.openedTransientEditors];\n\t\tthis.reset();\n\t\tif (editorViewState) {\n\t\t\tfor (const editor of openedTransientEditors) {');
      contents = replaceOnce(contents, '\t\t\tawait this.editorViewState.group.openEditor(this.editorViewState.editor, {\n\t\t\t\tviewState: this.editorViewState.state,',
        '\t\t\tawait editorViewState.group.openEditor(editorViewState.editor, {\n\t\t\t\tviewState: editorViewState.state,');
      contents = replaceOnce(contents, '\n\t\t\tthis.reset();\n\t\t}\n\t}\n\n\treset()', '\n\t\t}\n\t}\n\n\treset()');
    }
    if (path === join(upstream, 'src/vs/platform/quickinput/common/quickAccess.ts')) {
      contents = replaceOnce(contents, 'export interface IQuickAccessOptions {',
        'export interface IQuickAccessOptions {\n/** 仅 pick 显式启用原 QuickPick 多选，show 保持原打开行为。 */\nreadonly canPickMany?: boolean;');
    }
    if (path === join(upstream, 'src/vs/platform/quickinput/browser/quickAccess.ts')) {
      contents = replaceOnce(contents, '@IContextKeyService private readonly contextKeyService: IContextKeyService',
        '@IContextKeyService private readonly contextKeyService: IContextKeyService,\n@IEditorService private readonly editors: IEditorService');
      const entry = 'private doShowOrPick(value: string, pick: boolean, options?: IQuickAccessOptions): Promise<IQuickPickItem[] | undefined> | void {';
      contents = replaceOnce(contents, entry, `${entry}\noptions = prepareSearchInvocation(options, this.editors);`);
      const picker = 'const picker = disposables.add(this.quickInputService.createQuickPick({ useSeparators: true }));';
      contents = replaceOnce(contents, picker, `${picker}\nconst canPickMany = pick && options?.canPickMany === true;\npicker.canSelectMany = canPickMany;`);
      contents = replaceOnce(contents, 'let pickPromise: DeferredPromise<IQuickPickItem[]> | undefined = undefined;',
        'let pickPromise: DeferredPromise<IQuickPickItem[]> | undefined = undefined;\nlet didAccept = false;');
      contents = replaceOnce(contents, 'e.veto();\n\t\t\t\tpicker.hide();', 'e.veto();\n\t\t\t\tdidAccept = true;\n\t\t\t\tpicker.hide();');
      // 勾选会提前写 selectedItems，只有明确接受才能将多选结果交给调用方。
      contents = replaceOnce(contents, 'if (picker.selectedItems.length === 0) {',
        'const selectedItems = canPickMany && !didAccept ? [] : picker.selectedItems.slice(0);\nif (selectedItems.length === 0) {');
      contents = replaceOnce(contents, 'pickPromise?.complete(picker.selectedItems.slice(0));', 'pickPromise?.complete(selectedItems);');
      contents = `import { prepareSearchInvocation } from ${JSON.stringify(join(root, 'src/search/target.js'))};\nimport { IEditorService } from ${JSON.stringify(join(root, 'upstream/src/vs/workbench/services/editor/common/editorService.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/platform/quickinput/browser/helpQuickAccess.ts')) {
      contents = replaceOnce(contents, 'provide(picker: IQuickPick<IHelpQuickAccessPickItem, { useSeparators: true }>): IDisposable {',
        'provide(picker: IQuickPick<IHelpQuickAccessPickItem, { useSeparators: true }>, _token: CancellationToken, options?: AnythingQuickAccessProviderRunOptions): IDisposable {');
      contents = replaceOnce(contents, 'this.quickInputService.quickAccess.show(item.prefix, { preserveValue: true });',
        'const providerOptions: AnythingQuickAccessProviderRunOptions = { ...options, includeHelp: item.prefix ? options?.includeHelp : false };\nthis.quickInputService.quickAccess.show(item.prefix, { preserveValue: true, providerOptions });');
      contents = replaceOnce(contents, 'this.quickInputService.quickAccess.show(providerDescriptor.prefix, { preserveValue: true });',
        'this.quickInputService.quickAccess.show(providerDescriptor.prefix, { preserveValue: true, providerOptions: options });');
      contents = `import type { CancellationToken } from '../../../base/common/cancellation.js';\nimport type { AnythingQuickAccessProviderRunOptions } from '../common/quickAccess.js';\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/search.contribution.ts')) {
      contents = replaceOnce(contents, 'ctorDescriptor: new SyncDescriptor(SearchView),', 'ctorDescriptor: new SyncDescriptor(ScopedSearchView),');
      contents = replaceOnce(contents, "import { SearchView } from './searchView.js';",
        `import { ScopedSearchView } from ${JSON.stringify(join(root, 'src/search/searchView.js'))};`);
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({', 'quickAccessRegistry.registerQuickAccessProvider(selectTextSearchDescriptor({');
      contents = replaceOnce(contents, '\n});\n\n// Configuration', '\n}));\n\n// Configuration');
      contents = `import { selectTextSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/textQuickAccess.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchQuickAccess.contribution.ts')) {
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({\n\tctor: AnythingQuickAccessProvider,',
        'quickAccessRegistry.registerQuickAccessProvider(selectDefaultSearchDescriptor({\n\tctor: AnythingQuickAccessProvider,');
      contents = replaceOnce(contents, '\n});\n\nquickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,',
        '\n}));\n\nquickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,');
      contents = replaceOnce(contents, 'quickAccessRegistry.registerQuickAccessProvider({\n\tctor: SymbolsQuickAccessProvider,',
        'quickAccessRegistry.registerQuickAccessProvider(selectSymbolSearchDescriptor({\n\tctor: SymbolsQuickAccessProvider,');
      contents = replaceOnce(contents, "commandId: 'workbench.action.showAllSymbols' }]\n});", "commandId: 'workbench.action.showAllSymbols' }]\n}));");
      contents = `import { selectDefaultSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/quickAccess.js'))};\nimport { selectSymbolSearchDescriptor } from ${JSON.stringify(join(root, 'src/search/symbolsQuickAccess.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/browser/parts/views/viewPaneContainer.ts')) {
      contents = replaceOnce(contents, 'return this.instantiationService.createInstance(viewDescriptor.ctorDescriptor.ctor, ...(viewDescriptor.ctorDescriptor.staticArguments || []), options);',
        'return createSearchView(this.instantiationService, viewDescriptor, options);');
      contents = `import { createSearchView } from ${JSON.stringify(join(root, 'src/search/viewFactory.js'))};\n` + contents;
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchView.ts')) {
      contents = replaceOnce(contents, '@IWorkspaceContextService private readonly contextService: IWorkspaceContextService,',
        '@IWorkspaceContextService protected readonly contextService: IWorkspaceContextService,');
      contents = replaceOnce(contents, 'private pauseSearching = false;', 'protected pauseSearching = false;');
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchTreeModel/searchModel.ts')) {
      const declaration = 'export class SearchModelImpl extends Disposable implements ISearchModel {';
      contents = replaceOnce(contents, declaration, `${declaration}\npublic ownSearchContext(context: IDisposable): void { this._register(context); }`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/searchTreeModel/match.ts')) {
      // lcut 允许整词超出长度；有界片段的长前缀不能耗尽额度并隐藏真正命中。
      const before = "const fullBefore = this._oneLinePreviewText.substring(0, this._rangeInPreviewText.startColumn - 1), before = lcut(fullBefore, 26, '…');";
      contents = replaceOnce(contents, before, `const fullBefore = this._oneLinePreviewText.substring(0, this._rangeInPreviewText.startColumn - 1);
let before = lcut(fullBefore, 26, '…');
if (before.length > 26) before = '…' + Array.from(before).slice(-26).join('');`);
    }
    if (path === join(upstream, 'src/vs/workbench/contrib/search/browser/quickTextSearch/textSearchQuickAccess.ts')) {
      // 由原 QueryBuilder 消费用户的 search.useIgnoreFiles，交目标文件 owner 解释规则。
      contents = replaceOnce(contents, '\tdisregardIgnoreFiles: false,\n', '');
      const transfer = `\t\tthis._viewsService.openView(VIEW_ID, false);
\t\tconst viewlet: SearchView | undefined = this._viewsService.getActiveViewWithId(VIEW_ID) as SearchView;
\t\tawait viewlet.replaceSearchModel(this.searchModel, this.currentAsyncSearch);

\t\tthis.searchModel = this._instantiationService.createInstance(SearchModelImpl);
\t\tthis.searchModel.location = SearchModelLocation.QUICK_ACCESS;`;
      contents = replaceOnce(contents, transfer, `const transferredModel = this.searchModel;
this._store.deleteAndLeak(transferredModel);
transferSearchContext(this._contextService, transferredModel);
const asyncSearch = this.currentAsyncSearch;
this.searchModel = this._register(this._instantiationService.createInstance(SearchModelImpl));
this.searchModel.location = SearchModelLocation.QUICK_ACCESS;
let viewlet: SearchView;
try {
  const view = await this._viewsService.openView<SearchView>(VIEW_ID, false);
  if (!view) throw new Error('原搜索视图不可用');
  viewlet = view;
  await viewlet.replaceSearchModel(transferredModel, asyncSearch);
} catch (error) {
  transferredModel.dispose();
  throw error;
}`);
      contents = `import { transferSearchContext } from ${JSON.stringify(join(root, 'src/search/scope.js'))};\n` + contents;
    }
    contents = transformOutline(root, path, contents, replaceOnce);
    contents = transformMcp(root, path, contents, replaceOnce);
    return transformSearchEditor(root, path, contents, replaceOnce);
  };
}

export function buildSourcePlugin(root, transformSource) {
  return {
    name: 'upstream-build-configuration',
    setup(builder) {
      builder.onResolve({ filter: /\.css\?text$/ }, ({ path, resolveDir }) => ({ path: resolve(resolveDir, path.slice(0, -5)), namespace: 'session-stylesheet' }));
      builder.onLoad({ filter: /.*/, namespace: 'session-stylesheet' }, async ({ path }) => ({ contents: await readFile(path, 'utf8'), loader: 'text' }));
      builder.onResolve({ filter: /^\.\/codicon\.ttf$/ }, () => ({ path: join(root, 'node_modules/@vscode/codicons/dist/codicon.ttf') }));
      builder.onLoad({ filter: /\.ts$/ }, async ({ path }) => {
        if (path.endsWith('.d.ts')) return;
        const source = await readFile(path, 'utf8');
        const contents = await transformSource(path, source);
        if (contents !== source) return { contents, loader: 'ts' };
      });
      builder.onResolve({ filter: /node_modules\// }, ({ path }) => ({ path: join(root, 'node_modules', path.split('node_modules/').at(-1)) }));
    },
  };
}
