import { join } from 'node:path';

export const extensionInstanceSourcePaths = [
  'src/vs/platform/extensions/common/extensions.ts',
  'src/vs/workbench/services/extensions/common/extensionDescriptionRegistry.ts',
];

/** 安装实例沿原描述 owner 贯穿；公开扩展 ID 和宿主内协议保持原值。 */
export function transformExtensionInstances(root, path, source, replaceOnce) {
  if (path === join(root, 'upstream', extensionInstanceSourcePaths[0])) {
    let contents = `import type { ITargetWebHostConnection } from ${JSON.stringify(join(root, 'src/extensions/targetContext.js'))};
import type { ExtensionHostTargetDTO } from ${JSON.stringify(join(root, '../web/src/types/protocol_generated/boxteam/gateway/v2/public.js'))};\n` + source;
    contents = replaceOnce(contents, 'export interface IExtension {', `
export type ExtensionInstallationTarget =
  | { readonly kind: 'web'; readonly profileId: string }
  | { readonly kind: 'backend'; readonly gatewayId: string; readonly backendId: string };

export interface IExtensionInstanceIdentifier {
  readonly identifier: ExtensionIdentifier;
  readonly installationTarget: ExtensionInstallationTarget;
}

export interface IExtensionWorkspaceView {
  readonly installationTarget: Extract<ExtensionInstallationTarget, { kind: 'backend' }>;
  readonly viewId: string;
  readonly workspaceIds: readonly string[];
  readonly capabilities: { readonly installation_manage: boolean; readonly host_manage: boolean };
  readonly gatewayConnectionId?: string;
  readonly nodeRuntime?: ExtensionHostTargetDTO;
  readonly webHost?: ITargetWebHostConnection;
}

export type IBoundExtensionDescription = IExtensionDescription & IExtensionInstanceIdentifier;

export function installationTargetKey(target: ExtensionInstallationTarget): string {
  if (!target || (target.kind === 'web' ? !target.profileId : target.kind !== 'backend' || !target.gatewayId || !target.backendId)) {
    throw new Error('Extension installation target is invalid');
  }
  return target.kind === 'web' ? JSON.stringify(['web', target.profileId])
    : JSON.stringify(['backend', target.gatewayId, target.backendId]);
}

export function extensionInstanceKey(extension: Pick<IExtensionDescription, 'identifier' | 'installationTarget'> | ExtensionIdentifier | string,
  installationTarget?: ExtensionInstallationTarget): string {
  const description = typeof extension === 'object' && 'identifier' in extension ? extension : undefined;
  const target = description ? description.installationTarget : installationTarget;
  const id = description ? description.identifier : extension as ExtensionIdentifier | string;
  // 原始 scanner 对象尚未绑定；该身份不等同于 Web、本机或任意 Backend。
  return JSON.stringify([target ? installationTargetKey(target) : null, ExtensionIdentifier.toKey(id)]);
}

export function bindExtensionDescription(extension: IExtensionDescription, installationTarget: ExtensionInstallationTarget): IBoundExtensionDescription {
  return { ...extension, installationTarget };
}

export function requireExtensionTarget(extension: IExtensionDescription): IBoundExtensionDescription {
  if (!extension.installationTarget) throw new Error('Extension installation target is unbound: ' + extension.identifier.value);
  installationTargetKey(extension.installationTarget);
  return extension as IBoundExtensionDescription;
}

export interface IExtension {
  readonly installationTarget?: ExtensionInstallationTarget;
  readonly extensionKey?: string;
  readonly installationProfileId?: string;
  readonly gatewayConnectionId?: string;
  readonly nativeLocation?: URI;
  readonly enablementView?: string;`);
    return replaceOnce(contents, 'export interface IRelaxedExtensionDescription extends IRelaxedExtensionManifest {',
      `export interface IRelaxedExtensionDescription extends IRelaxedExtensionManifest {
  installationTarget?: ExtensionInstallationTarget;
  extensionKey?: string;
  installationProfileId?: string;
  gatewayConnectionId?: string;
  enablementView?: string;
  isValid?: boolean;
  validations?: readonly [number, string][];`);
  }
  if (path !== join(root, 'upstream', extensionInstanceSourcePaths[1])) return source;
  let contents = `import { equals } from '../../../../base/common/objects.js';\n` + source;
  contents = replaceOnce(contents, "import { ExtensionIdentifier, ExtensionIdentifierMap, ExtensionIdentifierSet, IExtensionDescription }", "import { ExtensionIdentifier, ExtensionInstallationTarget, IExtensionInstanceIdentifier, IBoundExtensionDescription, extensionInstanceKey, requireExtensionTarget, IExtensionDescription }");
  contents = replaceOnce(contents, 'private _extensionsMap!: ExtensionIdentifierMap<IExtensionDescription>;', 'private _extensionsMap!: Map<string, IExtensionDescription>;');
  contents = replaceOnce(contents, 'extensionDescriptions: IExtensionDescription[]\n\t) {', 'extensionDescriptions: IExtensionDescription[],\n    private readonly _requireTarget = false\n\t) {');
  contents = replaceOnce(contents, 'this._extensionsMap = new ExtensionIdentifierMap<IExtensionDescription>();', 'this._extensionsMap = new Map<string, IExtensionDescription>();');
  const duplicate = `\t\t\tif (this._extensionsMap.has(extensionDescription.identifier)) {
\t\t\t\t// No overwriting allowed!
\t\t\t\tconsole.error('Extension \x60' + extensionDescription.identifier.value + '\x60 is already registered');
\t\t\t\tcontinue;
\t\t\t}

\t\t\tthis._extensionsMap.set(extensionDescription.identifier, extensionDescription);`;
  contents = replaceOnce(contents, duplicate, `
      if (this._requireTarget) requireExtensionTarget(extensionDescription);
      const key = extensionInstanceKey(extensionDescription);
      if (this._extensionsMap.has(key)) throw new Error('Duplicate extension instance: ' + key);
      this._extensionsMap.set(key, extensionDescription);`);
  contents = replaceOnce(contents, '\t\t}\n\t}\n\n\tpublic set(extensionDescriptions:', `\t\t}
    if (this._requireTarget) this.getWindowExtensionDescriptions();
\t}

\tpublic set(extensionDescriptions:`);
  const deltaStart = contents.indexOf('\tpublic deltaExtensions(');
  const queryStart = contents.indexOf('\tpublic containsActivationEvent(', deltaStart);
  contents = replaceOnce(contents, contents.slice(deltaStart, queryStart), `
\tpublic deltaExtensions(toAdd: IExtensionDescription[], toRemove: readonly (ExtensionIdentifier | IExtensionInstanceIdentifier)[]): DeltaExtensionsResult {
    const next = removeExtensions(this._extensionDescriptions, toRemove).concat(toAdd);
    const validated = new ExtensionDescriptionRegistry(this._activationEventsReader, next, this._requireTarget);
    validated.dispose();
    const looping = ExtensionDescriptionRegistry._findLoopingExtensions(next);
    const candidate = new ExtensionDescriptionRegistry(this._activationEventsReader,
      removeExtensions(next, looping.map(extension => extension.installationTarget ? requireExtensionTarget(extension) : extension.identifier)), this._requireTarget);
    try {
      ExtensionDescriptionRegistry.replaceAll([{ target: this, source: candidate }]);
      return new DeltaExtensionsResult(this._versionId, looping);
    } finally { candidate.dispose(); }
  }

  private static _findLoopingExtensions(extensions: IExtensionDescription[]): IExtensionDescription[] {
    const remaining = new Map(extensions.map(extension => [extensionInstanceKey(extension), extension]));
    let changed: boolean;
    do {
      changed = false;
      for (const [key, extension] of remaining) {
        if ((extension.extensionDependencies ?? []).every(id => !remaining.has(extensionInstanceKey(id, extension.installationTarget)))) {
          remaining.delete(key);
          changed = true;
        }
      }
    } while (changed);
    return [...remaining.values()];
  }

  public getWindowExtensionDescriptions(): IExtensionDescription[] {
    const shared = new Map<string, IExtensionDescription>();
    for (const extension of this._extensionsArr) {
      const key = ExtensionIdentifier.toKey(extension.identifier);
      const previous = shared.get(key);
      if (!previous) { shared.set(key, extension); continue; }
      if (!sameWindowContributions(previous, extension)) {
        throw new Error('Conflicting window contributions for ' + extension.identifier.value + ': ' + extensionInstanceKey(previous) + ' / ' + extensionInstanceKey(extension));
      }
    }
    return [...shared.values()];
  }

`);
  contents = replaceOnce(contents, 'public containsExtension(extensionId: ExtensionIdentifier): boolean {\n\t\treturn this._extensionsMap.has(extensionId);',
    'public containsExtension(extensionId: ExtensionIdentifier, installationTarget?: ExtensionInstallationTarget): boolean {\n\t\treturn this.getExtensionDescription(extensionId, installationTarget) !== undefined;');
  const getStart = contents.indexOf('\tpublic getExtensionDescription(extensionId:');
  const classEnd = contents.indexOf('\n}\n\nexport class ExtensionDescriptionRegistrySnapshot', getStart);
  contents = replaceOnce(contents, contents.slice(getStart, classEnd), `
  public getExtensionDescription(extensionId: ExtensionIdentifier | string, installationTarget?: ExtensionInstallationTarget): IExtensionDescription | undefined {
    if (installationTarget) return this._extensionsMap.get(extensionInstanceKey(extensionId, installationTarget));
    return uniqueDescription(this._extensionsArr.filter(extension => ExtensionIdentifier.equals(extension.identifier, extensionId)), String(typeof extensionId === 'string' ? extensionId : extensionId.value));
  }

  public getExtensionDescriptionByUUID(uuid: string, installationTarget?: ExtensionInstallationTarget): IExtensionDescription | undefined {
    return uniqueDescription(this._extensionsArr.filter(extension => extension.uuid === uuid &&
      (!installationTarget || extensionInstanceKey(extension) === extensionInstanceKey(extension.identifier, installationTarget))), uuid);
  }

  public getExtensionDescriptionByIdOrUUID(extensionId: ExtensionIdentifier | string, uuid: string | undefined, installationTarget?: ExtensionInstallationTarget): IExtensionDescription | undefined {
    return this.getExtensionDescription(extensionId, installationTarget) ?? (uuid ? this.getExtensionDescriptionByUUID(uuid, installationTarget) : undefined);
  }
`);
  contents = replaceOnce(contents, 'this._actual = new ExtensionDescriptionRegistry(activationEventsReader, []);', 'this._actual = new ExtensionDescriptionRegistry(activationEventsReader, [], true);');
  contents = contents.replaceAll('toRemove: ExtensionIdentifier[]', 'toRemove: readonly (ExtensionIdentifier | IExtensionInstanceIdentifier)[]');
  contents = contents.replaceAll('containsExtension(extensionId: ExtensionIdentifier):', 'containsExtension(extensionId: ExtensionIdentifier, installationTarget?: ExtensionInstallationTarget):');
  contents = contents.replaceAll('getExtensionDescription(extensionId: ExtensionIdentifier | string):', 'getExtensionDescription(extensionId: ExtensionIdentifier | string, installationTarget?: ExtensionInstallationTarget):');
  contents = contents.replaceAll('getExtensionDescriptionByUUID(uuid: string):', 'getExtensionDescriptionByUUID(uuid: string, installationTarget?: ExtensionInstallationTarget):');
  contents = contents.replaceAll('getExtensionDescriptionByIdOrUUID(extensionId: ExtensionIdentifier | string, uuid: string | undefined):', 'getExtensionDescriptionByIdOrUUID(extensionId: ExtensionIdentifier | string, uuid: string | undefined, installationTarget?: ExtensionInstallationTarget):');
  contents = contents.replaceAll('return this._actual.containsExtension(extensionId);', 'return this._actual.containsExtension(extensionId, installationTarget);');
  contents = contents.replaceAll('return this._actual.getExtensionDescription(extensionId);', 'return this._actual.getExtensionDescription(extensionId, installationTarget);');
  contents = contents.replaceAll('return this._actual.getExtensionDescriptionByUUID(uuid);', 'return this._actual.getExtensionDescriptionByUUID(uuid, installationTarget);');
  contents = contents.replaceAll('return this._actual.getExtensionDescriptionByIdOrUUID(extensionId, uuid);', 'return this._actual.getExtensionDescriptionByIdOrUUID(extensionId, uuid, installationTarget);');
  contents = replaceOnce(contents, '\tpublic getSnapshot(): ExtensionDescriptionRegistrySnapshot {\n\t\treturn this._actual.getSnapshot();\n\t}', `
  public getSnapshot(): ExtensionDescriptionRegistrySnapshot { return this._actual.getSnapshot(); }
  public getWindowExtensionDescriptions(): IExtensionDescription[] { return this._actual.getWindowExtensionDescriptions(); }
  public getAllExtensionInstances(): IBoundExtensionDescription[] { return this._actual.getAllExtensionDescriptions().map(requireExtensionTarget); }
`);
  const removeStart = contents.indexOf('function removeExtensions(');
  contents = contents.slice(0, removeStart) + `
function removeExtensions(arr: IExtensionDescription[], toRemove: readonly (ExtensionIdentifier | IExtensionInstanceIdentifier)[]): IExtensionDescription[] {
  const keys = new Set<string>();
  for (const item of toRemove) {
    if ('identifier' in item) keys.add(extensionInstanceKey(item));
    else {
      const extension = uniqueDescription(arr.filter(extension => ExtensionIdentifier.equals(extension.identifier, item)), item.value);
      if (extension) keys.add(extensionInstanceKey(extension));
    }
  }
  return arr.filter(extension => !keys.has(extensionInstanceKey(extension)));
}

function uniqueDescription(candidates: IExtensionDescription[], id: string): IExtensionDescription | undefined {
  if (candidates.length > 1) throw new Error('Ambiguous extension instance: ' + id);
  return candidates[0];
}

function sameWindowContributions(a: IExtensionDescription, b: IExtensionDescription): boolean {
  if (!equals(a.contributes ?? {}, b.contributes ?? {})) return false;
  if (!a.contributes || !Object.keys(a.contributes).length) return true;
  if (a.displayName !== b.displayName || a.name !== b.name || a.publisher !== b.publisher) return false;
  // 原配置扩展点消费信任限制和 proposed API 准入；不同机器上的相同路径不能跳过比较。
  if (a.contributes.configuration && (!equals(a.capabilities?.untrustedWorkspaces, b.capabilities?.untrustedWorkspaces)
    || !equals(a.enabledApiProposals, b.enabledApiProposals))) return false;
  if (a.extensionLocation.toString() === b.extensionLocation.toString()) return true;
  // 这些原扩展点只消费声明字面值；其它扩展点可能按包目录读取资源，不能推测同版本即同制品。
  const literalPoints = new Set(['commands', 'menus', 'keybindings', 'configuration', 'configurationDefaults']);
  for (const point of Object.keys(a.contributes)) {
    if (!literalPoints.has(point)) return false;
  }
  const commands = a.contributes.commands;
  return !commands || (Array.isArray(commands) ? commands : [commands]).every(command => !('icon' in command) || !command.icon);
}
`;
  return contents;
}
