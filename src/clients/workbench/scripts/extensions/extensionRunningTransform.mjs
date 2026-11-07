import { join } from 'node:path';

export const extensionRunningSourcePaths = [
  'src/vs/workbench/services/extensions/common/extensionRunningLocation.ts',
  'src/vs/workbench/services/extensions/common/extensionRunningLocationTracker.ts',
  'src/vs/workbench/services/extensions/common/extensionHostKind.ts',
];

export function transformExtensionRunning(root, path, source, replaceOnce) {
  const paths = extensionRunningSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = `import { IExtensionWorkspaceView, installationTargetKey } from '../../../../platform/extensions/common/extensions.js';\n` + source;
    const webStart = contents.indexOf('export class LocalWebWorkerRunningLocation {');
    const webEnd = contents.indexOf('\nexport class RemoteRunningLocation', webStart);
    contents = replaceOnce(contents, contents.slice(webStart, webEnd), `export class LocalWebWorkerRunningLocation {
  public readonly kind = ExtensionHostKind.LocalWebWorker;
  constructor(public readonly affinity: number,
    public readonly installationTarget?: IExtensionWorkspaceView['installationTarget'],
    public readonly viewId?: string, public readonly workspaceIds?: readonly string[]) {
    if (installationTarget) {
      installationTargetKey(installationTarget);
      if (!viewId || !workspaceIds || new Set(workspaceIds).size !== workspaceIds.length) throw new Error('Invalid Web extension workspace view');
    } else if (viewId || workspaceIds) throw new Error('Web extension workspace view is missing its target');
  }
  public equals(other: ExtensionRunningLocation): boolean {
    return this.kind === other.kind && this.affinity === other.affinity
      && (this.installationTarget ? !!other.installationTarget && installationTargetKey(this.installationTarget) === installationTargetKey(other.installationTarget) : !other.installationTarget)
      && this.viewId === other.viewId && JSON.stringify(this.workspaceIds) === JSON.stringify(other.workspaceIds);
  }
  public asString(): string {
    return this.installationTarget ? JSON.stringify(['LocalWebWorker', installationTargetKey(this.installationTarget), this.viewId, this.workspaceIds])
      : this.affinity === 0 ? 'LocalWebWorker' : 'LocalWebWorker' + this.affinity;
  }
}
`);
    contents = replaceOnce(contents, 'export class RemoteRunningLocation {', `export class RemoteRunningLocation {
  constructor(
    public readonly installationTarget: IExtensionWorkspaceView['installationTarget'],
    public readonly viewId: string,
    public readonly workspaceIds: readonly string[],
  ) {
    installationTargetKey(installationTarget);
    if (!viewId || !workspaceIds.length || new Set(workspaceIds).size !== workspaceIds.length) throw new Error('Invalid extension workspace view');
  }`);
    contents = replaceOnce(contents, 'return (this.kind === other.kind);', `return this.kind === other.kind && installationTargetKey(this.installationTarget) === installationTargetKey(other.installationTarget)
      && this.viewId === other.viewId && JSON.stringify(this.workspaceIds) === JSON.stringify(other.workspaceIds);`);
    return replaceOnce(contents, "return 'Remote';", "return JSON.stringify(['Remote', installationTargetKey(this.installationTarget), this.viewId, this.workspaceIds]);");
  }
  if (path === paths[2]) {
    let contents = replaceOnce(source, 'ExtensionIdentifier, IExtensionDescription', 'ExtensionIdentifier, extensionInstanceKey, IExtensionDescription');
    contents = replaceOnce(contents, 'return ExtensionIdentifier.toKey(this.desc.identifier);', 'return extensionInstanceKey(this.desc);');
    return contents;
  }
  if (path !== paths[1]) return source;
  // 保留原 affinity 分组算法，只把所有顶点与边迁到同一安装实例身份。
  let contents = `import { targetExecutionKind } from ${JSON.stringify(join(root, 'src/extensions/targetContext.js'))};\n` + source.replace("import { Schemas } from '../../../../base/common/network.js';\n", '');
  contents = replaceOnce(contents, 'ExtensionIdentifier, ExtensionIdentifierMap, IExtensionDescription', 'ExtensionIdentifier, ExtensionInstallationTarget, IExtensionWorkspaceView, IExtensionInstanceIdentifier, extensionInstanceKey, installationTargetKey, requireExtensionTarget, IExtensionDescription');
  contents = contents.replaceAll('ExtensionIdentifierMap<', 'Map<string, ');
  contents = contents.replaceAll('new Map<string, ', 'new Map<string, ');
  contents = replaceOnce(contents, '\tprivate _maxLocalProcessAffinity:', `
  private readonly _remoteViews = new Map<string, IExtensionWorkspaceView>();
  public setRemoteViews(views: readonly IExtensionWorkspaceView[]): void {
    if (this._runningLocation.size) throw new Error('Extension workspace view changes require a window reload');
    const next = new Map<string, IExtensionWorkspaceView>();
    for (const view of views) {
      const key = installationTargetKey(view.installationTarget);
      if (next.has(key)) throw new Error('Multiple enablement views for one Backend are not supported yet');
      next.set(key, view);
    }
    this._remoteViews.clear();
    for (const [key, view] of next) this._remoteViews.set(key, view);
  }
  public get workspaceViews(): readonly IExtensionWorkspaceView[] { return [...this._remoteViews.values()]; }
  public get remoteRunningLocations(): RemoteRunningLocation[] {
    return this.workspaceViews.filter(view => view.capabilities.host_manage && view.nodeRuntime?.channel_state === 'available')
      .map(view => new RemoteRunningLocation(view.installationTarget, view.viewId, view.workspaceIds));
  }
  public get targetWebRunningLocations(): LocalWebWorkerRunningLocation[] {
    return this.workspaceViews.filter(view => view.capabilities.host_manage && view.webHost)
      .map(view => new LocalWebWorkerRunningLocation(0, view.installationTarget, view.viewId, view.workspaceIds));
  }
  public getHostExtensions(extensions: readonly IExtensionDescription[], location: ExtensionRunningLocation): IExtensionDescription[] {
    const targetLocation = location.kind === ExtensionHostKind.Remote || location.kind === ExtensionHostKind.LocalWebWorker && location.installationTarget ? location : undefined;
    const own = extensions.filter(extension => {
      const target = requireExtensionTarget(extension).installationTarget;
      return targetLocation
        ? installationTargetKey(target) === installationTargetKey(targetLocation.installationTarget!)
        : target.kind === 'web';
    });
    const selected = new Map(own.map(extension => [ExtensionIdentifier.toKey(extension.identifier), extension]));
    const candidates = new Map<string, IExtensionDescription[]>();
    for (const extension of extensions) {
      const key = ExtensionIdentifier.toKey(extension.identifier), target = requireExtensionTarget(extension).installationTarget;
      if (selected.has(key) || extension.api !== 'none' || !(extension.main || extension.browser)
        || targetLocation && target.kind !== 'web') continue;
      const group = candidates.get(key) ?? [];
      group.push(extension); candidates.set(key, group);
    }
    // 原 api:none 跨宿主依赖只在合法且唯一的集合中保留，绝不借另一 Backend 同名依赖。
    for (const [key, group] of candidates) if (group.length === 1) selected.set(key, group[0]);
    return [...selected.values()];
  }
  private _remoteLocation(extension: IExtensionDescription): RemoteRunningLocation {
    const target = requireExtensionTarget(extension).installationTarget;
    const view = this._remoteViews.get(installationTargetKey(target));
    if (target.kind !== 'backend' || !view?.capabilities.host_manage) throw new Error('Extension has no authorized Backend workspace view: ' + extension.identifier.value);
    return new RemoteRunningLocation(target, view.viewId, view.workspaceIds);
  }
  private _webLocation(extension: IExtensionDescription): LocalWebWorkerRunningLocation {
    const target = requireExtensionTarget(extension).installationTarget;
    const view = this._remoteViews.get(installationTargetKey(target));
    if (target.kind !== 'backend' || !view?.capabilities.host_manage || !view.webHost) throw new Error('Extension has no authorized Web workspace view: ' + extension.identifier.value);
    return new LocalWebWorkerRunningLocation(0, target, view.viewId, view.workspaceIds);
  }
  private _pickHostKind(extension: IExtensionDescription): ExtensionHostKind | null {
    const target = requireExtensionTarget(extension).installationTarget;
    const kinds = this.readExtensionKinds(extension);
    if (target.kind === 'web') return this._extensionHostKindPicker.pickExtensionHostKind(extension.identifier, kinds, true, false, ExtensionRunningPreference.None);
    const view = this._remoteViews.get(installationTargetKey(target));
    if (!view?.capabilities.host_manage) return null;
    const kind = targetExecutionKind(extension, kinds, view.nodeRuntime?.channel_state === 'available');
    if (kind === 'web') return ExtensionHostKind.LocalWebWorker;
    if (kind === 'node') return ExtensionHostKind.Remote;
    if (extension.main && kinds.includes('workspace')) this._logService.error('Node 扩展没有可用的目标运行时：' + extension.identifier.value + ' / ' + (view.nodeRuntime?.reason ?? 'runtime_missing'));
    return null;
  }
  private _key(extension: ExtensionIdentifier | IExtensionDescription, target?: ExtensionInstallationTarget): string {
    if ('identifier' in extension) return extensionInstanceKey(requireExtensionTarget(extension));
    const description = this._registry.getExtensionDescription(extension, target);
    if (!description) throw new Error('Unknown extension instance: ' + extension.value);
    return extensionInstanceKey(requireExtensionTarget(description));
  }
\tprivate _maxLocalProcessAffinity:`);
  contents = replaceOnce(contents, 'public set(extensionId: ExtensionIdentifier, runningLocation: ExtensionRunningLocation)', 'public set(extensionId: ExtensionIdentifier | IExtensionDescription, runningLocation: ExtensionRunningLocation, target?: ExtensionInstallationTarget)');
  contents = replaceOnce(contents, 'this._runningLocation.set(extensionId, runningLocation);', 'this._runningLocation.set(this._key(extensionId, target), runningLocation);');
  contents = replaceOnce(contents, 'public getRunningLocation(extensionId: ExtensionIdentifier): ExtensionRunningLocation | null {\n\t\treturn this._runningLocation.get(extensionId) || null;', 'public getRunningLocation(extensionId: ExtensionIdentifier | IExtensionDescription, target?: ExtensionInstallationTarget): ExtensionRunningLocation | null {\n    if (!(\'identifier\' in extensionId) && !this._registry.getExtensionDescription(extensionId, target)) return null;\n\t\treturn this._runningLocation.get(this._key(extensionId, target)) || null;');
  contents = contents.replaceAll('.set(extension.identifier,', '.set(extensionInstanceKey(extension),');
  contents = contents.replaceAll('.get(extension.identifier)', '.get(extensionInstanceKey(extension))');
  contents = contents.replaceAll('.has(extension.identifier)', '.has(extensionInstanceKey(extension))');
  contents = contents.replaceAll('groups.get(depId)', 'groups.get(extensionInstanceKey(depId, extension.installationTarget))');
  contents = contents.replaceAll('groups.get(colocateId)', 'groups.get(extensionInstanceKey(colocateId, extension.installationTarget))');
  contents = replaceOnce(contents, 'if (runningLocation && runningLocation.kind === extensionHostKind) {', "if (runningLocation && runningLocation.kind === extensionHostKind && extension.installationTarget?.kind === 'web') {");
  // 配置仍按公开 ID 声明；此算法只处理本窗口唯一 Web profile／本机宿主，Backend 走明确 Remote view。
  contents = replaceOnce(contents, 'const group = groups.get(extensionId);', 'const group = [...extensions.values()].find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId));\n                const configuredGroup = group ? groups.get(extensionInstanceKey(group)) : undefined;');
  contents = replaceOnce(contents, 'if (!group) {', 'if (!configuredGroup) {');
  contents = contents.replaceAll('resultingAffinities.get(group);', 'resultingAffinities.get(configuredGroup);');
  contents = contents.replaceAll('resultingAffinities.set(group, affinity2);', 'resultingAffinities.set(configuredGroup, affinity2);');
  contents = contents.replaceAll('resultingAffinities.set(group, affinity3);', 'resultingAffinities.set(configuredGroup, affinity3);');
  contents = replaceOnce(contents, 'runningLocation = new RemoteRunningLocation();\n\t\t\t}\n\t\t\tresult.set', 'runningLocation = this._remoteLocation(extensions.get(extensionIdKey)!);\n\t\t\t}\n\t\t\tresult.set');
  const determineStart = contents.indexOf('\t\tconst extensionHostKinds = determineExtensionHostKinds(');
  const determineEnd = contents.indexOf('\n\n\t\tconst extensions', determineStart);
  contents = replaceOnce(contents, contents.slice(determineStart, determineEnd), `const extensionHostKinds = new Map(localExtensions.concat(remoteExtensions).map(extension =>
      [extensionInstanceKey(extension), this._pickHostKind(extension)]));`);
  contents = replaceOnce(contents, 'localWebWorkerExtensions.push(extensionDescription);', `if (extensionDescription.installationTarget?.kind === 'backend') runningLocation = this._webLocation(extensionDescription);
          else localWebWorkerExtensions.push(extensionDescription);`);
  const deltaStart = contents.indexOf('\tpublic deltaExtensions(');
  const deltaEnd = contents.indexOf('\n\t/**', deltaStart);
  contents = replaceOnce(contents, contents.slice(deltaStart, deltaEnd), `
  public deltaExtensions(toAdd: IExtensionDescription[], toRemove: readonly IExtensionInstanceIdentifier[]): Map<string, ExtensionRunningLocation | null> {
    const removed = new Map<string, ExtensionRunningLocation | null>();
    for (const extension of toRemove) {
      const key = extensionInstanceKey(extension);
      removed.set(key, this._runningLocation.get(key) ?? null);
      this._runningLocation.delete(key);
    }
    this._updateRunningLocationForAddedExtensions(toAdd);
    return removed;
  }
`);
  contents = replaceOnce(contents, `const extensionKind = this.readExtensionKinds(extension);
\t\t\tconst isRemote = extension.extensionLocation.scheme === Schemas.vscodeRemote;
\t\t\tconst extensionHostKind = this._extensionHostKindPicker.pickExtensionHostKind(extension.identifier, extensionKind, !isRemote, isRemote, ExtensionRunningPreference.None);`, 'const extensionHostKind = this._pickHostKind(extension);');
  contents = replaceOnce(contents, 'localWebWorkerExtensions.push(extension);', `if (extension.installationTarget?.kind === 'backend') runningLocation = this._webLocation(extension);
        else localWebWorkerExtensions.push(extension);`);
  contents = replaceOnce(contents, 'runningLocation = new RemoteRunningLocation();', 'runningLocation = this._remoteLocation(extension);');
  contents = replaceOnce(contents, 'runningLocation.get(ext.identifier)', 'runningLocation.get(extensionInstanceKey(ext))');
  const helperStart = contents.indexOf('export function filterExtensionIdentifiers(');
  return contents.slice(0, helperStart).replace(', determineExtensionHostKinds', '') + `export function filterExtensionIdentifiers(extensions: readonly IExtensionInstanceIdentifier[], runningLocation: Map<string, ExtensionRunningLocation | null>, predicate: (location: ExtensionRunningLocation) => boolean): ExtensionIdentifier[] {
  return extensions.filter(extension => {
    const location = runningLocation.get(extensionInstanceKey(extension));
    return location && predicate(location);
  }).map(extension => extension.identifier);
}\n`;
}
