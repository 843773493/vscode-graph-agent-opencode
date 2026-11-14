import { join } from 'node:path';
import ts from 'typescript';
import { pruneUnusedImportsAndConstants } from './extensionManagementTransform.mjs';

export const extensionBrowserSourcePaths = [
  'src/vs/workbench/services/extensions/browser/extensionService.ts',
  'src/vs/workbench/services/extensions/browser/webWorkerExtensionHost.ts',
  'src/vs/workbench/services/extensions/common/workspaceContains.ts',
  'src/vs/workbench/contrib/chat/browser/remoteAgentHost/remoteAgentHost.contribution.ts',
  'src/vs/workbench/contrib/chat/browser/agentSessions/agentHost/agentHost.contribution.ts',
  'src/vs/workbench/services/agentHost/browser/editorRemoteAgentHostServiceClient.ts',
  'src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'src/vs/workbench/contrib/chat/common/chatService/chatServiceImpl.ts',
  'src/vs/workbench/contrib/chat/browser/chatEditing/chatEditingServiceImpl.ts',
];

/** 原窗口 Worker 只加载产品内置贡献；不再发现或连接用户目标宿主。 */
export function transformExtensionBrowser(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[7])) {
    // React 会话由业务后端保存；没有原版 ChatModel 时不排入空的异步文件写入。
    let contents = replaceOnce(source, 'this._chatSessionStore.storeSessions(liveLocalChats);',
      'if (liveLocalChats.length) this._chatSessionStore.storeSessions(liveLocalChats);');
    return replaceOnce(contents, 'this._chatSessionStore.storeSessionsMetadataOnly(liveNonLocalChats);',
      'if (liveNonLocalChats.length) this._chatSessionStore.storeSessionsMetadataOnly(liveNonLocalChats);');
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[8])) {
    return replaceOnce(source, '\t\t\tstorageTask = Promise.resolve(storageTask)',
      '\t\t\tif (tasks.length === 0) return;\n\t\t\tstorageTask = Promise.resolve(storageTask)');
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[6])) {
    let contents = replaceOnce(source, "import { isCI } from '../../../../base/common/platform.js';",
      "import { isCI, isWeb } from '../../../../base/common/platform.js';");
    // Web 文档退出不能等待异步 join；内置 Worker 和通信资源沿原集合同步释放。
    contents = replaceOnce(contents, 'this._register(this._lifecycleService.onWillShutdown(event => {',
      'this._register(this._lifecycleService.onWillShutdown(event => {\nif (isWeb) { this._extensionHostManagers.dispose(); return; }');
    return contents;
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[5])) {
    // 本地 Web 没有配置 Code OSS Remote Authority 是正常状态；显式远端缺连接仍保留警告。
    return replaceOnce(source,
      'this._logService.warn(`${LOG_PREFIX} No remote agent connection available. Not connecting.`);',
      'if (environmentService.remoteAuthority) this._logService.warn(`${LOG_PREFIX} No remote agent connection available. Not connecting.`);');
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[1])) {
    // 这里只执行随产品交付的可信内置代码；用户插件由 Gateway 独立进程托管。
    // 同源 iframe 的 allow-scripts + allow-same-origin 本就不能形成安全边界。
    const start = source.indexOf('\t\tif (platform.isWeb) {');
    const end = source.indexOf('\n\t\tconst relativeExtensionHostIframeSrc', start);
    if (start < 0 || end < 0) throw new Error('内置 Worker 地址转换边界缺失');
    let contents = replaceOnce(source, source.slice(start, end), '');
    for (const line of [
      "import { parentOriginHash } from '../../../../base/browser/iframe.js';\n",
      "import { IStorageService, StorageScope, StorageTarget } from '../../../../platform/storage/common/storage.js';\n",
      '\t\t@IStorageService private readonly _storageService: IStorageService,\n',
      "\t\tiframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');\n",
    ]) contents = replaceOnce(contents, line, '');
    return contents;
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[2])) {
    // workbench 是窗口设置／布局的容器，业务工作区由 boxteam-file 搜索 Provider 持有。
    const entry = 'export function checkActivateWorkspaceContainsExtension(host: IExtensionActivationHost, desc: IExtensionDescription): Promise<IExtensionActivationResult | undefined> {';
    return replaceOnce(source, entry, `${entry}\n  host = { ...host, folders: host.folders.filter(folder => folder.scheme !== 'workbench') };\n  if (host.folders.length === 0) return Promise.resolve(undefined);`);
  }
  if (extensionBrowserSourcePaths.slice(3, 5).some(relative => path === join(root, 'upstream', relative))) {
    // 退役原 Copilot／GitHub 的自动连接贡献；通用服务仍按需登记。
    const registrations = /^registerWorkbenchContribution2\([^\n]+\);\n/gm;
    const expected = path === join(root, 'upstream', extensionBrowserSourcePaths[3]) ? 3 : 13;
    if ([...source.matchAll(registrations)].length !== expected) throw new Error('原 Agent 自动贡献数量与固定上游不匹配');
    let contents = source.replace(registrations, '');
    const parsed = ts.createSourceFile(path, contents, ts.ScriptTarget.Latest, true);
    for (const node of [...parsed.statements].reverse()) {
      if (ts.isClassDeclaration(node) && !node.modifiers?.some(item => item.kind === ts.SyntaxKind.ExportKeyword)) {
        contents = contents.slice(0, node.getFullStart()) + contents.slice(node.end);
      }
    }
    return pruneUnusedImportsAndConstants(path, contents);
  }
  if (path !== join(root, 'upstream', extensionBrowserSourcePaths[0])) return source;
  let contents = source;
  const replaceRegion = (start, end, replacement) => {
    const at = contents.indexOf(start), until = contents.indexOf(end, at);
    if (at < 0 || until < 0) throw new Error('内置 Worker 转换边界缺失：' + start);
    contents = replaceOnce(contents, contents.slice(at, until), replacement);
  };
  replaceRegion('\tprotected override async _initialize(): Promise<void> {', '\n\tprivate _scanWebExtensionsPromise', '');
  replaceRegion('\tprivate async _scanWebExtensions():', '\n\tprotected _resolveExtensions()', `
  private async _scanWebExtensions(): Promise<IExtensionDescription[]> {
    return this._scanWebExtensionsPromise ??= (async () => {
      const [system, custom] = await Promise.all([
        this._webExtensionsScannerService.scanSystemExtensions(),
        this._webExtensionsScannerService.scanCustomBuiltinExtensions({ skipInvalidExtensions: true }),
      ]);
      return dedupExtensions(system.map(e => toExtensionDescription(e)), custom.map(e => toExtensionDescription(e)), [], [], this._logService);
    })();
  }
`);
  replaceRegion('\tprivate async _doResolveExtensions(', '\n\tprotected async _onExtensionHostExit', `
  private async _doResolveExtensions(emitter: AsyncIterableEmitter<ResolvedExtensions>): Promise<void> {
    emitter.emitOne(new LocalExtensions(await this._scanWebExtensions()));
  }
`);
  replaceRegion('\tprivate _createRemoteExtensionHostDataProvider(', '\n}\n\nexport class BrowserExtensionHostKindPicker', '');
  replaceRegion('\t\t\tcase ExtensionHostKind.Remote: {', '\n\t\t}\n\t}', '\t\t\tcase ExtensionHostKind.Remote: return null;');
  replaceRegion('\tpublic static pickRunningLocation(', '\n\t}\n}\n\nregisterSingleton', `
  public static pickRunningLocation(extensionKinds: ExtensionKind[], isInstalledLocally: boolean, _isInstalledRemotely: boolean, _preference: ExtensionRunningPreference): ExtensionHostKind | null {
    return isInstalledLocally && extensionKinds.includes('web') ? ExtensionHostKind.LocalWebWorker : null;`);
  contents = replaceOnce(contents, '\t\t\tremoteAgentService,\n\t\t\tremoteAuthorityResolverService,\n\t\t\textensionEnablementService,', '\t\t\textensionEnablementService,');
  for (const line of [
    "import { PersistentConnectionEventType } from '../../../../platform/remote/common/remoteAgentConnection.js';\n",
    "import { IRemoteExtensionHostDataProvider, IRemoteExtensionHostInitData, RemoteExtensionHost } from '../common/remoteExtensionHost.js';\n",
    "import { IUserDataInitializationService } from '../../userData/browser/userDataInit.js';\n",
    "import { IUserDataProfileService } from '../../userDataProfile/common/userDataProfile.js';\n",
    "import { IWorkspaceTrustManagementService } from '../../../../platform/workspace/common/workspaceTrust.js';\n",
    "import { IRemoteExplorerService } from '../../remote/common/remoteExplorerService.js';\n",
    '\t\t@IUserDataInitializationService private readonly _userDataInitializationService: IUserDataInitializationService,\n',
    '\t\t@IUserDataProfileService private readonly _userDataProfileService: IUserDataProfileService,\n',
    '\t\t@IWorkspaceTrustManagementService private readonly _workspaceTrustManagementService: IWorkspaceTrustManagementService,\n',
    '\t\t@IRemoteExplorerService private readonly _remoteExplorerService: IRemoteExplorerService,\n',
    '\t\t@IRemoteAgentService private readonly _remoteAgentService: IRemoteAgentService,\n',
    '\t\t@IRemoteAuthorityResolverService private readonly _remoteAuthorityResolverService: IRemoteAuthorityResolverService,\n',
  ]) contents = replaceOnce(contents, line, '');
  contents = contents.replace('private readonly _browserEnvironmentService:', '_browserEnvironmentService:')
    .replace('IRemoteAuthorityResolverService, RemoteAuthorityResolverError, ResolverResult', 'IRemoteAuthorityResolverService, ResolverResult')
    .replace('LocalExtensions, RemoteExtensions, ResolvedExtensions, ResolverExtensions, checkEnabledAndProposedAPI, isResolverExtension', 'LocalExtensions, ResolvedExtensions, checkEnabledAndProposedAPI');
  return contents;
}
