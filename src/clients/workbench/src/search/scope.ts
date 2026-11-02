import { Emitter } from '../../upstream/src/vs/base/common/event.js';
import { DisposableStore, IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { isEqual, isEqualOrParent } from '../../upstream/src/vs/base/common/resources.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IWorkspaceContextService, IWorkspaceFolderData, IWorkspaceFoldersChangeEvent, IWorkspaceFoldersWillChangeEvent, Workspace, WorkspaceFolder, WorkbenchState } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import type { IFolderQuery } from '../../upstream/src/vs/workbench/services/search/common/search.js';
import type { WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceFileSource, workspaceFileUri } from '../workspaceFiles.js';

export interface SearchScope {
  version: 1;
  workspaces: readonly (WorkspaceOwner & { rootUri: string; label: string })[];
}

/** 仅投影这次搜索的目录，不改变整个 Workbench 的虚拟工作区或存储身份。 */
export class SearchWorkspaceContext implements IWorkspaceContextService {
  declare readonly _serviceBrand: undefined;
  private readonly lifetime = new DisposableStore();
  private readonly stateChanged = this.lifetime.add(new Emitter<WorkbenchState>());
  private readonly nameChanged = this.lifetime.add(new Emitter<void>());
  private readonly foldersChanging = this.lifetime.add(new Emitter<IWorkspaceFoldersWillChangeEvent>());
  private readonly foldersChanged = this.lifetime.add(new Emitter<IWorkspaceFoldersChangeEvent>());
  readonly onDidChangeWorkbenchState = this.stateChanged.event;
  readonly onDidChangeWorkspaceName = this.nameChanged.event;
  readonly onWillChangeWorkspaceFolders = this.foldersChanging.event;
  readonly onDidChangeWorkspaceFolders = this.foldersChanged.event;
  private workspace: Workspace;
  private transferred = false;
  private updateGeneration = 0;

  constructor(public scope: SearchScope = { version: 1, workspaces: [] }) {
    this.workspace = this.project(scope);
  }

  private project(scope: SearchScope): Workspace {
    if (scope.version !== 1) throw new Error('搜索范围版本不受支持');
    const folders = scope.workspaces.map((target, index) => {
      const uri = workspaceFileUri(target, target.rootUri, target.rootUri);
      return new WorkspaceFolder({ uri, name: target.label, index }, { name: target.label, path: uri.path });
    });
    return new Workspace(JSON.stringify(folders.map(folder => folder.uri.toString())), folders, true, null, () => false);
  }

  async updateScope(scope: SearchScope): Promise<void> {
    const generation = ++this.updateGeneration;
    if (!scope.workspaces.length) throw new Error('请先选择明确搜索工作区');
    const next = this.project(scope), previousState = this.getWorkbenchState();
    const changes = { added: next.folders, removed: this.workspace.folders, changed: [] };
    const waits: Promise<void>[] = [];
    this.foldersChanging.fire({ changes, fromCache: false, join: promise => waits.push(promise) });
    await Promise.all(waits);
    if (generation !== this.updateGeneration) return;
    this.scope = scope; this.workspace = next;
    this.foldersChanged.fire(changes);
    this.nameChanged.fire();
    if (previousState !== this.getWorkbenchState()) this.stateChanged.fire(this.getWorkbenchState());
  }

  getWorkspace() { return this.workspace; }
  async getCompleteWorkspace() { return this.workspace; }
  getWorkbenchState() { return this.workspace.folders.length === 0 ? WorkbenchState.EMPTY : this.workspace.folders.length === 1 ? WorkbenchState.FOLDER : WorkbenchState.WORKSPACE; }
  getWorkspaceFolder(resource: URI) { return this.workspace.getFolder(resource); }
  isInsideWorkspace(resource: URI) { return !!this.getWorkspaceFolder(resource); }
  isCurrentWorkspace(target: Parameters<IWorkspaceContextService['isCurrentWorkspace']>[0]) {
    return URI.isUri(target) ? this.workspace.folders.some(folder => isEqual(folder.uri, target)) : target.id === this.workspace.id;
  }
  hasWorkspaceData() { return this.workspace.folders.length > 0; }
  ownChild<T extends IDisposable>(child: T): T { return this.lifetime.add(child); }
  transferTo(model: { ownSearchContext(context: IDisposable): void }): void {
    model.ownSearchContext(this.lifetime);
    this.transferred = true;
  }
  dispose(): void { ++this.updateGeneration; if (!this.transferred) this.lifetime.dispose(); }
}

/** 原 QueryBuilder 的同一构建入口在读取模型／扩展文件之前约束输入。 */
export function scopeQueryOptions<T extends { extraFileResources?: URI[]; changedFileUris?: URI[] }>(
  context: IWorkspaceContextService, folders: (IWorkspaceFolderData | URI)[], options: T,
): T {
  if (!(context instanceof SearchWorkspaceContext)) return options;
  if (!context.scope.workspaces.length) throw new Error('请先选择明确搜索工作区');
  assertSearchFolders(context, folders.map(folder => ({ folder: URI.isUri(folder) ? folder : folder.uri })));
  return { ...options,
    extraFileResources: options.extraFileResources?.filter(resource => context.isInsideWorkspace(resource)),
    changedFileUris: options.changedFileUris?.filter(resource => context.isInsideWorkspace(resource)),
  };
}

export function assertSearchFolders(context: IWorkspaceContextService, folders: readonly IFolderQuery[]): void {
  if (!(context instanceof SearchWorkspaceContext)) return;
  if (folders.some(folder => !context.scope.workspaces.some(target => isEqualOrParent(folder.folder,
    workspaceFileUri(target, target.rootUri, target.rootUri))))) throw new Error('查询目录超出本次明确搜索范围');
}

export function scopedSearch(context: IWorkspaceContextService): boolean { return context instanceof SearchWorkspaceContext; }

/** 转交结果时让原搜索模型接管 child，关闭浮层不销毁 SearchView 正在使用的服务。 */
export function transferSearchContext(context: IWorkspaceContextService, model: { ownSearchContext(context: IDisposable): void }): void {
  if (context instanceof SearchWorkspaceContext) context.transferTo(model);
}

export function scopeFromQuery(query: { folderQueries: readonly IFolderQuery[] } | null): SearchScope {
  if (!query?.folderQueries.length) throw new Error('搜索结果缺少明确目录范围');
  const targets = new Map<string, SearchScope['workspaces'][number]>();
  for (const folder of query.folderQueries) {
    const source = workspaceFileSource(folder.folder);
    const target = { gatewayId: source.gatewayId, workspaceId: source.workspaceId, rootUri: source.root,
      label: folder.folderName ?? source.workspaceId };
    targets.set(JSON.stringify([target.gatewayId, target.workspaceId, target.rootUri]), target);
  }
  return { version: 1, workspaces: [...targets.values()] };
}
