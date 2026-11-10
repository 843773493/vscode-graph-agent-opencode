import { join, resolve } from 'node:path';

export const settingsEditorSourcePaths = [
  'src/vs/workbench/services/preferences/common/preferencesEditorInput.ts',
  'src/vs/workbench/contrib/preferences/browser/preferences.contribution.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsEditor2.ts',
  'src/vs/workbench/services/preferences/browser/preferencesService.ts',
];
function section(source, start, end, replacement, replaceOnce) {
  const from = source.indexOf(start), to = source.indexOf(end, from + start.length);
  if (from < 0 || to < 0) throw new Error(`设置转换锚点缺失：${start}`);
  return replaceOnce(source, source.slice(from, to), replacement);
}

/** 业务设置沿原 SettingsEditor 和文本文件来源；窗口展示配置保留原服务。 */
export function transformSettingsEditor(root, path, contents, replaceOnce) {
  const paths = settingsEditorSourcePaths.map(value => join(root, 'upstream', value));
  const editor = JSON.stringify(resolve(root, 'src/settings/editor.js'));
  const scope = JSON.stringify(resolve(root, 'src/settings/scope.js'));
  if (path === paths[0]) {
    contents = `import { CancellationToken } from '../../../../base/common/cancellation.js';
import { CancellationError } from '../../../../base/common/errors.js';
import { DisposableStore, toDisposable } from '../../../../base/common/lifecycle.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { parseSettingsLocation, prepareSettingsPane, settingsEditorIdentity, type SourceSettings } from ${editor};
import { ITargetSettingsContext, type TargetSettingsLocation } from ${scope};
import type { SourceDocument } from ${JSON.stringify(resolve(root, 'src/settings/sourceOwner.js'))};
` + contents;
    contents = section(contents, 'export class SettingsEditor2Input extends EditorInput {', 'const PreferencesEditorIcon =', `export class SettingsEditor2Input extends EditorInput {
  static readonly ID: string = 'workbench.input.settings2';
  readonly resource: URI;
  private settingsLocation: TargetSettingsLocation | undefined;
  get location(): TargetSettingsLocation | undefined { return this.settingsLocation; }
  private readonly settingsModel: Settings2EditorModel | undefined;
  private readonly sources = new Map<IInstantiationService, SourceSettings>();
  private readonly groupModels = new WeakMap<IInstantiationService, Settings2EditorModel>();
  private readonly writers = new Map<number, { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }>();

  constructor(location: TargetSettingsLocation | undefined, private initialContext: SourceDocument | undefined,
    @IPreferencesService preferencesService: IPreferencesService) {
    super();
    this.settingsLocation = location && parseSettingsLocation(location);
    this.resource = URI.from({ scheme: Schemas.vscodeSettings, path: 'settingseditor',
      query: this.location ? settingsEditorIdentity(this.location) : '' });
    this.settingsModel = this.location ? undefined : preferencesService.createSettings2EditorModel();
  }

  override get editorPaneScope(): EditorInput | undefined { return this.location ? this : undefined; }

  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    if (!this.location) return parent;
    const version = this.editorPaneScopeVersion;
    const initial = this.initialContext;
    this.initialContext = undefined;
    const child = await prepareSettingsPane(parent, this.location, initial, disposables, token);
    if (token.isCancellationRequested || this.isDisposed() || this.editorPaneScopeVersion !== version) throw new CancellationError();
    const source = child.invokeFunction(accessor => accessor.get(ITargetSettingsContext)).target!;
    const model = disposables.add(source.createModel(child));
    this.sources.set(child, source);
    disposables.add(source.onDidChange(() => this.notifySettingsDirty()));
    disposables.add(toDisposable(() => this.sources.delete(child)));
    this.groupModels.set(child, model);
    disposables.add(toDisposable(() => this.groupModels.delete(child)));
    return child;
  }

  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    return super.matches(other) || other instanceof SettingsEditor2Input && isEqual(this.resource, other.resource);
  }
  override get typeId(): string { return SettingsEditor2Input.ID; }
  override getName(): string { return this.location ? '设置 · ' + this.location.name : nls.localize('settingsEditor2InputName', 'Settings'); }
  override getDescription(): string | undefined { return this.location ? this.location.gateway_id + ' · ' + (this.location.workspace_id ?? 'Gateway') + ' · ' + this.location.source_key : undefined; }
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
  override isDirty(): boolean { return [...this.writers.values()].some(writer => writer.pending()) || [...this.sources.values()].some(source => source.model.isDirty()); }
  override async save(): Promise<EditorInput> {
    await Promise.all([...this.writers.values()].map(writer => writer.save()));
    for (const source of this.sources.values()) if (source.model.isDirty() && !await source.model.save()) throw new Error('配置来源未保存，草稿仍保留');
    return this;
  }
  override async revert(): Promise<void> {
    await Promise.all([...this.writers.values()].map(writer => writer.revert()));
    for (const source of this.sources.values()) await source.model.revert();
    this.notifySettingsDirty();
  }
  override dispose(): void {
    super.dispose();
    this.settingsModel?.dispose();
  }
}

`, replaceOnce);
  }
  if (path === paths[3]) {
    contents = replaceOnce(contents, 'new SettingsEditor2Input(this)', 'new SettingsEditor2Input(undefined, undefined, this)');
  }
  if (path === paths[1]) {
    contents = `import { parseSettingsLocation, settingsHistoryLocation } from ${editor};\n` + contents;
    contents = section(contents, '\tserialize(input: SettingsEditor2Input): string {', '\n}\n\nRegistry.as<IEditorFactoryRegistry>', `  serialize(input: SettingsEditor2Input): string {
    return input.location ? JSON.stringify(settingsHistoryLocation(input.location)) : '';
  }
  deserialize(instantiationService: IInstantiationService, serialized: string): SettingsEditor2Input {
    return instantiationService.createInstance(SettingsEditor2Input, serialized ? settingsHistoryLocation(parseSettingsLocation(JSON.parse(serialized))) : undefined, undefined);
  }
`, replaceOnce);
  }
  if (path === paths[2]) {
    contents = `import { ITargetSettingsContext } from ${scope};
import { SettingsContextHeader } from ${JSON.stringify(resolve(root, 'src/settings/header.js'))};
import { captureSettingsLocation } from ${editor};
import { selectSettingsManagementTargetCommand, type ManagementTarget } from ${JSON.stringify(resolve(root, 'src/managementTarget.js'))};
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { IEditorService } from '../../../services/editor/common/editorService.js';
` + contents;
    contents = replaceOnce(contents, 'export class SettingsEditor2 extends EditorPane {', `interface PendingSettingsUpdate {
  key: string; value: unknown; languageFilter: string | undefined; manualReset: boolean;
  scope: ConfigurationScope | undefined; target: SettingsTarget | null; inFlight: boolean;
}
export class SettingsEditor2 extends EditorPane {`);
    contents = replaceOnce(contents, '\tprivate pendingSettingUpdate: { key: string; value: unknown; languageFilter: string | undefined } | null = null;', `  private readonly pendingSettingUpdates = new Map<string, PendingSettingsUpdate>();
  private pendingSettingWrite: Promise<void> | undefined;
  private settingsWriterInput: SettingsEditor2Input | undefined;
  private settingsContextHeader: SettingsContextHeader | undefined;`);
    // 三种搜索共用原查询取消源；输入去抖、scope 变化及 Pane 释放不能留下独立 AI 任务。
    contents = replaceOnce(contents, 'CancelablePromise, createCancelablePromise, Delayer, raceTimeout', 'Delayer, raceTimeout');
    contents = replaceOnce(contents, '\tprivate aiSearchPromise: CancelablePromise<void> | null = null;\n', '');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(true);', '\t\tthis.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(true);');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(false);', '\t\tthis.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(false);');
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
    @IEditorService private readonly editorService: IEditorService,`);
    contents = replaceOnce(contents, 'const model = await this.input.resolve();', 'const model = await input.resolve(this.instantiationService);');
    contents = replaceOnce(contents, 'if (token.isCancellationRequested || !(model instanceof Settings2EditorModel)) {', 'if (token.isCancellationRequested || this.input !== input || !(model instanceof Settings2EditorModel)) {');
    contents = replaceOnce(contents, 'this.viewState = { settingsTarget: ConfigurationTarget.USER_LOCAL };', 'this.viewState = { settingsTarget: this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL };');
    contents = replaceOnce(contents, 'options = options || validateSettingsEditorOptions({});', 'options = options || validateSettingsEditorOptions({});\n    if (this.targetSettingsContext.target) options = { ...options, target: ConfigurationTarget.USER_REMOTE };');
    contents = replaceOnce(contents, 'this.settingsTargetsWidget.settingsTarget = ConfigurationTarget.USER_LOCAL;', 'this.settingsTargetsWidget.settingsTarget = this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL;');
    contents = replaceOnce(contents, '\t\tthis.modelDisposables.clear();', `\t\tthis.modelDisposables.clear();
    this.settingsWriterInput = input;
    this.modelDisposables.add({ dispose: () => { this.settingsWriterInput = undefined; } });
    this.modelDisposables.add(input.registerSettingsWriter(this.group.id, {
      pending: () => this.pendingSettingUpdates.size > 0 || !!this.pendingSettingWrite,
      save: () => this.flushSettingsUpdates(), revert: () => this.discardSettingsUpdates(),
    }));
    const source = this.targetSettingsContext.target;
    if (source) this.modelDisposables.add(source.onDidChange(reload => {
      this.settingsContextHeader?.update();
      if (reload) this.cancelSettingsSearch();
      (reload ? this.refreshInstalledExtensionsList() : Promise.resolve())
        .then(() => this.onConfigUpdate(undefined, reload, reload)).catch(error => this.logService.error(error));
    }));`);
    contents = replaceOnce(contents, '\t\tconst installedExtensions = await this.extensionManagementService.getInstalled();', `    if (this.targetSettingsContext.target) {
      this.installedExtensionIds = this.targetSettingsContext.target.groups.flatMap(group => group.extensionInfo?.id ? [group.extensionInfo.id] : []);
      return;
    }
\t\tconst installedExtensions = await this.extensionManagementService.getInstalled();`);
    contents = replaceOnce(contents, '\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,', `    const targetButton = this._register(new Button(headerControlsContainer, defaultButtonStyles));
    targetButton.label = this.targetSettingsContext.target ? '目标：' + this.targetSettingsContext.target.location.name : '目标：当前工作台';
    this._register(targetButton.onDidClick(() => this.switchSettingsGateway().catch(error => {
      if (!isCancellationError(error)) this.notificationService.error(error);
    })));
    this.settingsContextHeader = this._register(this.instantiationService.createInstance(SettingsContextHeader,
      DOM.append(headerControlsContainer, DOM.$('div')), () => { if (this.dimension) this.layoutSplitView(this.dimension); }));
\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,`);
    contents = replaceOnce(contents, 'dimension.height - (72 + 11 + 14 /* header height + editor padding */)', 'dimension.height - (this.headerContainer.offsetHeight + 11 + 14)');
    contents = replaceOnce(contents, '{ enableRemoteSettings: true }', '{ enableRemoteSettings: true, beforeTargetChange: () => this.flushSettingsUpdates() }');
    contents = replaceOnce(contents, '\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {', `\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {
    await this.flushSettingsUpdates();
    const source = this.targetSettingsContext.target;
    if (source) {
      const input = { resource: source.resource, options: { pinned: true }, languageId: 'jsonc', forceFile: true };
      return this.editorService.openEditor(input);
    }`);
    contents = replaceOnce(contents, '\t\tconst settingsResult = resolveSettingsTree(tocData, coreSettingsGroups, filter, this.logService);', `    const source = this.targetSettingsContext.target;
    const settingsResult = source ? { tree: { id: 'root', label: '来源设置', children: groups.map(group => ({
      id: group.id, label: group.title, settings: group.sections.flatMap(section => section.settings),
    })) } as ITOCEntry<ISetting>, leftoverSettings: new Set<ISetting>() } : resolveSettingsTree(tocData, coreSettingsGroups, filter, this.logService);`);
    contents = replaceOnce(contents, '\t\tconst additionalGroups: ISettingsGroup[] = [];', '\t\tif (!source) {\n\t\tconst additionalGroups: ISettingsGroup[] = [];');
    contents = replaceOnce(contents, '\t\tif (!this.workspaceTrustManagementService.isWorkspaceTrusted() && (this.viewState.settingsTarget instanceof URI || this.viewState.settingsTarget === ConfigurationTarget.WORKSPACE)) {', '\t\t}\n\t\tif (!source && !this.workspaceTrustManagementService.isWorkspaceTrusted() && (this.viewState.settingsTarget instanceof URI || this.viewState.settingsTarget === ConfigurationTarget.WORKSPACE)) {');
    contents = section(contents, '\tprivate onDidChangeSetting(key:', '\tprivate updateTreeScrollSync()', `  private onDidChangeSetting(key: string, value: unknown, type: SettingValueType | SettingValueType[], manualReset: boolean, scope: ConfigurationScope | undefined): void {
    const languageFilter = parseQuery(this.searchWidget.getValue()).languageFilter;
    const source = this.targetSettingsContext.target;
    if (source) {
      try { source.edit(key, value, languageFilter); this.renderTree(key, manualReset); }
      catch (error) { this.notificationService.error(error); }
      return;
    }
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
    const selected = await this.commandService.executeCommand<ManagementTarget | undefined>(selectSettingsManagementTargetCommand);
    if (!selected || this._store.isDisposed) return;
    const captured = await captureSettingsLocation(this.instantiationService, CancellationToken.None, selected);
    if (!captured || this._store.isDisposed) return;
    // 新来源使用新输入；原 dirty 输入留在原编辑器组，不重绑正文或保存基线。
    await this.group.openEditor(this.instantiationService.createInstance(SettingsEditor2Input, captured.location, captured.document), { pinned: true });
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
    contents = replaceOnce(contents, '\t\tconst settingsTarget = this.settingsTargetsWidget.settingsTarget;\n\t\tconst resource = URI.isUri(settingsTarget)', `\t\tconst resource = URI.isUri(settingsTarget)`);
    contents = replaceOnce(contents, '\t\t\t\tthis.pendingSettingUpdate = null;\n', '');
    contents = contents.replaceAll('settingsTarget: this.settingsTargetsWidget.settingsTarget as SettingsTarget', 'settingsTarget: settingsTarget as SettingsTarget');
  }
  return contents;
}
