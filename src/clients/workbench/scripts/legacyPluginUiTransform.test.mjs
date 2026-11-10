import { expect, test } from 'bun:test';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { transformLegacyCustomizationPlugins } from './legacyCustomizationPluginTransform.mjs';

const root = resolve('src/clients/workbench');
const upstream = join(root, 'upstream');

async function source(relativePath) {
  return readFile(join(upstream, relativePath), 'utf8');
}

test('停用独立插件浏览视图并从管理区隐藏插件 section', async () => {
  const viewPath = 'src/vs/workbench/contrib/chat/browser/agentPluginsView.ts';
  expect(transformLegacyCustomizationPlugins(root, join(upstream, viewPath), await source(viewPath))).toBe('export {};\n');

  const sectionsPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationWorkspaceService.ts';
  const sections = await source(sectionsPath);
  const transformed = transformLegacyCustomizationPlugins(root, join(upstream, sectionsPath), sections, (input, marker, replacement) => {
    expect(input.split(marker)).toHaveLength(2);
    return input.replace(marker, replacement);
  });
  expect(transformed).not.toContain('\t\tAICustomizationManagementSection.Plugins,\n');
  expect(transformed).toContain('AICustomizationManagementSection.McpServers');
  expect(transformed).toContain('AICustomizationManagementSection.Agents');

  const editorPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagementEditor.ts';
  const editor = await source(editorPath);
  const transformedEditor = transformLegacyCustomizationPlugins(root, join(upstream, editorPath), editor);
  expect(transformedEditor).not.toContain('if (hasSections.has(AICustomizationManagementSection.Plugins))');
  expect(transformedEditor).not.toContain('AICustomizationManagementSection.Plugins');
  expect(transformedEditor).not.toContain('isPluginsSection');
  expect(transformedEditor).not.toContain("if (this.viewMode === 'pluginDetail')");
  expect(transformedEditor).not.toContain("case 'pluginDetail':");
  expect(transformedEditor).not.toContain('pluginDetail');
  expect(transformedEditor).not.toContain('PluginMarketplaces');
  expect(transformedEditor).not.toContain('isPluginMarketplaceCustomizationMigrationCandidate');
  expect(transformedEditor).toContain('CustomizationMigrationType.McpServers');

  const contributionPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagement.contribution.ts';
  const contribution = transformLegacyCustomizationPlugins(root, join(upstream, contributionPath), await source(contributionPath), (input, marker, replacement) => {
    if (input.split(marker).length !== 2) throw new Error(`锚点不唯一：${marker.slice(0, 80)}`);
    return input.replace(marker, replacement);
  });
  expect(contribution).not.toContain('SHOW_PLUGIN_AI_CUSTOMIZATION_ID');
  expect(contribution).not.toContain('UNINSTALL_PLUGIN_AI_CUSTOMIZATION_ID');
  expect(contribution).not.toContain('WHEN_ITEM_IS_PLUGIN');
  expect(contribution).not.toContain('Uninstall Plugin');
  expect(contribution).toContain('This item is provided by a plugin package and cannot be deleted individually.');
  expect(contribution).toContain('AICustomizationManagementCommands.GenerateDebugReport');

  const mcpPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts';
  const mcp = transformLegacyCustomizationPlugins(root, join(upstream, mcpPath), await source(mcpPath), (input, marker, replacement) => {
    if (input.split(marker).length !== 2) throw new Error(`锚点不唯一：${marker.slice(0, 80)}`);
    return input.replace(marker, replacement);
  });
  expect(mcp).not.toContain("'mcpServer.uninstallPlugin'");
  expect(mcp).not.toContain("'mcpServer.showPlugin'");
  expect(mcp).not.toContain('createInstalledPluginItem');
  expect(mcp).not.toContain('openPlugin');
  expect(mcp).toContain('getMcpEntrySource');
  expect(mcp).toContain('label: plugin.name');
  expect(mcp).toContain("localize('fromPlugin'");

  const toolsPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/toolsListWidget.ts';
  const tools = transformLegacyCustomizationPlugins(root, join(upstream, toolsPath), await source(toolsPath), (input, marker, replacement) => {
    if (input.split(marker).length !== 2) throw new Error(`锚点不唯一：${marker.slice(0, 80)}`);
    return input.replace(marker, replacement);
  });
  expect(tools).not.toContain('onDidRequestShowPlugin');
  expect(tools).not.toContain('createInstalledPluginItem');
  expect(tools).toContain('selection: file.range');

  const windowsRoot = 'C:\\work\\boxteam\\src\\clients\\workbench';
  const windowsToolsPath = `${windowsRoot}\\upstream\\${toolsPath.replaceAll('/', '\\')}`;
  const windowsTools = transformLegacyCustomizationPlugins(
    windowsRoot,
    windowsToolsPath,
    await source(toolsPath),
    (input, marker, replacement) => {
      if (input.split(marker).length !== 2) throw new Error(`Windows 转换锚点不唯一：${marker.slice(0, 80)}`);
      return input.replace(marker, replacement);
    },
  );
  expect(windowsTools).toBe(tools);

  const nonUpstream = transformLegacyCustomizationPlugins(
    windowsRoot,
    'C:\\work\\boxteam\\reference_repo\\src\\vs\\workbench\\' + toolsPath.slice('src/vs/workbench/'.length).replaceAll('/', '\\'),
    await source(toolsPath),
    () => { throw new Error('工作台转换不得匹配上游目录外的路径'); },
  );
  expect(nonUpstream).toBe(await source(toolsPath));

  const marketplacePath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceWorkbenchService.ts';
  const marketplace = transformLegacyCustomizationPlugins(root, join(upstream, marketplacePath), await source(marketplacePath), (input, marker, replacement) => {
    if (input.split(marker).length !== 2) throw new Error(`锚点不唯一：${marker.slice(0, 80)}`);
    return input.replace(marker, replacement);
  });
  expect(marketplace).not.toContain('createPluginCustomizationMarketplaceProviders');
  expect(marketplace).not.toContain('getPluginCustomizationMarketplaceSourceInfos');
  expect(marketplace).not.toContain('CustomizationMarketplaceSources.PluginMarketplaces');
  expect(marketplace).toContain('getProviderMarketplaceSources');
  expect(marketplace).toContain('CustomizationMarketplaceSources.CopilotConnectors');
  expect(marketplace).toContain('CustomizationMarketplaceSources.AgentFinderPublicFeed');

  const pluginListPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginListWidget.ts';
  const pluginList = transformLegacyCustomizationPlugins(root, join(upstream, pluginListPath), await source(pluginListPath));
  expect(pluginList).not.toContain('MANAGE_PLUGIN_MARKETPLACES_COMMAND_ID');
  expect(pluginList).not.toContain('showMarketplaceActions');
  expect(pluginList).not.toContain('marketplaceActionsButton');
  expect(pluginList).not.toContain('skipReveal');
  expect(pluginList).toContain("executeCommand<boolean>('workbench.action.chat.installPluginFromSource')");

  const discoveryPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationDiscoveryPage.ts';
  const discovery = transformLegacyCustomizationPlugins(root, join(upstream, discoveryPath), await source(discoveryPath));
  expect(discovery).not.toContain('MANAGE_PLUGIN_MARKETPLACES_COMMAND_ID');
  expect(discovery).not.toContain('getPluginCustomizationMarketplaceSourceId');
  expect(discovery).toContain('marketplace?.sourceId === this.selectedSourceId');

  const installServicePath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts';
  const installService = transformLegacyCustomizationPlugins(root, join(upstream, installServicePath), await source(installServicePath));
  expect(installService).not.toContain('configuredPlugin');
  expect(installService).not.toContain('getPluginMarketplaceIdentifier');
  expect(installService).toContain("source.kind === 'providerPlugin'");
  expect(installService).toContain("source.kind === 'mcpGallery'");

  const pluginMarketplacePath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginCustomizationMarketplaceProvider.ts';
  expect(transformLegacyCustomizationPlugins(root, join(upstream, pluginMarketplacePath), await source(pluginMarketplacePath))).toBe('export {}\n');

  const migrationServicePath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationServiceImpl.ts';
  const migrationService = transformLegacyCustomizationPlugins(root, join(upstream, migrationServicePath), await source(migrationServicePath), (input, marker, replacement) => {
    if (input.split(marker).length !== 2) throw new Error(`锚点不唯一：${marker.slice(0, 80)}`);
    return input.replace(marker, replacement);
  });
  expect(migrationService).not.toContain('PluginMarketplaces');
  expect(migrationService).not.toContain('parseMarketplaceReferences');
  expect(migrationService).toContain('CustomizationMigrationType.McpServers');
  expect(migrationService).toContain('migrateMcpServers');

  const migrationCategoryPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationCategories.ts';
  const migrationCategory = transformLegacyCustomizationPlugins(root, join(upstream, migrationCategoryPath), await source(migrationCategoryPath));
  expect(migrationCategory).not.toContain('PluginMarketplaces');
  expect(migrationCategory).not.toContain('isPluginMarketplaceCustomizationMigrationCandidate');
  expect(migrationCategory).toContain('CustomizationMigrationCategoryId.McpServers');

  const migrationContractPath = 'src/vs/workbench/contrib/chat/common/promptSyntax/service/customizationMigrationService.ts';
  const migrationContract = transformLegacyCustomizationPlugins(root, join(upstream, migrationContractPath), await source(migrationContractPath));
  expect(migrationContract).not.toContain('PluginMarketplaces');
  expect(migrationContract).not.toContain('IPluginMarketplaceCustomizationMigration');
  expect(migrationContract).toContain('migrateMcpServers');

  const migrationHelperPath = 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigration.ts';
  const migrationHelper = transformLegacyCustomizationPlugins(root, join(upstream, migrationHelperPath), await source(migrationHelperPath));
  expect(migrationHelper).not.toContain('isPluginMarketplaceCustomizationMigrationCandidate');
  expect(migrationHelper).toContain('isMcpServerCustomizationMigrationCandidate');

  const oldActionsPath = 'src/vs/workbench/contrib/chat/browser/actions/chatPluginActions.ts';
  const oldActions = transformLegacyCustomizationPlugins(root, join(upstream, oldActionsPath), await source(oldActionsPath));
  expect(oldActions).toContain('export class InstallFromSourceAction extends Action2');
  expect(oldActions).toContain('PluginInstallProviderAvailabilityContribution');
  expect(oldActions).toContain('PluginInstallProviderAvailableContext');
  expect(oldActions).toContain('registerWorkbenchContribution2(PluginInstallProviderAvailabilityContribution.ID, PluginInstallProviderAvailabilityContribution, WorkbenchPhase.BlockRestore)');
  expect(oldActions).toContain('registerAction2(InstallFromSourceAction)');
  expect(oldActions).toContain('precondition: ContextKeyExpr.and(ChatContextKeys.enabled, PluginInstallProviderAvailableContext)');
  expect(oldActions).toContain('available.set(this.customizationHarnessService.getDescriptorForSession(sessionResource)?.pluginInstallProvider !== undefined)');
  expect(oldActions).toContain('getDescriptorForSession(customizationHarnessService.activeSessionResource.get())?.pluginInstallProvider');
  expect(oldActions).toContain('if (!pluginInstallProvider)');
  expect(oldActions).toContain('pluginInstallProvider.installFromSource(sessionResource, source)');
  expect(oldActions).not.toContain('IPluginInstallService');
  expect(oldActions).not.toContain('pluginInstallService.validatePluginSource');
  expect(oldActions).not.toContain('pluginInstallService.installPluginFromSource');
  expect(oldActions).not.toContain('IExtensionsWorkbenchService');
  expect(oldActions).not.toContain('parseMarketplaceReference');
  expect(oldActions).not.toContain('extensionsWorkbenchService.openSearch');
  expect(oldActions).not.toContain('MANAGE_PLUGIN_MARKETPLACES_COMMAND_ID');
  expect(oldActions).not.toContain('ManagePluginMarketplacesAction');
  expect(oldActions).not.toContain('AddPluginMarketplaceAction');
  expect(oldActions).toContain('INotificationService');

});
