import { CancellationTokenSource, type CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError, isCancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { Disposable, MutableDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { SelectBox } from '../../upstream/src/vs/base/browser/ui/selectBox/selectBox.js';
import { defaultSelectBoxStyles } from '../../upstream/src/vs/platform/theme/browser/defaultStyles.js';
import { IContextViewService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IEditorProgressService } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { SettingsTreeGroupElement } from '../../upstream/src/vs/workbench/contrib/preferences/browser/settingsTreeModels.js';
import type { ITreeElement } from '../../upstream/src/vs/base/browser/ui/tree/tree.js';
import { t } from '../../../shared/core/i18n/index.js';
import { ITargetSettingsContext } from './scope.js';
import { captureSettingsLocation } from './editor.js';
import type { TargetSettingsLocation } from './scope.js';
import type { SourceDocument } from './sourceOwner.js';
import { settingsCategories, type SettingsCategory, type SettingsScope } from './categories.js';
import { createManagementTargetLifecycle, listManagementTargets, managementGateway, managementWorkspace,
  selectManagementTarget, managementTargetDisplayName, type ManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import type { GatewayRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import './navigation.css';

/** 功能沿原 TOC 导航；范围只决定当前功能编辑哪个正式来源，不改变运行或导航状态。 */
export class SettingsCategoryNavigation extends Disposable {
  private readonly nodes = settingsCategories.map(category => new SettingsTreeGroupElement(`boxteam.settings.category.${category.id}`, undefined, t(category.label), 0, false));
  private readonly tocGroups = new Map<string, SettingsTreeGroupElement>();
  private readonly tocChildren = new Map<SettingsCategory, SettingsTreeGroupElement[]>();
  private readonly nodeGroups = new Map<string, { category: SettingsCategory; groupId: string }>();
  private currentRoot: SettingsTreeGroupElement | undefined;
  private readonly selectorContainers = new Map<SelectBox, HTMLElement>();
  private readonly categorySelector: SelectBox;
  private readonly targetSelector: SelectBox;
  private readonly scopeSelector: SelectBox;
  private readonly workspaceSelector: SelectBox;
  private readonly controls = document.createElement('div');
  private readonly ready: Promise<ReturnType<typeof createManagementTargetLifecycle>>;
  private targets: ManagementTarget[] = [];
  private category: SettingsCategory;
  private readonly opening = this._register(new MutableDisposable<CancellationTokenSource>());
  private businessLocation: TargetSettingsLocation | undefined;
  constructor(element: HTMLElement,
    private readonly beforeChange: () => Promise<void>, private readonly changed: () => void,
    private readonly bindSource: (location: TargetSettingsLocation | undefined, document: SourceDocument | undefined, token: CancellationToken, category: SettingsCategory) => Promise<void>,
    @ITargetSettingsContext private readonly context: ITargetSettingsContext,
    @IInstantiationService private readonly parent: IInstantiationService,
    @IContextViewService contextView: IContextViewService,
    @INotificationService private readonly notifications: INotificationService,
    @IEditorProgressService private readonly progress: IEditorProgressService) {
    super();
    this.category = context.target ? 'runtime' : 'client';
    this.businessLocation = context.target?.location;
    element.className = 'settings-category-context';
    const controls = this.controls;
    controls.style.cssText = 'display:flex;gap:8px;align-items:center;min-width:0';
    element.style.cssText = 'min-width:0';
    element.append(controls);
    const select = (label: string) => {
      const container = document.createElement('div');
      container.style.cssText = 'flex:0 1 240px;min-width:0;max-width:100%';
      controls.append(container);
      const selector = this._register(new SelectBox([], 0, contextView, defaultSelectBoxStyles, { ariaLabel: label, useCustomDrawn: true }));
      selector.render(container); this.selectorContainers.set(selector, container);
      return selector;
    };
    this.categorySelector = select(t('settings.category.select'));
    this.categorySelector.setOptions(settingsCategories.map(item => ({ text: t(item.label) })), settingsCategories.findIndex(item => item.id === this.category));
    this.targetSelector = select(t('workbench.settings.pickTarget'));
    this.targetSelector.setOptions([{ text: t('settings.category.loadingTarget') }]);
    this.scopeSelector = select(t('settings.category.scope'));
    this.workspaceSelector = select(t('settings.category.workspace'));
    this._register(this.categorySelector.onDidSelect(({ index }) => this.run(() => this.openCategory(settingsCategories[index].id))));
    this._register(this.targetSelector.onDidSelect(({ index }) => this.run(() => this.open(this.category, this.targets[index], this.currentScope()))));
    this._register(this.scopeSelector.onDidSelect(({ index }) => this.run(() => this.open(this.category, this.currentTarget(), this.definition.scopes[index]))));
    this._register(this.workspaceSelector.onDidSelect(({ index }) => this.run(() => this.open(this.category, this.currentTarget(), 'workspace', this.workspaces[index].workspace_id))));
    // 设置可以先于导航恢复。目标目录只读一次；不会为了标题追加轮询或健康检查。
    this.ready = workspaceContext.ensureReady().then(async () => {
      if (this._store.isDisposed) throw new CancellationError();
      const lifetime = this._register(createManagementTargetLifecycle(t('workbench.settings.reopenSource')));
      this.targets = await listManagementTargets(lifetime.signal);
      lifetime.check(); this.update(); this.changed();
      return lifetime;
    });
    this.ready.catch(error => { if (!this._store.isDisposed) this.notifications.error(error); });
    this.update();
  }
  get selected(): SettingsCategory { return this.category; }
  get selectedNode(): SettingsTreeGroupElement { return this.nodes[settingsCategories.findIndex(item => item.id === this.category)]; }
  private get definition() { return settingsCategories.find(item => item.id === this.category)!; }
  private get workspaces() { return workspaceContext.getSnapshot().workspaces.filter(item => item.gateway_id === this.currentTarget().gateway_id); }
  private currentScope(): SettingsScope {
    const source = this.context.target;
    return !source?.location.workspace_id ? 'gateway' : ['inline', 'user', 'user_local'].includes(source.document.layer) ? 'backend' : 'workspace';
  }
  private currentTarget(): ManagementTarget {
    const location = this.context.target?.location ?? this.businessLocation;
    const target = this.targets.find(item => item.gateway_id === (location?.gateway_id ?? workspaceContext.getSnapshot().gatewayId)
      && (!location?.gateway_connection_id || item.gateway_connection_id === location.gateway_connection_id))
      ?? managementGateway('settings', location?.gateway_id);
    return location ? { ...target, gateway_connection_id: location.gateway_connection_id } : target;
  }
  setCategory(category: SettingsCategory): void { this.category = category; this.update(); }
  setNarrow(narrow: boolean): void { this.selectorContainers.get(this.categorySelector)!.style.display = narrow ? '' : 'none'; }
  categoryForNode(element: SettingsTreeGroupElement | null) {
    const id = element && (settingsCategories.find(category => `boxteam.settings.category.${category.id}` === element.id)?.id
      ?? this.nodeGroups.get(element.id)?.category);
    return settingsCategories.find(category => category.id === id);
  }
  currentGroup(id: string): SettingsTreeGroupElement | undefined {
    const groupId = this.nodeGroups.get(id)?.groupId ?? id;
    const find = (group: SettingsTreeGroupElement): SettingsTreeGroupElement | undefined => group.id === groupId ? group
      : group.children.filter((child): child is SettingsTreeGroupElement => child instanceof SettingsTreeGroupElement)
        .map(find).find(Boolean);
    return this.currentRoot && find(this.currentRoot);
  }
  decorate(root: SettingsTreeGroupElement, children: (group: SettingsTreeGroupElement) => Iterable<ITreeElement<SettingsTreeGroupElement>>): ITreeElement<SettingsTreeGroupElement>[] {
    this.currentRoot = root;
    // TOC 仅保留分组名称与结构；来源字段仍只由当前模型持有。选择不改变其它分类的展开状态。
    const project = (group: SettingsTreeGroupElement): SettingsTreeGroupElement => {
      const id = `boxteam.settings.navigation.${this.category}.${group.id}`;
      let node = this.tocGroups.get(id);
      if (!node) { node = this._register(new SettingsTreeGroupElement(id, undefined, group.label, group.level, group.isFirstGroup)); this.tocGroups.set(id, node); }
      node.label = group.label;
      this.nodeGroups.set(node.id, { category: this.category, groupId: group.id });
      node.children = group.children.filter((child): child is SettingsTreeGroupElement => child instanceof SettingsTreeGroupElement).map(project);
      return node;
    };
    const selected = this.selectedNode;
    for (const node of this.nodes) if (node !== selected) node.children = [];
    // 高级设置只接收没有专属分类的分组；目录与原设置搜索共用这份成员关系。
    const groups = root.children.filter((child): child is SettingsTreeGroupElement => child instanceof SettingsTreeGroupElement
      && (this.category === 'client' || (this.category === 'runtime'
        ? !settingsCategories.some(category => (category.groups as readonly string[]).includes(child.id))
        : (this.definition.groups as readonly string[]).includes(child.id))));
    selected.children = groups;
    this.tocChildren.set(this.category, groups.map(project));
    return this.nodes.map((element, index) => ({ element, children: this.tocChildren.get(settingsCategories[index].id)
      ?.map(group => ({ element: group, children: children(group), collapsed: undefined })), collapsed: undefined }));
  }
  get filter(): SettingsTreeGroupElement | undefined { return this.category === 'client' ? undefined : this.nodes.find(node => node.id === `boxteam.settings.category.${this.category}`); }
  private update(): void {
    this.categorySelector.select(settingsCategories.findIndex(item => item.id === this.category));
    const business = this.category !== 'client';
    for (const selector of [this.targetSelector, this.scopeSelector, this.workspaceSelector]) this.selectorContainers.get(selector)!.style.display = business ? '' : 'none';
    if (!business) { this.controls.title = ''; return; }
    const scope = this.currentScope(), location = this.context.target?.location;
    const labels = { gateway: 'settings.category.gateway', backend: 'settings.category.backend', workspace: 'workbench.settings.scope.workspace' } as const;
    this.scopeSelector.setOptions(this.definition.scopes.map(scope => ({ text: t(labels[scope]) })), (this.definition.scopes as readonly string[]).indexOf(scope));
    this.selectorContainers.get(this.workspaceSelector)!.style.display = scope === 'workspace' ? '' : 'none';
    if (!this.targets.length) return;
    const target = this.currentTarget();
    this.targetSelector.setOptions(this.targets.map(target => ({ text: managementTargetDisplayName(target) })), this.targets.findIndex(item => item.gateway_id === target.gateway_id && item.gateway_connection_id === target.gateway_connection_id));
    this.workspaceSelector.setOptions(this.workspaces.map(workspace => ({ text: workspace.name })), this.workspaces.findIndex(item => item.workspace_id === location?.workspace_id));
    this.controls.title = t(`settings.category.${scope}Hint`, managementTargetDisplayName(target), this.workspaces.find(item => item.workspace_id === location?.workspace_id)?.name ?? '');
  }
  private run(action: () => Promise<void>): void {
    action().catch(error => { if (!this._store.isDisposed && !isCancellationError(error)) this.notifications.error(error); })
      .finally(() => { if (!this._store.isDisposed) this.update(); });
  }
  async openCategory(category: SettingsCategory): Promise<void> {
    this.opening.value?.cancel();
    if (category === this.category) return;
    if (category === 'client') {
      const request = new CancellationTokenSource();
      this.opening.value = request;
      await this.beforeChange();
      await this.bindSource(undefined, undefined, request.token, category);
      if (request.token.isCancellationRequested) return;
      this.setCategory(category); this.changed();
      return;
    }
    const request = new CancellationTokenSource();
    this.opening.value = request;
    await this.ready;
    if (request.token.isCancellationRequested) return;
    const definition = settingsCategories.find(item => item.id === category)!;
    if (this.context.target && (definition.scopes as readonly string[]).includes(this.currentScope())) {
      this.opening.value?.cancel(); this.opening.clear(); this.setCategory(category); this.changed(); return;
    }
    return this.open(category, this.currentTarget(), definition.scopes[0], this.businessLocation?.workspace_id, undefined, request);
  }
  async openSource(sourceKey: string): Promise<void> {
    return this.open(this.category, this.currentTarget(), this.currentScope(), this.context.target?.location.workspace_id, sourceKey);
  }
  async openGatewayConfiguration(target: GatewayRequestTarget): Promise<void> {
    return this.open('observability', { ...managementGateway('settings', target.gateway_id),
      gateway_connection_id: target.gateway_connection_id }, 'gateway');
  }
  private async open(category: SettingsCategory, target: ManagementTarget, scope?: SettingsScope, workspaceId?: string, sourceKey?: string, request = new CancellationTokenSource()): Promise<void> {
    if (this.opening.value !== request) { this.opening.value?.cancel(); this.opening.value = request; }
    const progress = this.progress.show(true, 300);
    try {
      const lifetime = await this.ready;
      if (request.token.isCancellationRequested) return;
      await this.beforeChange(); lifetime.check();
      const workspace = scope === 'gateway' ? undefined : workspaceId
        ?? (this.context.target?.location.gateway_id === target.gateway_id ? this.context.target.location.workspace_id : undefined)
        ?? managementWorkspace('settings', target).workspace_id;
      const captured = await captureSettingsLocation(this.parent, request.token, target,
        { workspace_id: workspace, source_key: sourceKey, source_layer: scope === 'workspace' ? 'workspace' : 'user' });
      lifetime.check();
      if (!captured || request.token.isCancellationRequested || this._store.isDisposed) return;
      await this.bindSource(captured.location, captured.document, request.token, category);
      if (request.token.isCancellationRequested || this._store.isDisposed) return;
      if (captured.location.workspace_id) this.businessLocation = captured.location;
      selectManagementTarget('settings', target, workspace);
      this.setCategory(category); this.changed();
    } finally { progress.done(); }
  }
  override dispose(): void {
    this.opening.value?.cancel();
    super.dispose();
  }
}
