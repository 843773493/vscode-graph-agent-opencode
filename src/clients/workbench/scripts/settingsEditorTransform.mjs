import { join, resolve } from 'node:path';

export const settingsEditorSourcePaths = [
  'src/vs/workbench/services/preferences/common/preferencesEditorInput.ts',
  'src/vs/workbench/services/preferences/browser/preferencesService.ts',
  'src/vs/workbench/contrib/preferences/browser/preferences.contribution.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsEditor2.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorHandler.ts',
  'src/vs/workbench/services/preferences/common/preferencesModels.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsTree.ts',
];

function section(source, start, end, replacement, replaceOnce) {
  const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
  if (from < 0 || to < 0) throw new Error(`设置转换锚点缺失：${start}`);
  return replaceOnce(source, source.slice(from, to), replacement);
}

/** 原 SettingsEditor、文件输入和服务沿同一冻结目标派生 DI，不替换窗口单例。 */
export function transformSettingsEditor(root, path, source, replaceOnce) {
  const paths = settingsEditorSourcePaths.map(value => join(root, 'upstream', value));
  const editor = JSON.stringify(resolve(root, 'src/settings/editor.js'));
  const scope = JSON.stringify(resolve(root, 'src/settings/scope.js'));
  const context = JSON.stringify(resolve(root, 'src/extensions/targetContext.js'));
  let contents = source;
  if (path === paths[0]) {
    contents = `import { CancellationToken } from '../../../../base/common/cancellation.js';
import { CancellationError } from '../../../../base/common/errors.js';
import { DisposableStore, toDisposable } from '../../../../base/common/lifecycle.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { parseSettingsLocation, prepareSettingsPane, settingsEditorIdentity } from ${editor};
import type { TargetSettingsLocation } from ${scope};
import type { TargetConfigurationContext } from ${context};
` + contents;
    contents = section(contents, 'export class SettingsEditor2Input extends EditorInput {', 'const PreferencesEditorIcon =', `export class SettingsEditor2Input extends EditorInput {
  static readonly ID: string = 'workbench.input.settings2';
  readonly resource: URI;
  private settingsLocation: TargetSettingsLocation | undefined;
  get location(): TargetSettingsLocation | undefined { return this.settingsLocation; }
  private readonly settingsModel: Settings2EditorModel | undefined;
  private readonly groupModels = new WeakMap<IInstantiationService, Settings2EditorModel>();
  private readonly writers = new Map<number, { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }>();

  constructor(location: TargetSettingsLocation | undefined, private initialContext: TargetConfigurationContext | undefined,
    @IPreferencesService preferencesService: IPreferencesService) {
    super();
    this.settingsLocation = location && parseSettingsLocation(location);
    this.resource = URI.from({ scheme: Schemas.vscodeSettings, path: 'settingseditor',
      query: this.location ? settingsEditorIdentity(this.location) : '' });
    this.settingsModel = this.location ? undefined : preferencesService.createSettings2EditorModel();
  }

  override get editorPaneScope(): EditorInput | undefined { return this.location ? this : undefined; }

  bindSettingsTarget(location: TargetSettingsLocation, context: TargetConfigurationContext): void {
    if (!this.location || settingsEditorIdentity(this.location) !== settingsEditorIdentity(location)) {
      throw new Error('设置输入不能重新绑定其它目标或视图');
    }
    const changed = this.location.gateway_connection_id !== location.gateway_connection_id;
    this.settingsLocation = parseSettingsLocation(location);
    this._onDidChangeLabel.fire();
    if (!changed) return;
    this.initialContext = context;
    this.invalidateEditorPaneScope();
  }

  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    if (!this.location) return parent;
    const version = this.editorPaneScopeVersion;
    const initial = this.initialContext;
    this.initialContext = undefined;
    const child = await prepareSettingsPane(parent, this.location, initial, disposables, token);
    if (token.isCancellationRequested || this.isDisposed() || this.editorPaneScopeVersion !== version) throw new CancellationError();
    const model = disposables.add(child.invokeFunction(accessor => accessor.get(IPreferencesService)).createSettings2EditorModel());
    this.groupModels.set(child, model);
    disposables.add(toDisposable(() => this.groupModels.delete(child)));
    return child;
  }

  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    return super.matches(other) || other instanceof SettingsEditor2Input && isEqual(this.resource, other.resource);
  }
  override get typeId(): string { return SettingsEditor2Input.ID; }
  override getName(): string { return this.location ? '设置 · ' + this.location.name : nls.localize('settingsEditor2InputName', 'Settings'); }
  override getDescription(): string | undefined { return this.location ? this.location.gateway_id + ' · ' + this.location.backend_id : undefined; }
  override getIcon(): ThemeIcon { return SettingsEditorIcon; }
  override async resolve(services?: IInstantiationService): Promise<Settings2EditorModel> {
    const model = this.settingsModel ?? (services && this.groupModels.get(services));
    if (!model) throw new Error('设置模型尚未绑定当前编辑器组');
    return model;
  }
  registerSettingsWriter(group: number, writer: { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }) {
    this.writers.set(group, writer);
    return toDisposable(() => { if (this.writers.get(group) === writer) this.writers.delete(group); });
  }
  notifySettingsDirty(): void { this._onDidChangeDirty.fire(); }
  override isDirty(): boolean { return [...this.writers.values()].some(writer => writer.pending()); }
  override async save(): Promise<EditorInput> {
    await Promise.all([...this.writers.values()].map(writer => writer.save()));
    return this;
  }
  override async revert(): Promise<void> {
    await Promise.all([...this.writers.values()].map(writer => writer.revert()));
    this.notifySettingsDirty();
  }
  override dispose(): void {
    super.dispose();
    this.settingsModel?.dispose();
  }
}

`, replaceOnce);
  }
  if (path === paths[1]) {
    contents = `import { FileEditorInput } from '../../../contrib/files/browser/editors/fileEditorInput.js';
import { settingsFileLocation, settingsEditorIdentity } from ${editor};
import { ITargetSettingsContext } from ${scope};
import { targetSettings, targetConfigurationSourceFilter } from ${context};
` + contents;
    contents = replaceOnce(contents, '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,',
      '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,\n    @ITargetSettingsContext private readonly targetSettingsContext: ITargetSettingsContext,');
    contents = contents.replaceAll('target, this.configurationService)', 'target, this.configurationService, targetConfigurationSourceFilter(this.targetSettingsContext.target?.configuration))');
    contents = replaceOnce(contents, '\t\t// The default keybindings.json updates based on keyboard layouts,', '\t\tif (!this.targetSettingsContext.target) {\n\t\t// The default keybindings.json updates based on keyboard layouts,');
    contents = replaceOnce(contents, '\t\tthis._register(urlService.registerHandler(this));', '\t\tthis._register(urlService.registerHandler(this));\n    }');
    contents = section(contents, '\tprivate createOrGetCachedSettingsEditor2Input()', '\tgetFolderSettingsResource(', `  private async createSettingsEditor2Input(): Promise<SettingsEditor2Input> {
    const target = this.targetSettingsContext.target;
    // 普通产品设置命令仍可在未配置扩展管理组件时编辑当前工作台；目标入口由顶部动作明确进入。
    if (target) return this.instantiationService.createInstance(SettingsEditor2Input, target.location, target.configuration);
    if (!this._cachedSettingsEditor2Input || this._cachedSettingsEditor2Input.isDisposed()) {
      this._cachedSettingsEditor2Input = this.instantiationService.createInstance(SettingsEditor2Input, undefined, undefined);
    }
    return this._cachedSettingsEditor2Input;
  }

`, replaceOnce);
    contents = replaceOnce(contents, 'const input = this.createOrGetCachedSettingsEditor2Input();', 'const input = await this.createSettingsEditor2Input();');
    contents = replaceOnce(contents, 'const remoteEnvironment = await this.remoteAgentService.getEnvironment();\n\t\tconst remoteSettingsUri = remoteEnvironment ? remoteEnvironment.settingsPath : null;',
      'const remoteSettingsUri = await this.getEditableSettingsURI(ConfigurationTarget.USER_REMOTE);');
    contents = section(contents, '\tasync openRemoteSettings(', '\topenWorkspaceSettings(', `  async openRemoteSettings(options: IOpenSettingsOptions = {}): Promise<IEditorPane | undefined> {
    const resource = await this.getEditableSettingsURI(ConfigurationTarget.USER_REMOTE);
    return resource ? this.open(resource, { ...options, target: ConfigurationTarget.USER_REMOTE }) : undefined;
  }

`, replaceOnce);
    contents = replaceOnce(contents, '\t\t\tcase ConfigurationTarget.USER_REMOTE: {', `\t\t\tcase ConfigurationTarget.USER_REMOTE: {
        if (this.targetSettingsContext.target) return URI.parse(targetSettings(this.targetSettingsContext.target.configuration).resource_uri);`);
    contents = replaceOnce(contents, '\t\treturn this.editorService.openEditor({ resource, options: validateSettingsEditorOptions({ ...options, pinned: true }) }, group);', `    if (this.targetSettingsContext.target) {
      return this.editorService.openEditor(this.createSettingsFileInput(resource), validateSettingsEditorOptions({ ...options, pinned: true }), group);
    }
\t\treturn this.editorService.openEditor({ resource, options: validateSettingsEditorOptions({ ...options, pinned: true }) }, group);`);
    contents = replaceOnce(contents, '\t\treturn this.editorService.openEditor({ resource, options: editorOptions }, group);', `    if (this.targetSettingsContext.target) {
      return this.editorService.openEditor(this.createSplitJsonEditorInput(configurationTarget, resource), editorOptions, group);
    }
\t\treturn this.editorService.openEditor({ resource, options: editorOptions }, group);`);
    contents = replaceOnce(contents, '\t\tconst editableSettingsEditorInput = this.textEditorService.createTextEditor({ resource });', '\t\tconst editableSettingsEditorInput = this.createSettingsFileInput(resource);');
    contents = replaceOnce(contents, '\tpublic createSplitJsonEditorInput(', `  private createSettingsFileInput(resource: URI): EditorInput {
    const input = this.textEditorService.createTextEditor({ resource, forceFile: true });
    const target = this.targetSettingsContext.target;
    // 当前工作台 profile 保持窗口服务，目标 profile/view/folder 才携带冻结上下文。
    if (target && (resource.scheme === 'boxteam' || resource.scheme === 'boxteam-file')) {
      if (!(input instanceof FileEditorInput)) throw new Error('目标设置 JSON 没有使用原文件编辑器');
      const location = settingsFileLocation(target.location, target.configuration, resource);
      input.bindSettingsTarget(location, settingsEditorIdentity(location) === settingsEditorIdentity(target.location) ? target.configuration : undefined);
    }
    return input;
  }

\tpublic createSplitJsonEditorInput(`);
  }
  if (path === paths[2]) {
    contents = `import { FileEditorInput } from '../../files/browser/editors/fileEditorInput.js';
import { SideBySideEditorInput } from '../../../common/editor/sideBySideEditorInput.js';
import { IContextKey, IContextKeyService } from '../../../../platform/contextkey/common/contextkey.js';
import { parseSettingsLocation, settingsHistoryLocation, prepareSettingsContext, settingsFileOptions } from ${editor};
` + contents;
    contents = section(contents, '\tserialize(input: SettingsEditor2Input): string {', '\n}\n\nRegistry.as<IEditorFactoryRegistry>', `  serialize(input: SettingsEditor2Input): string {
    return input.location ? JSON.stringify(settingsHistoryLocation(input.location)) : '';
  }
  deserialize(instantiationService: IInstantiationService, serialized: string): SettingsEditor2Input {
    return instantiationService.createInstance(SettingsEditor2Input, serialized ? settingsHistoryLocation(parseSettingsLocation(JSON.parse(serialized))) : undefined, undefined);
  }
`, replaceOnce);
    contents = replaceOnce(contents, 'model && /\\.(json|code-workspace)$/.test(model.uri.path)',
      "model && (/\\.(json|code-workspace)$/.test(model.uri.path) || model.uri.scheme === 'boxteam')");
    // 异步模型创建按原编辑器生命周期结算，切回同一文档也不能接收上一轮结果。
    contents = replaceOnce(contents, '\t\tthis.disposables.clear();\n\t\tthis.currentRenderer = undefined;', '\t\tconst version = ++this.settingsRendererVersion;\n\t\tthis.disposables.clear();\n\t\tthis.currentRenderer = undefined;');
    contents = replaceOnce(contents, 'const settingsModel = await this.preferencesService.createPreferencesEditorModel(model.uri);', `const settingsModel = await this.preferencesService.createPreferencesEditorModel(model.uri);
      if (version !== this.settingsRendererVersion || this.disposables.isDisposed || this.editor.getModel() !== model) {
        settingsModel?.dispose();
        return;
      }`);
    contents = replaceOnce(contents, 'private currentRenderer: IPreferencesRenderer | undefined;', 'private currentRenderer: IPreferencesRenderer | undefined;\n  private settingsRendererVersion = 0;\n  private readonly settingsJsonContext: IContextKey<boolean>;');
    contents = replaceOnce(contents, '@IWorkspaceContextService private readonly workspaceContextService: IWorkspaceContextService\n\t) {\n\t\tsuper();', `@IWorkspaceContextService private readonly workspaceContextService: IWorkspaceContextService,
    @IContextKeyService contextKeyService: IContextKeyService
\t) {
\t\tsuper();
    this.settingsJsonContext = CONTEXT_SETTINGS_JSON_EDITOR.bindTo(contextKeyService);`);
    contents = replaceOnce(contents, '\t\tthis.currentRenderer = undefined;', '\t\tthis.currentRenderer = undefined;\n    this.settingsJsonContext.reset();');
    contents = replaceOnce(contents, '\t\t\t\tthis.disposables.add(settingsModel);', '\t\t\t\tthis.disposables.add(settingsModel);\n        this.settingsJsonContext.set(true);');
    contents = replaceOnce(contents, 'ResourceContextKey.Resource.isEqualTo(this.userDataProfilesService.defaultProfile.settingsResource.toString())),',
      'ResourceContextKey.Resource.isEqualTo(this.userDataProfilesService.defaultProfile.settingsResource.toString()), CONTEXT_SETTINGS_JSON_EDITOR),');
    contents = replaceOnce(contents, '\t\t\t\t\treturn accessor.get(IPreferencesService).openUserSettings({ jsonEditor: false, ...sanitizedArgs, groupId });', `          const group = groupId === undefined ? undefined : accessor.get(IEditorGroupsService).getGroup(groupId);
          const active = group?.activeEditor;
          const file = active instanceof SideBySideEditorInput ? active.primary : active;
          if (file instanceof FileEditorInput && file.settingsLocation && group) {
            const location = file.settingsLocation, resource = file.resource, services = accessor.get(IInstantiationService);
            return (async () => {
              const context = await prepareSettingsContext(location, new AbortController().signal);
              if (file.isDisposed()) return undefined;
              const input = services.createInstance(SettingsEditor2Input, location, context);
              return group.openEditor(input, { ...sanitizedArgs, ...settingsFileOptions(context, resource), pinned: true });
            })();
          }
\t\t\t\t\treturn accessor.get(IPreferencesService).openUserSettings({ jsonEditor: false, ...sanitizedArgs, groupId });`);
  }
  if (path === paths[3]) {
    contents = `import { CancellationError } from '../../../../base/common/errors.js';
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { ConfirmResult, IFileDialogService } from '../../../../platform/dialogs/common/dialogs.js';
import { EditorInput } from '../../../common/editor/editorInput.js';
import { SideBySideEditorInput } from '../../../common/editor/sideBySideEditorInput.js';
import { FileEditorInput } from '../../files/browser/editors/fileEditorInput.js';
import { ITargetSettingsContext } from ${scope};
import { SettingsContextHeader } from ${JSON.stringify(resolve(root, 'src/settings/header.js'))};
import { captureSettingsLocation, prepareSettingsContext, rebindSettingsFiles, settingsEditorIdentity } from ${editor};
import { selectSettingsManagementTargetCommand, type ManagementTarget } from ${JSON.stringify(resolve(root, 'src/managementTarget.js'))};
import { onGatewayUserSessionInvalidated } from ${JSON.stringify(resolve(root, '../web/src/api/http.js'))};
import { workspaceContext } from ${JSON.stringify(resolve(root, 'src/workspaceContext.js'))};
` + contents;
    contents = replaceOnce(contents, 'export class SettingsEditor2 extends EditorPane {', `interface PendingSettingsUpdate {
  key: string; value: unknown; languageFilter: string | undefined; manualReset: boolean;
  scope: ConfigurationScope | undefined; target: SettingsTarget | null; inFlight: boolean;
}

export class SettingsEditor2 extends EditorPane {`);
    contents = replaceOnce(contents, '\tprivate pendingSettingUpdate: { key: string; value: unknown; languageFilter: string | undefined } | null = null;', `  private readonly pendingSettingUpdates = new Map<string, PendingSettingsUpdate>();
  private pendingSettingWrite: Promise<void> | undefined;
  private settingsWriterInput: SettingsEditor2Input | undefined;
  private readonly settingsTargetSelection = this._register(new MutableDisposable<DisposableStore>());
  private settingsContextHeader: SettingsContextHeader | undefined;`);
    // 三种搜索共用原查询取消源；输入去抖、scope 变化及 Pane 释放不能留下独立 AI 任务。
    contents = replaceOnce(contents, 'CancelablePromise, createCancelablePromise, Delayer, raceTimeout', 'Delayer, raceTimeout');
    contents = replaceOnce(contents, '\tprivate aiSearchPromise: CancelablePromise<void> | null = null;\n', '');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(true);', '\t\tthis.settingsTargetSelection.clear();\n    this.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(true);');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(false);', '\t\tthis.settingsTargetSelection.clear();\n    this.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(false);');
    contents = replaceOnce(contents, '\tprivate async onSearchInputChanged(expandResults: boolean): Promise<void> {', `  private cancelSettingsSearch(): void {
    this.searchInProgress?.dispose(true);
    this.searchInProgress = null;
    this.searchDelayer.cancel();
    this.searchInputDelayer.cancel();
    this.disableAiSearchToggle();
  }

  override dispose(): void {
    this.cancelSettingsSearch();
    super.dispose();
  }

\tprivate async onSearchInputChanged(expandResults: boolean): Promise<void> {`);
    contents = replaceOnce(contents, '\t\t\tconst searchVal = this.searchWidget.getValue();', '\t\t\tthis.cancelSettingsSearch();\n\t\t\tconst searchVal = this.searchWidget.getValue();');
    contents = replaceOnce(contents, 'this.searchInputDelayer.trigger(() => this.onSearchInputChanged(true));', `this.searchInputDelayer.trigger(() => this.onSearchInputChanged(true)).catch(error => {
        if (!isCancellationError(error)) this.logService.error(error);
      });`);
    contents = replaceOnce(contents, '\tprivate async triggerSearch(query: string, expandResults: boolean): Promise<void> {', `\tprivate async triggerSearch(query: string, expandResults: boolean): Promise<void> {
    this.cancelSettingsSearch();
    const search = this.searchInProgress = new CancellationTokenSource();
    const token = search.token;`);
    contents = replaceOnce(contents, '\t\tconst showAdvanced = this.canShowAdvancedSettings();', '\t\ttry {\n\t\tconst showAdvanced = this.canShowAdvancedSettings();');
    contents = replaceOnce(contents, '\t\tthis.settingsTargetsWidget.updateLanguageFilterIndicators(this.viewState.languageFilter);', '\t\tif (token.isCancellationRequested) return;\n\t\tthis.settingsTargetsWidget.updateLanguageFilterIndicators(this.viewState.languageFilter);');
    contents = replaceOnce(contents, 'await this.triggerFilterPreferences(query, expandResults, progressRunner);', 'await this.triggerFilterPreferences(query, expandResults, token);\n      if (token.isCancellationRequested) return;');
    contents = replaceOnce(contents, `\t\t\tthis.searchDelayer.cancel();
\t\t\tif (this.searchInProgress) {
\t\t\t\tthis.searchInProgress.dispose(true);
\t\t\t\tthis.searchInProgress = null;
\t\t\t}
`, '');
    contents = replaceOnce(contents, '\t\t\tprogressRunner.done();\n\t\t}\n\t}\n\n\t/**\n\t * Return a fake SearchResultModel', `\t\t}
    } catch (error) {
      if (!isCancellationError(error)) throw error;
    } finally { progressRunner.done(); }
\t}

\t/**
\t * Return a fake SearchResultModel`);
    contents = replaceOnce(contents, `\tprivate async triggerFilterPreferences(query: string, expandResults: boolean, progressRunner: IProgressRunner): Promise<void> {
\t\tif (this.searchInProgress) {
\t\t\tthis.searchInProgress.dispose(true);
\t\t\tthis.searchInProgress = null;
\t\t}

\t\tconst searchInProgress = this.searchInProgress = new CancellationTokenSource();`, '\tprivate async triggerFilterPreferences(query: string, expandResults: boolean, token: CancellationToken): Promise<void> {');
    contents = contents.replaceAll('searchInProgress.token', 'token');
    contents = replaceOnce(contents, '\t\t\tthis.disableAiSearchToggle();\n', '');
    contents = replaceOnce(contents, 'this.onDidFinishSearch(expandResults, progressRunner);', 'this.onDidFinishSearch(expandResults, undefined);');
    contents = replaceOnce(contents, `\t\t\tif (this.aiSearchPromise) {
\t\t\t\tthis.aiSearchPromise.cancel();
\t\t\t}

`, '');
    contents = replaceOnce(contents, '\t\t\t\tthis.aiSearchPromise = createCancelablePromise(token => {\n\t\t\t\t\treturn this.doAiSearch(query, token).then((results) => {', '\t\t\t\tvoid this.doAiSearch(query, token).then((results) => {');
    contents = replaceOnce(contents, 'if (results && this.showAiResultsAction) {', 'if (!token.isCancellationRequested && results && this.showAiResultsAction) {');
    contents = replaceOnce(contents, '\t\t\t\t\t});\n\t\t\t\t});\n\t\t\t}\n\n\t\t\tthis.onDidFinishSearch', '\t\t\t\t\t});\n\t\t\t}\n\n\t\t\tthis.onDidFinishSearch');
    contents = replaceOnce(contents, '\tprivate onDidSettingsTargetChange(target: SettingsTarget): void {', '\tprivate onDidSettingsTargetChange(target: SettingsTarget): void {\n    this.cancelSettingsSearch();');
    contents = replaceOnce(contents, '\t\tthis.onConfigUpdate(undefined, true);\n\t}\n\n\tprivate onDidDismissExtensionSetting', '\t\tthis.onConfigUpdate(undefined, true, true);\n\t}\n\n\tprivate onDidDismissExtensionSetting');
    contents = replaceOnce(contents, '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,', `\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,
    @ITargetSettingsContext private readonly targetSettingsContext: ITargetSettingsContext,
    @ICommandService private readonly commandService: ICommandService,
    @INotificationService private readonly notificationService: INotificationService,
    @IFileDialogService private readonly fileDialogService: IFileDialogService,`);
    contents = replaceOnce(contents, 'const model = await this.input.resolve();', 'const model = await input.resolve(this.instantiationService);');
    contents = replaceOnce(contents, 'this.viewState = { settingsTarget: ConfigurationTarget.USER_LOCAL };', 'this.viewState = { settingsTarget: this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL };');
    contents = replaceOnce(contents, 'this.settingsTargetsWidget.settingsTarget = ConfigurationTarget.USER_LOCAL;', 'this.settingsTargetsWidget.settingsTarget = this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL;');
    contents = replaceOnce(contents, 'options.target = ConfigurationTarget.USER_LOCAL;', 'options.target = this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL;');
    contents = replaceOnce(contents, 'if (token.isCancellationRequested || !(model instanceof Settings2EditorModel)) {', 'if (token.isCancellationRequested || this.input !== input || !(model instanceof Settings2EditorModel)) {');
    contents = replaceOnce(contents, '\t\tthis.modelDisposables.clear();', `\t\tthis.modelDisposables.clear();
    this.settingsWriterInput = input;
    this.modelDisposables.add({ dispose: () => { this.settingsWriterInput = undefined; } });
    this.modelDisposables.add(input.registerSettingsWriter(this.group.id, {
      pending: () => this.pendingSettingUpdates.size > 0 || !!this.pendingSettingWrite,
      save: () => this.flushSettingsUpdates(), revert: () => this.discardSettingsUpdates(),
    }));`);
    contents = replaceOnce(contents, '\t\tconst installedExtensions = await this.extensionManagementService.getInstalled();', `    const target = this.targetSettingsContext.target;
    if (target) {
      if (target.configuration.kind !== 'settings') throw new Error('设置输入缺少配置专用声明');
      this.installedExtensionIds = target.configuration.connection.extensions.map(extension => extension.extension_id);
      return;
    }
\t\tconst installedExtensions = await this.extensionManagementService.getInstalled();`);
    contents = replaceOnce(contents, 'createTocTreeForExtensionSettings(this.extensionService, extensionSettingsGroups, filter)', `createTocTreeForExtensionSettings(this.extensionService, extensionSettingsGroups, filter,
      this.targetSettingsContext.target?.configuration.kind === 'settings'
        ? new Map(this.targetSettingsContext.target.configuration.connection.extensions.map(extension => [extension.extension_id.toLowerCase(),
          { name: extension.name, displayName: extension.display_name ?? undefined }])) : undefined)`);
    contents = replaceOnce(contents, '\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,', `    const target = this.targetSettingsContext.target;
    const targetButton = this._register(new Button(headerControlsContainer, defaultButtonStyles));
    targetButton.label = target ? '目标：' + target.location.name + ' · ' + target.location.backend_id : '目标：本地 · 当前工作台';
    targetButton.element.title = target ? target.location.gateway_id + ' · profile ' + target.location.profile_id : '当前工作台 profile；选择 Gateway 和配置域';
    this._register(targetButton.onDidClick(() => this.switchSettingsGateway().catch(error => {
      if (!isCancellationError(error)) this.notificationService.error(error);
    })));
    this.settingsContextHeader = this._register(this.instantiationService.createInstance(SettingsContextHeader,
      DOM.append(headerControlsContainer, DOM.$('div')), () => { if (this.dimension) this.layoutSplitView(this.dimension); }));

\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,`);
    contents = replaceOnce(contents, '\t\treturn cachedState ?? null;', `    this.settingsContextHeader?.update(this.settingsTargetsWidget.settingsTarget, this.viewState.languageFilter);
\t\treturn cachedState ?? null;`);
    contents = replaceOnce(contents, '\tprivate async onConfigUpdate(keys?: ReadonlySet<string>, forceRefresh = false, triggerSearch = false): Promise<void> {', `\tprivate async onConfigUpdate(keys?: ReadonlySet<string>, forceRefresh = false, triggerSearch = false): Promise<void> {
    this.settingsContextHeader?.update(this.settingsTargetsWidget.settingsTarget, this.viewState.languageFilter);`);
    contents = replaceOnce(contents, 'dimension.height - (72 + 11 + 14 /* header height + editor padding */)',
      'dimension.height - (this.headerContainer.offsetHeight + 11 + 14 /* 顶部实际高度与原编辑器留白 */)');
    contents = replaceOnce(contents, '{ enableRemoteSettings: true }', '{ enableRemoteSettings: true, beforeTargetChange: () => this.flushSettingsUpdates() }');
    contents = replaceOnce(contents, '\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {', '\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {\n    await this.flushSettingsUpdates();');
    contents = section(contents, '\tprivate onDidChangeSetting(key:', '\tprivate updateTreeScrollSync()', `  private onDidChangeSetting(key: string, value: unknown, type: SettingValueType | SettingValueType[], manualReset: boolean, scope: ConfigurationScope | undefined): void {
    const languageFilter = parseQuery(this.searchWidget.getValue()).languageFilter;
    const target = this.settingsTargetsWidget.settingsTarget;
    const identity = JSON.stringify([key, languageFilter, URI.isUri(target) ? target.toString() : target]);
    this.pendingSettingUpdates.set(identity, { key, value, languageFilter, manualReset, scope, target, inFlight: false });
    this.notifySettingsDirty();
    const delayer = SettingsEditor2.shouldSettingUpdateFast(type) ? this.settingFastUpdateDelayer : this.settingSlowUpdateDelayer;
    delayer.trigger(() => this.flushSettingsUpdates(), manualReset ? 0 : undefined).catch(error => {
      if (!isCancellationError(error)) this.logService.error(error);
    });
  }

  private async switchSettingsGateway(): Promise<void> {
    const selection = new DisposableStore(), controller = new AbortController(), owner = this.input;
    selection.add({ dispose: () => controller.abort(new CancellationError()) });
    this.settingsTargetSelection.value = selection;
    const token = CancellationToken.fromAbortSignal(controller.signal, selection);
    const entry = workspaceContext.getSnapshot().gatewayId;
    const invalidate = () => controller.abort(new CancellationError());
    selection.add({ dispose: onGatewayUserSessionInvalidated(0, invalidate) });
    selection.add({ dispose: workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== entry) invalidate(); }) });
    const isCurrentSelection = () => !controller.signal.aborted && this.settingsTargetSelection.value === selection
      && owner === this.input && !owner?.isDisposed();
    try {
      const selected = await this.commandService.executeCommand<ManagementTarget | undefined>(selectSettingsManagementTargetCommand);
      if (!selected || !isCurrentSelection()) return;
      const captured = await captureSettingsLocation(this.instantiationService, token, selected);
      if (!isCurrentSelection()) return;
      const identity = settingsEditorIdentity(captured.location);
      const children = (input: EditorInput): EditorInput[] => input instanceof SideBySideEditorInput
        ? [input, ...children(input.primary), ...children(input.secondary)] : [input];
      const groups = this.editorGroupService.groups;
      const opened = [...new Set(groups.flatMap(group => group.editors.flatMap(children)))];
      const settings = opened.filter((input): input is SettingsEditor2Input => input instanceof SettingsEditor2Input
        && input.location?.gateway_id === captured.location.gateway_id && input.location.backend_id === captured.location.backend_id
        && input.location.profile_id === captured.location.profile_id);
      const files = opened.filter((input): input is FileEditorInput => input instanceof FileEditorInput
        && input.settingsLocation?.gateway_id === captured.location.gateway_id && input.settingsLocation.backend_id === captured.location.backend_id
        && input.settingsLocation.profile_id === captured.location.profile_id);
      const contexts = new Map([[identity, captured]]);
      // 所有原视图先完成准备；任何一个失败都不改现存 Input、模型引用或路由。
      for (const input of [...settings, ...files]) {
        const existing = input instanceof SettingsEditor2Input ? input.location! : input.settingsLocation!;
        const location = { ...existing, name: captured.location.name, gateway_connection_id: captured.location.gateway_connection_id };
        const key = settingsEditorIdentity(location);
        if (existing.gateway_connection_id !== location.gateway_connection_id && !contexts.has(key)) {
          contexts.set(key, { location, context: await prepareSettingsContext(location, controller.signal) });
          if (!isCurrentSelection()) return;
        }
      }
      const affected = new Set<EditorInput>([...settings, ...files].filter(input =>
        (input instanceof SettingsEditor2Input ? input.location : input.settingsLocation)?.gateway_connection_id !== captured.location.gateway_connection_id));
      const settling = new Set(settings.filter(input => affected.has(input)));
      if (owner instanceof SettingsEditor2Input && (!owner.location || settingsEditorIdentity(owner.location) !== identity
        || owner.location.gateway_connection_id !== captured.location.gateway_connection_id)) settling.add(owner);
      if (!isCurrentSelection()) return;
      const dirty = [...settling].filter(input => input.isDirty());
      if (dirty.length) {
        // 重绑会释放旧 writer；先沿原保存确认处理草稿，不把旧队列静默改投新连接。
        const confirmation = await this.fileDialogService.showSaveConfirm(dirty.map(input => input.getName()));
        if (confirmation === ConfirmResult.CANCEL || !isCurrentSelection()) return;
        await Promise.all(dirty.map(input => confirmation === ConfirmResult.SAVE ? input.save() : input.revert()));
      }
      if (!isCurrentSelection()) return;
      if ([...settling].some(input => input.isDirty())) throw new Error('设置仍有未结清的修改，请重新确认保存或放弃后再切换目标。');
      const views = groups.map(group => ({ group, input: group.activeEditor, viewState: group.activeEditorPane?.getViewState(),
        options: group.activeEditorPane?.options })).filter(view => view.input && children(view.input).some(input => affected.has(input)));
      const references = new DisposableStore();
      try {
        if (affected.size) references.add(rebindSettingsFiles(this.instantiationService, [...contexts.values()].map(prepared => prepared.context)));
        const next = settings.find(input => this.group.editors.includes(input) && settingsEditorIdentity(input.location!) === identity)
          ?? this.instantiationService.createInstance(SettingsEditor2Input, captured.location, captured.context);
        for (const input of settings) {
          const location = { ...input.location!, name: captured.location.name, gateway_connection_id: captured.location.gateway_connection_id };
          input.bindSettingsTarget(location, contexts.get(settingsEditorIdentity(location))?.context ?? captured.context);
        }
        for (const file of files) {
          const location = { ...file.settingsLocation!, name: captured.location.name, gateway_connection_id: captured.location.gateway_connection_id };
          file.bindSettingsTarget(location, contexts.get(settingsEditorIdentity(location))?.context);
        }
        await Promise.all(views.filter(view => view.group !== this.group).map(view => view.group.openEditor(view.input!, {
          ...view.options, viewState: view.viewState, preserveFocus: true,
        })));
        const current = views.find(view => view.group === this.group && view.input === next);
        await this.group.openEditor(next, { pinned: true, viewState: current?.viewState });
      } finally { references.dispose(); }
    } finally {
      if (this.settingsTargetSelection.value === selection) this.settingsTargetSelection.clear();
    }
  }

  private notifySettingsDirty(): void {
    this.settingsWriterInput?.notifySettingsDirty();
  }

  private async discardSettingsUpdates(): Promise<void> {
    this.settingFastUpdateDelayer.cancel();
    this.settingSlowUpdateDelayer.cancel();
    // 用户已明确放弃草稿；旧请求仍结算并保留错误，不声称撤销已经发送的写入。
    await this.pendingSettingWrite?.catch(error => this.logService.error(error));
    for (const [identity, update] of this.pendingSettingUpdates) {
      if (!update.inFlight) this.pendingSettingUpdates.delete(identity);
    }
    this.onConfigUpdate(undefined, true);
  }

  private async flushSettingsUpdates(): Promise<void> {
    this.settingFastUpdateDelayer.cancel();
    this.settingSlowUpdateDelayer.cancel();
    const pending = [...this.pendingSettingUpdates].filter(([, update]) => !update.inFlight);
    if (!pending.length) return this.pendingSettingWrite;
    for (const [, update] of pending) update.inFlight = true;
    const run = async () => {
      try {
        for (const [identity, update] of pending) {
          await this.updateChangedSetting(update.key, update.value, update.manualReset, update.languageFilter, update.scope, update.target);
          if (this.pendingSettingUpdates.get(identity) === update) this.pendingSettingUpdates.delete(identity);
        }
      } finally {
        for (const [, update] of pending) update.inFlight = false;
        this.notifySettingsDirty();
      }
    };
    const write = (this.pendingSettingWrite ?? Promise.resolve()).then(run, run);
    this.pendingSettingWrite = write;
    try { await write; }
    finally {
      if (this.pendingSettingWrite === write) this.pendingSettingWrite = undefined;
      this.notifySettingsDirty();
    }
  }

`, replaceOnce);
    contents = replaceOnce(contents, 'private updateChangedSetting(key: string, value: unknown, manualReset: boolean, languageFilter: string | undefined, scope: ConfigurationScope | undefined): Promise<void>',
      'private updateChangedSetting(key: string, value: unknown, manualReset: boolean, languageFilter: string | undefined, scope: ConfigurationScope | undefined, settingsTarget: SettingsTarget | null): Promise<void>');
    contents = replaceOnce(contents, '\t\tconst settingsTarget = this.settingsTargetsWidget.settingsTarget;\n\t\tconst resource = URI.isUri(settingsTarget)', '\t\tconst resource = URI.isUri(settingsTarget)');
    contents = replaceOnce(contents, '\t\t\t\tthis.pendingSettingUpdate = null;\n', '');
    contents = replaceOnce(contents, 'settingsTarget: this.settingsTargetsWidget.settingsTarget as SettingsTarget', 'settingsTarget: settingsTarget as SettingsTarget');
    contents = replaceOnce(contents, 'if (this.userDataSyncWorkbenchService.enabled && this.userDataSyncEnablementService.canToggleEnablement()) {', 'if (!this.targetSettingsContext.target && this.userDataSyncWorkbenchService.enabled && this.userDataSyncEnablementService.canToggleEnablement()) {');
  }
  if (path === paths[4]) {
    contents = `import { CancellationToken } from '../../../../../base/common/cancellation.js';
import { CancellationError } from '../../../../../base/common/errors.js';
import { prepareSettingsPane, retainSettingsFile, parseSettingsLocation, settingsEditorIdentity } from ${editor};
import { ITargetSettingsContext, type TargetSettingsLocation } from ${scope};
import type { TargetConfigurationContext } from ${context};
` + contents;
    contents = replaceOnce(contents, '\toverride get typeId(): string {', `  private settingsTarget: { location: TargetSettingsLocation; context?: TargetConfigurationContext } | undefined;
  private verifiedSettingsContext: TargetConfigurationContext | undefined;
  get settingsLocation(): TargetSettingsLocation | undefined { return this.settingsTarget?.location; }
  override get editorPaneScope(): EditorInput | undefined { return this.settingsTarget ? this : undefined; }
  bindSettingsTarget(location: TargetSettingsLocation, context?: TargetConfigurationContext): void {
    const existing = this.settingsTarget?.location;
    if (existing) {
      if (settingsEditorIdentity(existing) !== settingsEditorIdentity(location)) {
        throw new Error('同一设置文件不能重新绑定其它目标或视图');
      }
      // 历史恢复只有稳定归属，不能覆盖仍活跃的显式连接。
      if (!context) return;
      if (existing.gateway_connection_id === location.gateway_connection_id) return;
    }
    this.settingsTarget = { location: parseSettingsLocation(location), context };
    this.verifiedSettingsContext = undefined;
    this.setPreferredLanguageId('jsonc');
    if (existing) this.invalidateEditorPaneScope();
  }
  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    const target = this.settingsTarget;
    if (!target) return parent;
    const version = this.editorPaneScopeVersion;
    const initial = target.context;
    target.context = undefined;
    const child = await prepareSettingsPane(parent, target.location, initial, disposables, token, this.resource);
    if (token.isCancellationRequested || this.isDisposed() || version !== this.editorPaneScopeVersion) throw new CancellationError();
    this.verifiedSettingsContext = child.invokeFunction(accessor => accessor.get(ITargetSettingsContext)).target?.configuration;
    return child;
  }

\toverride get typeId(): string {`);
    contents = replaceOnce(contents, '\t\treturn this.doResolveAsText(options);', `    const open = () => this.doResolveAsText(options);
    return this.verifiedSettingsContext ? retainSettingsFile(this.instantiationService, this.verifiedSettingsContext, this.resource, open) : open();`);
  }
  if (path === paths[5]) {
    contents = `import type { TargetSettingsLocation } from ${scope};\nimport { settingsHistoryLocation } from ${editor};\n` + contents;
    contents = replaceOnce(contents, 'interface ISerializedFileEditorInput {', "interface ISerializedFileEditorInput {\n  settingsTarget?: Omit<TargetSettingsLocation, 'gateway_connection_id'>;");
    contents = replaceOnce(contents, '\t\t\tresourceJSON: resource.toJSON(),', '\t\t\tresourceJSON: resource.toJSON(),\n      settingsTarget: fileEditorInput.settingsLocation && settingsHistoryLocation(fileEditorInput.settingsLocation),');
    contents = replaceOnce(contents, '\t\t\treturn fileEditorInput;', '\t\t\tif (serializedFileEditorInput.settingsTarget) fileEditorInput.bindSettingsTarget(settingsHistoryLocation(serializedFileEditorInput.settingsTarget));\n\t\t\treturn fileEditorInput;');
  }
  if (path === paths[6]) {
    contents = `import type { ConfigurationDefaultSource } from '../../../../platform/configuration/common/configurationRegistry.js';\n` + contents;
    contents = replaceOnce(contents, 'readonly configurationService: IConfigurationService\n\t)',
      `readonly configurationService: IConfigurationService,
    private readonly configurationSourceFilter: (source: ConfigurationDefaultSource | undefined) => boolean =
      source => typeof source !== 'object' || !source?.configurationOnly
\t)`);
    contents = replaceOnce(contents, 'registry.getConfigurationProperties()', 'registry.getConfigurationProperties(this.configurationSourceFilter)');
    contents = replaceOnce(contents, 'registry.getExcludedConfigurationProperties()', 'registry.getExcludedConfigurationProperties(this.configurationSourceFilter)');
  }
  if (path === paths[7]) {
    contents = replaceOnce(contents,
      'export async function createTocTreeForExtensionSettings(extensionService: IExtensionService, groups: ISettingsGroup[], filter: ITOCFilter | undefined): Promise<ITOCEntry<ISetting>> {',
      `export async function createTocTreeForExtensionSettings(extensionService: IExtensionService, groups: ISettingsGroup[], filter: ITOCFilter | undefined,
    descriptions?: ReadonlyMap<string, { name: string; displayName?: string }>): Promise<ITOCEntry<ISetting>> {`);
    contents = replaceOnce(contents, 'const extension = await extensionService.getExtension(extensionId);',
      'const extension = descriptions ? descriptions.get(extensionId.toLowerCase()) : await extensionService.getExtension(extensionId);');
  }
  return contents;
}
