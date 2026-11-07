import { join, resolve } from 'node:path';

export const settingsTreeScopeSourcePaths = [
  'src/vs/workbench/contrib/preferences/browser/preferencesWidgets.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsTreeModels.ts',
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
    contents = replaceOnce(contents, `	private resetLabels() {
		const remoteAuthority = this.environmentService.remoteAuthority;
		const hostLabel = remoteAuthority && this.labelService.getHostLabel(Schemas.vscodeRemote, remoteAuthority);
		this.userLocalSettings.label = localize('userSettings', "User");
		this.userRemoteSettings.label = localize('userSettingsRemote', "Remote") + (hostLabel ? \` [\${hostLabel}]\` : '');`, `	private get userLocalLabel(): string {
		return this.targetSettingsContext.target ? '当前工作台 profile' : localize('userSettings', "User");
	}

	private get userRemoteLabel(): string {
		const target = this.targetSettingsContext.target;
		if (target) {
			return '目标 profile · ' + (target.location.name || target.location.backend_id);
		}
		const remoteAuthority = this.environmentService.remoteAuthority;
		const hostLabel = remoteAuthority && this.labelService.getHostLabel(Schemas.vscodeRemote, remoteAuthority);
		return localize('userSettingsRemote', "Remote") + (hostLabel ? \` [\${hostLabel}]\` : '');
	}

	private resetLabels() {
		this.userLocalSettings.label = this.userLocalLabel;
		this.userRemoteSettings.label = this.userRemoteLabel;
		this.userLocalSettings.tooltip = this.userLocalLabel;
		this.userRemoteSettings.tooltip = this.userRemoteLabel;`);
    contents = replaceOnce(contents, '\t\tthis.userLocalSettings.tooltip = localize(\'userSettings\', "User");\n', '');
    contents = replaceOnce(contents, `		const remoteAuthority = this.environmentService.remoteAuthority;
		const hostLabel = remoteAuthority && this.labelService.getHostLabel(Schemas.vscodeRemote, remoteAuthority);
		this.userRemoteSettings.tooltip = localize('userSettingsRemote', "Remote") + (hostLabel ? \` [\${hostLabel}]\` : '');
`, '');
    contents = replaceOnce(contents, '\t\t\tlet label = localize(\'userSettings\', "User");', '\t\t\tlet label = this.userLocalLabel;');
    contents = replaceOnce(contents, 'this.options.enableRemoteSettings && this.environmentService.remoteAuthority',
      'this.options.enableRemoteSettings && (this.targetSettingsContext.target || this.environmentService.remoteAuthority)');
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
    contents = replaceOnce(contents, '\tprivate getTargetToInspect(setting: ISetting): SettingsTarget {', `	private getTargetToInspect(setting: ISetting): SettingsTarget {
		if (this.targetSettingsScope && this.settingsTarget === ConfigurationTarget.USER_REMOTE) {
			return this.settingsTarget;
		}`);
    contents = replaceOnce(contents, '\t\tconst configTarget = URI.isUri(scope) ? ConfigurationTarget.WORKSPACE_FOLDER : scope;', `		const configTarget = URI.isUri(scope) ? ConfigurationTarget.WORKSPACE_FOLDER : scope;
		if (this.targetSettingsScope && this.setting.extensionInfo && (configTarget === ConfigurationTarget.USER_LOCAL || configTarget === ConfigurationTarget.APPLICATION)) {
			return false;
		}`);
    contents = replaceOnce(contents, 'return REMOTE_MACHINE_SCOPES.includes(this.setting.scope) || USER_LOCAL_AND_REMOTE_SETTINGS.includes(this.setting.key);',
      'return !!(this.targetSettingsScope && this.setting.extensionInfo && this.setting.scope === ConfigurationScope.APPLICATION) || REMOTE_MACHINE_SCOPES.includes(this.setting.scope) || USER_LOCAL_AND_REMOTE_SETTINGS.includes(this.setting.key);');
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
  return contents;
}
