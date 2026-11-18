import { join, resolve } from 'node:path';

export const settingsEditorSourcePaths = [
  'src/vs/workbench/services/preferences/common/preferencesEditorInput.ts',
  'src/vs/workbench/contrib/preferences/browser/preferences.contribution.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsEditor2.ts',
  'src/vs/workbench/services/preferences/browser/preferencesService.ts',
  'src/vs/workbench/contrib/preferences/browser/settingsLayout.ts',
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
  if (path === paths[4]) {
    // 先由原细分类消费，再把同一类别的新设置放入父类；未知类别仍报告遗漏。
    contents = replaceOnce(contents, "settings: ['workbench.*'],", "settings: ['workbench.*', 'onboarding.*', 'boxteam.*'],");
    contents = replaceOnce(contents, "label: localize('chat', \"Chat\"),", "label: localize('chat', \"Chat\"),\n\t\t\tsettings: ['chat.*', 'sessions.chat*', 'agents.voice.*', 'dictation.*', 'github.copilot.chat.*', 'imageCarousel.chat.*'],");
    return replaceOnce(contents, "settings: ['explorer.*', 'outline.*']", "settings: ['explorer.*', 'outline.*', 'imageCarousel.explorer*', 'mediaPreview.*']");
  }
  if (path === paths[0]) {
    const i18n = JSON.stringify(resolve(root, '../shared/core/i18n/index.js'));
    contents = `import { CancellationToken } from '../../../../base/common/cancellation.js';
import { CancellationError } from '../../../../base/common/errors.js';
import { DisposableStore, toDisposable } from '../../../../base/common/lifecycle.js';
import { IInstantiationService } from '../../../../platform/instantiation/common/instantiation.js';
import { ServiceCollection } from '../../../../platform/instantiation/common/serviceCollection.js';
import { parseSettingsLocation, prepareSettingsPane, settingsEditorIdentity, type SourceSettings } from ${editor};
import { ITargetSettingsContext, type TargetSettingsLocation } from ${scope};
import type { SourceDocument } from ${JSON.stringify(resolve(root, 'src/settings/sourceOwner.js'))};
import { t } from ${i18n};
` + contents;
    contents = section(contents, 'export class SettingsEditor2Input extends EditorInput {', 'const PreferencesEditorIcon =', `export class SettingsEditor2Input extends EditorInput {
  static readonly ID: string = 'workbench.input.settings2';
  readonly resource: URI;
  private settingsLocation: TargetSettingsLocation | undefined;
  get location(): TargetSettingsLocation | undefined { return this.settingsLocation; }
  private settingsModel: Settings2EditorModel | undefined;
  private readonly sources = new Map<IInstantiationService, Map<string, { source: SourceSettings; model: Settings2EditorModel }>>();
  private readonly paneLifetimes = new WeakMap<IInstantiationService, DisposableStore>();
  private readonly writers = new Map<number, { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }>();

  constructor(location: TargetSettingsLocation | undefined, private initialContext: SourceDocument | undefined,
    @IPreferencesService private readonly preferencesService: IPreferencesService) {
    super();
    this.settingsLocation = location && parseSettingsLocation(location);
    this.resource = URI.from({ scheme: Schemas.vscodeSettings, path: 'settingseditor',
      query: this.location ? settingsEditorIdentity(this.location) : '' });
    this.settingsModel = this.location ? undefined : preferencesService.createSettings2EditorModel();
  }

  override get editorPaneScope(): EditorInput | undefined { return this; }

  override async prepareEditorPane(parent: IInstantiationService, disposables: DisposableStore, token: CancellationToken): Promise<IInstantiationService> {
    const context: ITargetSettingsContext = { _serviceBrand: undefined, target: undefined };
    const child = parent.createChild(new ServiceCollection([ITargetSettingsContext, context]), disposables);
    this.sources.set(child, new Map());
    this.paneLifetimes.set(child, disposables);
    disposables.add(toDisposable(() => { this.sources.delete(child); this.paneLifetimes.delete(child); }));
    if (this.location) await this.selectSource(child, this.location, this.initialContext, token);
    this.initialContext = undefined;
    return child;
  }

  /** 分类与范围只重绑当前 Pane；原来源模型持续持有草稿、撤销和保存身份。 */
  async selectSource(services: IInstantiationService, location: TargetSettingsLocation | undefined,
    document: SourceDocument | undefined, token: CancellationToken): Promise<void> {
    const context = services.invokeFunction(accessor => accessor.get(ITargetSettingsContext));
    const bindings = this.sources.get(services), lifetime = this.paneLifetimes.get(services);
    if (!bindings || !lifetime || lifetime.isDisposed) throw new CancellationError();
    let binding = location ? bindings.get(settingsEditorIdentity(location)) : undefined;
    if (location && !binding) {
      const preparation = new DisposableStore();
      try {
        const child = await prepareSettingsPane(services, location, document, preparation, token);
        if (token.isCancellationRequested || lifetime.isDisposed || this.isDisposed()) throw new CancellationError();
        const source = child.invokeFunction(accessor => accessor.get(ITargetSettingsContext)).target!;
        const model = preparation.add(source.createModel(child));
        binding = { source, model };
        bindings.set(settingsEditorIdentity(location), binding);
        preparation.add(source.onDidChange(() => this.notifySettingsDirty()));
        lifetime.add(preparation);
      } catch (error) { preparation.dispose(); throw error; }
    }
    context.target?.setVisible(false);
    context.target = binding?.source;
    context.target?.setVisible(true);
    if (!binding) this.settingsModel ??= this.preferencesService.createSettings2EditorModel();
    this.notifySettingsDirty();
  }

  override matches(other: EditorInput | IUntypedEditorInput): boolean {
    return super.matches(other) || other instanceof SettingsEditor2Input && isEqual(this.resource, other.resource);
  }
  override get typeId(): string { return SettingsEditor2Input.ID; }
  // 来源由各 Pane 的范围栏显示；共享 Input 的标签不绑定最初打开的来源。
  override getName(): string { return t('workbench.settings.editorName'); }
  override getIcon(): ThemeIcon { return SettingsEditorIcon; }
  override async resolve(services?: IInstantiationService): Promise<Settings2EditorModel> {
    const source = services?.invokeFunction(accessor => accessor.get(ITargetSettingsContext)).target;
    const model = services && source ? this.sources.get(services)?.get(settingsEditorIdentity(source.location))?.model : this.settingsModel;
    if (!model) throw new Error('设置模型尚未绑定当前编辑器组');
    return model;
  }
  registerSettingsWriter(group: number, writer: { pending(): boolean; save(): Promise<void>; revert(): Promise<void> }) {
    this.writers.set(group, writer);
    return toDisposable(() => { if (this.writers.get(group) === writer) this.writers.delete(group); });
  }
  notifySettingsDirty(): void { this._onDidChangeDirty.fire(); }
  override isDirty(): boolean { return [...this.writers.values()].some(writer => writer.pending()) || [...this.sources.values()].some(bindings => [...bindings.values()].some(({ source }) => source.model.isDirty())); }
  override async save(): Promise<EditorInput> {
    await Promise.all([...this.writers.values()].map(writer => writer.save()));
    for (const bindings of this.sources.values()) for (const { source } of bindings.values()) if (source.model.isDirty() && !await source.model.save()) throw new Error('配置来源未保存，草稿仍保留');
    return this;
  }
  override async revert(): Promise<void> {
    await Promise.all([...this.writers.values()].map(writer => writer.revert()));
    for (const bindings of this.sources.values()) for (const { source } of bindings.values()) await source.model.revert();
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
    // 原生 Pane 与内部 SplitView 共用最小宽度，窄窗口不能继续撑出 500px 正文。
    contents = replaceOnce(contents, '\tprivate static EDITOR_MIN_WIDTH: number = 500;', '\tprivate static EDITOR_MIN_WIDTH: number = 320;');
    contents = `import { ITargetSettingsContext, type TargetSettingsLocation } from ${scope};
import type { SourceDocument } from ${JSON.stringify(resolve(root, 'src/settings/sourceOwner.js'))};
import { settingsHistoryLocation } from ${editor};
import { SettingsContextHeader } from ${JSON.stringify(resolve(root, 'src/settings/header.js'))};
import { ConfigurationEntityView, type DefinitionEditorOptions } from ${JSON.stringify(resolve(root, 'src/settings/entities.js'))};
import { SettingsCategoryNavigation } from ${JSON.stringify(resolve(root, 'src/settings/navigation.js'))};
import { SettingsManagementView } from ${JSON.stringify(resolve(root, 'src/settings/management.js'))};
import { settingsCategory, type SettingsCategory } from ${JSON.stringify(resolve(root, 'src/settings/categories.js'))};
import { INotificationService } from '../../../../platform/notification/common/notification.js';
import { IEditorService } from '../../../services/editor/common/editorService.js';
import { CancellationError } from '../../../../base/common/errors.js';
` + contents;
    contents = replaceOnce(contents, 'export class SettingsEditor2 extends EditorPane {', `interface PendingSettingsUpdate {
  key: string; value: unknown; languageFilter: string | undefined; manualReset: boolean;
  scope: ConfigurationScope | undefined; target: SettingsTarget | null; inFlight: boolean;
}
export class SettingsEditor2 extends EditorPane {`);
    contents = replaceOnce(contents, '\tprivate pendingSettingUpdate: { key: string; value: unknown; languageFilter: string | undefined } | null = null;', `  private readonly pendingSettingUpdates = new Map<string, PendingSettingsUpdate>();
  private pendingSettingWrite: Promise<void> | undefined;
  private settingsWriterInput: SettingsEditor2Input | undefined;
  private settingsContextHeader: SettingsContextHeader | undefined;
  private settingsNavigation!: SettingsCategoryNavigation;
  private settingsSourceHeader!: HTMLElement;
  private windowSettingsTarget: SettingsTarget | undefined;
  private readonly settingsHeaderLifetime = this._register(new DisposableStore());
  private entityView: ConfigurationEntityView | undefined;
  private entityContainer!: HTMLElement;
  private entityKind: DefinitionEditorOptions['definitionKind'];
  private managementView!: SettingsManagementView;`);
    contents = replaceOnce(contents, '\t\tthis.createSettingsTree(this.settingsTreeContainer);', `
    this.rootElement.classList.add('boxteam-settings');
    this.tocTree.updateOptions({ expandOnlyOnTwistieClick: true });
    this.settingsNavigation = this._register(this.instantiationService.createInstance(SettingsCategoryNavigation,
      DOM.append(this.headerContainer.querySelector<HTMLElement>('.settings-header-controls')!, DOM.$('div')), () => this.flushSettingsUpdates(),
      () => this.showCategory(this.settingsNavigation.selected),
      (location, document, token, category) => this.switchSettingsSource(location, document, token, category)));
    this.settingsSourceHeader = DOM.append(this.headerContainer.querySelector<HTMLElement>('.settings-header-controls')!, DOM.$('div'));
    this.updateSettingsSourceHeader();
    this.createSettingsTree(this.settingsTreeContainer);
    this.settingsTreeContainer.append(this.noResultsMessage);
    this.entityContainer = DOM.append(this.settingsTreeContainer, DOM.$('div'));
    this.entityContainer.style.cssText = 'display:none;min-height:0';
    this.managementView = this._register(this.instantiationService.createInstance(SettingsManagementView,
      DOM.append(this.settingsTreeContainer, DOM.$('div')),
      DOM.append(this.headerContainer.querySelector<HTMLElement>('.settings-header-controls')!, DOM.$('div')),
      (active: boolean) => {
        // 管理层覆盖正文时保留原树测量，避免后台更新把行高测成零。
        this.settingsTree.getHTMLElement().style.visibility = active ? 'hidden' : '';
        this.settingsTree.getHTMLElement().style.display = this.entityKind ? 'none' : '';
        this.entityContainer.style.display = !active && this.entityKind ? 'block' : 'none';
        this.rootElement.classList.toggle('boxteam-settings-management', active);
      }, target => this.settingsNavigation.openGatewayConfiguration(target)));`);
    contents = replaceOnce(contents, '\t\t\t\tthis.settingsTree.layout(height, width);', `
        if (height !== undefined) this.entityContainer.style.height = height + 'px';
        this.settingsTree.layout(height, width);`);
    contents = replaceOnce(contents, '\t\treturn this.viewState;\n\t}\n\n\toverride setOptions', `\t\treturn { ...this.viewState, settingsLocation: this.targetSettingsContext.target && settingsHistoryLocation(this.targetSettingsContext.target.location), settingsCategory: this.settingsNavigation.selected, ...(this.entityKind ? this.entityView?.selection : {}) };
\t}\n\n\toverride setOptions`);
    contents = replaceOnce(contents, '\tprivate _setOptions(options: ISettingsEditorOptions): void {', `\tprivate _setOptions(options: ISettingsEditorOptions): void {
    const definition = options as ISettingsEditorOptions & DefinitionEditorOptions;
    const restored = (options.viewState ?? (options.query === undefined && this.input
      ? this.editorMemento.loadEditorState(this.group, this.input) : undefined)) as DefinitionEditorOptions | undefined;
    const category = definition.settingsCategory ?? definition.definitionKind ?? (options.query === undefined ? restored?.settingsCategory : undefined);
    if (category) this.showCategory(category, definition.definitionId ?? (!definition.settingsCategory && !definition.definitionKind ? restored?.definitionId : undefined), definition);
    else if (definition.definitionKind) this.showDefinitions(definition.definitionKind, definition.definitionId, true, definition.definitionModelId);
    else if (options.query !== undefined) this.showDefinitions();
    else if (restored?.definitionKind) this.showDefinitions(restored.definitionKind, restored.definitionId, true, restored.definitionModelId);`);
    // 业务 Pane 跟随来源独立创建；切换 Tab 前沿原生按组／输入保存视图选择。
    contents = replaceOnce(contents, '\t\tsuper.clearInput();', `    if (this.input && this.targetSettingsContext.target) {
      this.editorMemento.saveEditorState(this.group, this.input, {
        searchQuery: this.searchWidget.getValue().trim(), target: this.settingsTargetsWidget.settingsTarget as SettingsTarget,
        settingsLocation: this.targetSettingsContext.target && settingsHistoryLocation(this.targetSettingsContext.target.location), settingsCategory: this.settingsNavigation.selected, ...(this.entityKind ? this.entityView?.selection : {}),
      });
    }
    this.managementView.hide();
    if (this.entityView) {
      this.entityView.dispose(); this.entityView = undefined; this.entityKind = undefined;
      this.entityContainer.style.display = 'none'; this.settingsTree.getHTMLElement().style.display = '';
    }
\t\tsuper.clearInput();`);
    contents = replaceOnce(contents, 'interface ISettingsEditor2State {', 'interface ISettingsEditor2State extends DefinitionEditorOptions { settingsLocation?: TargetSettingsLocation;');
    contents = replaceOnce(contents, '{ searchQuery, target });', '{ searchQuery, target, settingsLocation: this.targetSettingsContext.target && settingsHistoryLocation(this.targetSettingsContext.target.location), settingsCategory: this.settingsNavigation.selected, ...(this.entityKind ? this.entityView?.selection : {}) });');
    contents = replaceOnce(contents, '\t\tsuper.focus();\n\n\t\tif (this._currentFocusContext', '\t\tsuper.focus();\n    if (this.managementView.active) { this.managementView.focus(); return; }\n    if (this.entityKind) { this.entityView?.focus(); return; }\n\n\t\tif (this._currentFocusContext');
    contents = replaceOnce(contents, '\tprivate layoutSplitView(dimension: DOM.Dimension): void {', `  private updateSettingsSourceHeader(): void {
    const expanded = this.settingsSourceHeader.querySelector('details')?.open ?? false;
    this.settingsHeaderLifetime.clear();
    DOM.clearNode(this.settingsSourceHeader);
    this.settingsContextHeader = this.settingsHeaderLifetime.add(this.instantiationService.createInstance(SettingsContextHeader,
      this.settingsSourceHeader, () => { if (this.dimension) this.layoutSplitView(this.dimension); },
      (sourceKey: string) => this.settingsNavigation.openSource(sourceKey)));
    const details = this.settingsSourceHeader.querySelector('details');
    if (details) details.open = expanded;
    const targets = this.headerContainer.querySelector<HTMLElement>('.settings-target-container');
    this.rootElement.classList.toggle('boxteam-business-settings', Boolean(this.targetSettingsContext.target));
    if (targets) targets.style.display = this.targetSettingsContext.target ? 'none' : '';
  }

  private async switchSettingsSource(location: TargetSettingsLocation | undefined, document: SourceDocument | undefined, token: CancellationToken, category: SettingsCategory): Promise<void> {
    const input = this.input;
    if (!(input instanceof SettingsEditor2Input)) throw new CancellationError();
    if (!this.targetSettingsContext.target) this.windowSettingsTarget = this.settingsTargetsWidget.settingsTarget ?? undefined;
    this.cancelSettingsSearch();
    await input.selectSource(this.instantiationService, location, document, token);
    if (token.isCancellationRequested || this.input !== input) throw new CancellationError();
    this.settingsNavigation.setCategory(category);
    this.searchResultModel = null;
    this.settingsTreeModel.clear();
    this.tocFocusedElement = null;
    this.viewState.categoryFilter = undefined;
    this.viewState.settingsTarget = this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : this.windowSettingsTarget ?? ConfigurationTarget.USER_LOCAL;
    this.settingsTargetsWidget.settingsTarget = this.viewState.settingsTarget;
    this.updateSettingsSourceHeader();
    const target = this.viewState.settingsTarget;
    const options: ISettingsEditorOptions & DefinitionEditorOptions = { ...(URI.isUri(target) ? { folderUri: target } : { target }), settingsCategory: this.settingsNavigation.selected };
    await this.setInput(input, options, {}, token);
  }

  private showCategory(category: SettingsCategory, id?: string, options: DefinitionEditorOptions = {}): void {
    const selection = this.entityKind === category ? this.entityView?.selection : undefined;
    id ??= selection?.definitionId;
    const modelId = options.definitionModelId ?? (id === selection?.definitionId ? selection?.definitionModelId : undefined);
    this.settingsNavigation.setCategory(category);
    this.refreshTOCTree();
    this.viewState.categoryFilter = this.settingsNavigation.filter;
    const node = this.settingsNavigation.selectedNode;
    if (this.currentSettingsModel && this.tocTree.hasElement(node)) {
      this.tocTree.setSelection([node]);
    }
    this.showDefinitions(category === 'providers' || category === 'agents' ? category : undefined, id, false, modelId);
    if (this.currentSettingsModel) this.renderTree(undefined, true);
    this.managementView.show(category, this.targetSettingsContext.target, options, !!this.searchWidget.getValue().trim());
  }

  private showDefinitions(kind?: DefinitionEditorOptions['definitionKind'], id?: string, hideManagement = true, modelId?: string): void {
    if (hideManagement) this.managementView.hide();
    const source = this.targetSettingsContext.target;
    this.entityKind = source?.location.workspace_id && !this.searchWidget.getValue().trim() ? kind : undefined;
    this.settingsTree.getHTMLElement().style.display = this.entityKind ? 'none' : '';
    this.entityContainer.style.display = this.entityKind ? 'block' : 'none';
    if (source && this.entityKind) {
      this.entityView ??= this.modelDisposables.add(this.instantiationService.createInstance(ConfigurationEntityView, this.entityContainer, source, this.group.id));
      this.entityView.show(this.entityKind, id, modelId);
    } else this.entityView?.setVisible(false);
    if (this.dimension) this.layoutSplitView(this.dimension);
  }

\tprivate layoutSplitView(dimension: DOM.Dimension): void {`);
    contents = replaceOnce(contents, '\t\t\tthis.splitView.setViewVisible(0, firstViewVisible);', '\t\t\tthis.splitView.setViewVisible(0, firstViewVisible);\n      this.settingsNavigation.setNarrow(!firstViewVisible);');
    contents = replaceOnce(contents, '\t\t\tthis.tocFocusedElement = element;', `
      const category = this.settingsNavigation.categoryForNode(element);
      if (category) {
        this.tocFocusedElement = element;
        this.tocTree.setSelection(element ? [element] : []);
        if (category.id === this.settingsNavigation.selected && element === this.settingsNavigation.selectedNode) return;
        void this.settingsNavigation.openCategory(category.id).then(() => {
          if (this.settingsNavigation.selected !== category.id) return;
          if (element && !settingsCategory(element.id)) {
            const group = this.settingsNavigation.currentGroup(element.id);
            if (group) {
              this.tocFocusedElement = element;
              this.tocTree.setFocus([element]); this.tocTree.setSelection([element]);
              this.showDefinitions(); this.viewState.categoryFilter = group;
              this.renderTree(undefined, true); this.settingsTree.scrollTop = 0;
            }
          }
        }).catch(error => { if (!isCancellationError(error)) this.notificationService.error(error); });
        return;
      }
      this.showDefinitions();
\t\t\tthis.tocFocusedElement = element;`);
    contents = replaceOnce(contents, 'this.tocTree.setChildren(null, createTOCIterator(this.tocTreeModel, this.tocTree));',
      'this.tocTree.setChildren(null, this.settingsNavigation.decorate(this.tocTreeModel.settingsTreeRoot, group => createTOCIterator(group, this.tocTree)));');
    contents = replaceOnce(contents, '\tprivate refreshTOCTree(): void {\n\t\tif (this.isVisible()) {', '\tprivate refreshTOCTree(): void {\n\t\tif (this.isVisible() && this.currentSettingsModel) {');
    contents = replaceOnce(contents, 'const firstCategory = rootChildren[0];', 'const firstCategory = this.settingsNavigation.selectedNode;');
    contents = replaceOnce(contents, "this.splitView.el.style.visibility = count === 0 ? 'hidden' : 'visible';", "this.splitView.el.style.visibility = 'visible';");
    contents = replaceOnce(contents, '\t\t\t\tthis.refreshTree();\n\t\t\t\tthis.tocTree.collapseAll();', '\t\t\t\tthis.refreshTree();');
    // 三种搜索共用原查询取消源；输入去抖、scope 变化及 Pane 释放不能留下独立 AI 任务。
    contents = replaceOnce(contents, 'CancelablePromise, createCancelablePromise, Delayer, raceTimeout', 'Delayer, raceTimeout');
    contents = replaceOnce(contents, '\tprivate aiSearchPromise: CancelablePromise<void> | null = null;\n', '');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(true);', '\t\tthis.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(true);');
    contents = replaceOnce(contents, '\t\tthis.inSettingsEditorContextKey.set(false);', '\t\tthis.cancelSettingsSearch();\n\t\tthis.inSettingsEditorContextKey.set(false);');
    contents = replaceOnce(contents, '\t\tsuper.setEditorVisible(visible);', '\t\tsuper.setEditorVisible(visible);\n\t\tthis.targetSettingsContext.target?.setVisible(visible);\n\t\tthis.managementView?.setVisible(visible);\n\t\tthis.entityView?.setVisible(visible && Boolean(this.entityKind));');
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
    contents = replaceOnce(contents, '\t\t\tconst searchVal = this.searchWidget.getValue();', '\t\t\tif (this.entityKind || this.managementView.active) this.showDefinitions();\n\t\t\tthis.cancelSettingsSearch();\n\t\t\tconst searchVal = this.searchWidget.getValue();');
    contents = replaceOnce(contents, 'this.searchInputDelayer.trigger(() => this.onSearchInputChanged(true));', `this.searchInputDelayer.trigger(() => this.onSearchInputChanged(true)).catch(error => {
        if (!isCancellationError(error)) this.logService.error(error);
      });`);
    contents = replaceOnce(contents, "await this.triggerSearch(query.replace(/\\u203A/g, ' '), expandResults);", `await this.triggerSearch(query.replace(/\\u203A/g, ' '), expandResults);
    if (!this.searchWidget.getValue().trim() && this.settingsNavigation.selected !== 'client') this.showCategory(this.settingsNavigation.selected);`);
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
    @INotificationService private readonly notificationService: INotificationService,
    @IEditorService private readonly editorService: IEditorService,`);
    contents = replaceOnce(contents, 'const model = await this.input.resolve();', `
    const restored = !this.defaultSettingsEditorModel ? (options?.viewState ?? this.editorMemento.loadEditorState(this.group, input)) as ISettingsEditor2State | undefined : undefined;
    if (restored?.settingsLocation) await input.selectSource(this.instantiationService, restored.settingsLocation, undefined, token);
    this.updateSettingsSourceHeader();
    const model = await input.resolve(this.instantiationService);`);
    contents = replaceOnce(contents, 'if (token.isCancellationRequested || !(model instanceof Settings2EditorModel)) {', 'if (token.isCancellationRequested || this.input !== input || !(model instanceof Settings2EditorModel)) {');
    contents = replaceOnce(contents, 'this.viewState = { settingsTarget: ConfigurationTarget.USER_LOCAL };', 'this.viewState = { settingsTarget: this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL };');
    contents = replaceOnce(contents, 'options = options || validateSettingsEditorOptions({});', 'options = options || validateSettingsEditorOptions({});\n    if (this.targetSettingsContext.target) options = { ...options, target: ConfigurationTarget.USER_REMOTE };');
    contents = replaceOnce(contents, 'this.settingsTargetsWidget.settingsTarget = ConfigurationTarget.USER_LOCAL;', 'this.settingsTargetsWidget.settingsTarget = this.targetSettingsContext.target ? ConfigurationTarget.USER_REMOTE : ConfigurationTarget.USER_LOCAL;');
    contents = replaceOnce(contents, '\t\tthis.modelDisposables.clear();', `\t\tthis.modelDisposables.clear();
    this.entityView = undefined;
    this.showDefinitions();
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
    contents = replaceOnce(contents, "const targetWidgetContainer = DOM.append(headerControlsContainer, $('.settings-target-container'));", "const targetWidgetContainer = DOM.append(headerControlsContainer, $('.settings-target-container'));\n    targetWidgetContainer.style.display = this.targetSettingsContext.target ? 'none' : '';\n    targetWidgetContainer.style.minWidth = '0';");
    contents = replaceOnce(contents, 'dimension.height - (72 + 11 + 14 /* header height + editor padding */)', 'Math.max(0, dimension.height - (this.headerContainer.offsetHeight + 11 + 14))');
    contents = replaceOnce(contents, '{ enableRemoteSettings: true }', '{ enableRemoteSettings: true, beforeTargetChange: () => this.flushSettingsUpdates() }');
    contents = replaceOnce(contents, '\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {', `\tprivate async openSettingsFile(options?: ISettingsEditorOptions): Promise<IEditorPane | undefined> {
    const source = this.targetSettingsContext.target, groupId = this.group.id;
    await this.flushSettingsUpdates();
    if (source) {
      const reveal = options?.revealSetting;
      const input = { resource: source.resource, options: { pinned: true,
        selection: reveal ? source.settingSelection(reveal.key, !!reveal.edit) : undefined }, languageId: 'jsonc', forceFile: true };
      return this.editorService.openEditor(input, groupId);
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
