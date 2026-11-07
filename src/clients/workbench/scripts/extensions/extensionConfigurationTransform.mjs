import { join } from 'node:path';

export const extensionConfigurationSourcePaths = [
  'src/vs/workbench/services/configuration/browser/configurationService.ts',
  'src/vs/workbench/services/configuration/browser/configuration.ts',
  'src/vs/workbench/api/browser/mainThreadConfiguration.ts',
  'src/vs/platform/configuration/common/configurationModels.ts',
  'src/vs/platform/configuration/common/configurations.ts',
];

/** 每个目标视图使用原配置模型；原窗口及 Remote 入口继续使用各自已登记的来源。 */
export function transformExtensionConfiguration(root, path, source, replaceOnce) {
  const paths = extensionConfigurationSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = replaceOnce(source,
      '{ remoteAuthority, configurationCache }: { remoteAuthority?: string; configurationCache: IConfigurationCache },',
      `{ remoteAuthority, configurationCache, remoteSettingsResource, resolveWorkspaceFolders, configurationSourceFilter }: {
        remoteAuthority?: string; configurationCache: IConfigurationCache; remoteSettingsResource?: URI;
        configurationSourceFilter?: (source: ConfigurationDefaultSource | undefined) => boolean;
        resolveWorkspaceFolders?: (folders: IStoredWorkspaceFolder[], configuration: URI) => WorkspaceFolder[];
      },`);
    contents = replaceOnce(contents, 'private configurationEditing: Promise<ConfigurationEditing> | undefined;',
      `private configurationEditing: Promise<ConfigurationEditing> | undefined;
      private readonly remoteSettingsResource?: URI;
      private readonly resolveWorkspaceFolders: (folders: IStoredWorkspaceFolder[], configuration: URI) => WorkspaceFolder[];`);
    contents = replaceOnce(contents, 'this.configurationCache = configurationCache;',
      `this.configurationCache = configurationCache;
      this.remoteSettingsResource = remoteSettingsResource;
      this.resolveWorkspaceFolders = resolveWorkspaceFolders ?? ((folders, configuration) => toWorkspaceFolders(folders, configuration, this.uriIdentityService.extUri));`);
    contents = replaceOnce(contents, 'new DefaultConfiguration(userDataProfileService.currentProfile.id, configurationCache, environmentService, logService)',
      'new DefaultConfiguration(userDataProfileService.currentProfile.id, configurationCache, environmentService, logService, configurationSourceFilter)');
    contents = replaceOnce(contents, 'new RemoteUserConfiguration(remoteAuthority, configurationCache, fileService, uriIdentityService, remoteAgentService, logService)',
      'new RemoteUserConfiguration(remoteAuthority, configurationCache, fileService, uriIdentityService, remoteAgentService, logService, remoteSettingsResource)');
    contents = replaceOnce(contents, 'ConfigurationModel, ConfigurationChangeEvent, mergeChanges',
      'ConfigurationModel, ConfigurationChangeEvent, mergeChanges, ConfigurationParseOptions');
    contents = replaceOnce(contents, '\n\tprivate createApplicationConfiguration(): void {', `
    // 扩展键由原注册来源判定；窗口布局等内置设置继续使用当前 profile。
    private localUserConfigurationOptions(scopes?: ConfigurationScope[]): ConfigurationParseOptions {
      return { scopes, ...(this.remoteSettingsResource ? {
        propertyFilter: (_key, schema) => typeof schema?.source !== 'object' || !schema.source.id,
      } : {}) };
    }
\n\tprivate createApplicationConfiguration(): void {`);
    for (const argument of ['userDataProfileService.currentProfile, !!remoteAuthority', 'e.profile, !!this.remoteUserConfiguration']) {
      contents = replaceOnce(contents, `{ scopes: getLocalUserConfigurationScopes(${argument}) }`,
        `this.localUserConfigurationOptions(getLocalUserConfigurationScopes(${argument}))`);
    }
    contents = replaceOnce(contents,
      'new ApplicationConfiguration(this.userDataProfilesService, this.fileService, this.uriIdentityService, this.logService)',
      'new ApplicationConfiguration(this.userDataProfilesService, this.fileService, this.uriIdentityService, this.logService, this.localUserConfigurationOptions().propertyFilter)');
    contents = replaceOnce(contents, 'this.toEditableConfigurationTarget(target, key)', 'this.toEditableConfigurationTarget(target, key, value)');
    contents = replaceOnce(contents, 'OVERRIDE_PROPERTY_PATTERN,', 'OVERRIDE_PROPERTY_PATTERN, OVERRIDE_PROPERTY_REGEX,');
    contents = replaceOnce(contents, '\n\tprivate toEditableConfigurationTarget(target: ConfigurationTarget, key: string): EditableConfigurationTarget | null {', `
    private toEditableConfigurationTarget(target: ConfigurationTarget, key: string, value: unknown): EditableConfigurationTarget | null {
    if (this.remoteSettingsResource) {
      const properties = this.configurationRegistry.getConfigurationProperties();
      const domains = new Set<boolean>();
      const visit = (name: string, entry: unknown): void => {
        const schema = properties[name];
        if (!schema && entry !== null && typeof entry === 'object' && !Array.isArray(entry)) {
          for (const [child, item] of Object.entries(entry)) visit(name ? name + '.' + child : child, item);
        } else domains.add(typeof schema?.source === 'object' && !!schema.source.id);
      };
      if (OVERRIDE_PROPERTY_REGEX.test(key)) visit('', value ?? this.inspect(key).userValue ?? {});
      else visit(key, value);
      if ((target === ConfigurationTarget.USER || target === ConfigurationTarget.USER_LOCAL || target === ConfigurationTarget.USER_REMOTE || target === ConfigurationTarget.APPLICATION) && domains.size > 1) {
        throw new Error('语言覆盖同时包含窗口与目标扩展设置，请分别更新各设置键：' + key);
      }
      if (domains.has(true)) {
        if (target === ConfigurationTarget.USER_REMOTE || target === ConfigurationTarget.USER) return EditableConfigurationTarget.USER_REMOTE;
        if (target === ConfigurationTarget.USER_LOCAL || target === ConfigurationTarget.APPLICATION) throw new Error('目标扩展设置不能写入当前窗口 profile：' + key);
      }
    }
`);
    for (const [folders, configuration] of [
      ['newStoredFolders', 'workspaceConfigPath'],
      ['this.workspaceConfiguration.getFolders()', 'workspaceConfigPath'],
      ['this.workspaceConfiguration.getFolders()', 'this.workspace.configuration'],
    ]) contents = replaceOnce(contents,
      `toWorkspaceFolders(${folders}, ${configuration}, this.uriIdentityService.extUri)`,
      `this.resolveWorkspaceFolders(${folders}, ${configuration})`);
    return replaceOnce(contents, 'const remoteSettingsResource = (await this.remoteAgentService.getEnvironment())?.settingsPath ?? null;',
      'const remoteSettingsResource = this.remoteSettingsResource ?? (await this.remoteAgentService.getEnvironment())?.settingsPath ?? null;');
  }
  if (path === paths[1]) {
    source = replaceOnce(source, 'IConfigurationRegistry,', 'IConfigurationRegistry, ConfigurationDefaultSource,');
    // 目标默认值从原来源重新投影，不能读取窗口已合并的 defaults 缓存。
    source = replaceOnce(source, 'logService: ILogService,\n\t) {\n\t\tsuper(logService);',
      `logService: ILogService,
    private readonly configurationSourceFilter?: (source: ConfigurationDefaultSource | undefined) => boolean,
\t) {
    super(logService, configurationSourceFilter ?? (source => typeof source !== 'object' || !source?.configurationOnly));`);
    source = replaceOnce(source, '\t\treturn this.cachedConfigurationDefaultsOverrides;',
      '\t\treturn this.configurationSourceFilter ? {} : this.cachedConfigurationDefaultsOverrides;');
    source = replaceOnce(source, '\t\tawait this.initializeCachedConfigurationDefaultsOverrides();',
      '\t\tif (!this.configurationSourceFilter) await this.initializeCachedConfigurationDefaultsOverrides();');
    source = replaceOnce(source, '\tprivate async updateCachedConfigurationDefaultsOverrides(): Promise<void> {',
      '\tprivate async updateCachedConfigurationDefaultsOverrides(): Promise<void> {\n    if (this.configurationSourceFilter) return;');
    source = replaceOnce(source, 'if (defaultConfiguration.donotCache) {',
      "if (defaultConfiguration.donotCache || typeof defaultConfiguration.source === 'object' && defaultConfiguration.source?.configurationOnly) {");
    const start = source.indexOf('export class RemoteUserConfiguration extends Disposable {');
    const end = source.indexOf('\nclass FileServiceBasedRemoteUserConfiguration', start);
    let contents = source.slice(start, end);
    contents = replaceOnce(contents, 'logService: ILogService\n\t)', 'logService: ILogService,\n    private readonly settingsResource?: URI\n\t)');
    // 明确目标不等待窗口的 RemoteAgent，不读取另一目标的缓存。
    contents = replaceOnce(contents, '\n\t\tremoteAgentService.getEnvironment().then', `
    if (settingsResource) {
      this._userConfiguration = this._register(new FileServiceBasedRemoteUserConfiguration(settingsResource, this.parseOptions(), fileService, uriIdentityService, logService));
      this._register(this._userConfiguration.onDidChangeConfiguration(model => this._onDidChangeConfiguration.fire(model)));
      return;
    }
\n\t\tremoteAgentService.getEnvironment().then`);
    contents = replaceOnce(contents, 'return this._userConfiguration.initialize();', `const model = await this._userConfiguration.initialize();
      if (this.settingsResource) this._onDidInitialize.fire(model);
      return model;`);
    contents = replaceOnce(contents, 'return this._userConfiguration.reparse({ scopes: REMOTE_MACHINE_SCOPES });',
      'return this._userConfiguration.reparse(this.parseOptions());');
    contents = replaceOnce(contents, '\n\tgetRestrictedSettings(): string[] {', `
    private parseOptions(): ConfigurationParseOptions {
      return this.settingsResource ? {
        scopes: [...REMOTE_MACHINE_SCOPES, ConfigurationScope.APPLICATION],
        propertyFilter: (key, schema) => schema?.scope !== ConfigurationScope.APPLICATION
          || USER_LOCAL_AND_REMOTE_SETTINGS.includes(key)
          || (typeof schema.source === 'object' && !!schema.source.id),
      } : { scopes: REMOTE_MACHINE_SCOPES };
    }
\n\tgetRestrictedSettings(): string[] {`);
    let result = replaceOnce(source, source.slice(start, end), contents);
    const applicationStart = result.indexOf('export class ApplicationConfiguration extends UserSettings {');
    const applicationEnd = result.indexOf('\nexport class UserConfiguration', applicationStart);
    let application = result.slice(applicationStart, applicationEnd);
    application = replaceOnce(application, 'logService: ILogService,\n\t)',
      "logService: ILogService,\n    propertyFilter?: ConfigurationParseOptions['propertyFilter'],\n\t)");
    application = replaceOnce(application, '{ scopes: APPLICATION_SCOPES, skipUnregistered: true }',
      '{ scopes: APPLICATION_SCOPES, skipUnregistered: true, propertyFilter }');
    result = replaceOnce(result, result.slice(applicationStart, applicationEnd), application);
    // 默认值／信任变化只重解析已加载正文；磁盘变化仍由原 watcher 刷新持久缓存。
    result = replaceOnce(result, `const configurationModel = this.folderConfiguration.reparse({ scopes: this.scopes, skipRestricted: this.isUntrusted() });
\t\tthis.updateCache();
\t\treturn configurationModel;`,
      'return this.folderConfiguration.reparse({ scopes: this.scopes, skipRestricted: this.isUntrusted() });');
    // 目标来源不可用不能被原本面向本地缺省文件的空模型吞掉；文件缺失仍沿原缺省语义。
    result = replaceOnce(result, "this.logService.trace(`Error while resolving configuration file '${resource.toString()}': ${errors.getErrorMessage(error)}`);",
      `if ((resource.scheme === 'boxteam' || resource.scheme === 'boxteam-file')
        && (<FileOperationError>error).fileOperationResult !== FileOperationResult.FILE_NOT_FOUND
        && (<FileOperationError>error).fileOperationResult !== FileOperationResult.FILE_NOT_DIRECTORY) throw error;
      this.logService.trace(\`Error while resolving configuration file '\${resource.toString()}': \${errors.getErrorMessage(error)}\`);`);
    const remoteStart = result.indexOf('class FileServiceBasedRemoteUserConfiguration');
    const remoteEnd = result.indexOf('\nclass CachedRemoteUserConfiguration', remoteStart);
    const remote = result.slice(remoteStart, remoteEnd);
    result = replaceOnce(result, remote, replaceOnce(remote, '} catch (e) {\n\t\t\treturn ConfigurationModel.createEmptyModel(this.logService);',
      `} catch (e) {
      if (this.configurationResource.scheme === 'boxteam' && (<FileOperationError>e).fileOperationResult !== FileOperationResult.FILE_NOT_FOUND) throw e;
      return ConfigurationModel.createEmptyModel(this.logService);`));
    result = replaceOnce(result, "import { URI } from '../../../../base/common/uri.js';",
      "import { URI } from '../../../../base/common/uri.js';\nimport { USER_LOCAL_AND_REMOTE_SETTINGS } from '../../../../platform/request/common/request.js';");
    return replaceOnce(result, 'const exists = await this.fileService.exists(this._workspaceIdentifier.configPath);',
      `if (this._workspaceIdentifier.configPath.scheme === 'boxteam') throw error;
      const exists = await this.fileService.exists(this._workspaceIdentifier.configPath);`);
  }
  if (path === paths[2]) {
    let contents = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'ConfigurationScope, getScopes', 'ConfigurationScope, getScopes, OVERRIDE_PROPERTY_REGEX');
    contents = replaceOnce(contents, 'private readonly _configurationListener: IDisposable;', 'private readonly _configurationListener: IDisposable;\n  private disposed = false;');
    contents = replaceOnce(contents, 'this._configurationListener.dispose();', 'this.disposed = true;\n    this._configurationListener.dispose();');
    contents = replaceOnce(contents, 'proxy.$acceptConfigurationChanged(this._getConfigurationData(), e.change);',
      'if (!this.disposed) proxy.$acceptConfigurationChanged(this._getConfigurationData(), e.change);');
    contents = replaceOnce(contents, 'target = target !== null && target !== undefined ? target : this.deriveConfigurationTarget(key, overrides);',
      `if (this.disposed) return Promise.reject(new Error('配置所属宿主代际已经关闭'));
      if (this.extHostContext.extensionHost.targetContext && overrides.resource && !this._workspaceContextService.isInsideWorkspace(overrides.resource)) {
        return Promise.reject(new Error('配置资源不属于发起宿主的工作区视图：' + overrides.resource.toString()));
      }
      target = target !== null && target !== undefined ? target : this.deriveConfigurationTarget(key, overrides);`);
    contents = replaceOnce(contents, 'const configurationValue = this.configurationService.inspect(key, overrides);', `
      const source = Registry.as<IConfigurationRegistry>(ConfigurationExtensions.Configuration).getConfigurationProperties()[key]?.source;
      if (this.extHostContext.extensionHost.targetContext && target === ConfigurationTarget.USER && !OVERRIDE_PROPERTY_REGEX.test(key) && typeof source === 'object' && source.id) {
        target = ConfigurationTarget.USER_REMOTE;
      }
      const configurationValue = this.configurationService.inspect(key, overrides);`);
    return contents;
  }
  if (path === paths[3]) {
    let contents = replaceOnce(source, 'export interface ConfigurationParseOptions {', `export interface ConfigurationParseOptions {
      propertyFilter?: (key: string, schema: IRegisteredConfigurationPropertySchema | undefined) => boolean;`);
    contents = replaceOnce(contents, '\n\t\tconst { contents, keys, overrides, restricted, hasExcludedProperties } = this.doParseRaw(raw, options);', `
      // 归属过滤也约束 inspect 的 raw；scope／信任的原 raw 诊断语义不变。
      if (options?.propertyFilter) {
        const registry = Registry.as<IConfigurationRegistry>(Extensions.Configuration);
        raw = this.filter(raw, registry.getConfigurationProperties(), registry.getExcludedConfigurationProperties(), true,
          { propertyFilter: options.propertyFilter }).raw;
        options = { ...options, propertyFilter: undefined };
      }
\n\t\tconst { contents, keys, overrides, restricted, hasExcludedProperties } = this.doParseRaw(raw, options);`);
    contents = replaceOnce(contents, "!options?.exclude?.length)", "!options?.exclude?.length && !options?.propertyFilter)");
    contents = replaceOnce(contents, 'json.setObjectProperty(raw, key, result.raw);',
      'if (!options.propertyFilter || !result.hasExcludedProperties || Object.keys(result.raw).length) json.setObjectProperty(raw, key, result.raw);');
    return replaceOnce(contents, '\n\t\tif (options.exclude?.includes(key)) {', `
      if (options.propertyFilter && !options.propertyFilter(key, propertySchema ?? excludedPropertySchema)) return false;
\n\t\tif (options.exclude?.includes(key)) {`);
  }
  if (path === paths[4]) {
    let contents = replaceOnce(source, 'IConfigurationRegistry,', 'IConfigurationRegistry, ConfigurationDefaultSource,');
    contents = replaceOnce(contents, 'constructor(private readonly logService: ILogService) {',
      'constructor(private readonly logService: ILogService, private readonly sourceFilter?: (source: ConfigurationDefaultSource | undefined) => boolean) {');
    contents = replaceOnce(contents, 'registry.getConfigurationProperties()', 'registry.getConfigurationProperties(this.sourceFilter)');
    return replaceOnce(contents, 'registry.getExcludedConfigurationProperties()', 'registry.getExcludedConfigurationProperties(this.sourceFilter)');
  }
  return source;
}
