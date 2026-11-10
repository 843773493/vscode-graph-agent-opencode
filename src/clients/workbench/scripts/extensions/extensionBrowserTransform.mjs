import { join } from 'node:path';

export const extensionBrowserSourcePaths = ['src/vs/workbench/services/extensions/browser/extensionService.ts'];

/** 原窗口 Worker 只加载产品内置贡献；不再发现或连接用户目标宿主。 */
export function transformExtensionBrowser(root, path, source, replaceOnce) {
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
