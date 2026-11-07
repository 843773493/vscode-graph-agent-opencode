import { join, resolve } from 'node:path';

export const settingsEditorSourcePaths = [
  'src/vs/workbench/services/preferences/common/preferencesEditorInput.ts',
  'src/vs/workbench/services/preferences/browser/preferencesService.ts',
  'src/vs/workbench/contrib/preferences/browser/preferences.contribution.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsEditor2.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorInput.ts',
  'src/vs/workbench/contrib/files/browser/editors/fileEditorHandler.ts',
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
  readonly location: TargetSettingsLocation | undefined;
  private readonly settingsModel: Settings2EditorModel | undefined;
  private readonly groupModels = new WeakMap<IInstantiationService, Settings2EditorModel>();
  private readonly writers = new Map<number, { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }>();

  constructor(location: TargetSettingsLocation | undefined, private initialContext: TargetConfigurationContext | undefined,
    @IPreferencesService preferencesService: IPreferencesService) {
    super();
    this.location = location && parseSettingsLocation(location);
    this.resource = URI.from({ scheme: Schemas.vscodeSettings, path: 'settingseditor',
      query: this.location ? settingsEditorIdentity(this.location) : '' });
    this.settingsModel = this.location ? undefined : preferencesService.createSettings2EditorModel();
  }

  override get editorPaneScope(): EditorInput | undefined { return this.location ? this : undefined; }

  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    if (!this.location) return parent;
    const initial = this.initialContext;
    this.initialContext = undefined;
    const child = await prepareSettingsPane(parent, this.location, initial, disposables, token);
    if (token.isCancellationRequested || this.isDisposed()) throw new CancellationError();
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
import { targetSettings } from ${context};
` + contents;
    contents = replaceOnce(contents, '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,',
      '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,\n    @ITargetSettingsContext private readonly targetSettingsContext: ITargetSettingsContext,');
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
import { parseSettingsLocation, prepareSettingsContext, settingsFileOptions } from ${editor};
` + contents;
    contents = section(contents, '\tserialize(input: SettingsEditor2Input): string {', '\n}\n\nRegistry.as<IEditorFactoryRegistry>', `  serialize(input: SettingsEditor2Input): string {
    return input.location ? JSON.stringify(input.location) : '';
  }
  deserialize(instantiationService: IInstantiationService, serialized: string): SettingsEditor2Input {
    return instantiationService.createInstance(SettingsEditor2Input, serialized ? parseSettingsLocation(JSON.parse(serialized)) : undefined, undefined);
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
    contents = `import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { ITargetSettingsContext } from ${scope};
import { captureSettingsLocation } from ${editor};
import { selectSettingsManagementTargetCommand, type ManagementTarget } from ${JSON.stringify(resolve(root, 'src/managementTarget.js'))};
` + contents;
    contents = replaceOnce(contents, 'export class SettingsEditor2 extends EditorPane {', `interface PendingSettingsUpdate {
  key: string; value: unknown; languageFilter: string | undefined; manualReset: boolean;
  scope: ConfigurationScope | undefined; target: SettingsTarget | null; inFlight: boolean;
}

export class SettingsEditor2 extends EditorPane {`);
    contents = replaceOnce(contents, '\tprivate pendingSettingUpdate: { key: string; value: unknown; languageFilter: string | undefined } | null = null;', `  private readonly pendingSettingUpdates = new Map<string, PendingSettingsUpdate>();
  private pendingSettingWrite: Promise<void> | undefined;
  private settingsWriterInput: SettingsEditor2Input | undefined;`);
    contents = replaceOnce(contents, '\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,', `\t\t@IWorkbenchEnvironmentService private readonly environmentService: IWorkbenchEnvironmentService,
    @ITargetSettingsContext private readonly targetSettingsContext: ITargetSettingsContext,
    @ICommandService private readonly commandService: ICommandService,
    @INotificationService private readonly notificationService: INotificationService,`);
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
      // TODO: 配置只读身份的独立 schema 发现尚待接入；不借 VSIX 库存权限或启动宿主补全。
      this.installedExtensionIds = this.extensionService.extensions.filter(extension => {
        const owner = extension.installationTarget;
        return owner?.kind === 'backend' && owner.gatewayId === target.location.gateway_id && owner.backendId === target.location.backend_id
          && !!extension.contributes?.configuration;
      }).map(extension => extension.identifier.value);
      return;
    }
\t\tconst installedExtensions = await this.extensionManagementService.getInstalled();`);
    contents = replaceOnce(contents, '\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,', `    const target = this.targetSettingsContext.target;
    const targetButton = this._register(new Button(headerControlsContainer, defaultButtonStyles));
    targetButton.label = target ? '目标：' + target.location.name + ' · ' + target.location.backend_id : '选择设置目标';
    targetButton.element.title = target ? target.location.gateway_id + ' · profile ' + target.location.profile_id : '选择 Gateway 和配置域';
    this._register(targetButton.onDidClick(() => this.switchSettingsGateway().catch(error => this.notificationService.error(error))));

\t\tconst targetWidgetContainer = DOM.append(headerControlsContainer,`);
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
    await this.flushSettingsUpdates();
    const selected = await this.commandService.executeCommand<ManagementTarget | undefined>(selectSettingsManagementTargetCommand);
    if (!selected) return;
    // TODO: 连接被删除并以新 ID 重建时，显式重选还需刷新同归属 Input/文件模型的传输引用。
    const captured = await captureSettingsLocation(this.instantiationService, CancellationToken.None);
    const next = this.instantiationService.createInstance(SettingsEditor2Input, captured.location, captured.context);
    await this.group.openEditor(next, { pinned: true });
  }

  private notifySettingsDirty(): void {
    this.settingsWriterInput?.notifySettingsDirty();
  }

  private async discardSettingsUpdates(): Promise<void> {
    this.settingFastUpdateDelayer.cancel();
    this.settingSlowUpdateDelayer.cancel();
    await this.pendingSettingWrite;
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
      return;
    }
    this.settingsTarget = { location: parseSettingsLocation(location), context };
    this.setPreferredLanguageId('jsonc');
  }
  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    const target = this.settingsTarget;
    if (!target) return parent;
    const initial = target.context;
    target.context = undefined;
    const child = await prepareSettingsPane(parent, target.location, initial, disposables, token, this.resource);
    this.verifiedSettingsContext = child.invokeFunction(accessor => accessor.get(ITargetSettingsContext)).target?.configuration;
    return child;
  }

\toverride get typeId(): string {`);
    contents = replaceOnce(contents, '\t\treturn this.doResolveAsText(options);', `    const open = () => this.doResolveAsText(options);
    return this.verifiedSettingsContext ? retainSettingsFile(this.instantiationService, this.verifiedSettingsContext, this.resource, open) : open();`);
  }
  if (path === paths[5]) {
    contents = `import type { TargetSettingsLocation } from ${scope};\n` + contents;
    contents = replaceOnce(contents, 'interface ISerializedFileEditorInput {', 'interface ISerializedFileEditorInput {\n  settingsTarget?: TargetSettingsLocation;');
    contents = replaceOnce(contents, '\t\t\tresourceJSON: resource.toJSON(),', '\t\t\tresourceJSON: resource.toJSON(),\n      settingsTarget: fileEditorInput.settingsLocation,');
    contents = replaceOnce(contents, '\t\t\treturn fileEditorInput;', '\t\t\tif (serializedFileEditorInput.settingsTarget) fileEditorInput.bindSettingsTarget(serializedFileEditorInput.settingsTarget);\n\t\t\treturn fileEditorInput;');
  }
  return contents;
}
