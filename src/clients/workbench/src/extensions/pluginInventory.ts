import { rcompare } from '../../upstream/src/vs/base/common/semver/semver.js';
import type { PluginBinding, PluginInventoryResultDTO, PluginPackageManifest, ServicePluginManifest } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';

export interface InstalledPlugin {
  manifest: PluginPackageManifest;
  plugin: ServicePluginManifest;
}

/** 库存保留所有版本；用户列表按身份合并，各插件独立选择自己的绑定版本。 */
export function installedPluginGroups(inventory: PluginInventoryResultDTO, bindings: readonly PluginBinding[], scope: string) {
  const packages = new Map<string, PluginPackageManifest[]>();
  for (const item of inventory.packages) {
    if (!item.manifest) continue;
    const versions = packages.get(item.manifest.package_id) ?? [];
    versions.push(item.manifest);
    packages.set(item.manifest.package_id, versions);
  }
  return [...packages.values()].map(versions => {
    versions.sort((left, right) => rcompare(left.version, right.version));
    const ids = new Set(versions.flatMap(manifest => manifest.plugins.filter(plugin => plugin.instance_scope === scope).map(plugin => plugin.id)));
    const plugins: InstalledPlugin[] = [];
    for (const id of ids) {
      const binding = bindings.find(item => item.package_id === versions[0].package_id && item.plugin_id === id);
      const manifest = versions.find(item => (!binding || item.version === binding.package_version)
        && item.plugins.some(plugin => plugin.id === id && plugin.instance_scope === scope));
      // 缺失的绑定版本由原缺失声明入口展示，不用其它版本冒充。
      if (manifest) plugins.push({ manifest, plugin: manifest.plugins.find(plugin => plugin.id === id && plugin.instance_scope === scope)! });
    }
    return { manifest: versions[0], versions, plugins };
  });
}
