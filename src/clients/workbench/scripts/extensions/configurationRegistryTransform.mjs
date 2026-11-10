import { join } from 'node:path';

export const configurationRegistrySourcePaths = [
  'src/vs/platform/configuration/common/configurationRegistry.ts',
];

/** 同一扩展 ID 的配置贡献更新时，允许原 registry 以 remove/add delta 替换其属性。 */
export function transformConfigurationRegistry(root, path, source, replaceOnce) {
  if (path !== join(root, 'upstream', configurationRegistrySourcePaths[0])) return source;
  let contents = replaceOnce(source,
    'export function validateProperty(property: string, schema: IRegisteredConfigurationPropertySchema, extensionId?: string): string | null {',
    'export function validateProperty(property: string, schema: IRegisteredConfigurationPropertySchema, extensionId?: string, replacing = false): string | null {');
  contents = replaceOnce(contents,
    'if (configurationRegistry.getConfigurationProperties()[property] !== undefined && (!extensionId || !EXTENSION_UNIFICATION_EXTENSION_IDS.has(extensionId.toLowerCase()))) {',
    'if (!replacing && configurationRegistry.getConfigurationProperties()[property] !== undefined && (!extensionId || !EXTENSION_UNIFICATION_EXTENSION_IDS.has(extensionId.toLowerCase()))) {');
  contents = replaceOnce(contents,
    'if (schema.policy?.name && configurationRegistry.getPolicyConfigurations().get(schema.policy?.name) !== undefined) {',
    `const policyOwner = schema.policy?.name ? configurationRegistry.getPolicyConfigurations().get(schema.policy.name) : undefined;
  if (policyOwner !== undefined && (!replacing || policyOwner !== property)) {`);
  return replaceOnce(contents,
    'property, schema.policy?.name, configurationRegistry.getPolicyConfigurations().get(schema.policy?.name));',
    'property, schema.policy?.name, policyOwner);');
}
