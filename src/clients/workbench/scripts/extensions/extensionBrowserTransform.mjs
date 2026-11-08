import { join } from 'node:path';
import ts from 'typescript';

export const extensionBrowserSourcePaths = ['src/vs/workbench/services/extensions/browser/extensionService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensions.contribution.ts',
  'src/vs/workbench/services/extensions/browser/webWorkerExtensionHost.ts',
  'src/vs/workbench/services/extensions/common/extensionHostProtocol.ts'];

export function transformExtensionBrowser(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[2])) {
    let contents = replaceOnce(source, 'private readonly _onDidExit', 'protected readonly _onDidExit');
    return replaceOnce(contents, 'private async _createExtHostInitData()', 'protected async _createExtHostInitData()');
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[3])) {
    return replaceOnce(source, 'export interface IExtensionHostInitData {', 'export interface IExtensionHostInitData {\n  readonly isTargetExtensionHost?: boolean;');
  }
  if (path === join(root, 'upstream', extensionBrowserSourcePaths[1])) return transformInstallationActions(root, path, source, replaceOnce);
  if (path !== join(root, 'upstream', extensionBrowserSourcePaths[0])) return source;
  let contents = `import { scanTargetExtensions, reopenTargetWebHost } from ${JSON.stringify(join(root, 'src/extensions/inventory.js'))};
import { browserGatewayId } from ${JSON.stringify(join(root, '../web/src/hooks/settings/browserUiSettings.js'))};
import { TargetExtensionHost } from ${JSON.stringify(join(root, 'src/extensions/targetHost.js'))};
import { TargetWebExtensionHost } from ${JSON.stringify(join(root, 'src/extensions/webHost.js'))};
import { DisposableStore } from '../../../../base/common/lifecycle.js';\n` + source;
  contents = replaceOnce(contents, 'export class ExtensionService extends AbstractExtensionService implements IExtensionService {',
    'export class ExtensionService extends AbstractExtensionService implements IExtensionService {\n  private readonly _targetExtensionLifetime = this._register(new DisposableStore());');
  contents = replaceOnce(contents, 'ExtensionIdentifier, IExtensionDescription', 'ExtensionIdentifier, ExtensionInstallationTarget, IExtension, bindExtensionDescription, IExtensionDescription');
  contents = replaceOnce(contents, "import { IRemoteExtensionHostDataProvider, IRemoteExtensionHostInitData, RemoteExtensionHost } from '../common/remoteExtensionHost.js';\n", '');
  contents = replaceOnce(contents, 'private async _scanWebExtensions(): Promise<IExtensionDescription[]> {', `private async _scanWebExtensions(): Promise<IExtensionDescription[]> {
    const target = { kind: 'web' as const, profileId: this._userDataProfileService.currentProfile.id };`);
  contents = replaceOnce(contents, '\t\t\t\t} catch (error) {\n\t\t\t\t\tthis._logService.error(error);\n\t\t\t\t}', '\t\t\t\t} catch (error) {\n\t\t\t\t\tthis._logService.error(error);\n          throw error;\n\t\t\t\t}');
  contents = replaceOnce(contents, 'scanUserExtensions(this._userDataProfileService.currentProfile.extensionsResource, { skipInvalidExtensions: true })', 'scanCustomBuiltinExtensions({ skipInvalidExtensions: true })');
  contents = replaceOnce(contents, 'return dedupExtensions(system, user, [], development, this._logService);', 'return dedupExtensions(system, user, [], development, this._logService).map(extension => bindExtensionDescription(extension, target));');
  const resolveStart = contents.indexOf('\tprivate async _resolveExtensionsDefault(');
  const resolveEnd = contents.indexOf('\n\tprotected _resolveExtensions()', resolveStart);
  contents = replaceOnce(contents, contents.slice(resolveStart, resolveEnd), `
  private async _resolveExtensionsDefault(emitter: AsyncIterableEmitter<ResolvedExtensions>) {
    const [localExtensions, inventory] = await Promise.all([this._scanWebExtensions(),
      browserGatewayId(0).then(gatewayId => scanTargetExtensions(this._workspaceContext,
        extension => this._manifestProperties.getExtensionKind(extension), this._targetExtensionLifetime, gatewayId, this._logService))]);
    this._setExtensionWorkspaceViews(inventory.views);
    emitter.emitOne(new RemoteExtensions(inventory.extensions));
    emitter.emitOne(new LocalExtensions(localExtensions));
  }
`);
  contents = replaceOnce(contents, '@IWorkspaceContextService contextService: IWorkspaceContextService,', '@IWorkspaceContextService private readonly _workspaceContext: IWorkspaceContextService,');
  contents = replaceOnce(contents, '@IExtensionManifestPropertiesService extensionManifestPropertiesService: IExtensionManifestPropertiesService,', '@IExtensionManifestPropertiesService private readonly _manifestProperties: IExtensionManifestPropertiesService,');
  contents = replaceOnce(contents, '\n\t\t\textensionManifestPropertiesService,', '\n\t\t\t_manifestProperties,');
  contents = replaceOnce(contents, '\n\t\t\tcontextService,', '\n\t\t\t_workspaceContext,');
  contents = replaceOnce(contents, '\tprivate _initFetchFileSystem(): void {', `
  protected override async _prepareExtensionHostRestart(location: ExtensionRunningLocation, pending: DisposableStore) {
    if (location.kind !== ExtensionHostKind.LocalWebWorker || !location.installationTarget) return undefined;
    const view = this._runningLocations.workspaceViews.find(view =>
      installationTargetKey(view.installationTarget) === installationTargetKey(location.installationTarget!));
    if (!view?.webHost || location.viewId !== view.viewId || JSON.stringify(location.workspaceIds) !== JSON.stringify(view.workspaceIds)) {
      throw new Error('目标 Web 宿主恢复没有匹配原工作区视图，请重新载入窗口');
    }
    const previous = this._runningLocations.filterByRunningLocation(this.extensions, location);
    const next = await reopenTargetWebHost(view, previous, pending);
    await this._rebindWebExtensionHost(view, next.webHost, previous, next.extensions, pending);
    return next.webHost;
  }
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
  contents = replaceOnce(contents, 'return this._instantiationService.createInstance(WebWorkerExtensionHost, runningLocation, startup, this._createLocalExtensionHostDataProvider(runningLocations, runningLocation, isInitialStart));', `const provider = this._createLocalExtensionHostDataProvider(runningLocations, runningLocation, isInitialStart);
        const view = runningLocation.installationTarget ? runningLocations.workspaceViews.find(view =>
          installationTargetKey(view.installationTarget) === installationTargetKey(runningLocation.installationTarget!)) : undefined;
        if (runningLocation.installationTarget) {
          if (!view?.webHost) throw new Error('目标 Web Worker 缺少该视图的冻结发布资格');
          return this._instantiationService.createInstance(TargetWebExtensionHost, runningLocation, startup, provider, view.webHost);
        }
        return this._instantiationService.createInstance(WebWorkerExtensionHost, runningLocation, startup, provider);`);
  contents = replaceOnce(contents, 'ExtensionIdentifier, ExtensionInstallationTarget, IExtension, bindExtensionDescription, IExtensionDescription',
    'ExtensionIdentifier, ExtensionInstallationTarget, IExtension, bindExtensionDescription, installationTargetKey, IExtensionDescription');
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

function transformInstallationActions(root, path, source, replaceOnce) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const actions = new Map();
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(parsed) === 'this.registerExtensionAction') {
      const options = node.arguments[0];
      if (options && ts.isObjectLiteralExpression(options)) {
        const fields = new Map(options.properties.filter(ts.isPropertyAssignment).map(field => [field.name.getText(parsed), field.initializer]));
        const id = fields.get('id')?.getText(parsed);
        if (id === 'SELECT_INSTALL_VSIX_EXTENSION_COMMAND_ID' || id === 'INSTALL_EXTENSION_FROM_VSIX_COMMAND_ID') {
          if (actions.has(id) || !fields.has('run') || !fields.has('menu')) throw new Error('VSIX 原安装 action 锚点不唯一或不完整');
          actions.set(id, fields);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(parsed);
  if (actions.size !== 2) throw new Error('未找到两条原 VSIX 安装 action');
  let contents = source;
  for (const [id, fields] of actions) {
    const select = id === 'SELECT_INSTALL_VSIX_EXTENSION_COMMAND_ID';
    contents = replaceOnce(contents, fields.get('run').getText(parsed), select
      ? '(accessor: ServicesAccessor) => installTargetVsix(accessor, { select: true })'
      : '(accessor: ServicesAccessor, resources: URI[] | URI) => installTargetVsix(accessor, { resources })');
    contents = replaceOnce(contents, fields.get('menu').getText(parsed), select ? `[
      { id: MenuId.CommandPalette },
      { id: MenuId.ViewContainerTitle, when: ContextKeyExpr.equals('viewContainer', VIEWLET_ID), group: '3_install', order: 1 }
    ]` : `[{ id: MenuId.ExplorerContext, group: 'extensions',
      when: ContextKeyExpr.and(ResourceContextKey.Extension.isEqualTo('.vsix'), ResourceContextKey.Scheme.isEqualTo('boxteam-file')) }]`);
  }
  contents = replaceOnce(contents, "import { mnemonicButtonLabel } from '../../../../base/common/labels.js';\n", '');
  contents = replaceOnce(contents, "import { INotificationService, Severity } from '../../../../platform/notification/common/notification.js';\n", '');
  contents = replaceOnce(contents, "import { IHostService } from '../../../services/host/browser/host.js';\n", '');
  contents = replaceOnce(contents, 'ExtensionEditorTab, ExtensionRuntimeActionType,', 'ExtensionEditorTab,');
  return `import { installTargetVsix } from ${JSON.stringify(join(root, 'src/extensions/installation.js'))};\n` + contents;
}
