import { join } from 'node:path';

export const extensionConfigurationSourcePaths = [
  'src/vs/workbench/services/configuration/browser/configurationService.ts',
  'src/vs/workbench/services/configuration/browser/configuration.ts',
  'src/vs/workbench/api/browser/mainThreadConfiguration.ts',
];

/** 每个目标视图使用原配置模型；原窗口及 Remote 入口继续使用各自已登记的来源。 */
export function transformExtensionConfiguration(root, path, source, replaceOnce) {
  const paths = extensionConfigurationSourcePaths.map(value => join(root, 'upstream', value));
  if (path === paths[0]) {
    let contents = replaceOnce(source,
      '{ remoteAuthority, configurationCache }: { remoteAuthority?: string; configurationCache: IConfigurationCache },',
      `{ remoteAuthority, configurationCache, remoteSettingsResource, resolveWorkspaceFolders }: {
        remoteAuthority?: string; configurationCache: IConfigurationCache; remoteSettingsResource?: URI;
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
    contents = replaceOnce(contents, 'new RemoteUserConfiguration(remoteAuthority, configurationCache, fileService, uriIdentityService, remoteAgentService, logService)',
      'new RemoteUserConfiguration(remoteAuthority, configurationCache, fileService, uriIdentityService, remoteAgentService, logService, remoteSettingsResource)');
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
    const start = source.indexOf('export class RemoteUserConfiguration extends Disposable {');
    const end = source.indexOf('\nclass FileServiceBasedRemoteUserConfiguration', start);
    let contents = source.slice(start, end);
    contents = replaceOnce(contents, 'logService: ILogService\n\t)', 'logService: ILogService,\n    private readonly settingsResource?: URI\n\t)');
    // 明确目标不等待窗口的 RemoteAgent，不读取另一目标的缓存。
    contents = replaceOnce(contents, '\n\t\tremoteAgentService.getEnvironment().then', `
    if (settingsResource) {
      this._userConfiguration = this._register(new FileServiceBasedRemoteUserConfiguration(settingsResource, { scopes: REMOTE_MACHINE_SCOPES }, fileService, uriIdentityService, logService));
      this._register(this._userConfiguration.onDidChangeConfiguration(model => this._onDidChangeConfiguration.fire(model)));
      return;
    }
\n\t\tremoteAgentService.getEnvironment().then`);
    contents = replaceOnce(contents, 'return this._userConfiguration.initialize();', `const model = await this._userConfiguration.initialize();
      if (this.settingsResource) this._onDidInitialize.fire(model);
      return model;`);
    let result = replaceOnce(source, source.slice(start, end), contents);
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
    return replaceOnce(result, 'const exists = await this.fileService.exists(this._workspaceIdentifier.configPath);',
      `if (this._workspaceIdentifier.configPath.scheme === 'boxteam') throw error;
      const exists = await this.fileService.exists(this._workspaceIdentifier.configPath);`);
  }
  if (path === paths[2]) {
    let contents = replaceOnce(source, 'extHostContext: IExtHostContext,', 'private readonly extHostContext: IExtHostContext,');
    contents = replaceOnce(contents, 'private readonly _configurationListener: IDisposable;', 'private readonly _configurationListener: IDisposable;\n  private disposed = false;');
    contents = replaceOnce(contents, 'this._configurationListener.dispose();', 'this.disposed = true;\n    this._configurationListener.dispose();');
    contents = replaceOnce(contents, 'proxy.$acceptConfigurationChanged(this._getConfigurationData(), e.change);',
      'if (!this.disposed) proxy.$acceptConfigurationChanged(this._getConfigurationData(), e.change);');
    contents = replaceOnce(contents, 'target = target !== null && target !== undefined ? target : this.deriveConfigurationTarget(key, overrides);',
      `if (this.disposed) return Promise.reject(new Error('配置所属宿主代际已经关闭'));
      if (this.extHostContext.extensionHost.binding && overrides.resource && !this._workspaceContextService.isInsideWorkspace(overrides.resource)) {
        return Promise.reject(new Error('配置资源不属于发起宿主的工作区视图：' + overrides.resource.toString()));
      }
      target = target !== null && target !== undefined ? target : this.deriveConfigurationTarget(key, overrides);`);
    return contents;
  }
  return source;
}
