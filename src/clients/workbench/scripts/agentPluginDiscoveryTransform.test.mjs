import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pinnedUpstreamCommit, sourceTransform } from './sourceTransform.mjs';

const workbenchRoot = resolve('src/clients/workbench');
const transform = sourceTransform(workbenchRoot, { commit: pinnedUpstreamCommit }, []);

async function transformUpstream(relativePath) {
  const path = join(workbenchRoot, 'upstream', relativePath);
  const original = await readFile(path, 'utf8');
  const transformed = await transform(path, original);
  expect(await readFile(path, 'utf8')).toBe(original);
  return transformed;
}

test('Workbench不启动旧Agent Plugin发现来源，保留产品Agent与Prompt服务', async () => {
  const contribution = await transformUpstream('src/vs/workbench/contrib/chat/browser/chat.shared.contribution.ts');
  for (const discovery of [
    'ConfiguredAgentPluginDiscovery',
    'MarketplaceAgentPluginDiscovery',
    'ExtensionAgentPluginDiscovery',
    'CopilotCliAgentPluginDiscovery',
  ]) {
    expect(contribution).not.toContain(`new SyncDescriptor(${discovery})`);
  }
  expect(contribution).toContain('registerSingleton(IChatAgentService, ChatAgentService, InstantiationType.Delayed);');
  expect(contribution).toContain('registerSingleton(IPromptsService, PromptsService, InstantiationType.Delayed);');
  expect(contribution).toContain('registerSingleton(IAgentPluginService, AgentPluginService, InstantiationType.Delayed);');

  const service = await transformUpstream('src/vs/workbench/contrib/chat/common/plugins/agentPluginServiceImpl.ts');
  expect(service).not.toContain('agentPluginDiscoveryRegistry');
  expect(service).not.toContain('agentPluginDiscoveryRegistry.getAll()');
  expect(service).not.toContain('instantiationService.createInstance(registration.descriptor)');
  expect(service).not.toContain("import { IInstantiationService }");
  expect(service).toContain("import { IAgentPluginEnablementService, IAgentPluginService, type IAgentPlugin } from './agentPluginService.js';");
  expect(service).toContain("import { type IEnablementModel } from '../enablement.js';");
  for (const retiredImplementation of [
    'ConfiguredAgentPluginDiscovery',
    'MarketplaceAgentPluginDiscovery',
    'ExtensionAgentPluginDiscovery',
    'CopilotCliAgentPluginDiscovery',
    '_readInstalledPlugins',
    'detectPluginFormat',
  ]) {
    expect(service).not.toContain(retiredImplementation);
  }
  expect(service).toContain('export class AgentPluginService extends Disposable implements IAgentPluginService');
  expect(service).toContain("observableValue<readonly IAgentPlugin[]>('discoveredAgentPlugins', [])");
});
