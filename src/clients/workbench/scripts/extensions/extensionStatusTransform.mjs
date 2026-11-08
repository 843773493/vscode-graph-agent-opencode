import { join } from 'node:path';

export const extensionStatusSourcePaths = [
  'src/vs/workbench/contrib/extensions/browser/abstractRuntimeExtensionsEditor.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsWorkbenchService.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionFeaturesTab.ts',
  'src/vs/workbench/contrib/notebook/browser/contrib/kernelDetection/notebookKernelDetection.ts',
  'src/vs/workbench/contrib/debug/browser/debugAdapterManager.ts',
  'src/vs/workbench/contrib/chat/browser/chatSetup/chatSetupRunner.ts',
  'src/vs/workbench/contrib/chat/browser/actions/chatGettingStarted.ts',
  'src/vs/workbench/api/browser/mainThreadChatAgents2.ts',
  'src/vs/workbench/api/browser/mainThreadLanguageModels.ts',
  'src/vs/workbench/api/browser/mainThreadWebviewPanels.ts',
  'src/vs/workbench/contrib/extensions/browser/extensionsActions.ts',
  'src/vs/workbench/services/userDataSync/browser/userDataSyncInit.ts',
  'src/vs/workbench/contrib/externalUriOpener/common/contributedOpeners.ts',
];

export function transformExtensionStatus(root, path, source, replaceOnce) {
  const paths = extensionStatusSourcePaths.map(value => join(root, 'upstream', value));
  const index = paths.indexOf(path);
  if (index < 0) return source;
  if (index === 10) {
    let contents = `import { targetRuntimeUnavailableReason } from ${JSON.stringify(join(root, 'src/extensions/targetContext.js'))};\n`
      + source.replace('IExtensionService, toExtension, toExtensionDescription', 'IExtensionService, toExtension');
    contents = replaceOnce(contents, 'ExtensionType, ExtensionIdentifier,', 'ExtensionType, installationTargetKey, ExtensionIdentifier,');
    contents = replaceOnce(contents, 'canAddExtension(toExtensionDescription(extension))', 'canAddExtension(extension)');
    contents = replaceOnce(contents, 'canAddExtension(toExtensionDescription(this.extension!.local))', 'canAddExtension(this.extension!.local)');
    contents = replaceOnce(contents, 'const isRunning = this.extensionService.extensions.some(e => areSameExtensions({ id: e.identifier.value, uuid: e.uuid }, this.extension!.identifier));', `const target = this.extension.local.installationTarget;
    const isRunning = this.extensionService.extensions.some(e => areSameExtensions({ id: e.identifier.value, uuid: e.uuid }, this.extension!.identifier)
      && (target ? !!e.installationTarget && installationTargetKey(e.installationTarget) === installationTargetKey(target) : e.installationTarget?.kind === 'web'));
    if (isEnabled && !isRunning) {
      const reason = targetRuntimeUnavailableReason(this.extension.local,
        this.extensionManifestPropertiesService.getExtensionKind(this.extension.local.manifest), this.extensionService.getExtensionWorkspaceViews());
      if (reason) {
        this.updateStatus({ icon: warningIcon, message: new MarkdownString().appendText(reason) }, true);
        return;
      }
    }`);
    return replaceOnce(contents, 'canRemoveExtension(toExtensionDescription(this.extension!.local))', 'canRemoveExtension(this.extension!.local)');
  }
  if (index === 11) {
    const contents = replaceOnce(source, 'IExtensionService, toExtensionDescription', 'IExtensionService');
    return replaceOnce(contents, 'canAddExtension(toExtensionDescription(e))', 'canAddExtension(e)');
  }
  if (index === 12) {
    const contents = replaceOnce(source, 'import { IExtensionService }', 'import { IExtensionService, toExtension }');
    return replaceOnce(contents, 'canRemoveExtension(extension)', 'canRemoveExtension(toExtension(extension))');
  }
  if (index === 0 || index === 3) {
    let contents = `import { extensionInstanceKey } from ${JSON.stringify(join(root, 'upstream/src/vs/platform/extensions/common/extensions.js'))};\n` + source;
    return contents.replaceAll('[extensionDescription.identifier.value]', '[extensionInstanceKey(extensionDescription)]').replaceAll('[extension.identifier.value]', '[extensionInstanceKey(extension)]');
  }
  if (index === 1) {
    const start = source.indexOf('\tgetExtensionRuntimeStatus('), end = source.indexOf('\n\tasync updateRunningExtensions', start);
    let contents = `import { targetRuntimeUnavailableReason } from ${JSON.stringify(join(root, 'src/extensions/targetContext.js'))};\n`
      + replaceOnce(replaceOnce(source, 'IExtensionManifest, ExtensionType,', 'IExtensionManifest, ExtensionType, installationTargetKey,'), source.slice(start, end), `
  getExtensionRuntimeStatus(extension: IExtension): IExtensionRuntimeStatus | undefined {
    const target = extension.local?.installationTarget;
    const candidates = Object.values(this.extensionService.getExtensionsStatus()).filter(status =>
      areSameExtensions({ id: status.id.value }, extension.identifier) && (target
        ? installationTargetKey(status.installationTarget) === installationTargetKey(target) : status.installationTarget.kind === 'web'));
    if (candidates.length > 1) throw new Error('Extension runtime status requires an explicit installation target');
    return candidates[0];
  }
`);
    contents = contents.replace('toExtension, toExtensionDescription', 'toExtension');
    contents = contents.replaceAll('canAddExtension(toExtensionDescription(extension.local))', 'canAddExtension(extension.local)');
    contents = replaceOnce(contents, '// Extension is not running\n\t\t\telse {', `// Extension is not running
\t\t\telse {
        if (isEnabled && extension.local.isValid && targetRuntimeUnavailableReason(extension.local,
          this.extensionManifestPropertiesService.getExtensionKind(extension.local.manifest), this.extensionService.getExtensionWorkspaceViews())) return undefined;`);
    return replaceOnce(contents, 'canRemoveExtension(runningExtension)', 'canRemoveExtension(toExtension(runningExtension))');
  }
  if (index === 2) {
    let contents = replaceOnce(source, 'const status = this.extensionService.getExtensionsStatus()[extensionId.value];', 'const statuses = Object.values(this.extensionService.getExtensionsStatus()).filter(status => ExtensionIdentifier.equals(status.id, extensionId));\n    const status = { runtimeErrors: statuses.flatMap(status => status.runtimeErrors), messages: statuses.flatMap(status => status.messages) };');
    contents = replaceOnce(contents, 'if (this.extensionService.extensions.some(extension => ExtensionIdentifier.equals(extension.identifier, extensionId))) {\n\t\t\tconst data', 'for (const status of statuses) {\n\t\t\tconst data');
    return replaceOnce(contents, 'data.appendMarkdown(`### ${localize(\'activation\', "Activation")}\\n\\n`);', 'data.appendMarkdown(`### ${localize(\'activation\', "Activation")} · ${status.installationTarget.kind === \'backend\' ? status.installationTarget.gatewayId + \' / \' + status.installationTarget.backendId : \'Web\'}\\n\\n`);');
  }
  if (index === 4) return replaceOnce(source, 'this.earlyActivatedExtensions.add(id);', 'this.earlyActivatedExtensions.add(status[id].id.value);');
  if (index === 5) return replaceOnce(source, 'ExtensionIdentifier.equals(id, defaultChat.chatExtensionId)', 'ExtensionIdentifier.equals(status[id].id, defaultChat.chatExtensionId) && status[id].installationTarget.kind === \'web\'');
  if (index === 6) return replaceOnce(source, 'extensionStatus[ext.value].activationTimes', 'Object.values(extensionStatus).some(status => ExtensionIdentifier.equals(status.id, ext) && status.installationTarget.kind === \'web\' && status.activationTimes)');
  let contents = index === 9
    ? replaceOnce(source, 'context: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,')
    : replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
  if (index === 9) contents = contents.replaceAll('context.getProxy(', 'this.extHostContext.getProxy(');
  if (index === 8) contents = contents.replace('@IExtensionService private readonly _extensionService: IExtensionService,', '@IExtensionService _extensionService: IExtensionService,');
  // MainThread 公开 ID 查询限定发起宿主合法集合，不以窗口级同名实例消歧。
  contents = contents.replaceAll('await this._extensionService.getExtension(extensionId.value)', 'this.extHostContext.extensionHost.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, extensionId))');
  contents = contents.replaceAll('await this._extensionService.getExtension(allowedExtension.id)', 'this.extHostContext.extensionHost.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, allowedExtension.id))');
  contents = contents.replaceAll('await this._extensionService.getExtension(oldExtension.id.value)', 'this.extHostContext.extensionHost.extensions?.allExtensions.find(extension => ExtensionIdentifier.equals(extension.identifier, oldExtension.id))');
  if (!contents.includes('import { ExtensionIdentifier }') && !/import \{[^\n]*ExtensionIdentifier[, }]/.test(contents)) {
    contents = `import { ExtensionIdentifier } from '../../../platform/extensions/common/extensions.js';\n` + contents;
  }
  return contents;
}
