import { join } from 'node:path';
import ts from 'typescript';

export const legacyExtensionDetailsSourcePaths = [
  'src/vs/workbench/api/browser/mainThreadProgress.ts',
  'src/vs/workbench/api/browser/mainThreadMessageService.ts',
  'src/vs/workbench/browser/parts/statusbar/statusbarActions.ts',
  'src/vs/workbench/browser/parts/statusbar/statusbarPart.ts',
  'src/vs/workbench/contrib/issue/browser/issueQuickAccess.ts',
  'src/vs/workbench/contrib/preferences/browser/keybindingsEditor.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsTree.ts',
  'src/vs/workbench/contrib/chat/browser/chatDebug/chatCustomizationDiscoveryRenderer.ts',
  'src/vs/workbench/contrib/userDataProfile/browser/userDataProfilesEditorModel.ts',
  'src/vs/workbench/services/userDataProfile/browser/extensionsResource.ts',
  'src/vs/workbench/contrib/authentication/browser/actions/manageTrustedExtensionsForAccountAction.ts',
  'src/vs/workbench/contrib/chat/browser/actions/chatLanguageModelActions.ts',
  'src/vs/workbench/contrib/chat/browser/aiCustomization/mcpListWidget.ts',
  'src/vs/workbench/contrib/chat/browser/actions/chatToolPicker.ts',
  'src/vs/workbench/contrib/localization/common/localizationsActions.ts',
];

export function transformLegacyExtensionDetails(root, path, source, replaceOnce) {
  const upstream = join(root, 'upstream');
  const wb = join(upstream, 'src/vs/workbench');
  const edit = (file, from, to) => path === join(wb, file) ? to(source, replaceOnce) : undefined;

  let result = edit('api/browser/mainThreadProgress.ts', null, (text, replace) => {
    text = replace(text, "import { ICommandService } from '../../../platform/commands/common/commands.js';\n", '');
    text = replace(text, '\t\t@ICommandService private readonly _commandService: ICommandService\n', '');
    text = replace(text, `\t\t\t\tsecondaryActions: [withNotificationActionTelemetry(toAction({
\t\t\t\t\tid: extensionId,
\t\t\t\t\tlabel: localize('manageExtension', "Manage Extension"),
\t\t\t\t\trun: () => this._commandService.executeCommand('_extensions.manage', extensionId)
\t\t\t\t}), NotificationActionTelemetryId.ManageExtension)]
`, '');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('api/browser/mainThreadMessageService.ts', null, (text, replace) => {
    text = replace(text, "import { ICommandService } from '../../../platform/commands/common/commands.js';\n", '');
    text = replace(text, '\t\t@ICommandService private readonly _commandService: ICommandService,\n', '');
    text = replace(text, '\t\t\tconst secondaryActions: IAction[] = [];\n', '');
    text = replace(text, `\t\t\tif (options.source) {
\t\t\t\tsecondaryActions.push(withNotificationActionTelemetry(toAction({
\t\t\t\t\tid: options.source.identifier.value,
\t\t\t\t\tlabel: nls.localize('manageExtension', "Manage Extension"),
\t\t\t\t\trun: () => {
\t\t\t\t\t\treturn this._commandService.executeCommand('_extensions.manage', options.source!.identifier.value);
\t\t\t\t\t}
\t\t\t\t}), NotificationActionTelemetryId.ManageExtension));
\t\t\t}

`, '');
    text = replace(text, 'actions: { primary: primaryActions, secondary: secondaryActions },', 'actions: { primary: primaryActions },');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('browser/parts/statusbar/statusbarActions.ts', null, text => pruneImports(path, removeClass(path, text, 'ManageExtensionAction')));
  if (result !== undefined) return result;

  result = edit('browser/parts/statusbar/statusbarPart.ts', null, (text, replace) => {
    text = replace(text,
      "import { HideStatusbarEntryAction, ManageExtensionAction, ToggleStatusbarEntryVisibilityAction } from './statusbarActions.js';",
      "import { HideStatusbarEntryAction, ToggleStatusbarEntryVisibilityAction } from './statusbarActions.js';");
    return replace(text, `\t\t\tif (statusEntryUnderMouse.extensionId) {\n\t\t\t\tactions.push(this.instantiationService.createInstance(ManageExtensionAction, statusEntryUnderMouse.extensionId));\n\t\t\t}\n`, '');
  });
  if (result !== undefined) return result;

  result = edit('contrib/issue/browser/issueQuickAccess.ts', null, (text, replace) => {
    text = replace(text, `\t\tconst buttons = [{
\t\t\ticonClass: ThemeIcon.asClassName(Codicon.info),
\t\t\ttooltip: localize('contributedIssuePage', "Open Extension Page")
\t\t}];

`, '');
    text = replace(text, '\t\tlet trigger: () => TriggerAction;\n', '');
    text = replace(text, `\t\t\ttrigger = () => {
\t\t\t\tif ('source' in action.item && action.item.source) {
\t\t\t\t\tthis.commandService.executeCommand('extension.open', action.item.source.id);
\t\t\t\t}
\t\t\t\treturn TriggerAction.CLOSE_PICKER;
\t\t\t};
`, '');
    text = replace(text, `\t\t\ttrigger = () => {
\t\t\t\tthis.commandService.executeCommand('extension.open', extension.identifier.value);
\t\t\t\treturn TriggerAction.CLOSE_PICKER;
\t\t\t};
`, '');
    text = replace(text, '\t\t\t\tbuttons,\n\t\t\t\ttrigger,\n', '');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('contrib/preferences/browser/keybindingsEditor.ts', null, (text, replace) => {
    text = replace(text, '\textensionLabel: HTMLAnchorElement;\n', '\textensionLabel: HTMLElement;\n');
    const parsed = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
    const helper = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'onClick');
    if (!helper) throw new Error('快捷键扩展来源点击辅助函数缺失');
    text = text.slice(0, helper.getFullStart()) + text.slice(helper.end);
    text = replace(text, '\t\t@IExtensionsWorkbenchService private readonly extensionsWorkbenchService: IExtensionsWorkbenchService,\n', '');
    text = replace(text, '\tdisposables: DisposableStore;\n', '');
    text = replace(text, '\t\treturn { sourceColumn, sourceColumnHover, sourceLabel, extensionLabel, extensionContainer, extensionId, disposables: new DisposableStore() };', '\t\treturn { sourceColumn, sourceColumnHover, sourceLabel, extensionLabel, extensionContainer, extensionId };');
    const rendererStart = text.indexOf('class SourceColumnRenderer implements');
    const rendererEnd = text.indexOf('\nclass WhenInputWidget', rendererStart);
    if (rendererStart < 0 || rendererEnd < 0) throw new Error('快捷键来源渲染器结构缺失');
    let renderer = text.slice(rendererStart, rendererEnd);
    renderer = replace(renderer, '\ttemplateData.disposables.clear();\n', '');
    renderer = replace(renderer, '\ttemplateData.disposables.dispose();\n', '');
    text = text.slice(0, rendererStart) + renderer + text.slice(rendererEnd);
    text = replace(text, "const extensionLabel = DOM.append<HTMLAnchorElement>(extensionContainer, $('a.extension-label', { tabindex: 0 }));", "const extensionLabel = DOM.append(extensionContainer, $('span.extension-label'));");
    text = replace(text, `\t\t\ttemplateData.disposables.add(onClick(templateData.extensionLabel, () => {
\t\t\t\tthis.extensionsWorkbenchService.open(extension.identifier.value);
\t\t\t}));
`, '');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('contrib/preferences/browser/settingsTree.ts', null, (text, replace) => {
    text = replace(text, '\tactionButton: Button;\n', '');
    text = replace(text, `\t\tconst actionButton = new Button(common.containerElement, {
\t\t\ttitle: false,
\t\t\t...defaultButtonStyles
\t\t});
\t\tactionButton.element.classList.add('setting-item-extension-toggle-button');
\t\tactionButton.label = localize('showExtension', "Show Extension");

`, '');
    text = replace(text, '\t\t\tactionButton,\n', '');
    text = replace(text, `\t\ttemplate.elementDisposables.add(template.actionButton.onDidClick(async () => {
\t\t\tthis._telemetryService.publicLog2<{ extensionId: String }, ManageExtensionClickTelemetryClassification>('ManageExtensionClick', { extensionId });
\t\t\tthis._commandService.executeCommand('extension.open', extensionId);
\t\t}));

`, '');
    return text;
  });
  if (result !== undefined) return result;

  result = edit('contrib/chat/browser/chatDebug/chatCustomizationDiscoveryRenderer.ts', null, text => {
    text = replaceOnce(text, '\t\tconst firstFile = files[0];\n', '');
    const parsed = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
    const matches = [];
    const visit = node => {
      if (ts.isIfStatement(node) && node.expression.getText(parsed) === 'firstFile.extensionId') matches.push(node);
      ts.forEachChild(node, visit);
    };
    visit(parsed);
    if (matches.length !== 1) throw new Error('扩展来源标签详情链接与固定上游不匹配');
    const node = matches[0];
    return text.slice(0, node.getStart(parsed)) + "DOM.append(groupHeader, $('span.chat-debug-file-list-group-label', undefined, locationLabel));" + text.slice(node.end);
  });
  if (result !== undefined) return result;

  result = edit('contrib/userDataProfile/browser/userDataProfilesEditorModel.ts', null, (text, replace) => {
    text = replace(text, '\t\t\topenAction: toAction({\n\t\t\t\tid: \'_openChild\',', '\t\t\topenAction: child.parent.type === ProfileResourceType.Extensions ? undefined : toAction({\n\t\t\t\tid: \'_openChild\',');
    text = replace(text, `\t\t\t\t\tif (child.parent.type === ProfileResourceType.Extensions) {
\t\t\t\t\t\tawait this.commandService.executeCommand('extension.open', child.handle, undefined, true, undefined, true);
\t\t\t\t\t} else if (child.resourceUri) {
\t\t\t\t\t\tawait this.commandService.executeCommand(API_OPEN_EDITOR_COMMAND_ID, child.resourceUri, [SIDE_GROUP], undefined);
\t\t\t\t\t}
`, `\t\t\t\t\tif (child.resourceUri) {
\t\t\t\t\t\tawait this.commandService.executeCommand(API_OPEN_EDITOR_COMMAND_ID, child.resourceUri, [SIDE_GROUP], undefined);
\t\t\t\t\t}
`);
    return text;
  });
  if (result !== undefined) return result;

  if (path === join(upstream, 'src/vs/workbench/services/userDataProfile/browser/extensionsResource.ts')) {
    return replaceOnce(source, `\t\t\tthemeIcon: Codicon.extensions,
\t\t\tcommand: {
\t\t\t\tid: 'extension.open',
\t\t\t\ttitle: '',
\t\t\t\targuments: [e.identifier.id, undefined, true]
\t\t\t}
`, `\t\t\tthemeIcon: Codicon.extensions
`);
  }

  result = edit('contrib/authentication/browser/actions/manageTrustedExtensionsForAccountAction.ts', null, (text, replace) => {
    text = text.replace("import { IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';\n", '');
    text = replace(text, `\tprivate readonly _viewDetailsButton = {
\t\ttooltip: localize('viewExtensionDetails', "View extension details"),
\t\ticonClass: ThemeIcon.asClassName(Codicon.info),
\t};

`, '');
    text = replace(text, '\t\t@IExtensionsWorkbenchService private readonly _extensionsWorkbenchService: IExtensionsWorkbenchService\n', '');
    text = replace(text, 'buttons: [this._viewDetailsButton, this._managePreferencesButton],', 'buttons: [this._managePreferencesButton],');
    text = replace(text, `\t\t\t} else if (e.button === this._viewDetailsButton) {
\t\t\t\tthis._extensionsWorkbenchService.open(e.item.extension.id);
`, '');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('contrib/chat/browser/actions/chatLanguageModelActions.ts', null, (text, replace) => {
    text = text.replace("import { IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';\n", '');
    text = text.replace("import { ThemeIcon } from '../../../../../base/common/themables.js';\n", '');
    text = replace(text, '\t\tconst extensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);\n', '');
    text = text.replace(/\n\s*buttons: \[\{\n\s*iconClass: ThemeIcon\.asClassName\(Codicon\.info\),\n\s*tooltip: localize\('openExtension', 'Open Extension'\),\n\s*\}\]/g, '');
    text = replace(text, `\t\t\t\tonDidTriggerSeparatorButton(context) {
\t\t\t\t\t// Handle separator button clicks
\t\t\t\t\tconst extId = context.separator.id;
\t\t\t\t\tif (extId) {
\t\t\t\t\t\t// Open the extension in the editor
\t\t\t\t\t\tvoid extensionsWorkbenchService.open(extId);
\t\t\t\t\t}
\t\t\t\t},
\t\t\t\tonDidTriggerItemButton(context) {
\t\t\t\t\t// Handle item button clicks
\t\t\t\t\tconst extId = context.item.id;
\t\t\t\t\tif (extId) {
\t\t\t\t\t\t// Open the extension in the editor
\t\t\t\t\t\tvoid extensionsWorkbenchService.open(extId);
\t\t\t\t\t}
\t\t\t\t},
`, '');
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  result = edit('contrib/chat/browser/aiCustomization/mcpListWidget.ts', null, (text, replace) => {
    text = replace(text, "import { ExtensionEditorTab, IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';", "import { IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';");
    text = replace(text, `\t\t\t\thover: localize('openExtensionDetails', "Open extension details for {0}", extensionName),
\t\t\t\tariaLabel: localize('openExtensionDetails', "Open extension details for {0}", extensionName),
\t\t\t\topen: () => extensionsWorkbenchService.open(extensionId.value, { tab: ExtensionEditorTab.Features, feature: 'mcp' }),`, `\t\t\t\thover: localize('mcpSourceExtension', "Provided by {0}", extensionName),
\t\t\t\tariaLabel: localize('mcpSourceExtension', "Provided by {0}", extensionName),`);
    text = replace(text, `\t\tif (extensionId) {
\t\t\treturn new Action('mcpServer.showExtension', localize('showMcpServerExtension', "Show Extension"), undefined, true, async () => {
\t\t\t\tawait this.extensionsWorkbenchService.open(extensionId.value, { tab: ExtensionEditorTab.Features, feature: 'mcp' });
\t\t\t});
\t\t}
`, '');
    text = replace(text, '\t\tconst extensionId = entry.extensionId;\n', '');
    return text;
  });
  if (result !== undefined) return result;

  result = edit('contrib/chat/browser/actions/chatToolPicker.ts', null, (text, replace) => {
    text = replace(text, "import { ExtensionEditorTab, IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';", "import { IExtensionsWorkbenchService } from '../../../extensions/common/extensions.js';");
    text = replace(text, `\t\t\t\tif (collection?.source) {
\t\t\t\t\tbuttons.push({
\t\t\t\t\t\ticonClass: ThemeIcon.asClassName(Codicon.settingsGear),
\t\t\t\t\t\ttooltip: localize('configMcpCol', "Configure {0}", collection.label),
\t\t\t\t\t\taction: () => collection.source ? collection.source instanceof ExtensionIdentifier ? extensionsWorkbenchService.open(collection.source.value, { tab: ExtensionEditorTab.Features, feature: 'mcp' }) : mcpWorkbenchService.open(collection.source, { tab: McpServerEditorTab.Configuration }) : undefined
\t\t\t\t\t});
\t\t\t\t} else if (collection?.presentation?.origin) {`, `\t\t\t\tif (collection?.source && !(collection.source instanceof ExtensionIdentifier)) {
\t\t\t\t\tconst source = collection.source;
\t\t\t\t\tbuttons.push({
\t\t\t\t\t\ticonClass: ThemeIcon.asClassName(Codicon.settingsGear),
\t\t\t\t\t\ttooltip: localize('configMcpCol', "Configure {0}", collection.label),
\t\t\t\t\t\taction: () => mcpWorkbenchService.open(source, { tab: McpServerEditorTab.Configuration })
\t\t\t\t\t});
\t\t\t\t} else if (collection?.presentation?.origin) {`);
    return pruneImports(path, text);
  });
  if (result !== undefined) return result;

  return edit('contrib/localization/common/localizationsActions.ts', null, (text, replace) => {
    text = text.replace("import { IExtensionsWorkbenchService } from '../../extensions/common/extensions.js';\n", '');
    text = replace(text, '\t\tconst extensionWorkbenchService: IExtensionsWorkbenchService = accessor.get(IExtensionsWorkbenchService);\n', '');
    text = replace(text, '\t\t\tqp.items = items.concat(this.withMoreInfoButton(installedLanguages));', '\t\t\tqp.items = items.concat(installedLanguages);');
    text = replace(text, '\t\t\t\t\t...this.withMoreInfoButton(newLanguages)', '\t\t\t\t\t...newLanguages');
    text = replace(text, `\t\tdisposables.add(qp.onDidTriggerItemButton(async e => {
\t\t\tqp.hide();
\t\t\tif (e.item.extensionId) {
\t\t\t\tawait extensionWorkbenchService.open(e.item.extensionId);
\t\t\t}
\t\t}));

`, '');
    const start = text.indexOf('\n\tprivate withMoreInfoButton(');
    const end = text.indexOf('\n\t}\n}', start);
    if (start < 0 || end < 0) throw new Error('语言包详情按钮辅助方法缺失');
    return pruneImports(path, text.slice(0, start) + text.slice(end + 3));
  });
}

function removeClass(path, source, name) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const declarations = parsed.statements.filter(node => ts.isClassDeclaration(node) && node.name?.text === name);
  if (declarations.length !== 1) throw new Error('待删除的旧扩展详情类缺失：' + name);
  const declaration = declarations[0];
  return source.slice(0, declaration.getFullStart()) + source.slice(declaration.end);
}

function pruneImports(path, source) {
  const parsed = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
  const references = new Map();
  const visit = node => {
    if (ts.isImportDeclaration(node)) return;
    if (ts.isIdentifier(node)) references.set(node.text, (references.get(node.text) ?? 0) + 1);
    ts.forEachChild(node, visit);
  };
  visit(parsed);
  const edits = [];
  for (const statement of parsed.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue;
    const clause = statement.importClause;
    const defaultName = clause.name && (references.get(clause.name.text) ?? 0) > 0 ? clause.name.text : undefined;
    const bindings = clause.namedBindings;
    let bindingText;
    if (bindings && ts.isNamespaceImport(bindings)) {
      bindingText = (references.get(bindings.name.text) ?? 0) > 0 ? bindings.getText(parsed) : undefined;
    } else if (bindings && ts.isNamedImports(bindings)) {
      const names = bindings.elements.filter(item => (references.get(item.name.text) ?? 0) > 0);
      if (names.length) bindingText = '{ ' + names.map(item => item.getText(parsed)).join(', ') + ' }';
    }
    if (!defaultName && !bindingText) {
      edits.push({ start: statement.getStart(parsed), end: statement.end, text: '' });
    } else {
      const imported = [defaultName, bindingText].filter(Boolean).join(', ');
      edits.push({ start: statement.getStart(parsed), end: statement.end,
        text: 'import ' + (clause.isTypeOnly ? 'type ' : '') + imported + ' from ' + statement.moduleSpecifier.getText(parsed) + ';' });
    }
  }
  for (const change of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, change.start) + change.text + source.slice(change.end);
  return source;
}

