import { Disposable, DisposableStore, MutableDisposable, toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Emitter, type Event } from '../../upstream/src/vs/base/common/event.js';
import { observableValue } from '../../upstream/src/vs/base/common/observable.js';
import { MarkdownString } from '../../upstream/src/vs/base/common/htmlContent.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { CancellationToken } from '../../upstream/src/vs/base/common/cancellation.js';
import { IIterativePage, IIterativePager } from '../../upstream/src/vs/base/common/paging.js';
import { IMcpGalleryManifest } from '../../upstream/src/vs/platform/mcp/common/mcpGalleryManifest.js';
import { IGalleryMcpServer, IMcpGalleryService, IQueryOptions } from '../../upstream/src/vs/platform/mcp/common/mcpManagement.js';
import { IInstantiationService } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { ContributionEnablementState, type IEnablementModel } from '../../upstream/src/vs/workbench/contrib/chat/common/enablement.js';
import {
  IMcpService, IMcpWorkbenchService, IWorkbenchMcpServer,
  LazyCollectionState, McpServerInstallState, type IMcpServerEditorOptions,
} from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { McpWorkbenchServer } from '../../upstream/src/vs/workbench/contrib/mcp/browser/mcpWorkbenchService.js';
import { McpServerEditorInput } from '../../upstream/src/vs/workbench/contrib/mcp/browser/mcpServerEditorInput.js';
import { IWorkbenchLocalMcpServer } from '../../upstream/src/vs/workbench/services/mcp/common/mcpWorkbenchManagementService.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IExtensionsWorkbenchService } from '../../upstream/src/vs/workbench/contrib/extensions/common/extensions.js';
import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { WorkspaceMcpCatalog } from './catalog.js';
import { WorkspaceMcpServer } from './server.js';

export const unavailableWorkspaceMcpWrite = () => new Error('此目标的 MCP 制品安装、配置与凭据写入、Node host 尚未接入；已有动态命令不表示已安装制品');
const unavailable = unavailableWorkspaceMcpWrite;

/** 原 IMcpService 的唯一实现：查询／操作 Gateway 后的项目 owner，不启动浏览器 MCP client。 */
export class WorkspaceMcpService extends Disposable implements IMcpService {
  declare readonly _serviceBrand: undefined;
  readonly servers = observableValue<readonly WorkspaceMcpServer[]>(this, []);
  readonly lazyCollectionState: IMcpService['lazyCollectionState'] = observableValue(this, { state: LazyCollectionState.AllKnown, collections: [] });
  private readonly updates = this._register(new Emitter<IWorkbenchMcpServer | undefined>());
  readonly onChange = this.updates.event;
  private readonly resets = this._register(new Emitter<void>());
  readonly onReset = this.resets.event;
  private readonly scopes = new Map<string, { catalog: WorkspaceMcpCatalog; models: Map<string, WorkspaceMcpServer> }>();
  private selected?: WorkspaceMcpCatalog;
  private selectionError?: Error;
  private visibleLists = 0;
  private readonly visibleDetails = new Map<WorkspaceMcpCatalog, number>();
  private readonly reportedErrors = new WeakMap<WorkspaceMcpCatalog, string>();
  readonly enablementModel: IEnablementModel = {
    readEnabled: (key, reader) => this.servers.read(reader).find(server => server.id === key)?.enablement.read(reader) ?? ContributionEnablementState.DisabledWorkspace,
    readProfileEnabled: (key, reader) => Boolean(this.servers.read(reader).find(server => server.id === key)?.data?.enabled),
    canSetEnabled: () => false,
    setEnabled: () => { throw unavailable(); },
    remove: () => { throw unavailable(); },
  };

  constructor(
    @IEditorService private readonly editors: IEditorService,
    @INotificationService private readonly notifications: INotificationService,
  ) {
    super();
    this._register(toDisposable(workspaceContext.subscribe(() => this.synchronizeTargets())));
    const timer = window.setInterval(() => {
      for (const { catalog } of this.scopes.values()) {
        if (!catalog.isBusy && ((catalog === this.selected && this.visibleLists > 0) || this.visibleDetails.has(catalog) || catalog.pending)) {
          void catalog.refresh().then(() => this.reportedErrors.delete(catalog), error => this.report(catalog, error));
        }
      }
    }, 2000);
    this._register(toDisposable(() => { window.clearInterval(timer); for (const { catalog } of this.scopes.values()) catalog.dispose(); }));
    this.synchronizeTargets();
  }

  get local(): WorkspaceMcpServer[] { return this.servers.get().filter(server => server.catalog === this.selected); }
  get hasCurrentCatalog(): boolean {
    const context = workspaceContext.getSnapshot();
    return !context.loading && !context.error && Boolean(this.selected?.snapshot.catalog) && !this.selected?.snapshot.error && !this.selectionError;
  }

  private synchronizeTargets(): void {
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.loading || snapshot.error) return;
    for (const [key, scope] of this.scopes) {
      if (!snapshot.workspaces.some(workspace => gatewayWorkspaceKey(workspace) === key)) {
        scope.catalog.dispose();
        scope.catalog.snapshot = { ...scope.catalog.snapshot, error: '此工作区已从 Gateway 移除' };
        for (const model of scope.models.values()) if (model.changed()) this.updates.fire(model);
        this.scopes.delete(key);
      }
    }
    const selectedKey = new URLSearchParams(location.search).get('mcp_workspace') ?? snapshot.activeId;
    if (!selectedKey) { this.selectionError = undefined; this.selected = undefined; this.publish(); this.resets.fire(); return; }
    let target;
    try { target = requireGatewayWorkspaceRequestTarget(snapshot.workspaces, selectedKey); }
    catch (error) {
      this.selectionError = error instanceof Error ? error : new Error(String(error));
      this.selected = undefined;
      this.publish();
      this.resets.fire();
      return;
    }
    this.selectionError = undefined;
    const key = gatewayWorkspaceKey(target);
    let scope = this.scopes.get(key);
    if (!scope) {
      const label = snapshot.workspaces.find(workspace => gatewayWorkspaceKey(workspace) === key)!.name;
      scope = { catalog: new WorkspaceMcpCatalog(target, label, () => this.publish()), models: new Map() };
      this.scopes.set(key, scope);
    }
    if (this.selected !== scope.catalog) {
      this.selected = scope.catalog;
      this.publish();
      this.resets.fire();
      const catalog = scope.catalog;
      if (this.visibleLists > 0) void catalog.refresh().catch(error => this.report(catalog, error));
    }
    this.publish();
  }

  private publish(): void {
    const models: WorkspaceMcpServer[] = [];
    const changed: WorkspaceMcpServer[] = [];
    for (const scope of this.scopes.values()) {
      for (const data of scope.catalog.snapshot.catalog?.data ?? []) {
        let model = scope.models.get(data.server_id);
        if (!model) {
          model = new WorkspaceMcpServer(scope.catalog, data.server_id, server => this.open(server), () => this.observe(scope.catalog));
          scope.models.set(data.server_id, model);
        }
        models.push(model);
      }
      for (const model of scope.models.values()) if (model.changed()) changed.push(model);
    }
    const previous = this.servers.get();
    const membershipChanged = previous.length !== models.length || previous.some((server, index) => server !== models[index]);
    this.servers.set(models, undefined);
    // 原列表的 onChange 消费 local；先替换目录成员，随后通知，避免新增／删除读到上一批对象。
    if (membershipChanged || !changed.length) this.updates.fire(undefined);
    else for (const model of changed) this.updates.fire(model);
  }

  async queryLocal(): Promise<WorkspaceMcpServer[]> {
    if (workspaceContext.getSnapshot().loading) await workspaceContext.refresh();
    this.synchronizeTargets();
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.error) throw new Error(snapshot.error);
    if (this.selectionError) throw this.selectionError;
    if (!this.selected) throw new Error('请先选择目标工作区，再读取 MCP 服务');
    await this.selected.refresh();
    return this.local;
  }

  private report(catalog: WorkspaceMcpCatalog, error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    if (this.reportedErrors.get(catalog) !== message) this.notifications.error(message);
    this.reportedErrors.set(catalog, message);
  }

  observe(catalog?: WorkspaceMcpCatalog): IDisposable {
    if (catalog) this.visibleDetails.set(catalog, (this.visibleDetails.get(catalog) ?? 0) + 1);
    else ++this.visibleLists;
    return toDisposable(() => {
      if (!catalog) --this.visibleLists;
      else {
        const count = this.visibleDetails.get(catalog)! - 1;
        if (count) this.visibleDetails.set(catalog, count); else this.visibleDetails.delete(catalog);
      }
    });
  }

  async open(server: IWorkbenchMcpServer | string, options?: IMcpServerEditorOptions): Promise<void> {
    const model = typeof server === 'string' ? this.servers.get().find(item => item.id === server) : server;
    if (!(model instanceof WorkspaceMcpServer)) throw new Error('MCP 详情必须属于精确 Gateway／工作区的项目目录');
    await this.editors.openEditor(new McpServerEditorInput(model), options);
  }
  async activateCollections(): Promise<void> { await this.queryLocal(); }
  resetCaches(): void { void this.queryLocal().catch(error => this.notifications.error(error)); }
  resetTrust(): void { throw new Error('MCP 授权归目标工作区 owner，Workbench 不维护第二套 trust 状态'); }
  autostart(): ReturnType<IMcpService['autostart']> { throw new Error('Workbench 不自动启动项目 MCP 服务，请对明确目标发起连接'); }
  cancelAutostart(): void { throw new Error('Workbench 没有自动启动的 MCP 操作，请取消原目标连接操作'); }
}

/** 管理服务只提供原 UI 的查询／打开 seam，业务镜像仍全部归同一个 IMcpService 实例。 */
export class WorkspaceMcpWorkbenchService implements IMcpWorkbenchService {
  declare readonly _serviceBrand: undefined;
  private readonly owner: WorkspaceMcpService;
  readonly whenInitialLocalMcpServersLoaded: Promise<void>;
  constructor(
    @IMcpService service: IMcpService,
    @IExtensionsWorkbenchService private readonly extensions: IExtensionsWorkbenchService,
    @INotificationService notifications: INotificationService,
    @IMcpGalleryService private readonly gallery: IMcpGalleryService,
    @IInstantiationService private readonly instantiation: IInstantiationService,
    @IEditorService private readonly editors: IEditorService,
  ) {
    if (!(service instanceof WorkspaceMcpService)) throw new Error('Workbench MCP 管理与连接服务必须使用同一个项目 owner');
    this.owner = service;
    this.whenInitialLocalMcpServersLoaded = this.owner.queryLocal().then(() => {}, error => { notifications.error(error); });
  }
  get onChange() { return this.owner.onChange; }
  get onReset() { return this.owner.onReset; }
  get local(): readonly WorkspaceMcpServer[] { return this.owner.local; }
  get isLocalCatalogAvailable(): boolean { return this.owner.hasCurrentCatalog; }
  observe(): IDisposable { return this.owner.observe(); }
  queryLocal(): Promise<WorkspaceMcpServer[]> { return this.owner.queryLocal(); }
  getEnabledLocalMcpServers(): IWorkbenchLocalMcpServer[] { throw unavailable(); }
  async queryGallery(options?: IQueryOptions, token = CancellationToken.None, manifest?: IMcpGalleryManifest): Promise<IIterativePager<IWorkbenchMcpServer>> {
    const pager = await this.gallery.query(options, token, manifest);
    const mapPage = (page: IIterativePage<IGalleryMcpServer>): IIterativePage<IWorkbenchMcpServer> => ({
      items: page.items.map(server => this.instantiation.createInstance(McpWorkbenchServer,
        () => McpServerInstallState.Uninstalled, () => undefined, undefined, server, undefined)),
      hasMore: page.hasMore,
    });
    return { firstPage: mapPage(pager.firstPage), getNextPage: async ct => mapPage(await pager.getNextPage(ct)) };
  }
  async getMcpServerFromGallery(): Promise<never> { throw unavailable(); }
  canInstall(): MarkdownString { return new MarkdownString().appendText(unavailable().message); }
  async install(): Promise<never> { throw unavailable(); }
  async uninstall(): Promise<never> { throw unavailable(); }
  async resolveEditableMcpServerConfiguration(): Promise<never> { throw unavailable(); }
  getMcpConfigPath(arg: IWorkbenchLocalMcpServer): undefined;
  getMcpConfigPath(arg: URI): Promise<undefined>;
  getMcpConfigPath(arg: IWorkbenchLocalMcpServer | URI): undefined | Promise<undefined> { return URI.isUri(arg) ? Promise.resolve(undefined) : undefined; }
  async openSearch(searchValue: string): Promise<void> {
    await this.extensions.openSearch(`@mcp ${searchValue}`);
  }
  async open(server: IWorkbenchMcpServer | string, options?: IMcpServerEditorOptions): Promise<void> {
    if (server instanceof McpWorkbenchServer && server.gallery) {
      await this.editors.openEditor(new McpServerEditorInput(server), options);
      return;
    }
    await this.owner.open(server, options);
  }
}

/** 原视图只在可见期间观察当前工作区；隐藏列表不维持永久轮询。 */
export function observeWorkspaceMcpView(view: { readonly onDidChangeBodyVisibility: Event<boolean>; isBodyVisible(): boolean }, service: IMcpWorkbenchService): IDisposable {
  if (!(service instanceof WorkspaceMcpWorkbenchService)) throw new Error('MCP 视图未接入项目管理服务');
  const store = new DisposableStore();
  const observation = store.add(new MutableDisposable());
  const update = (visible: boolean) => { observation.value = visible ? service.observe() : undefined; };
  store.add(view.onDidChangeBodyVisibility(update));
  update(view.isBodyVisible());
  return store;
}
