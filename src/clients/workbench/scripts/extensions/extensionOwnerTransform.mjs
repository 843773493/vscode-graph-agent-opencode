import { join } from 'node:path';

export const extensionOwnerSourcePaths = [
  'src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'src/vs/workbench/services/extensions/common/extensions.ts',
  'src/vs/workbench/services/extensions/common/extensionHostManagers.ts',
  'src/vs/workbench/services/extensions/common/extensionHostManager.ts',
  'src/vs/workbench/services/extensions/common/lazyCreateExtensionHostManager.ts',
  'src/vs/workbench/services/extensions/common/extHostCustomers.ts',
  'src/vs/workbench/api/browser/mainThreadWorkspace.ts',
  'src/vs/workbench/api/browser/mainThreadCommands.ts',
  'src/vs/workbench/services/commands/common/commandService.ts',
  'src/vs/workbench/api/browser/mainThreadFileSystem.ts',
];

/** 原协调 owner 继续持有唯一窗口 registry；宿主只消费从它派生的合法快照。 */
export function transformExtensionOwner(root, path, source, replaceOnce) {
  const paths = extensionOwnerSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[9]) {
    // provider info 的 URI 只标记 scheme，没有文件身份；不能交给真实路径转换。
    let contents = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, '\n\tdispose(): void {\n\t\tthis._disposables.dispose();', `
  private providerInfo(scheme: string): UriComponents {
    return { scheme: this.extHostContext.extensionHost.uriTransformer?.transformOutgoingScheme(scheme) ?? scheme, path: '/dummy' };
  }
\n\tdispose(): void {\n\t\tthis._disposables.dispose();`);
    contents = contents.replaceAll("URI.from({ scheme: entry.scheme, path: '/dummy' })", 'this.providerInfo(entry.scheme)');
    return contents.replaceAll("URI.from({ scheme: e.scheme, path: '/dummy' })", 'this.providerInfo(e.scheme)');
  }
  if (path === paths[1]) {
    let contents = `import type { IURITransformer } from '../../../../base/common/uriIpc.js';
import type { ExtensionHostConnectionDTO } from ${JSON.stringify(join(root, '../web/src/types/protocol_generated/boxteam/gateway/v2/public.js'))};\n` + source;
    contents = replaceOnce(contents, 'ExtensionIdentifier, ExtensionIdentifierMap,', 'ExtensionIdentifier, ExtensionInstallationTarget, ExtensionIdentifierMap,');
    contents = replaceOnce(contents, 'export interface IExtensionHost {', `export interface IExtensionHost {
  readonly binding?: ExtensionHostConnectionDTO;
  readonly uriTransformer?: IURITransformer;`);
    contents = replaceOnce(contents, 'export interface IExtensionsStatus {', 'export interface IExtensionsStatus {\n  installationTarget: ExtensionInstallationTarget;');
    contents = replaceOnce(contents, 'export interface IMessage {', 'export interface IMessage {\n  installationTarget?: ExtensionInstallationTarget;');
    contents = replaceOnce(contents, '\tgetExtension(id: string): Promise<IExtensionDescription | undefined>;', '\tgetExtension(id: string, target?: ExtensionInstallationTarget): Promise<IExtensionDescription | undefined>;\n  getExtensionHosts(): readonly IExtensionHost[];');
    contents = replaceOnce(contents, 'activateByEvent(activationEvent: string, activationKind?: ActivationKind): Promise<void>;', 'activateByEvent(activationEvent: string, activationKind?: ActivationKind, host?: IExtensionHost): Promise<void>;');
    contents = replaceOnce(contents, 'canAddExtension(extension: IExtensionDescription): boolean;', 'canAddExtension(extension: IExtension): boolean;');
    contents = replaceOnce(contents, 'canRemoveExtension(extension: IExtensionDescription): boolean;', 'canRemoveExtension(extension: IExtension): boolean;');
    contents = replaceOnce(contents, '\tgetExtension() { return Promise.resolve(undefined); }', '\tgetExtension() { return Promise.resolve(undefined); }\n  getExtensionHosts(): readonly IExtensionHost[] { return []; }');
    contents = replaceOnce(contents, '\t\tvalidations: [],\n\t\tisValid: true,', '\t\tvalidations: extensionDescription.validations ?? [],\n\t\tisValid: extensionDescription.isValid ?? true,\n    installationTarget: extensionDescription.installationTarget,\n    enablementView: extensionDescription.enablementView,');
    contents = replaceOnce(contents, '\t\t...extension.manifest\n\t};', '\t\t...extension.manifest,\n    installationTarget: extension.installationTarget,\n    enablementView: extension.enablementView,\n    isValid: extension.isValid,\n    validations: extension.validations\n\t};');
    return contents;
  }
  if (path === paths[2]) {
    let contents = replaceOnce(source, 'ExtensionHostStartup, IExtensionInspectInfo', 'ExtensionHostStartup, IExtensionHost, IExtensionInspectInfo');
    return replaceOnce(contents, 'export interface IExtensionHostManager {', 'export interface IExtensionHostManager {\n  readonly extensionHost: IExtensionHost;');
  }
  if (path === paths[3] || path === paths[4]) {
    let contents = replaceOnce(source, '\tpublic get pid():', '\tpublic get extensionHost(): IExtensionHost { return this._extensionHost; }\n\n\tpublic get pid():');
    if (path === paths[3]) {
      contents = replaceOnce(contents, 'new RPCProtocol(protocol, logger)', 'new RPCProtocol(protocol, logger, this._extensionHost.uriTransformer ?? null)');
      contents = replaceOnce(contents, 'const extHostContext: IInternalExtHostContext = {', 'const extHostContext: IInternalExtHostContext = {\n      extensionHost: this._extensionHost,');
    }
    return contents;
  }
  if (path === paths[5]) {
    let contents = replaceOnce(source, 'IInternalExtensionService }', 'IInternalExtensionService, IExtensionHost }');
    return replaceOnce(contents, 'export interface IExtHostContext extends IRPCProtocol {', 'export interface IExtHostContext extends IRPCProtocol {\n  readonly extensionHost: IExtensionHost;');
  }
  if (path === paths[6]) {
    let contents = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'private getWorkspaceData(workspace: IWorkspace): IWorkspaceData | null {', `private getWorkspaceData(workspace: IWorkspace): IWorkspaceData | null {
    const host = this.extHostContext.extensionHost, binding = host.binding;
    if (binding) {
      const projected = host.uriTransformer;
      const target = binding.initialization?.workspace;
      if (!projected || !target) throw new Error('Node 宿主工作区 binding 未初始化');
      return { id: target.id, name: target.name, transient: false, isUntitled: false,
        folders: target.folders.map((folder, index) => ({ index, name: folder.name, uri: URI.parse(folder.native_uri) })) };
    }`);
    contents = replaceOnce(contents, 'const workspaceFoldersToAdd = foldersToAdd.map', "if (this.extHostContext.extensionHost.binding) return Promise.reject(new Error('修改 Node 宿主工作区集合需要由窗口明确挂接并重新载入'));\n\t\tconst workspaceFoldersToAdd = foldersToAdd.map");
    return contents;
  }
  if (path === paths[7]) {
    let contents = `import { registerTargetCommand, executeHostCommand } from ${JSON.stringify(join(root, 'src/extensions/commands.js'))};\n` + source;
    contents = replaceOnce(contents, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, "CommandsRegistry.registerCommand('_generateCommandsDocumentation', () => this._generateCommandsDocumentation())", "registerTargetCommand(this._extensionService, this.extHostContext.extensionHost, '_generateCommandsDocumentation', () => this._generateCommandsDocumentation())");
    const registerStart = contents.indexOf('\t$registerCommand('), registerEnd = contents.indexOf('\n\t$unregisterCommand(', registerStart);
    contents = replaceOnce(contents, contents.slice(registerStart, registerEnd), `
  $registerCommand(id: string): void {
    this._commandRegistrations.set(id, registerTargetCommand(this._extensionService, this.extHostContext.extensionHost, id,
      async (...args) => revive(await this._proxy.$executeContributedCommand(id, ...args))));
  }
`);
    contents = contents.replaceAll('this._extensionService.activateByEvent(activationEvent)', 'this._extensionService.activateByEvent(activationEvent, undefined, this.extHostContext.extensionHost)');
    contents = contents.replaceAll('this._extensionService.activateByEvent(`onCommand:${id}`)', 'this._extensionService.activateByEvent(`onCommand:${id}`, undefined, this.extHostContext.extensionHost)');
    return replaceOnce(contents, 'this._commandService.executeCommand<T>(id, ...args)', 'executeHostCommand<T>(this._commandService, this.extHostContext.extensionHost, id, args)');
  }
  if (path === paths[8]) {
    let contents = `import { captureCommandHost, executeCommandForHost, unpackCommandSource } from ${JSON.stringify(join(root, 'src/extensions/commands.js'))};
import { IQuickInputService } from '../../../../platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../editor/common/editorService.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../../common/editor.js';
import type { IExtensionHost } from '../../extensions/common/extensions.js';\n` + source;
    contents = replaceOnce(contents, "this._logService.trace('CommandService#executeCommand', id);", `this._logService.trace('CommandService#executeCommand', id);
    const source = unpackCommandSource(args);
    args = source.args;
    const target = await captureCommandHost(this._extensionService, id,
      () => this._instantiationService.invokeFunction(accessor => EditorResourceAccessor.getCanonicalUri(accessor.get(IEditorService).activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY })),
      () => this._instantiationService.invokeFunction(accessor => accessor.get(IQuickInputService)), source.source);`);
    contents = contents.replaceAll('this._tryExecuteCommand(id, args)', 'this._tryExecuteCommand(id, args, target)');
    contents = contents.replaceAll('this._extensionService.activateByEvent(activationEvent)', 'this._extensionService.activateByEvent(activationEvent, undefined, target)');
    contents = replaceOnce(contents, 'private _tryExecuteCommand(id: string, args: unknown[]):', 'private _tryExecuteCommand(id: string, args: unknown[], target?: IExtensionHost):');
    return replaceOnce(contents, 'this._instantiationService.invokeFunction(command.handler, ...args)', 'this._instantiationService.invokeFunction(accessor => executeCommandForHost(command, accessor, args, target))');
  }
  if (path !== paths[0]) return source;
  let contents = replaceOnce(source, 'ExtensionIdentifier, ExtensionIdentifierMap,', 'ExtensionIdentifier, ExtensionIdentifierMap, ExtensionInstallationTarget, IExtensionWorkspaceView, extensionInstanceKey, requireExtensionTarget,');
  contents = replaceOnce(contents, 'ExtensionHostStartup, ExtensionPointContribution,', 'ExtensionHostStartup, ExtensionHostExtensions, ExtensionPointContribution,');
  contents = replaceOnce(contents, 'ExtensionDescriptionRegistryLock, ExtensionDescriptionRegistrySnapshot,', 'ExtensionDescriptionRegistry, ExtensionDescriptionRegistryLock, ExtensionDescriptionRegistrySnapshot,');
  contents = replaceOnce(contents, 'new ExtensionIdentifierMap<ExtensionStatus>()', 'new Map<string, ExtensionStatus>()');
  contents = replaceOnce(contents, 'private readonly _runningLocations:', 'protected readonly _runningLocations:');
  contents = replaceOnce(contents, '\tprotected _getExtensionHostManagers(', `
  protected _setExtensionWorkspaceViews(views: readonly IExtensionWorkspaceView[]): void { this._runningLocations.setRemoteViews(views); }
  protected _bindExtension(extension: IExtension): IExtension {
    if (!extension.installationTarget) throw new Error('Unbound extension management event: ' + extension.identifier.id);
    return extension;
  }
  protected _removalTarget(extension?: IExtension): ExtensionInstallationTarget {
    if (!extension?.installationTarget) throw new Error('Extension removal is missing its installation target');
    return extension.installationTarget;
  }
  public getExtensionHosts(): readonly IExtensionHost[] { return this._extensionHostManagers.map(manager => manager.extensionHost); }
\tprotected _getExtensionHostManagers(`);
  contents = replaceOnce(contents, 'this._registry.getExtensionDescription(extensionId);', 'this._registry.getExtensionDescription(extensionId, this._removalTarget(extension ?? undefined));');
  contents = replaceOnce(contents, 'toExtensionDescription(extension, false);', 'toExtensionDescription(this._bindExtension(extension), false);');
  contents = replaceOnce(contents, 'public canAddExtension(extension: IExtensionDescription): boolean {\n\t\treturn this._canAddExtension(extension, []);',
    'public canAddExtension(extension: IExtension): boolean {\n\t\treturn this._canAddExtension(toExtensionDescription(this._bindExtension(extension)), []);');
  contents = replaceOnce(contents, 'public canRemoveExtension(extension: IExtensionDescription): boolean {', `public canRemoveExtension(extension: IExtension): boolean {
    return this._canRemoveExtension(toExtensionDescription(this._bindExtension(extension)));
  }
  private _canRemoveExtension(extension: IExtensionDescription): boolean {`);
  contents = replaceOnce(contents, 'this.canRemoveExtension(extensionDescription)', 'this._canRemoveExtension(extensionDescription)');
  contents = contents.replaceAll('toRemove.map(e => e.identifier)', 'toRemove.map(requireExtensionTarget)');
  contents = replaceOnce(contents, 'private async _updateExtensionsOnExtHosts(versionId: number, toAdd: IExtensionDescription[], toRemove: ExtensionIdentifier[]):', 'private async _updateExtensionsOnExtHosts(versionId: number, toAdd: IExtensionDescription[], toRemove: ReturnType<typeof requireExtensionTarget>[]):');
  const updateStart = contents.indexOf('\tprivate async _updateExtensionsOnExtHost('), updateEnd = contents.indexOf('\n\tpublic canAddExtension', updateStart);
  contents = replaceOnce(contents, contents.slice(updateStart, updateEnd), `
  private async _updateExtensionsOnExtHost(manager: IExtensionHostManager, versionId: number, _toAdd: IExtensionDescription[], _toRemove: unknown, _removed: unknown): Promise<void> {
    await manager.ready();
    const host = manager.extensionHost, previous = host.extensions;
    if (!previous) throw new Error('Cannot update an uninitialized extension host');
    const all = this._runningLocations.getHostExtensions(this._registry.getAllExtensionDescriptions(), host.runningLocation);
    const mine = this._runningLocations.filterByExtensionHostManager(all, manager).map(extension => extension.identifier);
    const candidate = new ExtensionHostExtensions(previous.versionId, previous.allExtensions, previous.myExtensions);
    const delta = candidate.set(versionId, all, mine);
    try { await manager.deltaExtensions(delta); }
    catch (error) { throw await this._stopFailedExtensionHost(manager, error); }
  }
`);
  contents = replaceOnce(contents, 'this._registry.getExtensionDescriptionByIdOrUUID(extension.identifier, extension.id)', 'this._registry.getExtensionDescriptionByIdOrUUID(extension.identifier, extension.uuid, requireExtensionTarget(extension).installationTarget)');
  contents = replaceOnce(contents, 'ExtensionIdentifier.equals(extension.identifier, extensionDescription.identifier)', 'extensionInstanceKey(extension) === extensionInstanceKey(extensionDescription)');
  contents = contents.replaceAll('const isRemote = extension.extensionLocation.scheme === Schemas.vscodeRemote;', "const isRemote = requireExtensionTarget(extension).installationTarget.kind === 'backend';");
  contents = replaceOnce(contents, 'this._registry.getExtensionDescription(extension.identifier);', 'this._registry.getExtensionDescription(extension.identifier, requireExtensionTarget(extension).installationTarget);');
  contents = replaceOnce(contents, 'this._extensionStatus.get(extensionDescription.identifier)', 'this._extensionStatus.get(extensionInstanceKey(extensionDescription))');
  contents = replaceOnce(contents, "\t\tthis._startExtensionHostsIfNecessary(true, []);\n\n\t\tconst lock", '\t\tconst lock');
  contents = replaceOnce(contents, '\n\t\tthis._startExtensionHostsIfNecessary(true, []);\n\n\t\t// Some remote', '\n\t\t// Some remote');
  contents = replaceOnce(contents, '\t\tthis._runningLocations.initializeRunningLocation(localExtensions, remoteExtensions);', `new ExtensionDescriptionRegistry(this._activationEventReader, localExtensions.concat(remoteExtensions), true).dispose();
\t\tthis._runningLocations.initializeRunningLocation(localExtensions, remoteExtensions);`);
  contents = replaceOnce(contents, '\t\tthis._doHandleExtensionPoints(this._registry.getAllExtensionDescriptions(), false);', '\t\tthis._doHandleExtensionPoints(this._registry.getAllExtensionDescriptions(), false);\n    this._startExtensionHostsIfNecessary(true, []);');
  contents = contents.replaceAll('this._runningLocations.getRunningLocation(extension.identifier)', 'this._runningLocations.getRunningLocation(extension)');
  contents = replaceOnce(contents, 'runningLocation = new RemoteRunningLocation();', "throw new Error('Remote extension test requires an explicitly bound installation instance');");
  contents = replaceOnce(contents, 'locations.push(new RemoteRunningLocation());', 'locations.push(...this._runningLocations.remoteRunningLocations);');
  contents = replaceOnce(contents, 'const extensions = this._runningLocations.filterByExtensionHostManager(snapshot.extensions, extHostManager);', 'const all = this._runningLocations.getHostExtensions(snapshot.extensions, extHostManager.extensionHost.runningLocation);\n      const extensions = this._runningLocations.filterByExtensionHostManager(all, extHostManager);');
  contents = replaceOnce(contents, 'extHostManager.start(snapshot.versionId, snapshot.extensions, extensions.map', 'extHostManager.start(snapshot.versionId, all, extensions.map');
  contents = replaceOnce(contents, 'public getExtension(id: string):', 'public getExtension(id: string, target?: ExtensionInstallationTarget):');
  contents = replaceOnce(contents, 'return this._registry.getExtensionDescription(id);', 'return this._registry.getExtensionDescription(id, target);');
  contents = contents.replaceAll('const availableExtensions = this._registry.getAllExtensionDescriptions();', 'const availableExtensions = this._registry.getWindowExtensionDescriptions();');
  contents = replaceOnce(contents, 'const extensionStatus = this._extensionStatus.get(extension.identifier);', 'const extensionStatus = this._extensionStatus.get(extensionInstanceKey(extension));');
  contents = replaceOnce(contents, 'result[extension.identifier.value] = {', 'result[extensionInstanceKey(extension)] = {\n          installationTarget: requireExtensionTarget(extension).installationTarget,');
  const statusStart = contents.indexOf('\tprivate _getOrCreateExtensionStatus('), statusEnd = contents.indexOf('\n\tprivate _handleExtensionPointMessage', statusStart);
  contents = replaceOnce(contents, contents.slice(statusStart, statusEnd), `
  private _getOrCreateExtensionStatus(extensionId: ExtensionIdentifier, target?: ExtensionInstallationTarget): ExtensionStatus {
    const extension = this._registry.getExtensionDescription(extensionId, target);
    if (!extension) throw new Error('Unknown extension status instance: ' + extensionId.value);
    const key = extensionInstanceKey(extension);
    let status = this._extensionStatus.get(key);
    if (!status) { status = new ExtensionStatus(extensionId); this._extensionStatus.set(key, status); }
    return status;
  }
`);
  contents = replaceOnce(contents, 'const extensionStatus = this._getOrCreateExtensionStatus(msg.extensionId);\n\t\textensionStatus.addMessage(msg);', `for (const extension of this._registry.getAllExtensionDescriptions().filter(extension => ExtensionIdentifier.equals(extension.identifier, msg.extensionId))) {
      this._getOrCreateExtensionStatus(msg.extensionId, extension.installationTarget).addMessage({ ...msg, installationTarget: extension.installationTarget });
    }`);
  contents = replaceOnce(contents, 'const extension = this._registry.getExtensionDescription(msg.extensionId);', 'const extension = this._registry.getWindowExtensionDescriptions().find(extension => ExtensionIdentifier.equals(extension.identifier, msg.extensionId));');
  contents = replaceOnce(contents, 'this._onDidActivateExtension(extensionId, codeLoadingTime, activateCallTime, activateResolvedTime, activationReason);', 'this._onDidActivateExtension(extensionId, codeLoadingTime, activateCallTime, activateResolvedTime, activationReason, extensionHost.extensions!.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId))!.installationTarget);');
  contents = replaceOnce(contents, 'this._onExtensionRuntimeError(extensionId, error);', 'this._onExtensionRuntimeError(extensionId, error, extensionHost.extensions!.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId))!.installationTarget);');
  contents = replaceOnce(contents, 'private _onWillActivateExtension(extensionId: ExtensionIdentifier, runningLocation: ExtensionRunningLocation):', 'private _onWillActivateExtension(extensionId: ExtensionIdentifier, runningLocation: ExtensionRunningLocation):');
  contents = replaceOnce(contents, 'this._runningLocations.set(extensionId, runningLocation);', 'const target = runningLocation.kind === ExtensionHostKind.Remote ? runningLocation.installationTarget : this._registry.getAllExtensionDescriptions().find(extension => extension.installationTarget?.kind === \'web\' && ExtensionIdentifier.equals(extension.identifier, extensionId))?.installationTarget;\n    this._runningLocations.set(extensionId, runningLocation, target);');
  contents = replaceOnce(contents, 'this._getOrCreateExtensionStatus(extensionId);\n\t\textensionStatus.onWillActivate();', 'this._getOrCreateExtensionStatus(extensionId, target);\n\t\textensionStatus.onWillActivate();');
  contents = replaceOnce(contents, 'activationReason: ExtensionActivationReason): void {\n\t\tconst extensionStatus = this._getOrCreateExtensionStatus(extensionId);', 'activationReason: ExtensionActivationReason, target?: ExtensionInstallationTarget): void {\n\t\tconst extensionStatus = this._getOrCreateExtensionStatus(extensionId, target);');
  contents = replaceOnce(contents, 'private _onExtensionRuntimeError(extensionId: ExtensionIdentifier, err: Error): void {\n\t\tconst extensionStatus = this._getOrCreateExtensionStatus(extensionId);', 'private _onExtensionRuntimeError(extensionId: ExtensionIdentifier, err: Error, target?: ExtensionInstallationTarget): void {\n\t\tconst extensionStatus = this._getOrCreateExtensionStatus(extensionId, target);');
  const activationStart = contents.indexOf('    const managers = this._extensionHostManagers.filter(manager => manager.containsExtension(extensionId)');
  const activationEnd = contents.indexOf('\n    if (managers.length !== 1)', activationStart);
  contents = replaceOnce(contents, contents.slice(activationStart, activationEnd), `    const description = source?.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId));
    const managers = this._extensionHostManagers.filter(manager => manager.extensionHost.extensions?.allExtensions.some(extension =>
      manager.containsExtension(extension.identifier) && ExtensionIdentifier.equals(extension.identifier, extensionId)
      && (!description || extensionInstanceKey(description) === extensionInstanceKey(extension)))
      && (!source?.extensions?.containsExtension(extensionId) || manager.extensionHost === source));`);
  contents = replaceOnce(contents, 'reason: ExtensionActivationReason, source?: IExtensionHost): Promise<void> {\n    //', 'reason: ExtensionActivationReason, source?: IExtensionHost, installed?: IExtensionDescription): Promise<void> {\n    //');
  contents = replaceOnce(contents, 'const description = source?.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId));', 'const description = installed ?? source?.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId));');
  contents = replaceOnce(contents, 'await this._activateById(extensionDescription.identifier, { startup: false, extensionId: extensionDescription.identifier, activationEvent: shouldActivateReason });', 'await this._activateById(extensionDescription.identifier, { startup: false, extensionId: extensionDescription.identifier, activationEvent: shouldActivateReason }, undefined, extensionDescription);');
  // 目标命令不进入窗口级重放集合；每个宿主自己的 Manager 仍缓存本代已准入事件。
  contents = replaceOnce(contents, 'public activateByEvent(activationEvent: string, activationKind: ActivationKind = ActivationKind.Normal):', 'public activateByEvent(activationEvent: string, activationKind: ActivationKind = ActivationKind.Normal, host?: IExtensionHost):');
  contents = contents.replaceAll('this._allRequestedActivateEvents.add(activationEvent);', 'if (!host && !activationEvent.startsWith(\'onCommand:\')) this._allRequestedActivateEvents.add(activationEvent);');
  contents = contents.replaceAll('this._activateByEvent(activationEvent, activationKind)', 'this._activateByEvent(activationEvent, activationKind, host)');
  contents = replaceOnce(contents, 'private _activateByEvent(activationEvent: string, activationKind: ActivationKind):', 'private _activateByEvent(activationEvent: string, activationKind: ActivationKind, host?: IExtensionHost):');
  contents = replaceOnce(contents, 'this._pendingRemoteActivationEvents.add(activationEvent);', 'if (!host && !activationEvent.startsWith(\'onCommand:\')) this._pendingRemoteActivationEvents.add(activationEvent);');
  const resultMarker = '\n\t\tconst result = Promise.all(\n\t\t\tmanagers.map(extHostManager => extHostManager.activateByEvent(activationEvent, activationKind))';
  contents = replaceOnce(contents, resultMarker, `
    if (host) {
      const manager = this._extensionHostManagers.filter(manager => manager.extensionHost === host)[0];
      if (!manager) return Promise.reject(new Error('Extension command host generation is closed'));
      managers = [manager];
    } else if (activationEvent.startsWith('onCommand:')) {
      managers = managers.filter(manager => manager.extensionHost.extensions?.allExtensions.some(extension =>
        manager.containsExtension(extension.identifier) && this._activationEventReader.readActivationEvents(extension).includes(activationEvent)));
      if (managers.length > 1) return Promise.reject(new Error('Command activation requires an explicit extension host: ' + activationEvent));
    }
\t\tconst result = Promise.all(
\t\t\tmanagers.map(extHostManager => extHostManager.activateByEvent(activationEvent, activationKind))`);
  // 通道关闭不借单 RemoteAgent 获取另一个目标的退出信息，也不自动重放未知副作用。
  const crashStart = contents.indexOf('\tprotected _onExtensionHostCrashed('), crashEnd = contents.indexOf('\n\tprivate _getExtensionHostExitInfoWithTimeout', crashStart);
  contents = replaceOnce(contents, contents.slice(crashStart, crashEnd), `
  protected _onExtensionHostCrashed(manager: IExtensionHostManager, code: number, signal: string | null): void {
    this._logExtensionHostCrash(manager);
    const affected = manager.extensionHost.extensions?.allExtensions.filter(extension => manager.containsExtension(extension.identifier)) ?? [];
    for (const extension of affected) this._extensionStatus.get(extensionInstanceKey(extension))?.clearRuntimeStatus();
    this._onDidChangeExtensionsStatus.fire(affected.map(extension => extension.identifier));
    void this._stopFailedExtensionHost(manager, new Error('Extension host closed: ' + manager.friendyName + ' / ' + code + ' / ' + signal));
  }
`);
  const oldRemoteStart = contents.indexOf('\n\tprivate _getExtensionHostExitInfoWithTimeout('), oldRemoteEnd = contents.indexOf('\n\tprotected _logExtensionHostCrash(', oldRemoteStart);
  contents = replaceOnce(contents, contents.slice(oldRemoteStart, oldRemoteEnd), '\n');
  contents = contents.replace('ExtensionIdentifier, ExtensionIdentifierMap, ExtensionInstallationTarget,', 'ExtensionIdentifier, ExtensionInstallationTarget,');
  contents = contents.replace('LocalWebWorkerRunningLocation, RemoteRunningLocation', 'LocalWebWorkerRunningLocation');
  contents = contents.replace('ExtensionRunningLocationTracker, filterExtensionIdentifiers', 'ExtensionRunningLocationTracker');
  contents = contents.replace('IExtensionHostExitInfo, IRemoteAgentService', 'IRemoteAgentService');
  contents = contents.replace('NotificationTelemetryId, NotificationActionTelemetryId', 'NotificationTelemetryId');
  contents = contents.replace('\tprivate readonly _remoteCrashTracker = new ExtensionHostCrashTracker();\n', '');
  return contents;
}
