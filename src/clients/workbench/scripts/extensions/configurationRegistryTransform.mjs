import { join } from 'node:path';

export const configurationRegistrySourcePaths = [
  'src/vs/platform/configuration/common/configurationRegistry.ts',
];

/** 目标设置只投影原注册事实，默认值仍由原合并函数计算。 */
export function transformConfigurationRegistry(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', configurationRegistrySourcePaths[0])) return source;
  let contents = replaceOnce(source, "import * as types from '../../../base/common/types.js';",
    "import * as types from '../../../base/common/types.js';\nimport { deepClone } from '../../../base/common/objects.js';");
  contents = replaceOnce(contents, 'export interface IExtensionInfo {\n\tid: string;',
    'export interface IExtensionInfo {\n\t/** 仅设置发现持有此贡献；窗口运行来源存在时省略。 */\n\tconfigurationOnly?: boolean;\n\t/** 原窗口内置扩展贡献，不依赖目标的用户安装集合。 */\n\tisBuiltin?: boolean;\n\tid: string;');
  for (const method of ['getConfigurationProperties', 'getExcludedConfigurationProperties']) {
    contents = replaceOnce(contents, `${method}(): IStringDictionary<IRegisteredConfigurationPropertySchema>;`,
      `${method}(sourceFilter?: (source: ConfigurationDefaultSource | undefined) => boolean): IStringDictionary<IRegisteredConfigurationPropertySchema>;`);
    const property = method === 'getConfigurationProperties' ? 'configurationProperties' : 'excludedConfigurationProperties';
    contents = replaceOnce(contents, `${method}(): IStringDictionary<IRegisteredConfigurationPropertySchema> {\n\t\treturn this.${property};\n\t}`,
      `${method}(sourceFilter?: (source: ConfigurationDefaultSource | undefined) => boolean): IStringDictionary<IRegisteredConfigurationPropertySchema> {
    return sourceFilter ? this.projectConfigurationProperties(this.${property}, sourceFilter) : this.${property};
  }`);
  }
  contents = replaceOnce(contents, '\n\tgetPolicyConfigurations(): Map<PolicyName, string> {', `
  private projectConfigurationProperties(properties: IStringDictionary<IRegisteredConfigurationPropertySchema>,
    sourceFilter: (source: ConfigurationDefaultSource | undefined) => boolean): IStringDictionary<IRegisteredConfigurationPropertySchema> {
    const result: IStringDictionary<IRegisteredConfigurationPropertySchema> = {};
    for (const [key, schema] of Object.entries(properties)) {
      const isOverride = OVERRIDE_PROPERTY_REGEX.test(key);
      // 语言节点的 source 只是窗口最后一项，必须从原始贡献重新选择来源。
      if (!isOverride && !sourceFilter(schema.source)) continue;
      let defaultOverride: IConfigurationDefaultOverrideValue | undefined;
      let defaultSource: ConfigurationDefaultSource | undefined;
      for (const entry of this.configurationDefaultsOverrides.get(key)?.configurationDefaultOverrides ?? []) {
        if (!sourceFilter(entry.source)) continue;
        const { value, source } = deepClone(entry);
        defaultOverride = isOverride
          ? this.mergeDefaultConfigurationsForOverrideIdentifier(key, value as IStringDictionary<unknown>, source, defaultOverride)
          : this.mergeDefaultConfigurationsForConfigurationProperty(key, value, source, defaultOverride);
        defaultSource = source;
      }
      if (isOverride) {
        if (defaultOverride) result[key] = this.createDefaultOverrideProperty(key, defaultOverride, defaultSource);
      } else {
        const property = deepClone(schema);
        this.updatePropertyDefaultValue(key, property, defaultOverride ?? null);
        result[key] = property;
      }
    }
    return result;
  }

\tgetPolicyConfigurations(): Map<PolicyName, string> {`);
  contents = replaceOnce(contents,
    'private updateDefaultOverrideProperty(key: string, newDefaultOverride: IConfigurationDefaultOverrideValue, source: ConfigurationDefaultSource | undefined): void {\n\t\tconst property: IRegisteredConfigurationPropertySchema = {',
    'private createDefaultOverrideProperty(key: string, newDefaultOverride: IConfigurationDefaultOverrideValue, source: ConfigurationDefaultSource | undefined): IRegisteredConfigurationPropertySchema {\n\t\treturn {');
  contents = replaceOnce(contents,
    '\n\t\tthis.configurationProperties[key] = property;\n\t\tthis.defaultLanguageConfigurationOverridesNode.properties![key] = property;\n\t}\n\n\tprivate mergeDefaultConfigurationsForOverrideIdentifier',
    `
  }

  private updateDefaultOverrideProperty(key: string, newDefaultOverride: IConfigurationDefaultOverrideValue, source: ConfigurationDefaultSource | undefined): void {
    const property = this.createDefaultOverrideProperty(key, newDefaultOverride, source);
    this.configurationProperties[key] = property;
    this.defaultLanguageConfigurationOverridesNode.properties![key] = property;
  }

\tprivate mergeDefaultConfigurationsForOverrideIdentifier`);
  contents = replaceOnce(contents,
    'private updatePropertyDefaultValue(key: string, property: IRegisteredConfigurationPropertySchema): void {\n\t\tconst configurationdefaultOverride = this.configurationDefaultsOverrides.get(key)?.configurationDefaultOverrideValue;',
    `private updatePropertyDefaultValue(key: string, property: IRegisteredConfigurationPropertySchema,
    configurationdefaultOverride: IConfigurationDefaultOverrideValue | null = this.configurationDefaultsOverrides.get(key)?.configurationDefaultOverrideValue ?? null): void {`);
  contents = replaceOnce(contents,
    'export function validateProperty(property: string, schema: IRegisteredConfigurationPropertySchema, extensionId?: string): string | null {',
    'export function validateProperty(property: string, schema: IRegisteredConfigurationPropertySchema, extensionId?: string, replacing = false): string | null {');
  contents = replaceOnce(contents,
    'if (configurationRegistry.getConfigurationProperties()[property] !== undefined && (!extensionId || !EXTENSION_UNIFICATION_EXTENSION_IDS.has(extensionId.toLowerCase()))) {',
    'if (!replacing && configurationRegistry.getConfigurationProperties()[property] !== undefined && (!extensionId || !EXTENSION_UNIFICATION_EXTENSION_IDS.has(extensionId.toLowerCase()))) {');
  contents = replaceOnce(contents,
    'if (schema.policy?.name && configurationRegistry.getPolicyConfigurations().get(schema.policy?.name) !== undefined) {',
    `const policyOwner = schema.policy?.name ? configurationRegistry.getPolicyConfigurations().get(schema.policy.name) : undefined;
  if (policyOwner !== undefined && (!replacing || policyOwner !== property)) {`);
  contents = replaceOnce(contents,
    'property, schema.policy?.name, configurationRegistry.getPolicyConfigurations().get(schema.policy?.name));',
    'property, schema.policy?.name, policyOwner);');
  return contents;
}
