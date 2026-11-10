import { join } from 'node:path';

export const configurationContributionSourcePaths = [
  'src/vs/workbench/api/common/configurationExtensionPoint.ts',
  'src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'src/vs/workbench/services/extensions/common/extensions.ts',
  'src/vs/workbench/services/extensions/common/extensionsRegistry.ts',
];

/** 运行配置贡献仍由原扩展点校验和发布；同 ID 更新走原 registry 的 remove/add delta。 */
export function transformConfigurationContributions(root, path, source, replaceOnce) {
  const paths = configurationContributionSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[1]) {
    source = `import { acceptRuntimeConfigurationExtensions } from '../../../api/common/configurationExtensionPoint.js';\n` + source;
    return replaceOnce(source, '\t\tconst users: IExtensionPointUser<T>[] = [];', `
    if (extensionPoint.name === 'configuration' || extensionPoint.name === 'configurationDefaults') {
      acceptRuntimeConfigurationExtensions(availableExtensions, messageHandler);
      return;
    }
\t\tconst users: IExtensionPointUser<T>[] = [];`);
  }
  if (path === paths[2]) return replaceOnce(source,
    'export function isProposedApiEnabled(extension: IExtensionDescription, proposal: ApiProposalName): boolean {',
    "export function isProposedApiEnabled(extension: Pick<IExtensionDescription, 'enabledApiProposals'>, proposal: ApiProposalName): boolean {");
  if (path === paths[3]) {
    source = replaceOnce(source, 'private readonly _extension: IExtensionDescription;', "private readonly _extension: Pick<IExtensionDescription, 'identifier'>;");
    return replaceOnce(source, '\t\textension: IExtensionDescription,', "\t\textension: Pick<IExtensionDescription, 'identifier'>,");
  }
  if (path !== paths[0]) return source;
  source = replaceOnce(source, 'ExtensionsRegistry, IExtensionPointUser', 'ExtensionsRegistry, ExtensionMessageCollector');
  source = replaceOnce(source, 'ExtensionIdentifierMap, IExtensionManifest', 'ExtensionIdentifierMap, IExtensionManifest, IExtensionDescription');
  source = replaceOnce(source, "import { Disposable } from '../../../base/common/lifecycle.js';", "import { Disposable } from '../../../base/common/lifecycle.js';\nimport type { IMessage } from '../../services/extensions/common/extensions.js';");
  source = replaceOnce(source, 'let _configDelta: IConfigurationDelta | undefined;', `let _configDelta: IConfigurationDelta | undefined;

type ConfigurationDescription = Pick<IExtensionDescription,
  'identifier' | 'name' | 'publisher' | 'displayName' | 'contributes' | 'capabilities' | 'enabledApiProposals'>;
interface ConfigurationUser<T> {
  description: ConfigurationDescription;
  value: T;
  collector: ExtensionMessageCollector;
}
interface RuntimeConfigurationEntry {
  description: ConfigurationDescription;
  messageHandler: (message: IMessage) => void;
}
let published = new Map<string, RuntimeConfigurationEntry>();

function configurationSemantics(description: ConfigurationDescription) {
  return { name: description.name, publisher: description.publisher, displayName: description.displayName,
    configuration: description.contributes?.configuration, defaults: description.contributes?.configurationDefaults,
    untrusted: description.capabilities?.untrustedWorkspaces, proposals: description.enabledApiProposals ?? [] };
}

function configurationSource(description: ConfigurationDescription) {
  return { id: description.identifier.value, displayName: description.displayName };
}

export function acceptRuntimeConfigurationExtensions(extensions: readonly IExtensionDescription[], messageHandler: (message: IMessage) => void): void {
  const next: typeof published = new Map();
  for (const extension of extensions) {
    if (!extension.contributes?.configuration && !extension.contributes?.configurationDefaults) continue;
    const key = extension.identifier.value.toLowerCase(), existing = next.get(key);
    if (existing && !objects.equals(configurationSemantics(existing.description), configurationSemantics(extension))) {
      throw new Error('同一扩展 ID 的配置声明冲突：' + extension.identifier.value);
    }
    if (!existing) next.set(key, { description: extension, messageHandler });
  }
  const changed = (key: string) => !objects.equals(configurationSemantics(published.get(key)!.description), configurationSemantics(next.get(key)!.description));
  const removed = [...published].filter(([key]) => !next.has(key) || changed(key)).map(([, value]) => value);
  const added = [...next].filter(([key]) => !published.has(key) || changed(key)).map(([, value]) => value);
  if (removed.length || added.length) {
    const users = <K extends 'configuration' | 'configurationDefaults'>(entries: readonly RuntimeConfigurationEntry[], point: K): ConfigurationUser<NonNullable<IExtensionDescription['contributes']>[K]>[] =>
      entries.filter(entry => entry.description.contributes?.[point] !== undefined).map(entry => ({
        description: entry.description, value: entry.description.contributes![point],
        collector: new ExtensionMessageCollector(entry.messageHandler, entry.description, point),
      }));
    // 先预检全部来源，再沿原两扩展点的同一 delta 提交；同 ID 内容变化也必须 remove/add。
    handleDefaultConfigurations(users(added, 'configurationDefaults'), users(removed, 'configurationDefaults'));
    handleConfigurations(users(added, 'configuration'), users(removed, 'configuration'));
  }
  published = next;
}
`);
  source = replaceOnce(source, 'defaultConfigurationExtPoint.setHandler((extensions, { added, removed }) => {',
    "function handleDefaultConfigurations(added: readonly ConfigurationUser<NonNullable<IExtensionDescription['contributes']>['configurationDefaults']>[], removed: readonly ConfigurationUser<NonNullable<IExtensionDescription['contributes']>['configurationDefaults']>[]): void {");
  const start = source.indexOf('\n\tif (_configDelta) {'), end = source.indexOf('\n\tif (removed.length)', start);
  if (start < 0 || end < 0) throw new Error('配置默认贡献转换锚点缺失');
  source = replaceOnce(source, source.slice(start, end), '\n\t_configDelta = {};\n');
  source = replaceOnce(source, '});\n// END VSCode extension point `configurationDefaults`', '}\n// END VSCode extension point `configurationDefaults`');
  source = replaceOnce(source, 'configurationExtPoint.setHandler((extensions, { added, removed }) => {',
    "function handleConfigurations(added: readonly ConfigurationUser<NonNullable<IExtensionDescription['contributes']>['configuration']>[], removed: readonly ConfigurationUser<NonNullable<IExtensionDescription['contributes']>['configuration']>[]): void {");
  source = replaceOnce(source, '});\n// END VSCode extension point `configuration`', '}\n// END VSCode extension point `configuration`');
  source = replaceOnce(source, 'const configurationExtPoint = ExtensionsRegistry.registerExtensionPoint', 'ExtensionsRegistry.registerExtensionPoint');
  // 原 handler 只消费声明与原 collector，不要求运行入口或包位置。
  source = source.replaceAll('IExtensionPointUser<unknown>', 'ConfigurationUser<unknown>');
  source = replaceOnce(source, 'const seenProperties = new Set<string>();',
    'const seenProperties = new Set<string>();\n  const replacedProperties = getAllConfigurationProperties(_configDelta.removedConfigurations ?? []);');
  source = replaceOnce(source, 'validateProperty(key, propertyConfiguration, extension.description.identifier.value)',
    'validateProperty(key, propertyConfiguration, extension.description.identifier.value, Object.hasOwn(replacedProperties, key))');
  source = source.replaceAll('source: { id: extension.description.identifier.value, displayName: extension.description.displayName }',
    'source: configurationSource(extension.description)');
  return replaceOnce(source, 'configuration.extensionInfo = { id: extension.description.identifier.value, displayName: extension.description.displayName };',
    'configuration.extensionInfo = configurationSource(extension.description);');
}
