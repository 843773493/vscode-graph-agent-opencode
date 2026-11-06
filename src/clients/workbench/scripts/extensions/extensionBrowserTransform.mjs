import { join } from 'node:path';

export const extensionBrowserSourcePaths = ['src/vs/workbench/services/extensions/browser/extensionService.ts'];

export function transformExtensionBrowser(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', extensionBrowserSourcePaths[0])) return source;
  let contents = `import { scanTargetExtensions } from ${JSON.stringify(join(root, 'src/extensions/inventory.js'))};
import { TargetExtensionHost } from ${JSON.stringify(join(root, 'src/extensions/targetHost.js'))};\n` + source;
  contents = replaceOnce(contents, 'ExtensionIdentifier, IExtensionDescription', 'ExtensionIdentifier, ExtensionInstallationTarget, IExtension, bindExtensionDescription, IExtensionDescription');
  contents = replaceOnce(contents, "import { IRemoteExtensionHostDataProvider, IRemoteExtensionHostInitData, RemoteExtensionHost } from '../common/remoteExtensionHost.js';\n", '');
  contents = replaceOnce(contents, 'private async _scanWebExtensions(): Promise<IExtensionDescription[]> {', `private async _scanWebExtensions(): Promise<IExtensionDescription[]> {
    const target = { kind: 'web' as const, profileId: this._userDataProfileService.currentProfile.id };`);
  contents = replaceOnce(contents, '\t\t\t\t} catch (error) {\n\t\t\t\t\tthis._logService.error(error);\n\t\t\t\t}', '\t\t\t\t} catch (error) {\n\t\t\t\t\tthis._logService.error(error);\n          throw error;\n\t\t\t\t}');
  contents = replaceOnce(contents, 'return dedupExtensions(system, user, [], development, this._logService);', 'return dedupExtensions(system, user, [], development, this._logService).map(extension => bindExtensionDescription(extension, target));');
  const resolveStart = contents.indexOf('\tprivate async _resolveExtensionsDefault(');
  const resolveEnd = contents.indexOf('\n\tprotected _resolveExtensions()', resolveStart);
  contents = replaceOnce(contents, contents.slice(resolveStart, resolveEnd), `
  private async _resolveExtensionsDefault(emitter: AsyncIterableEmitter<ResolvedExtensions>) {
    const [localExtensions, inventory] = await Promise.all([this._scanWebExtensions(), scanTargetExtensions(this._workspaceContext)]);
    this._setExtensionWorkspaceViews(inventory.views);
    emitter.emitOne(new RemoteExtensions(inventory.extensions));
    emitter.emitOne(new LocalExtensions(localExtensions));
  }
`);
  contents = replaceOnce(contents, '@IWorkspaceContextService contextService: IWorkspaceContextService,', '@IWorkspaceContextService private readonly _workspaceContext: IWorkspaceContextService,');
  contents = replaceOnce(contents, '\n\t\t\tcontextService,', '\n\t\t\t_workspaceContext,');
  contents = replaceOnce(contents, '\tprivate _initFetchFileSystem(): void {', `
  protected override _bindExtension(extension: IExtension): IExtension {
    // 这些事件来自原 Web management owner；Backend 事件必须携带其原生可信 target。
    return { ...extension, installationTarget: extension.installationTarget ?? { kind: 'web', profileId: this._userDataProfileService.currentProfile.id } };
  }
  protected override _removalTarget(extension?: IExtension): ExtensionInstallationTarget {
    return extension?.installationTarget ?? { kind: 'web', profileId: this._userDataProfileService.currentProfile.id };
  }
\tprivate _initFetchFileSystem(): void {`);
  // 产品不运行单 remoteAuthority resolver；目标身份只从明确目录的实时发现取得。
  const doStart = contents.indexOf('\tprivate async _doResolveExtensions(');
  const doEnd = contents.indexOf('\n\tprotected async _onExtensionHostExit', doStart);
  contents = replaceOnce(contents, contents.slice(doStart, doEnd), `
  private async _doResolveExtensions(emitter: AsyncIterableEmitter<ResolvedExtensions>): Promise<void> {
    return this._resolveExtensionsDefault(emitter);
  }
`);
  contents = replaceOnce(contents, '\t\t\tremoteAgentService,\n\t\t\tremoteAuthorityResolverService,\n\t\t\textensionEnablementService,', '\t\t\textensionEnablementService,');
  contents = replaceOnce(contents, '\t\t@IRemoteAgentService private readonly _remoteAgentService: IRemoteAgentService,\n\t\t@IRemoteAuthorityResolverService private readonly _remoteAuthorityResolverService: IRemoteAuthorityResolverService,\n', '');
  const remoteStart = contents.indexOf('\t\t\tcase ExtensionHostKind.Remote: {');
  const remoteEnd = contents.indexOf('\n\t\t}\n\t}', remoteStart);
  contents = replaceOnce(contents, contents.slice(remoteStart, remoteEnd), `
      case ExtensionHostKind.Remote: {
        return this._instantiationService.createInstance(TargetExtensionHost, runningLocation, runningLocations, this._getExtensionRegistrySnapshotWhenReady);
      }`);
  const providerStart = contents.indexOf('\tprivate _createLocalExtensionHostDataProvider(');
  const factoryEnd = contents.indexOf('\n}\n\nexport class BrowserExtensionHostKindPicker', providerStart);
  contents = replaceOnce(contents, contents.slice(providerStart, factoryEnd), `
  private _createLocalExtensionHostDataProvider(runningLocations: ExtensionRunningLocationTracker, desiredRunningLocation: ExtensionRunningLocation, _isInitialStart: boolean): IWebWorkerExtensionHostDataProvider {
    return { getInitData: async (): Promise<IWebWorkerExtensionHostInitData> => {
      const snapshot = await this._getExtensionRegistrySnapshotWhenReady();
      const all = runningLocations.getHostExtensions(snapshot.extensions, desiredRunningLocation);
      const mine = runningLocations.filterByRunningLocation(all, desiredRunningLocation);
      return { extensions: new ExtensionHostExtensions(snapshot.versionId, all, mine.map(extension => extension.identifier)) };
    } };
  }
`);
  // UI-only Node 不能冒充 workspace 扩展；Backend 上的 browser-only 安装也不能混入窗口 Web profile。
  const picker = contents.indexOf('\tpublic static pickRunningLocation(');
  const bodyStart = contents.indexOf('\n\t\tconst result:', picker);
  contents = replaceOnce(contents, contents.slice(bodyStart, contents.lastIndexOf('\n\t}\n}')), `
    if (isInstalledRemotely) return extensionKinds.includes('workspace') ? ExtensionHostKind.Remote : null;
    return isInstalledLocally && extensionKinds.includes('web') ? ExtensionHostKind.LocalWebWorker : null;
`);
  contents = replaceOnce(contents, `\t\tconst extensionHostFactory = new BrowserExtensionHostFactory(
\t\t\textensionsProposedApi,
\t\t\t() => this._scanWebExtensions(),
\t\t\t() => this._getExtensionRegistrySnapshotWhenReady(),
\t\t\tinstantiationService,
\t\t\textensionEnablementService,
\t\t\tlogService
\t\t);`, `const extensionHostFactory = new BrowserExtensionHostFactory(() => this._getExtensionRegistrySnapshotWhenReady(), instantiationService);`);
  const ctorStart = contents.indexOf('\n\tconstructor(', contents.indexOf('class BrowserExtensionHostFactory'));
  const ctorEnd = contents.indexOf('\n\tcreateExtensionHost(', ctorStart);
  contents = replaceOnce(contents, contents.slice(ctorStart, ctorEnd), `
  constructor(private readonly _getExtensionRegistrySnapshotWhenReady: () => Promise<ExtensionDescriptionRegistrySnapshot>,
    @IInstantiationService private readonly _instantiationService: IInstantiationService) { }
`);
  contents = contents.replace("import { PersistentConnectionEventType } from '../../../../platform/remote/common/remoteAgentConnection.js';\n", '');
  contents = contents.replace('IRemoteAuthorityResolverService, RemoteAuthorityResolverError, ResolverResult', 'IRemoteAuthorityResolverService, ResolverResult');
  contents = contents.replace('ResolvedExtensions, ResolverExtensions, checkEnabledAndProposedAPI, isResolverExtension', 'ResolvedExtensions');
  contents = contents.replace('ExtensionRunningLocationTracker, filterExtensionDescriptions', 'ExtensionRunningLocationTracker');
  contents = contents.replace('private readonly _browserEnvironmentService:', '_browserEnvironmentService:');
  contents = contents.replace(/^import \{ IWorkspaceTrustManagementService \}[^\n]+\n/m, '');
  contents = contents.replace(/^import \{ IRemoteExplorerService \}[^\n]+\n/m, '');
  contents = contents.replace(/^\t\t@IWorkspaceTrustManagementService[^\n]+\n/m, '');
  return contents.replace(/^\t\t@IRemoteExplorerService[^\n]+\n/m, '');
}
