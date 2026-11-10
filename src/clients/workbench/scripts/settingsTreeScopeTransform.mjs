import { join, resolve } from 'node:path';

export const settingsTreeScopeSourcePaths = [
  'src/vs/workbench/contrib/preferences/browser/preferencesWidgets.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsTreeModels.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsTree.ts',
];

/** 原目标标签和树模型共用当前输入的冻结上下文，配置数据仍由原服务读取。 */
export function transformSettingsTreeScope(root, path, contents, replaceOnce) {
  const upstream = join(root, 'upstream');
  if (path === join(upstream, settingsTreeScopeSourcePaths[0])) {
    contents = `import { ITargetSettingsContext } from ${JSON.stringify(resolve(root, 'src/settings/scope.js'))};\n` + contents;
    contents = replaceOnce(contents, 'export interface ISettingsTargetsWidgetOptions {',
      'export interface ISettingsTargetsWidgetOptions {\n\tbeforeTargetChange?: () => Promise<void>;');
    contents = replaceOnce(contents, '@ILanguageService private readonly languageService: ILanguageService\n\t) {',
      '@ILanguageService private readonly languageService: ILanguageService,\n\t\t@ITargetSettingsContext private readonly targetSettingsContext: ITargetSettingsContext\n\t) {');
    contents = replaceOnce(contents, '\tprivate async update(): Promise<void> {', '\tprivate async update(): Promise<void> {\n\t\tif (this.targetSettingsContext.target) { this.userLocalSettings.enabled = false; this.userRemoteSettings.enabled = false; this.workspaceSettings.enabled = false; this.folderSettings.action.enabled = false; return; }');
    contents = replaceOnce(contents, '\tupdateTarget(settingsTarget: SettingsTarget): Promise<void> {',
      '\tprivate targetChangeVersion = 0;\n\n\tasync updateTarget(settingsTarget: SettingsTarget): Promise<void> {\n\t\tconst version = ++this.targetChangeVersion;');
    contents = replaceOnce(contents, '\t\tif (!isSameTarget) {\n\t\t\tthis.settingsTarget = settingsTarget;', `		if (!isSameTarget) {
			if (this.options.beforeTargetChange) {
				await this.options.beforeTargetChange();
			}
			if (version !== this.targetChangeVersion || this._store.isDisposed) return;
			this.settingsTarget = settingsTarget;`);
    contents = replaceOnce(contents, '\t\treturn Promise.resolve(undefined);\n\t}\n\n\tprivate async update()',
      '\t}\n\n\tprivate async update()');
    contents = replaceOnce(contents, '\t\t\tthis.updateTarget(isWorkspaceFolder(folder) ? folder.uri : ConfigurationTarget.USER_LOCAL);',
      '\t\t\tawait this.updateTarget(isWorkspaceFolder(folder) ? folder.uri : ConfigurationTarget.USER_LOCAL);');
  }

  if (path === join(upstream, settingsTreeScopeSourcePaths[1])) {
    contents = `import { ITargetSettingsContext } from ${JSON.stringify(resolve(root, 'src/settings/scope.js'))};\n` + contents;
    contents = replaceOnce(contents, '\t\tprivate readonly managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t) {',
      '\t\tprivate readonly managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t\tprivate readonly targetSettingsScope: ITargetSettingsContext[\'target\'] = undefined,\n\t) {');
    contents = replaceOnce(contents, '\tinspectSelf() {', '\tget isSourceReadOnly(): boolean { return !!this.targetSettingsScope && !this.targetSettingsScope.document.writable; }\n\n\tinspectSelf() {');
    contents = replaceOnce(contents, 'const inspectResult = inspectSetting(this.setting.key, targetToInspect, this.languageFilter, this.configurationService);', `const source = this.targetSettingsScope;
    const inspected = source?.inspect(this.setting.key, this.languageFilter);
    const inspectResult = source ? { inspected: inspected!, isConfigured: inspected!.userRemoteValue !== undefined,
      targetSelector: 'userRemoteValue' as const, inspectedLanguageOverrides: new Map<string, IConfigurationValue<unknown>>(), languageSelector: this.languageFilter }
      : inspectSetting(this.setting.key, targetToInspect, this.languageFilter, this.configurationService);`);
    contents = replaceOnce(contents, '\t\tconst configTarget = URI.isUri(scope) ? ConfigurationTarget.WORKSPACE_FOLDER : scope;', '\t\tconst configTarget = URI.isUri(scope) ? ConfigurationTarget.WORKSPACE_FOLDER : scope;\n\t\tif (this.targetSettingsScope) return configTarget === ConfigurationTarget.USER_REMOTE;');
    contents = replaceOnce(contents, '\t\t@IManagedSettingsPresentationService private readonly _managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t) {',
      '\t\t@IManagedSettingsPresentationService private readonly _managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t\t@ITargetSettingsContext private readonly _targetSettingsContext: ITargetSettingsContext,\n\t) {');
    contents = replaceOnce(contents, '\t\t\tthis._managedSettingsPresentationService);',
      '\t\t\tthis._managedSettingsPresentationService,\n\t\t\tthis._targetSettingsContext.target);');
    contents = replaceOnce(contents, '\t\t@IManagedSettingsPresentationService managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t) {',
      '\t\t@IManagedSettingsPresentationService managedSettingsPresentationService: IManagedSettingsPresentationService,\n\t\t@ITargetSettingsContext targetSettingsContext: ITargetSettingsContext,\n\t) {');
    contents = replaceOnce(contents, 'super(viewState, isWorkspaceTrusted, configurationService, languageService, userDataProfileService, productService, environmentService, experimentalSettingsService, managedSettingsPresentationService);',
      'super(viewState, isWorkspaceTrusted, configurationService, languageService, userDataProfileService, productService, environmentService, experimentalSettingsService, managedSettingsPresentationService, targetSettingsContext);');
    contents = replaceOnce(contents, 'configurationService.inspect(key, { overrideIdentifier })',
      'configurationService.inspect(key, { ...inspectOverrides, overrideIdentifier })');
  }
  if (path === join(upstream, settingsTreeScopeSourcePaths[2])) {
    contents = contents.replaceAll('dataElement.hasPolicyValue || dataElement.isAgentsWindowReadOnly', 'dataElement.hasPolicyValue || dataElement.isAgentsWindowReadOnly || dataElement.isSourceReadOnly');
    contents = contents.replaceAll('!dataElement.hasPolicyValue && !dataElement.isAgentsWindowReadOnly', '!dataElement.hasPolicyValue && !dataElement.isAgentsWindowReadOnly && !dataElement.isSourceReadOnly');
  }
  return contents;
}
