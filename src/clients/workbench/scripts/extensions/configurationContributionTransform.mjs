import { join } from 'node:path';

export const configurationContributionSourcePaths = [
  'src/vs/workbench/api/common/configurationExtensionPoint.ts',
  'src/vs/workbench/services/extensions/common/abstractExtensionService.ts',
  'src/vs/workbench/services/extensions/common/extensions.ts',
  'src/vs/workbench/services/extensions/common/extensionsRegistry.ts',
];

/** 配置贡献仍由原扩展点校验和发布；运行来源与独立设置来源只在这个 owner 合并。 */
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
  source = replaceOnce(source, "import { Disposable } from '../../../base/common/lifecycle.js';", "import { Disposable, IDisposable, toDisposable } from '../../../base/common/lifecycle.js';\nimport type { IMessage } from '../../services/extensions/common/extensions.js';");
  source = replaceOnce(source, 'let _configDelta: IConfigurationDelta | undefined;', `let _configDelta: IConfigurationDelta | undefined;

export type ConfigurationExtensionDescription = Pick<IExtensionDescription,
  'identifier' | 'name' | 'publisher' | 'displayName' | 'contributes' | 'capabilities' | 'enabledApiProposals' | 'isBuiltin'>;
type ConfigurationDescription = ConfigurationExtensionDescription & { configurationOnly?: boolean };
interface ConfigurationUser<T> {
  description: ConfigurationDescription;
  value: T;
  collector: ExtensionMessageCollector;
}
interface ConfigurationSource {
  extensions: readonly ConfigurationExtensionDescription[];
  messageHandler: (message: IMessage) => void;
}
let runtimeSource: ConfigurationSource = { extensions: [], messageHandler: () => {} };
let settingsSources = new Map<symbol, ConfigurationSource>();
let published = new Map<string, { description: ConfigurationDescription; messageHandler: ConfigurationSource['messageHandler'] }>();

function configurationSemantics(description: ConfigurationDescription) {
  return { name: description.name, publisher: description.publisher, displayName: description.displayName,
    configuration: description.contributes?.configuration, defaults: description.contributes?.configurationDefaults,
    untrusted: description.capabilities?.untrustedWorkspaces, proposals: description.enabledApiProposals ?? [],
    configurationOnly: description.configurationOnly, isBuiltin: description.isBuiltin };
}

function configurationSource(description: ConfigurationDescription) {
  return { id: description.identifier.value, displayName: description.displayName,
    configurationOnly: description.configurationOnly, isBuiltin: description.isBuiltin };
}

function publishConfigurationSources(runtime: ConfigurationSource, sources: ReadonlyMap<symbol, ConfigurationSource>): void {
  const next: typeof published = new Map();
  for (const [index, source] of [runtime, ...sources.values()].entries()) {
    for (const extension of source.extensions) {
      if (!extension.contributes?.configuration && !extension.contributes?.configurationDefaults) continue;
      const key = extension.identifier.value.toLowerCase(), existing = next.get(key);
      const description = { ...extension, configurationOnly: index === 0 ? undefined : true };
      if (existing) {
        if (!objects.equals(configurationSemantics({ ...existing.description, configurationOnly: undefined }),
          configurationSemantics({ ...description, configurationOnly: undefined }))) {
          throw new Error('扩展配置声明与窗口中已有来源冲突，请关闭旧设置或停用冲突版本：' + extension.identifier.value);
        }
      } else next.set(key, { description, messageHandler: source.messageHandler });
    }
  }
  const changed = (key: string) => !objects.equals(configurationSemantics(published.get(key)!.description), configurationSemantics(next.get(key)!.description));
  const removed = [...published].filter(([key]) => !next.has(key) || changed(key)).map(([, value]) => value);
  const added = [...next].filter(([key]) => !published.has(key) || changed(key)).map(([, value]) => value);
  if (removed.length || added.length) {
    const users = <K extends 'configuration' | 'configurationDefaults'>(entries: typeof added, point: K): ConfigurationUser<NonNullable<IExtensionDescription['contributes']>[K]>[] =>
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

export function acceptRuntimeConfigurationExtensions(extensions: readonly IExtensionDescription[], messageHandler: ConfigurationSource['messageHandler']): void {
  const source = { extensions, messageHandler };
  publishConfigurationSources(source, settingsSources);
  runtimeSource = source;
}

export function acquireConfigurationContributions(extensions: readonly ConfigurationExtensionDescription[], messageHandler: ConfigurationSource['messageHandler']): IDisposable {
  const key = Symbol(), sources = new Map(settingsSources);
  sources.set(key, { extensions, messageHandler });
  publishConfigurationSources(runtimeSource, sources);
  settingsSources = sources;
  return toDisposable(() => {
    const remaining = new Map(settingsSources);
    remaining.delete(key);
    publishConfigurationSources(runtimeSource, remaining);
    settingsSources = remaining;
  });
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
