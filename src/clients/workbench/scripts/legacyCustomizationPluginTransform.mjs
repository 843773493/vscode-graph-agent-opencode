import { join } from 'node:path';
import ts from 'typescript';
import { pruneUnusedImportsAndConstants } from './extensions/extensionManagementTransform.mjs';

export const legacyCustomizationPluginSourcePaths = [
  'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationWorkspaceService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagementEditor.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagement.contribution.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/toolsListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceWorkbenchService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationDiscoveryPage.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginCustomizationMarketplaceProvider.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/actions/chatPluginActions.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplace.contribution.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationCategories.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationAccessibility.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationDashboard.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationServiceImpl.ts',
  'src/vs/workbench/contrib/chat/common/promptSyntax/service/customizationMigrationService.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigration.ts',
  'src/vs/workbench/contrib/chat/browser/agentPluginsView.ts',
];

function removePluginOnlyBranches(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const containsPluginMarker = expression => {
    let found = false;
    const visit = node => {
      if (ts.isPropertyAccessExpression(node) && (
        node.expression.getText(file) === 'AICustomizationManagementSection' && node.name.text === 'Plugins'
        || ['pluginContentContainer', 'pluginListWidget', 'pluginDetailContainer', 'embeddedPluginDetail', 'pluginDetailScrollable'].includes(node.name.text)
      ) || ts.isStringLiteral(node) && node.text === 'pluginDetail') {
        found = true;
      }
      if (!found) ts.forEachChild(node, visit);
    };
    visit(expression);
    return found;
  };
  const isPluginOnly = expression => {
    if (ts.isParenthesizedExpression(expression)) return isPluginOnly(expression.expression);
    if (ts.isBinaryExpression(expression) && expression.operatorToken.kind === ts.SyntaxKind.BarBarToken) {
      return isPluginOnly(expression.left) && isPluginOnly(expression.right);
    }
    return containsPluginMarker(expression);
  };
  const visit = node => {
    if (ts.isPropertyAssignment(node) && node.name.getText(file) === '[AICustomizationManagementSection.Plugins]') {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isIfStatement(node) && ts.isBinaryExpression(node.expression) && node.expression.operatorToken.kind === ts.SyntaxKind.BarBarToken) {
      const { left, right } = node.expression;
      if (isPluginOnly(left) && !isPluginOnly(right)) {
        edits.push({ start: node.expression.getStart(file), end: node.expression.end, text: right.getText(file) });
      } else if (isPluginOnly(right) && !isPluginOnly(left)) {
        edits.push({ start: node.expression.getStart(file), end: node.expression.end, text: left.getText(file) });
      }
    }
    if (ts.isIfStatement(node) && isPluginOnly(node.expression)) {
      edits.push({ start: node.getStart(file), end: node.end, text: node.elseStatement?.getText(file) ?? '' });
      if (node.elseStatement) visit(node.elseStatement);
      return;
    }
    if (ts.isCaseClause(node) && containsPluginMarker(node.expression)) {
      edits.push({ start: node.getStart(file), end: node.end, text: '' });
      return;
    }
    if (ts.isConditionalExpression(node) && isPluginOnly(node.condition)) {
      edits.push({ start: node.getStart(file), end: node.end, text: node.whenFalse.getText(file) });
      return;
    }
    if (ts.isExpressionStatement(node) && node.getText(file).includes('onDidRequestShowPlugin')) {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
      return;
    }
    if (ts.isExpressionStatement(node) && /^this\.(?:pluginListWidget|embeddedPluginDetail|pluginDetail\w*)/.test(node.expression.getText(file).trim())
        || ts.isExpressionStatement(node) && node.getText(file).trim().startsWith('this.scheduleDetailScrollableScan(this.pluginDetailScrollable,')) {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  }
  return source;
}

function removeListElement(file, node) {
  const siblings = node.parent.properties ?? node.parent.elements ?? node.parent.members ?? node.parent.statements ?? node.parent.clauses ?? node.parent.declarations;
  const index = siblings?.indexOf(node) ?? -1;
  if (index > 0) return { start: siblings[index - 1].end, end: node.end };
  if (index === 0 && siblings.length > 1) return { start: node.getFullStart(), end: siblings[1].getFullStart() };
  return { start: node.getFullStart(), end: node.end };
}

function applyNonOverlappingEdits(source, edits) {
  const accepted = [];
  for (const edit of [...edits].sort((left, right) => left.start - right.start || right.end - left.end)) {
    if (accepted.length && accepted.at(-1).end > edit.start) continue;
    accepted.push(edit);
  }
  for (const edit of accepted.reverse()) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  return source;
}

function removeEditorPluginDetail(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const editor = file.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'AICustomizationManagementEditor');
  if (!editor) throw new Error('缺少 AICustomizationManagementEditor');
  const removedMembers = new Set([
    'pluginContentContainer', 'pluginListWidget', 'pluginDetailContainer', 'embeddedPluginDetail',
    'pluginDetailScrollable', 'pluginDetailBackButton', 'pluginDetailBackHover', 'pluginDetailDisposables',
    'pluginDetailScrollUpdate', 'pluginDetailInput', 'pluginDetailOrigin', 'createEmbeddedPluginDetail',
    'showEmbeddedPluginDetail', 'updatePluginDetailBackButton', 'openSkillFromPluginDetail',
    'openPromptsItemFromPluginDetail', 'openSectionFromPluginDetail', 'showPluginDetail', 'goBackFromPluginDetail',
    'ensureMarketplaceDetailInstallState',
  ]);
  const edits = editor.members.filter(member => {
    const name = member.name?.getText(file);
    return name !== undefined && removedMembers.has(name);
  }).map(member => ({ start: member.getFullStart(), end: member.end }));
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + source.slice(edit.end);
  }
  source = source.replace("type CustomizationDetailOrigin =\n\t| CustomizationDetailBaseOrigin\n\t| { readonly kind: 'pluginDetail'; readonly item: IAgentPluginItem; readonly origin: CustomizationDetailBaseOrigin };", 'type CustomizationDetailOrigin = CustomizationDetailBaseOrigin;');
  source = source.replace("private viewMode: 'list' | 'migration' | 'editor' | 'marketplaceDetail' | 'mcpDetail' | 'connectorDetail' | 'pluginDetail' = 'list';", "private viewMode: 'list' | 'migration' | 'editor' | 'marketplaceDetail' | 'mcpDetail' | 'connectorDetail' = 'list';");
  source = source.replace("\t\tconst isPluginDetailMode = this.viewMode === 'pluginDetail';\n\t\tconst isDetailMode = isMarketplaceDetailMode || isMcpDetailMode || isConnectorDetailMode || isPluginDetailMode;", "\t\tconst isDetailMode = isMarketplaceDetailMode || isMcpDetailMode || isConnectorDetailMode;");
  source = source.replace("\t\tconst isPluginsSection = this.selectedSection === AICustomizationManagementSection.Plugins;\n", '');
  source = source.replace("import { PluginListWidget } from './pluginListWidget.js';\n", '');
  source = source.replace("import { AgentPluginItemKind, IAgentPluginItem } from '../agentPluginEditor/agentPluginItems.js';\n", '');
  source = source.replace("import { IAgentPluginService } from '../../common/plugins/agentPluginService.js';\n", '');
  source = source.replace("import { EmbeddedAgentPluginDetail } from './embeddedAgentPluginDetail.js';\n", '');
  source = source.replace('\t\t@IAgentPluginService private readonly agentPluginService: IAgentPluginService,\n', '');
  source = source.replace('\t\tthis.editorDisposables.add(this.marketplaceInstallService.onDidChange(() => this.ensureMarketplaceDetailInstallState()));\n', '');
  source = source.replace('\t\t\t\t\tthis.embeddedPluginDetail?.clearInput();\n', '');
  source = source.replace('\t\t\t\t\t// The MCP detail editor uses automatic layout; plugin details flow with\n\t\t\t\t\t// their container, so neither requires an explicit layout call here.\n', '');
  return pruneUnusedImportsAndConstants(path, source);
}

function removePluginUninstallFallback(path, source, replaceOnce) {
  source = replaceOnce(source, '\t\t// Plugin-provided files: offer to uninstall the plugin\n', '');
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  let fallback;
  const visit = node => {
    if (ts.isIfStatement(node) && node.expression.getText(file) === 'source === AICustomizationSources.plugin'
      && node.getText(file).includes("localize('cannotDeletePluginItem'")) {
      fallback = node;
      return;
    }
    if (!fallback) ts.forEachChild(node, visit);
  };
  visit(file);
  if (!fallback) throw new Error('缺少插件文件删除保护分支');
  return source.slice(0, fallback.getStart(file))
    + `if (source === AICustomizationSources.plugin) {
			await dialogService.info(
				localize('cannotDeletePluginItem', "Cannot Delete Plugin Item"),
				localize('cannotDeletePluginItemDetail', "This item is provided by a plugin package and cannot be deleted individually.")
			);
			return;
		}`
    + source.slice(fallback.end);
}

function removePluginManagementActions(path, source, replaceOnce) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const ids = ['SHOW_PLUGIN_AI_CUSTOMIZATION_ID', 'UNINSTALL_PLUGIN_AI_CUSTOMIZATION_ID'];
  const edits = file.statements.filter(statement => {
    const text = statement.getText(file);
    return ids.some(id => text.includes(id)) || text.includes('WHEN_ITEM_IS_PLUGIN') && text.includes('SYNCED_CUSTOMIZATION_SCHEME');
  })
    .map(statement => ({ start: statement.getFullStart(), end: statement.end }));
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + source.slice(edit.end);
  }
  source = removePluginUninstallFallback(path, source, replaceOnce);
  source = replaceOnce(source, "import { SYNCED_CUSTOMIZATION_SCHEME } from '../../../../services/agentHost/common/agentHostFileSystemService.js';\n", '');
  const transformed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const extractPluginUri = transformed.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === 'extractPluginUri');
  if (extractPluginUri) source = source.slice(0, extractPluginUri.getFullStart()) + source.slice(extractPluginUri.end);
  return pruneUnusedImportsAndConstants(path, source);
}

function removeMcpPluginDetailActions(relative, path, source, replaceOnce) {
  if (relative.endsWith('/mcpListWidget.ts')) {
    source = replaceOnce(source,
      "function getMcpEntrySource(element: IMcpInstalledEntry, labelService: ILabelService, agentPluginService: IAgentPluginService, extensionsWorkbenchService?: IExtensionsWorkbenchService, openPlugin?: (plugin: IAgentPlugin) => void): { label: string; hover: string; ariaLabel?: string; open?(): void } | undefined {",
      "function getMcpEntrySource(element: IMcpInstalledEntry, labelService: ILabelService, agentPluginService: IAgentPluginService, extensionsWorkbenchService?: IExtensionsWorkbenchService): { label: string; hover: string; ariaLabel?: string; open?(): void } | undefined {");
    source = replaceOnce(source,
      "export function getAgentHostMcpServerSource(server: AgentHostMcpServer, plugin: { readonly name: string; readonly uri: string } | undefined, labelService: ILabelService, agentPluginService: IAgentPluginService, openPlugin?: (plugin: IAgentPlugin) => void, openFile?: (source: NonNullable<IMcpServerDetailInput['source']>) => void): ReturnType<typeof getMcpEntrySource> {",
      "export function getAgentHostMcpServerSource(server: AgentHostMcpServer, plugin: { readonly name: string; readonly uri: string } | undefined, labelService: ILabelService, agentPluginService: IAgentPluginService, openFile?: (source: NonNullable<IMcpServerDetailInput['source']>) => void): ReturnType<typeof getMcpEntrySource> {");
    source = replaceOnce(source,
      "\t\tconst pluginUri = URI.parse(plugin.uri);\n\t\tconst clientPlugin = agentPluginService.plugins.get().find(candidate => isEqual(candidate.uri, pluginUri));\n\t\tif (clientPlugin && openPlugin) {\n\t\t\treturn {\n\t\t\t\tlabel: clientPlugin.label,\n\t\t\t\thover: localize('openPluginDetails', \"Open plugin details for {0}\", clientPlugin.label),\n\t\t\t\tariaLabel: localize('openPluginDetails', \"Open plugin details for {0}\", clientPlugin.label),\n\t\t\t\topen: () => openPlugin(clientPlugin),\n\t\t\t};\n\t\t}\n", '');
    source = replaceOnce(source,
      "\t\t\t\thover: localize('openPluginDetails', \"Open plugin details for {0}\", plugin.label),\n\t\t\t\tariaLabel: localize('openPluginDetails', \"Open plugin details for {0}\", plugin.label),\n\t\t\t\topen: openPlugin ? () => openPlugin(plugin) : undefined,",
      "\t\t\t\thover: localize('fromPlugin', \"MCP server provided by {0}\", plugin.label),");
    source = replaceOnce(source,
      'const source = getMcpEntrySource(element, this.labelService, this.agentPluginService, this.extensionsWorkbenchService, this._openPlugin);',
      'const source = getMcpEntrySource(element, this.labelService, this.agentPluginService, this.extensionsWorkbenchService);');
    source = replaceOnce(source,
      '\t\tprivate readonly _openPlugin: (plugin: IAgentPlugin) => void,\n', '');
    source = replaceOnce(source,
      '\t\t\tplugin => this._onDidRequestShowPlugin.fire(createInstalledPluginItem(plugin)),\n', '');
    source = replaceOnce(source,
      '\t\tconst source = getMcpEntrySource(entry, this.labelService, this.agentPluginService, this.extensionsWorkbenchService, plugin => this._onDidRequestShowPlugin.fire(createInstalledPluginItem(plugin)));',
      '\t\tconst source = getMcpEntrySource(entry, this.labelService, this.agentPluginService, this.extensionsWorkbenchService);');
    source = replaceOnce(source,
      '\t\t@IDialogService private readonly dialogService: IDialogService,\n', '');
    source = removeClassMembersByNames(path, source, 'McpListWidget', ['_onDidRequestShowPlugin', 'onDidRequestShowPlugin']);
    source = removeClassPluginContextActions(path, source);
    source = removeTopLevelFunction(path, source, 'createInstalledPluginItem');
  } else {
    source = source.replace("import { IAgentPluginItem } from '../agentPluginEditor/agentPluginItems.js';\n", '');
    source = source.replace('createInstalledPluginItem, ', '');
    source = source.replace("\t\t\tthis._agentPluginService,\n\t\t\tplugin => this._onDidRequestShowPlugin.fire(createInstalledPluginItem(plugin)),\n\t\t\tfile => void this._editorService.openEditor({ resource: file.uri, options: { selection: file.range, pinned: true } }),",
      "\t\t\tthis._agentPluginService,\n\t\t\tfile => void this._editorService.openEditor({ resource: file.uri, options: { selection: file.range, pinned: true } }),");
    source = source.replace('\tprivate readonly _onDidRequestShowPlugin = this._register(new Emitter<IAgentPluginItem>());\n\treadonly onDidRequestShowPlugin = this._onDidRequestShowPlugin.event;\n', '');
    source = source.replace("\t\t\tthis._agentPluginService,\n\t\t\tplugin => this._onDidRequestShowPlugin.fire(createInstalledPluginItem(plugin)),\n", "\t\t\tthis._agentPluginService,\n");
  }
  return pruneUnusedImportsAndConstants(path, source);
}

function removeLegacyPluginMarketplaceBrowser(path, source, replaceOnce) {
  for (const [before, after] of [
    ["import { createPluginCustomizationMarketplaceProviders, getAllPluginCustomizationMarketplaceSourceInfos, getPluginCustomizationMarketplaceSourceInfos, isPluginMarketplaceAvailableInDiscover } from './pluginCustomizationMarketplaceProvider.js';\n", ''],
    ["type CustomizationMarketplaceBrowseSourceCategory = 'mcpGallery' | 'copilotConnectors' | 'publicFeed' | 'pluginMarketplace' | 'providerMarketplace' | 'other';", "type CustomizationMarketplaceBrowseSourceCategory = 'mcpGallery' | 'copilotConnectors' | 'publicFeed' | 'providerMarketplace' | 'other';"],
    ["\tif (sourceId.startsWith(CustomizationMarketplaceSources.PluginMarketplaces.id)) {\n\t\treturn 'pluginMarketplace';\n\t}\n", ''],
    ["\t\tconst pluginSources = this.marketplaceManagementProvider\n\t\t\t? []\n\t\t\t: getAllPluginCustomizationMarketplaceSourceInfos(this.pluginMarketplaceService);\n", ''],
    ['\t\t\t\t...pluginSources,\n', ''],
    ["\t\tconst githubFeedAvailable = this.isGitHubFeedAvailable();\n\t\tconst pluginSources = this.marketplaceManagementProvider\n\t\t\t? []\n\t\t\t: getPluginCustomizationMarketplaceSourceInfos(this.configurationService, this.pluginMarketplaceService, githubFeedAvailable);\n", "\t\tconst githubFeedAvailable = this.isGitHubFeedAvailable();\n"],
    ["\t\tif (this.configurationService.getValue<boolean>(ChatConfiguration.PluginsEnabled) !== true) {\n\t\t\treturn [];\n\t\t}\n", ''],
    ["\t\treturn this.getProviderMarketplaces(includeUnavailable)\n\t\t\t.map(marketplace => ({\n\t\t\t\tid: getProviderMarketplaceSourceId(marketplace.name),\n\t\t\t\tdisplayName: marketplace.name,\n\t\t\t\tenablementSetting: CustomizationMarketplaceConfiguration.MarketplaceEnabled,\n\t\t\t\tconfigurationDependencies: [ChatConfiguration.PluginsEnabled, CustomizationMarketplaceConfiguration.AgentFinderPublicFeedEnabled],\n\t\t\t}));", "\t\treturn this.getProviderMarketplaces(includeUnavailable)\n\t\t\t.map(marketplace => ({\n\t\t\t\tid: getProviderMarketplaceSourceId(marketplace.name),\n\t\t\t\tdisplayName: marketplace.name,\n\t\t\t\tenablementSetting: CustomizationMarketplaceConfiguration.MarketplaceEnabled,\n\t\t\t\tconfigurationDependencies: [CustomizationMarketplaceConfiguration.AgentFinderPublicFeedEnabled],\n\t\t\t}));"],
    ["\t\treturn this.providerMarketplaces.filter(marketplace => includeUnavailable ||\n\t\t\tmarketplace.available !== false && isPluginMarketplaceAvailableInDiscover(this.configurationService, marketplace.source, this.isGitHubFeedAvailable()));", "\t\treturn this.providerMarketplaces.filter(marketplace => includeUnavailable || marketplace.available !== false);"],
    ["\t\tconst pluginProviders = managementProvider\n\t\t\t? []\n\t\t\t: createPluginCustomizationMarketplaceProviders(\n\t\t\t\tthis.instantiationService,\n\t\t\t\tthis.configurationService,\n\t\t\t\tthis.pluginMarketplaceService,\n\t\t\t\tgithubFeedAvailable,\n\t\t\t);\n", ''],
    ["\t\t\t...pluginProviders.map(provider => provider.id),\n", ''],
    ["\t\t\t\t...pluginProviders,\n", ''],
    ["\t\tconst queriesPlugins = sources.some(source => (!options.sourceIds || options.sourceIds.includes(source.id)) &&\n\t\t\t(source.id.startsWith(CustomizationMarketplaceSources.PluginMarketplaces.id) || source.id.startsWith('providerPluginMarketplace.')));", "\t\tconst queriesProviderMarketplaces = sources.some(source => (!options.sourceIds || options.sourceIds.includes(source.id)) && source.id.startsWith('providerPluginMarketplace.'));"] ,
    ['if (queriesPlugins || queriesPublicFeed && strictPolicyActive !== this.pluginMarketplaceService.isStrictMarketplacePolicyActive()) {', 'if (queriesProviderMarketplaces || queriesPublicFeed && strictPolicyActive !== this.pluginMarketplaceService.isStrictMarketplacePolicyActive()) {'],
  ]) {
    if (before === '\t\t\t\t...pluginSources,\n') {
      const count = source.split(before).length - 1;
      if (count !== 2) throw new Error(`旧 Marketplace 来源锚点数量错误：${count}`);
      source = source.replaceAll(before, after);
    } else if (before) {
      source = replaceOnce(source, before, after);
    }
  }
  return pruneUnusedImportsAndConstants(path, source);
}

function removeLegacyPluginMarketplaceDiscovery(path, source) {
  source = source.replace("import { MANAGE_PLUGIN_MARKETPLACES_COMMAND_ID } from '../actions/chatPluginActions.js';\n", '');
  source = source.replace("import { getPluginCustomizationMarketplaceSourceId } from './pluginCustomizationMarketplaceProvider.js';\n", '');
  source = source.replace(/\n\t\tconst configure = disposables\.add\(new Action\([\s\S]*?\n\t\t\)\);\n\t\tthis\.contextMenuService\.showContextMenu\(\{\n\t\t\tgetAnchor: \(\) => this\.sourceButton\.element,\n\t\t\tgetActions: \(\) => \[allSources, \.\.\.sourceActions, new Separator\(\), configure\],/, '\n\t\tthis.contextMenuService.showContextMenu({\n\t\t\tgetAnchor: () => this.sourceButton.element,\n\t\t\tgetActions: () => [allSources, ...sourceActions],');
  source = source.replace(/\n\t\t\t\tconst pluginMarketplaceReference = item\.pluginDetail\?\.kind === AgentPluginItemKind\.Installed\n\t\t\t\t\t\? item\.pluginDetail\.plugin\.fromMarketplace\?\.marketplaceReference\n\t\t\t\t\t: undefined;\n\t\t\t\tconst sourceId = marketplace\?\.sourceId \?\? \(pluginMarketplaceReference \? getPluginCustomizationMarketplaceSourceId\(pluginMarketplaceReference\) : undefined\);\n\t\t\t\treturn sourceId === this\.selectedSourceId;/, '\n\t\t\t\treturn marketplace?.sourceId === this.selectedSourceId;');
  return pruneUnusedImportsAndConstants(path, source);
}

function removeConfiguredPluginInstallFlow(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const strip = node => {
    if (ts.isParenthesizedExpression(node)) {
      const inner = strip(node.expression);
      return inner ? `(${inner})` : undefined;
    }
    if (ts.isBinaryExpression(node) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind)) {
      const left = strip(node.left);
      const right = strip(node.right);
      if (node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken && (!left || !right)) return undefined;
      if (!left) return right;
      if (!right) return left;
      return `${left} ${node.operatorToken.getText(file)} ${right}`;
    }
    const text = node.getText(file);
    if (text.includes("kind === 'configuredPlugin'") || text.includes("kind !== 'configuredPlugin'")) return undefined;
    return text;
  };
  const add = (node, text) => edits.push({ start: node.getStart(file), end: node.end, text });
  const visit = node => {
    if (ts.isIfStatement(node)) {
      const condition = strip(node.expression);
      if (!condition) {
        edits.push({ start: node.getStart(file), end: node.end, text: node.elseStatement?.getText(file) ?? '' });
        if (node.elseStatement) visit(node.elseStatement);
        return;
      }
      if (condition !== node.expression.getText(file)) add(node.expression, condition);
    }
    if (ts.isConditionalExpression(node)) {
      const condition = strip(node.condition);
      if (!condition) {
        add(node, node.whenFalse.getText(file));
        return;
      }
      if (condition !== node.condition.getText(file)) add(node.condition, condition);
    }
    if (ts.isCaseClause(node) && node.expression.getText(file).includes("'configuredPlugin'")) {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && ['filter', 'some'].includes(node.expression.name.text) && node.arguments.length === 1
      && (ts.isArrowFunction(node.arguments[0]) || ts.isFunctionExpression(node.arguments[0]))) {
      const callback = node.arguments[0];
      const condition = strip(callback.body);
      if (!condition) add(node, node.expression.name.text === 'filter' ? node.expression.expression.getText(file) : 'true');
      else if (condition !== callback.body.getText(file)) add(callback.body, condition);
      return;
    }
    if (ts.isArrayLiteralExpression(node)) {
      for (const element of node.elements) {
        if (element.getText(file).includes("'configuredPlugin'")) edits.push({ ...removeListElement(file, element), text: '' });
      }
      return;
    }
    if (ts.isBinaryExpression(node)
      && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind)
      && !(ts.isBinaryExpression(node.parent) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.parent.operatorToken.kind))) {
      const expression = strip(node);
      if (expression !== node.getText(file)) add(node, expression ?? 'false');
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  source = applyNonOverlappingEdits(source, edits);
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const service = parsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'CustomizationMarketplaceInstallService');
  if (!service) throw new Error('缺少 CustomizationMarketplaceInstallService');
  const removedMembers = new Set(['isLegacyConfiguredPluginRecord', 'isLegacyConfiguredPluginResource', 'resolveConfiguredPlugin', 'getInstalledPlugin', 'getInstalledPlugins']);
  source = applyNonOverlappingEdits(source, service.members
    .filter(item => removedMembers.has(item.name?.getText(parsed) ?? ''))
    .map(item => ({ start: item.getFullStart(), end: item.end, text: '' })));
  const current = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const currentService = current.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'CustomizationMarketplaceInstallService');
  const lookup = currentService?.members.find(item => item.name?.getText(current) === 'getExistingInstalledPlugin');
  if (!lookup) throw new Error('缺少 getExistingInstalledPlugin');
  source = source.slice(0, lookup.getFullStart()) + `\tprivate async getExistingInstalledPlugin(record: ICustomizationMarketplaceInstallationRecord) {
\t\tconst target = record.target;
\t\tif (target.kind !== 'plugin') {
\t\t\treturn undefined;
\t\t}
\t\ttry {
\t\t\treturn (await this.fileService.resolve(target.uri)).isDirectory ? { pluginUri: target.uri } : undefined;
\t\t} catch (error) {
\t\t\tif (toFileOperationResult(error) === FileOperationResult.FILE_NOT_FOUND) {
\t\t\t\treturn undefined;
\t\t\t}
\t\t\tthrow error;
\t\t}
\t}\n` + source.slice(lookup.end);
  source = source.replace(' || this.isLegacyConfiguredPluginRecord(record, resource)', '');
  source = source.replace("\t\t\t(recorded, resource) => this.isLegacyConfiguredPluginResource(recorded, resource),\n", '');
  for (const parameter of ['agentPluginService']) {
    source = removeConstructorParameter(path, source, 'CustomizationMarketplaceInstallService', parameter);
  }
  source = source.replace(/^\s*this\.agentPluginService\.plugins\.read\(reader\);\n/m, '');
  source = source.replace(/^\s*this\.pluginMarketplaceService\.installedPlugins\.read\(reader\);\n/m, '');
  source = source.replace(/^\s*void this\.reconcileRecords\(this\.getRecordsByKind\('plugin'\)\);\n/m, '');
  source = source.replace("import { getPluginCustomizationMarketplaceSourceIdFromIdentifier, getPluginMarketplaceIdentifier } from './pluginCustomizationMarketplaceProvider.js';\n", '');
  return pruneUnusedImportsAndConstants(path, source);
}

function removePluginMarketplaceMigration(path, source, replaceOnce) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const classNode = file.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'CustomizationMigrationService');
  if (!classNode) throw new Error('缺少 CustomizationMigrationService');
  for (const name of [
    '_onDidChangeMarketplaceInventory', 'onDidChangePluginMarketplaces', 'migratePluginMarketplaces',
    'getConfiguredPluginMarketplaceCandidates', 'computePluginMarketplaceMigration',
    'getProviderMarketplaceCanonicalId', 'getPluginMarketplaceUserValues',
  ]) {
    source = removeClassMembersByNames(path, source, 'CustomizationMigrationService', [name]);
  }
  source = source.replace("import { parseMarketplaceReferences } from '../../common/plugins/marketplaceReference.js';\n", '');
  source = replaceOnce(source,
    "\t\tconst marketplaceProviderListeners = this._register(new DisposableStore());\n\t\tthis._register(autorun(reader => {\n\t\t\tconst providers = new Set(customizationHarnessService.availableHarnesses.read(reader)\n\t\t\t\t.flatMap(harness => harness.marketplaceManagementProvider ? [harness.marketplaceManagementProvider] : []));\n\t\t\tmarketplaceProviderListeners.clear();\n\t\t\tfor (const provider of providers) {\n\t\t\t\tmarketplaceProviderListeners.add(provider.onDidChange(() => this._onDidChangeMarketplaceInventory.fire()));\n\t\t\t}\n\t\t}));\n", '');
  source = replaceOnce(source,
    '\t\t\tEvent.filter(configurationService.onDidChangeConfiguration, event => event.affectsConfiguration(ChatConfiguration.PluginMarketplaces)),\n\t\t\tthis.onDidChangePluginMarketplaces,\n', '');
  source = replaceOnce(source,
    '\tcomputeMigration(sessionResource: URI, type: CustomizationMigrationType.PluginMarketplaces, token?: CancellationToken): Promise<PluginMarketplaceCustomizationMigration>;\n', '');
  source = replaceOnce(source,
    '\t\t\tcase CustomizationMigrationType.PluginMarketplaces: {\n\t\t\t\tconst marketplaceManagementProvider = this.customizationHarnessService.findHarnessById(getChatSessionType(sessionResource))?.marketplaceManagementProvider;\n\t\t\t\treturn marketplaceManagementProvider\n\t\t\t\t\t? this.computePluginMarketplaceMigration(sessionResource, marketplaceManagementProvider)\n\t\t\t\t\t: { type, candidates: [] };\n\t\t\t}\n', '');
  const marketplaceMigrationCall = '\t\t\tthis.computeMigration(sessionResource, CustomizationMigrationType.PluginMarketplaces, token),\n';
  const marketplaceMigrationCallCount = source.split(marketplaceMigrationCall).length - 1;
  if (marketplaceMigrationCallCount !== 2) throw new Error(`旧插件市场迁移调用数量错误：${marketplaceMigrationCallCount}`);
  source = source.replaceAll(marketplaceMigrationCall, '');
  source = replaceOnce(source,
    '\t\tif (type === CustomizationMigrationType.PluginMarketplaces) {\n\t\t\treturn { type, candidates: [] };\n\t\t}\n', '');
  source = replaceOnce(source,
    '\t\tconst [userDataMigration, promptFilesMigration, configuredLocationsMigration, mcpServerMigration, pluginMarketplacesMigration, chatParticipantsMigration, toolSetsMigration] = await Promise.all([',
    '\t\tconst [userDataMigration, promptFilesMigration, configuredLocationsMigration, mcpServerMigration, chatParticipantsMigration, toolSetsMigration] = await Promise.all([');
  source = replaceOnce(source,
    '\t\tconst pluginMarketplaceCandidates = pluginMarketplacesMigration.candidates.filter(candidate => !isIgnored(CustomizationMigrationType.PluginMarketplaces, candidate.storage));\n', '');
  source = replaceOnce(source, '\t\t\t+ pluginMarketplaceCandidates.length\n', '');
  source = replaceOnce(source, '\t\t\t\t{ type: CustomizationMigrationType.PluginMarketplaces, count: pluginMarketplaceCandidates.length },\n', '');
  return pruneUnusedImportsAndConstants(path, source);
}

function removePluginMarketplaceMigrationContract(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const removedNames = new Set([
    'IPluginMarketplaceCustomizationMigrationCandidate', 'PluginMarketplaceCustomizationMigration',
    'PluginMarketplaceCustomizationMigrationFailureReason', 'IPluginMarketplaceCustomizationMigrationFailure',
    'IPluginMarketplaceCustomizationMigrationResult', 'isPluginMarketplaceCustomizationMigrationCandidate',
  ]);
  const declarations = file.statements.filter(statement => {
    const name = statement.name?.text;
    return name !== undefined && removedNames.has(name);
  });
  for (const declaration of declarations.sort((left, right) => right.getStart(file) - left.getStart(file))) {
    source = source.slice(0, declaration.getFullStart()) + source.slice(declaration.end);
  }
  source = source.replace("\tPluginMarketplaces = 'pluginMarketplaces',\n", '');
  source = source.replace('export type CustomizationMigrationFailureReason = FileCustomizationMigrationFailureReason | McpServerCustomizationMigrationFailureReason | PluginMarketplaceCustomizationMigrationFailureReason;', 'export type CustomizationMigrationFailureReason = FileCustomizationMigrationFailureReason | McpServerCustomizationMigrationFailureReason;');
  source = source.replace('export type CustomizationMigrationCandidate = MigratableConfiguration | IMcpServerCustomizationMigrationCandidate | IPluginMarketplaceCustomizationMigrationCandidate;', 'export type CustomizationMigrationCandidate = MigratableConfiguration | IMcpServerCustomizationMigrationCandidate;');
  source = source.replace('\treturn !isMcpServerCustomizationMigrationCandidate(candidate) && !isPluginMarketplaceCustomizationMigrationCandidate(candidate);', '\treturn !isMcpServerCustomizationMigrationCandidate(candidate);');
  source = source.replace('export type CustomizationMigration = FileCustomizationMigration | McpServerCustomizationMigration | PluginMarketplaceCustomizationMigration | GuidanceCustomizationMigration;', 'export type CustomizationMigration = FileCustomizationMigration | McpServerCustomizationMigration | GuidanceCustomizationMigration;');
  const transformed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const service = transformed.statements.find(statement => ts.isInterfaceDeclaration(statement) && statement.name.text === 'ICustomizationMigrationService');
  if (!service) throw new Error('缺少 ICustomizationMigrationService');
  const removedMembers = new Set(['onDidChangePluginMarketplaces', 'migratePluginMarketplaces']);
  const memberEdits = service.members.filter(member => removedMembers.has(member.name?.getText(transformed) ?? ''))
    .map(member => ({ ...removeListElement(transformed, member), text: '' }));
  for (const edit of memberEdits.sort((left, right) => right.start - left.start)) source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  source = source.replace('\tcomputeMigration(sessionResource: URI, type: CustomizationMigrationType.PluginMarketplaces, token?: CancellationToken): Promise<PluginMarketplaceCustomizationMigration>;\n', '');
  return pruneUnusedImportsAndConstants(path, source);
}

function isLegacyPluginMigration(node, file) {
  const text = node.getText(file);
  return text.includes('PluginMarketplaces') || text.includes('pluginMarketplaceMigration')
    || text.includes('selectedPluginMarketplaceMigrationItems') || text.includes('knownPluginMarketplaceMigrationItems')
    || text.includes('isPluginMarketplaceCustomizationMigrationCandidate');
}

function stripLegacyPluginMigrationTerm(node, file) {
  if (ts.isParenthesizedExpression(node)) {
    const inner = stripLegacyPluginMigrationTerm(node.expression, file);
    return inner ? `(${inner})` : undefined;
  }
  if (ts.isPrefixUnaryExpression(node)) {
    const inner = stripLegacyPluginMigrationTerm(node.operand, file);
    return inner ? `${ts.tokenToString(node.operator)}${inner}` : undefined;
  }
  if (ts.isBinaryExpression(node) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind)) {
    const left = stripLegacyPluginMigrationTerm(node.left, file);
    const right = stripLegacyPluginMigrationTerm(node.right, file);
    if (!left) return right;
    if (!right) return left;
    return `${left} ${node.operatorToken.getText(file)} ${right}`;
  }
  if (isLegacyPluginMigration(node, file)) return undefined;
  return node.getText(file);
}

function removePluginMarketplaceEditorMigration(path, source) {
  source = source.replace(/\n\t\tpluginMarketplaceMigrationDiscoveryError: Error \| undefined = this\.pluginMarketplaceMigrationDiscoveryError,?/, '');
  source = source.replace(/\n\t\t\t\tpluginMarketplaceMigrationDiscoveryError\?\.type === CustomizationMigrationType\.PluginMarketplaces\n\t\t\t\t\t\? pluginMarketplaceMigrationDiscoveryError\.discoveryError\n\t\t\t\t\t: undefined,?/, '');
  const initial = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const editor = initial.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'AICustomizationManagementEditor');
  if (!editor) throw new Error('缺少 AICustomizationManagementEditor');
  for (const name of [
    'selectedPluginMarketplaceMigrationItems', 'knownPluginMarketplaceMigrationItems',
    'pluginMarketplaceMigrationDiscoveryError', 'migrateSelectedPluginMarketplaces',
  ]) {
    source = removeClassMembersByNames(path, source, 'AICustomizationManagementEditor', [name]);
  }

  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const addEdit = (node, text) => edits.push({ start: node.getStart(file), end: node.end, text });
  const visit = node => {
    if (ts.isCaseClause(node) && isLegacyPluginMigration(node.expression, file)) {
      const range = removeListElement(file, node);
      edits.push({ ...range, text: '' });
      return;
    }
    if (ts.isIfStatement(node)) {
      const expression = stripLegacyPluginMigrationTerm(node.expression, file);
      if (!expression) {
        addEdit(node, node.elseStatement?.getText(file) ?? '');
        return;
      }
      if (expression !== node.expression.getText(file)) addEdit(node.expression, expression);
    }
    if (ts.isConditionalExpression(node)) {
      const condition = stripLegacyPluginMigrationTerm(node.condition, file);
      if (!condition) {
        addEdit(node, node.whenFalse.getText(file));
        return;
      }
      if (condition !== node.condition.getText(file)) addEdit(node.condition, condition);
    }
    if (ts.isBinaryExpression(node)
      && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.operatorToken.kind)
      && !(ts.isBinaryExpression(node.parent) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken].includes(node.parent.operatorToken.kind))
      && !(ts.isIfStatement(node.parent) && node.parent.expression === node)
      && !(ts.isConditionalExpression(node.parent) && node.parent.condition === node)) {
      const expression = stripLegacyPluginMigrationTerm(node, file);
      if (!expression) {
        addEdit(node, 'true');
      } else if (expression !== node.getText(file)) {
        addEdit(node, expression);
      }
    }
    if (ts.isPropertyAssignment(node) && node.name.getText(file) === 'categoryErrors' && isLegacyPluginMigration(node.initializer, file)) {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isExpressionStatement(node) && isLegacyPluginMigration(node.expression, file)) {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isVariableDeclaration(node) && node.name.getText(file).includes('pluginMarketplaceMigration')) {
      const statement = node.parent;
      if (statement.declarations.length === 1) edits.push({ ...removeListElement(file, statement), text: '' });
      else edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)
      && ['filter', 'some'].includes(node.expression.name.text)
      && node.arguments.length === 1 && (ts.isArrowFunction(node.arguments[0]) || ts.isFunctionExpression(node.arguments[0]))) {
      const callback = node.arguments[0];
      const condition = stripLegacyPluginMigrationTerm(callback.body, file);
      if (condition === undefined && isLegacyPluginMigration(callback.body, file)) {
        if (node.expression.name.text === 'filter') addEdit(node, node.expression.expression.getText(file));
        else addEdit(callback, '() => true');
      } else if (condition !== undefined && condition !== callback.body.getText(file)) {
        addEdit(callback.body, condition);
      }
      return;
    }
    if (ts.isArrayLiteralExpression(node)) {
      for (const element of node.elements) {
        const item = element.getText(file).trim();
        if (item === 'CustomizationMigrationType.PluginMarketplaces' || item === 'CustomizationMigrationCategoryId.PluginMarketplaces' || item === 'pluginMarketplacesMigrationCategory') {
          edits.push({ ...removeListElement(file, element), text: '' });
        }
        else visit(element);
      }
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  source = applyNonOverlappingEdits(source, edits);
  return pruneUnusedImportsAndConstants(path, source);
}

function removePluginMarketplaceMigrationCategory(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const visit = node => {
    if (ts.isEnumMember(node) && node.name.getText(file) === 'PluginMarketplaces') {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isVariableDeclaration(node) && node.name.getText(file) === 'pluginMarketplacesMigrationCategory') {
      edits.push({ ...removeListElement(file, node.parent), text: '' });
      return;
    }
    if (ts.isArrayLiteralExpression(node)) {
      for (const element of node.elements) {
        if (element.getText(file).includes('PluginMarketplaces') || element.getText(file).includes('pluginMarketplacesMigrationCategory')) {
          edits.push({ ...removeListElement(file, element), text: '' });
        }
      }
    }
    if (ts.isIfStatement(node) && node.expression.getText(file) === 'isMcpServerCustomizationMigrationCandidate(customization) || isPluginMarketplaceCustomizationMigrationCandidate(customization)') {
      edits.push({ start: node.expression.getStart(file), end: node.expression.end, text: 'isMcpServerCustomizationMigrationCandidate(customization)' });
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
      && node.right.getText(file) === '!isPluginMarketplaceCustomizationMigrationCandidate(customization)') {
      edits.push({ start: node.getStart(file), end: node.end, text: node.left.getText(file) });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  source = applyNonOverlappingEdits(source, edits);
  source = source.replace("import { CustomizationMigrationCandidate, CustomizationMigrationType, IMcpServerCustomizationMigrationCandidate, IMcpServerCustomizationMigrationExclusion, IMcpServerCustomizationMigrationFailure, isConfiguredLocationMigrationCandidate, isMcpServerCustomizationMigrationCandidate, isPluginMarketplaceCustomizationMigrationCandidate, isPromptFileMigrationCandidate, isUserDataMigrationCandidate, McpServerCustomizationMigrationFailureReason, mcpServerCustomizationMigrationRemovableProperties, MigratableConfiguration } from '../../common/promptSyntax/service/customizationMigrationService.js';\n", "import { CustomizationMigrationCandidate, CustomizationMigrationType, IMcpServerCustomizationMigrationCandidate, IMcpServerCustomizationMigrationExclusion, IMcpServerCustomizationMigrationFailure, isConfiguredLocationMigrationCandidate, isMcpServerCustomizationMigrationCandidate, isPromptFileMigrationCandidate, isUserDataMigrationCandidate, McpServerCustomizationMigrationFailureReason, mcpServerCustomizationMigrationRemovableProperties, MigratableConfiguration } from '../../common/promptSyntax/service/customizationMigrationService.js';\n");
  return pruneUnusedImportsAndConstants(path, source);
}

function removePluginMarketplaceMigrationDashboard(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const visit = node => {
    if (ts.isCaseClause(node) && node.expression.getText(file).includes('PluginMarketplaces')) {
      edits.push({ ...removeListElement(file, node), text: '' });
      return;
    }
    if (ts.isArrayLiteralExpression(node)) {
      for (const element of node.elements) {
        if (element.getText(file).includes('PluginMarketplaces')) edits.push({ ...removeListElement(file, element), text: '' });
      }
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
      && node.right.getText(file) === 'category.id !== CustomizationMigrationCategoryId.PluginMarketplaces') {
      edits.push({ start: node.getStart(file), end: node.end, text: node.left.getText(file) });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  source = applyNonOverlappingEdits(source, edits);
  return pruneUnusedImportsAndConstants(path, source);
}

function removeClassMembersByNames(path, source, className, names) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const target = file.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === className);
  if (!target) throw new Error(`缺少 ${className}`);
  const selected = new Set(names);
  const edits = target.members.filter(member => selected.has(member.name?.getText(file) ?? ''))
    .map(member => ({ start: member.getFullStart(), end: member.end }));
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + source.slice(edit.end);
  }
  return source;
}

function removeConstructorParameter(path, source, className, parameterName) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const target = file.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === className);
  const constructor = target?.members.find(ts.isConstructorDeclaration);
  const parameter = constructor?.parameters.find(item => item.name.getText(file) === parameterName);
  if (!parameter || !constructor) throw new Error(`缺少 ${className}.${parameterName}`);
  const start = source.lastIndexOf('\n', parameter.getFullStart()) + 1;
  const end = source.indexOf('\n', parameter.end) + 1;
  if (start < 0 || end === 0 || !source.slice(start, end).includes(parameterName)) {
    throw new Error(`无法定位 ${className}.${parameterName} 构造参数行`);
  }
  return source.slice(0, start) + source.slice(end);
}

function removeClassPluginContextActions(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const classNode = file.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === 'McpListWidget');
  if (!classNode) throw new Error('缺少 McpListWidget');
  const method = classNode.members.find(member => ts.isMethodDeclaration(member) && member.name.getText(file) === 'getMcpServerManagementActions');
  if (!method) throw new Error('缺少 getMcpServerManagementActions');
  let action;
  const visit = node => {
    if (ts.isIfStatement(node) && node.expression.getText(file) === 'plugin'
      && node.getText(file).includes("'mcpServer.showPlugin'") && node.getText(file).includes("'mcpServer.uninstallPlugin'")) {
      action = node;
      return;
    }
    if (!action) ts.forEachChild(node, visit);
  };
  if (method.body) visit(method.body);
  if (!action) throw new Error('缺少旧 Plugin MCP 管理动作');
  source = source.slice(0, action.getFullStart()) + source.slice(action.end);
  return source;
}

function removeTopLevelFunction(path, source, name) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const fn = file.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === name);
  if (!fn) throw new Error(`缺少旧插件函数 ${name}`);
  return source.slice(0, fn.getFullStart()) + source.slice(fn.end);
}

/** 停用独立的旧 Agent Plugin 浏览视图入口。 */
export function transformLegacyCustomizationPlugins(root, path, source, replaceOnce) {
  const upstream = join(root, 'upstream');
  const relative = path.startsWith(upstream) ? path.slice(upstream.length + 1).replaceAll('\\', '/') : undefined;
  if (!relative || !legacyCustomizationPluginSourcePaths.includes(relative)) return source;

  switch (relative) {
    case 'src/vs/workbench/contrib/chat/browser/actions/chatPluginActions.ts':
      return removeLegacyPluginMarketplaceActions(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationWorkspaceService.ts':
      return replaceOnce(source, '\t\tAICustomizationManagementSection.Plugins,\n', '');
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagementEditor.ts':
      return removePluginMarketplaceEditorMigration(path, removeEditorPluginDetail(path, removePluginOnlyBranches(path, source)));
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationManagement.contribution.ts':
      return removePluginManagementActions(path, source, replaceOnce);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts':
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/toolsListWidget.ts':
      return removeMcpPluginDetailActions(relative, path, source, replaceOnce);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginCustomizationMarketplaceProvider.ts':
      return 'export {}\n';
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceWorkbenchService.ts':
      return pruneUnusedImportsAndConstants(path, removeConstructorParameter(path, removeLegacyPluginMarketplaceBrowser(path, source, replaceOnce), 'CustomizationMarketplaceWorkbenchService', 'instantiationService'));
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/aiCustomizationDiscoveryPage.ts':
      return removeLegacyPluginMarketplaceDiscovery(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/pluginListWidget.ts':
      return removeLegacyPluginMarketplaceListActions(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMarketplaceInstallService.ts':
      return removeConfiguredPluginInstallFlow(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationServiceImpl.ts':
      return removePluginMarketplaceMigration(path, source, replaceOnce);
    case 'src/vs/workbench/contrib/chat/common/promptSyntax/service/customizationMigrationService.ts':
      return removePluginMarketplaceMigrationContract(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigration.ts': {
      const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
      const edits = [];
      const visit = node => {
        if (ts.isIfStatement(node) && node.expression.getText(file) === 'isPluginMarketplaceCustomizationMigrationCandidate(customization)') {
          edits.push({ start: node.getFullStart(), end: node.end, text: '' });
          return;
        }
        ts.forEachChild(node, visit);
      };
      visit(file);
      return pruneUnusedImportsAndConstants(path, applyNonOverlappingEdits(source, edits));
    }
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationCategories.ts':
      return removePluginMarketplaceMigrationCategory(path, source);
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationAccessibility.ts':
      return source.replace(/\n\s*localize\('migrationHelpPluginMarketplaces',[\s\S]*?\),/, '');
    case 'src/vs/workbench/contrib/chat/browser/aiCustomization/customizationMigrationDashboard.ts':
      return removePluginMarketplaceMigrationDashboard(path, source);
    case 'src/vs/workbench/contrib/chat/browser/agentPluginsView.ts':
      return 'export {};\n';
    default:
      return source;
  }
}


function removeLegacyPluginMarketplaceListActions(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const edits = [];
  const visit = node => {
    if (ts.isMethodDeclaration(node) && node.name.getText(file) === 'showMarketplaceActions') {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
      return;
    }
    if (ts.isPropertyDeclaration(node) && node.name.getText(file) === 'marketplaceActionsButton') {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
    }
    if (ts.isVariableStatement(node) && node.declarationList.declarations.some(declaration => ts.isIdentifier(declaration.name) && declaration.name.text === 'moreLabel')) {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
    }
    if (ts.isExpressionStatement(node) && node.getText(file).includes('marketplaceActionsButton')) {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
      return;
    }
    if (ts.isImportDeclaration(node) && node.moduleSpecifier.getText(file) === "'../actions/chatPluginActions.js'") {
      edits.push({ start: node.getFullStart(), end: node.end, text: '' });
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    source = source.slice(0, edit.start) + edit.text + source.slice(edit.end);
  }
  source = source.replace(
    "this.commandService.executeCommand<boolean>('workbench.action.chat.installPluginFromSource', { skipReveal: true })",
    "this.commandService.executeCommand<boolean>('workbench.action.chat.installPluginFromSource')",
  );
  return pruneUnusedImportsAndConstants(path, source);
}

function removeLegacyPluginMarketplaceActions(path, source) {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const removedNames = new Set(['ManagePluginsAction', 'AddPluginMarketplaceAction', 'ManagePluginMarketplacesAction', 'IMarketplaceQuickPickItem', 'IProviderMarketplaceQuickPickItem', 'IInstallFromSourceActionOptions']);
  const edits = file.statements.filter(statement =>
    (ts.isClassDeclaration(statement) || ts.isInterfaceDeclaration(statement)) && removedNames.has(statement.name?.text ?? '')
    || ts.isVariableStatement(statement) && statement.getText(file).includes('MANAGE_PLUGIN_MARKETPLACES_COMMAND_ID')
  ).map(statement => ({ start: statement.getFullStart(), end: statement.end }));
  for (const edit of edits.sort((left, right) => right.start - left.start)) source = source.slice(0, edit.start) + source.slice(edit.end);

  const transformed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const action = transformed.statements.find(statement => ts.isClassDeclaration(statement) && statement.name?.text === 'InstallFromSourceAction');
  if (!action) throw new Error('缺少 provider 插件来源安装动作');
  const run = action.members.find(member => ts.isMethodDeclaration(member) && member.name.getText(transformed) === 'run');
  if (!run?.body) throw new Error('缺少 InstallFromSourceAction.run');
  const replacements = [
    ['async run(accessor: ServicesAccessor, options?: IInstallFromSourceActionOptions): Promise<boolean> {', 'async run(accessor: ServicesAccessor): Promise<boolean> {'],
    ['const pluginInstallService = accessor.get(IPluginInstallService);', ''],
    ['const extensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);', ''],
    ['const validationError = pluginInstallProvider ? undefined : pluginInstallService.validatePluginSource(source);\n\t\t\tif (validationError) {\n\t\t\t\tinputBox.validationMessage = validationError;\n\t\t\t\treturn;\n\t\t\t}', 'if (!pluginInstallProvider) {\n\t\t\t\tinputBox.validationMessage = localize(\'pluginSourceUnavailable\', "The selected agent does not support installing plugins from source.");\n\t\t\t\treturn;\n\t\t\t}'],
    ['let success: boolean;\n\t\t\t\tif (pluginInstallProvider) {\n\t\t\t\t\tawait pluginInstallProvider.installFromSource(sessionResource, source);\n\t\t\t\t\tsuccess = true;\n\t\t\t\t} else {\n\t\t\t\t\tconst result = await pluginInstallService.installPluginFromSource(source);\n\t\t\t\t\tsuccess = result.success;\n\t\t\t\t\tif (!success && result.message) {\n\t\t\t\t\t\tinputBox.validationMessage = result.message;\n\t\t\t\t\t}\n\t\t\t\t}', 'await pluginInstallProvider.installFromSource(sessionResource, source);\n\t\t\t\tinstalled = true;'],
    ['if (!success) {\n\t\t\t\t\tinputBox.show();\n\t\t\t\t} else {\n\t\t\t\t\tinstalled = true;\n\t\t\t\t\tif (!options?.skipReveal) {\n\t\t\t\t\t\tconst ref = parseMarketplaceReference(source);\n\t\t\t\t\t\tif (ref) {\n\t\t\t\t\t\t\textensionsWorkbenchService.openSearch(`@agentPlugins ${ref.displayLabel}`);\n\t\t\t\t\t\t}\n\t\t\t\t\t}\n\t\t\t\t\tstore.dispose();\n\t\t\t\t}', 'store.dispose();'],
  ];
  const runStart = run.getStart(transformed);
  let runText = source.slice(runStart, run.end);
  for (const [before, after] of replacements) {
    if (!runText.includes(before)) throw new Error(`provider插件安装转换锚点缺失：${before.slice(0, 80)}`);
    runText = runText.replace(before, after);
  }
  source = source.slice(0, runStart) + runText + source.slice(run.end);
  const actionPrecondition = 'precondition: ChatContextKeys.enabled,';
  if (source.split(actionPrecondition).length !== 2) throw new Error('InstallFromSourceAction precondition锚点数量错误');
  source = source.replace(actionPrecondition, 'precondition: ContextKeyExpr.and(ChatContextKeys.enabled, PluginInstallProviderAvailableContext),');
  const menuAnchor = `ContextKeyExpr.equals('view', InstalledAgentPluginsViewId),
					ChatContextKeys.Setup.hidden.negate(),`;
  if (source.split(menuAnchor).length !== 2) throw new Error('InstallFromSourceAction菜单锚点数量错误');
  source = source.replace(menuAnchor, `ContextKeyExpr.equals('view', InstalledAgentPluginsViewId),
					PluginInstallProviderAvailableContext,
					ChatContextKeys.Setup.hidden.negate(),`);
  const harnessServiceAnchor = `const customizationHarnessService = accessor.get(ICustomizationHarnessService);

		const store = new DisposableStore();`;
  if (source.split(harnessServiceAnchor).length !== 2) throw new Error('InstallFromSourceAction harness锚点数量错误');
  source = source.replace(harnessServiceAnchor, `const customizationHarnessService = accessor.get(ICustomizationHarnessService);
		if (!customizationHarnessService.getDescriptorForSession(customizationHarnessService.activeSessionResource.get())?.pluginInstallProvider) {
			accessor.get(INotificationService).info(localize('pluginSourceUnavailable', "The selected agent does not support installing plugins from source."));
			return false;
		}

		const store = new DisposableStore();`);

  source = source.replace("import { DisposableStore, toDisposable } from '../../../../../base/common/lifecycle.js';", "import { Disposable, DisposableStore, toDisposable } from '../../../../../base/common/lifecycle.js';");
  source = source.replace("import { ContextKeyExpr } from '../../../../../platform/contextkey/common/contextkey.js';", "import { ContextKeyExpr, IContextKeyService, RawContextKey } from '../../../../../platform/contextkey/common/contextkey.js';");
  source = source.replace("import { ServicesAccessor } from '../../../../../platform/instantiation/common/instantiation.js';", "import { ServicesAccessor } from '../../../../../platform/instantiation/common/instantiation.js';\nimport { IWorkbenchContribution, WorkbenchPhase, registerWorkbenchContribution2 } from '../../../../common/contributions.js';");
  const context = `const PluginInstallProviderAvailableContext = new RawContextKey<boolean>('chat.pluginInstallProviderAvailable', false);\n\nexport class PluginInstallProviderAvailabilityContribution extends Disposable implements IWorkbenchContribution {\n\tstatic readonly ID = 'workbench.contrib.chatPluginInstallProviderAvailability';\n\n\tconstructor(\n\t\t@ICustomizationHarnessService private readonly customizationHarnessService: ICustomizationHarnessService,\n\t\t@IContextKeyService contextKeyService: IContextKeyService,\n\t) {\n\t\tsuper();\n\t\tconst available = PluginInstallProviderAvailableContext.bindTo(contextKeyService);\n\t\tthis._register(autorun(reader => {\n\t\t\tthis.customizationHarnessService.availableHarnesses.read(reader);\n\t\t\tconst sessionResource = this.customizationHarnessService.activeSessionResource.read(reader);\n\t\t\tavailable.set(this.customizationHarnessService.getDescriptorForSession(sessionResource)?.pluginInstallProvider !== undefined);\n\t\t}));\n\t}\n}\n\n`;
  source = source.replace('export class InstallFromSourceAction extends Action2 {', context + 'export class InstallFromSourceAction extends Action2 {');
  const registration = 'export function registerChatPluginActions() {\n\tregisterAction2(ManagePluginsAction);\n\tregisterAction2(InstallFromSourceAction);\n\tregisterAction2(AddPluginMarketplaceAction);\n\tregisterAction2(ManagePluginMarketplacesAction);\n}';
  if (!source.includes(registration)) throw new Error('缺少旧插件动作注册函数');
  source = source.replace(registration, 'export function registerChatPluginActions() {\n\tregisterWorkbenchContribution2(PluginInstallProviderAvailabilityContribution.ID, PluginInstallProviderAvailabilityContribution, WorkbenchPhase.BlockRestore);\n\tregisterAction2(InstallFromSourceAction);\n}');
  return pruneUnusedImportsAndConstants(path, source);
}
