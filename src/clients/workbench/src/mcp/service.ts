import { Disposable, DisposableStore, MutableDisposable, toDisposable, type IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { Emitter, type Event } from '../../upstream/src/vs/base/common/event.js';
import { observableValue } from '../../upstream/src/vs/base/common/observable.js';
import { MarkdownString } from '../../upstream/src/vs/base/common/htmlContent.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { CancellationError } from '../../upstream/src/vs/base/common/errors.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { ContributionEnablementState, type IEnablementModel } from '../../upstream/src/vs/workbench/contrib/chat/common/enablement.js';
import {
  IMcpService, IMcpWorkbenchService, IWorkbenchMcpServer,
  LazyCollectionState, type IMcpServerEditorOptions,
} from '../../upstream/src/vs/workbench/contrib/mcp/common/mcpTypes.js';
import { McpServerEditorInput } from '../../upstream/src/vs/workbench/contrib/mcp/browser/mcpServerEditorInput.js';
import { IWorkbenchLocalMcpServer } from '../../upstream/src/vs/workbench/services/mcp/common/mcpWorkbenchManagementService.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { requireGatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { GatewayManagementDirectoryDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { managementGateway, onDidChangeMcpManagementTarget } from '../managementTarget.js';
import { WorkspaceMcpCatalog, mcpCatalogKey, pendingMcpOperation, type McpWorkspaceRequestTarget } from './catalog.js';
import { WorkspaceMcpServer } from './server.js';

export const unavailableWorkspaceMcpWrite = () => new Error('此目标的 MCP 工作区配置与凭据写入、Node host 尚未接入；已有动态命令不表示已安装制品');
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
  private selected = new Set<WorkspaceMcpCatalog>();
  private entryGatewayId?: string;
  private selectionError?: Error;
  private authorizationCatalog?: WorkspaceMcpCatalog;
  private authorizationOpening = false;
  private readonly authorizationController = new AbortController();
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
    @IQuickInputService private readonly quickInput: IQuickInputService,
  ) {
    super();
    this._register(toDisposable(workspaceContext.subscribe(() => this.synchronizeTargets())));
    this._register(onDidChangeMcpManagementTarget(() => this.synchronizeTargets()));
    this._register(toDisposable(onGatewayTokenInvalidated(0, () => {
      for (const { catalog } of this.scopes.values()) catalog.dispose();
      this.authorizationController.abort(new Error('原 MCP 授权入口的访问凭据已失效，请重新打开原对象'));
      this.authorizationCatalog = undefined;
      this.scopes.clear(); this.selected.clear(); this.publish(); this.resets.fire();
    })));
    const timer = window.setInterval(() => {
      for (const { catalog } of this.scopes.values()) {
        if (!catalog.isBusy && ((this.selected.has(catalog) && this.visibleLists > 0) || this.visibleDetails.has(catalog) || catalog.pending || catalog === this.authorizationCatalog)) {
          void catalog.refresh().then(async () => {
            this.reportedErrors.delete(catalog);
            if (catalog === this.authorizationCatalog) await this.openAuthorizationCatalog(catalog);
          }).catch(error => this.report(catalog, error));
        }
      }
    }, 2000);
    this._register(toDisposable(() => { window.clearInterval(timer); this.authorizationController.abort(); for (const { catalog } of this.scopes.values()) catalog.dispose(); }));
    this.synchronizeTargets();
    const authorizationWorkspace = new URLSearchParams(location.search).get('mcp_workspace');
    if (authorizationWorkspace) void this.openAuthorizationTarget(authorizationWorkspace).catch(error => this.notifications.error(error));
  }

  get local(): WorkspaceMcpServer[] { return this.servers.get().filter(server => this.selected.has(server.catalog)); }
  get hasCurrentCatalog(): boolean {
    const context = workspaceContext.getSnapshot();
    return !context.loading && !context.error && Boolean(context.gatewayId) && !this.selectionError
      && this.selected.size > 0
      && [...this.selected].every(catalog => Boolean(catalog.snapshot.catalog) && !catalog.snapshot.error);
  }

  private synchronizeTargets(): void {
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.loading || snapshot.error || !snapshot.gatewayId) return;
    if (this.entryGatewayId !== snapshot.gatewayId) {
      if (this.entryGatewayId) {
        this.authorizationController.abort(new Error('页面 Gateway 已改变，请重新打开原 MCP 授权对象'));
        this.authorizationCatalog = undefined;
      }
      for (const { catalog } of this.scopes.values()) catalog.dispose();
      this.scopes.clear(); this.selected.clear(); this.entryGatewayId = snapshot.gatewayId;
    }
    for (const [key, scope] of this.scopes) {
      if (!snapshot.workspaces.some(workspace => workspace.gateway_id === scope.catalog.target.gateway_id
        && workspace.workspace_id === scope.catalog.target.workspace_id && workspace.backend_id === scope.catalog.target.backend_id)) {
        scope.catalog.dispose();
        scope.catalog.snapshot = { ...scope.catalog.snapshot, error: '此工作区已从 Gateway 移除' };
        for (const model of scope.models.values()) if (model.changed()) this.updates.fire(model);
        this.scopes.delete(key);
      }
    }
    let target;
    try { target = managementGateway('mcp'); }
    catch (error) {
      this.selectionError = error instanceof Error ? error : new Error(String(error));
      this.selected.clear();
      this.publish();
      this.resets.fire();
      return;
    }
    this.selectionError = undefined;
    const selected = new Set<WorkspaceMcpCatalog>();
    for (const workspace of snapshot.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id)) {
      const route = { gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
        backend_id: workspace.backend_id, gateway_connection_id: target.gateway_connection_id };
      const key = mcpCatalogKey(route);
      let scope = this.scopes.get(key);
      if (!scope) {
        scope = { catalog: new WorkspaceMcpCatalog(route, workspace.name, () => this.publish()), models: new Map() };
        this.scopes.set(key, scope);
      }
      selected.add(scope.catalog);
    }
    if (selected.size !== this.selected.size || [...selected].some(catalog => !this.selected.has(catalog))) {
      this.selected = selected;
      this.publish();
      this.resets.fire();
      if (this.visibleLists > 0) for (const catalog of selected) void catalog.refresh().catch(error => this.report(catalog, error));
    }
    this.publish();
  }

  /** 深链只打开原工作区的授权对象，不改变普通管理视图的 Gateway 选择。 */
  private async openAuthorizationTarget(selection: string): Promise<void> {
    if (workspaceContext.getSnapshot().loading) await workspaceContext.refresh();
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.error) throw new Error(snapshot.error);
    const workspace = requireGatewayWorkspaceRequestTarget(snapshot.workspaces, selection);
    const signal = this.authorizationController.signal;
    signal.throwIfAborted();
    const explicit = new URLSearchParams(location.search).get('gateway_connection_id') ?? undefined;
    let connection: string | undefined;
    if (workspace.gateway_id !== snapshot.gatewayId) {
      const directory = await gateway.request<GatewayManagementDirectoryDTO>('/api/gateway/management/gateways', { signal });
      signal.throwIfAborted();
      if (directory.local_gateway_id !== snapshot.gatewayId || workspaceContext.getSnapshot().gatewayId !== snapshot.gatewayId) throw new Error('MCP 授权入口不再属于原页面 Gateway');
      const matches = directory.items.filter(item => item.gateway_id === workspace.gateway_id
        && item.connection_kind === 'remote_gateway' && (!explicit || item.gateway_connection_id === explicit));
      if (matches.length !== 1 || !matches[0].gateway_connection_id) throw new Error('MCP 授权工作区存在多个或缺失的 Gateway 连接，请使用精确连接重新打开原对象');
      connection = matches[0].gateway_connection_id;
    } else if (explicit) throw new Error('本地 MCP 授权对象不能携带远端 Gateway 连接');
    const target = { gateway_id: workspace.gateway_id, workspace_id: workspace.workspace_id,
      backend_id: workspace.backend_id, gateway_connection_id: connection };
    const key = mcpCatalogKey(target);
    let scope = this.scopes.get(key);
    if (!scope) {
      scope = { catalog: new WorkspaceMcpCatalog(target, workspace.name, () => this.publish()), models: new Map() };
      this.scopes.set(key, scope);
    }
    this.authorizationCatalog = scope.catalog;
    await scope.catalog.refresh();
    await this.openAuthorizationCatalog(scope.catalog);
  }

  private async openAuthorizationCatalog(catalog: WorkspaceMcpCatalog): Promise<void> {
    if (catalog !== this.authorizationCatalog || this.authorizationOpening) return;
    this.authorizationController.signal.throwIfAborted();
    this.authorizationOpening = true;
    const cancellation = new CancellationTokenSource();
    const abort = () => cancellation.cancel();
    this.authorizationController.signal.addEventListener('abort', abort, { once: true });
    try {
      const candidates = catalog.snapshot.operations.filter(pendingMcpOperation)
        .map(operation => ({ operation, server: this.scopes.get(catalog.key)?.models.get(operation.server_id) }))
        .filter(item => item.server !== undefined);
      if (!candidates.length) {
        this.authorizationCatalog = undefined;
        throw new Error('原工作区没有待继续的 MCP 连接操作，请查看实际工作区状态');
      }
      const selected = candidates.length === 1 ? candidates[0] : (await this.quickInput.pick(candidates.map(item => ({
        label: item.server!.name, description: item.operation.operation_id, item,
      })), { title: '选择原工作区的 MCP 连接操作', placeHolder: `${catalog.label} · ${catalog.target.gateway_id}` }, cancellation.token))?.item;
      this.authorizationController.signal.throwIfAborted();
      if (selected?.server) await this.open(selected.server);
      this.authorizationCatalog = undefined;
    } finally {
      this.authorizationController.signal.removeEventListener('abort', abort); cancellation.dispose(); this.authorizationOpening = false;
    }
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

  async queryLocal(options?: { refresh?: boolean }): Promise<WorkspaceMcpServer[]> {
    if (workspaceContext.getSnapshot().loading) await workspaceContext.refresh();
    this.synchronizeTargets();
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.error) throw new Error(snapshot.error);
    if (this.selectionError) throw this.selectionError;
    if (!snapshot.gatewayId) throw new Error('Gateway 身份尚未完成初始化，无法读取 MCP 服务');
    const catalogs = [...this.selected];
    await Promise.all(catalogs.map(catalog => catalog.refresh(options)));
    if (catalogs.length !== this.selected.size || catalogs.some(catalog => !this.selected.has(catalog))) throw new CancellationError();
    return this.servers.get().filter(server => catalogs.includes(server.catalog));
  }

  /** 快速访问只打开明确工作区的原对象，不改管理视图的目标或启动连接。 */
  async queryWorkspace(target: McpWorkspaceRequestTarget): Promise<WorkspaceMcpServer[]> {
    this.synchronizeTargets();
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.loading || snapshot.error || !snapshot.gatewayId) throw new Error(snapshot.error ?? 'Gateway 归属尚未完成初始化，请重新查找 MCP 服务');
    const workspace = requireGatewayWorkspaceRequestTarget(snapshot.workspaces,
      { gatewayId: target.gateway_id, workspaceId: target.workspace_id });
    if (workspace.status !== 'ready' || workspace.backend_id !== target.backend_id) throw new Error('原 MCP 工作区已停止或 Backend 身份已改变，请重新查找');
    const key = mcpCatalogKey(target);
    let scope = this.scopes.get(key);
    if (!scope) {
      scope = { catalog: new WorkspaceMcpCatalog(target, workspace.name, () => this.publish()), models: new Map() };
      this.scopes.set(key, scope);
    }
    await scope.catalog.refresh();
    if (this.scopes.get(key) !== scope || workspaceContext.getSnapshot().gatewayId !== snapshot.gatewayId) throw new CancellationError();
    return this.servers.get().filter(server => server.catalog === scope.catalog);
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
  resetCaches(): void { void this.queryLocal({ refresh: true }).catch(error => this.notifications.error(error)); }
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
    @INotificationService notifications: INotificationService,
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
  canInstall(_server: IWorkbenchMcpServer): MarkdownString { return new MarkdownString().appendText('MCP 服务由目标插件包管理器安装和启用'); }
  async install(_server: IWorkbenchMcpServer): Promise<never> { throw unavailable(); }
  async uninstall(): Promise<never> { throw unavailable(); }
  async resolveEditableMcpServerConfiguration(): Promise<never> { throw unavailable(); }
  getMcpConfigPath(arg: IWorkbenchLocalMcpServer): undefined;
  getMcpConfigPath(arg: URI): Promise<undefined>;
  getMcpConfigPath(arg: IWorkbenchLocalMcpServer | URI): undefined | Promise<undefined> { return URI.isUri(arg) ? Promise.resolve(undefined) : undefined; }
  async open(server: IWorkbenchMcpServer | string, options?: IMcpServerEditorOptions): Promise<void> { await this.owner.open(server, options); }
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
