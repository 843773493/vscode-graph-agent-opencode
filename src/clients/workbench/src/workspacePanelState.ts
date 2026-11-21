import { Disposable } from '../upstream/src/vs/base/common/lifecycle.js';
import { IStorageService, StorageScope, StorageTarget } from '../upstream/src/vs/platform/storage/common/storage.js';
import { INotificationService } from '../upstream/src/vs/platform/notification/common/notification.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { registerWorkbenchContribution2, WorkbenchPhase } from '../upstream/src/vs/workbench/common/contributions.js';
import { t } from '../../shared/core/i18n/index.js';
import type { WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget } from '../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from './workspaceContext.js';
import { managementGateway, managementWorkspace, onDidChangePanelManagementTarget, selectManagementTarget, type ManagementTarget } from './managementTarget.js';

interface PanelSelection { target: ManagementTarget; workspaceId?: string }

/** 显式资源定位只改变面板自己的范围，不激活导航工作区。 */
export function selectPanelWorkspace(owner: WorkspaceOwner): void {
  const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, owner);
  const target = managementGateway('panel', workspace.gateway_id);
  if (workspace.connection_kind === 'remote_gateway') {
    const connections = workspace.remote_gateways.filter(item => item.gateway_id === workspace.gateway_id);
    if (connections.length !== 1) throw new Error(t('workbench.resource.gatewayNotLoaded'));
    target.gateway_connection_id = connections[0].gateway_connection_id;
  }
  selectManagementTarget('panel', target, workspace.workspace_id);
}

/** 只持久化面板的范围选择；显隐、标签、尺寸沿用原生 Workbench 布局存储。 */
export class WorkspacePanelState extends Disposable {
  static readonly ID = 'boxteam.workspacePanelState';
  private entry?: string;
  private restoring = false;
  private openMcpAfterRestore = Boolean(new URLSearchParams(location.search).get('mcp_workspace'));

  constructor(
    @IStorageService private readonly storage: IStorageService,
    @INotificationService private readonly notifications: INotificationService,
    @IViewsService private readonly views: IViewsService,
  ) {
    super();
    this._register({ dispose: workspaceContext.subscribe(() => this.restore()) });
    this._register(onDidChangePanelManagementTarget(() => this.save()));
    this.restore();
  }

  private key(): string { return `boxteam.panel.selection.${this.entry}`; }
  private restore(): void {
    const snapshot = workspaceContext.getSnapshot();
    if (snapshot.loading || snapshot.error || !snapshot.gatewayId || this.entry === snapshot.gatewayId) return;
    this.entry = snapshot.gatewayId;
    this.restoring = true;
    try {
      const saved = this.storage.getObject<PanelSelection>(this.key(), StorageScope.WORKSPACE);
      if (saved) selectManagementTarget('panel', saved.target, saved.workspaceId);
    } catch (cause) {
      this.notifications.error(t('workbench.panel.restoreFailed', String(cause)));
    } finally { this.restoring = false; }
    if (this.openMcpAfterRestore) {
      this.openMcpAfterRestore = false;
      const explicit = new URLSearchParams(location.search).get('mcp_workspace')!;
      try { selectPanelWorkspace(gatewayWorkspaceOwner(requireGatewayWorkspaceRequestTarget(snapshot.workspaces, explicit))); }
      catch (cause) { this.notifications.error(String(cause)); }
      void this.views.openView('boxteam.mcp.view', true).catch((cause: unknown) => this.notifications.error(String(cause)));
    }
  }

  private save(): void {
    if (this.restoring || !this.entry || this.entry !== workspaceContext.getSnapshot().gatewayId) return;
    const target = managementGateway('panel');
    const workspace = workspaceContext.getSnapshot().workspaces.some(item => item.gateway_id === target.gateway_id)
      ? managementWorkspace('panel', target) : undefined;
    this.storage.store(this.key(), { target, workspaceId: workspace?.workspace_id }, StorageScope.WORKSPACE, StorageTarget.MACHINE);
  }
}

registerWorkbenchContribution2(WorkspacePanelState.ID, WorkspacePanelState, WorkbenchPhase.BlockRestore);
