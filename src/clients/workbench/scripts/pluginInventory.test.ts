import { expect, test } from 'bun:test';
import { PluginBinding, PluginInventoryResultDTO } from '../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { installedPluginGroups } from '../src/extensions/pluginInventory.js';

function inventory(versions: string[]) {
  return PluginInventoryResultDTO.fromPartial({ packages: versions.map(version => ({ manifest: {
    package_id: 'developer-tools', name: 'Developer Tools', version,
    plugins: ['terminal', 'browser'].map(id => ({ id, name: id, instance_scope: 'workspace' })),
  } })) });
}
function binding(plugin_id: string, package_version: string) {
  return PluginBinding.fromPartial({ package_id: 'developer-tools', plugin_id, package_version, enabled: true });
}

test('多版本库存只形成一组；同包插件分别选择自己的绑定版本', () => {
  const source = inventory(['0.1.0', '0.2.0-dev.1', '0.2.0']);
  const groups = installedPluginGroups(source, [binding('terminal', '0.1.0'), binding('browser', '0.2.0-dev.1')], 'workspace');
  expect(groups).toHaveLength(1);
  expect(groups[0].plugins.map(item => [item.plugin.id, item.manifest.version])).toEqual([
    ['terminal', '0.1.0'], ['browser', '0.2.0-dev.1'],
  ]);
  expect(groups[0].versions.map(item => item.version)).toEqual(['0.2.0', '0.2.0-dev.1', '0.1.0']);
  expect(source.packages.map(item => item.manifest?.version)).toEqual(['0.1.0', '0.2.0-dev.1', '0.2.0']);
});

test('未绑定时使用最新适用版本，绑定版本丢失时不冒充其它版本', () => {
  const groups = installedPluginGroups(inventory(['0.1.0', '0.2.0']), [binding('terminal', '0.0.9')], 'workspace');
  expect(groups[0].plugins.map(item => [item.plugin.id, item.manifest.version])).toEqual([['browser', '0.2.0']]);
  expect(installedPluginGroups(inventory(['0.1.0']), [], 'gateway')[0].plugins).toHaveLength(0);
});

test('新版本已移除的插件仍从原绑定声明展示，不被最新版覆盖', () => {
  const source = inventory(['0.1.0', '0.2.0']);
  source.packages[1].manifest!.plugins = source.packages[1].manifest!.plugins.filter(plugin => plugin.id !== 'terminal');
  const groups = installedPluginGroups(source, [binding('terminal', '0.1.0')], 'workspace');
  expect(groups[0].plugins.find(item => item.plugin.id === 'terminal')?.manifest.version).toBe('0.1.0');
});
