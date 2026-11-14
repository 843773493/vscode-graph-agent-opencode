import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { t } from '../../shared/core/i18n/index.js';
import actionTitles from '../../shared/core/i18n/actionTitles.en.json';
import { SyncDescriptor } from '../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { Extensions as ViewExtensions, IViewsRegistry, IViewContainersRegistry, ViewContainerLocation } from '../upstream/src/vs/workbench/common/views.js';
import { VIEW_CONTAINER } from '../upstream/src/vs/workbench/contrib/files/browser/explorerViewlet.js';
import { SessionNavigation } from './sessionNavigation.js';
import { ViewPaneContainer } from '../upstream/src/vs/workbench/browser/parts/views/viewPaneContainer.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { WorkspacePanel } from './workspacePanel.js';
import { registerAction2, Action2 } from '../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IViewsService } from '../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { workspaceContext } from './workspaceContext.js';
import { requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../web/src/api/workspaceIdentity.js';
import { createManagementTargetLifecycle } from './managementTarget.js';

registerAction2(class RevealAgentTools extends Action2 {
  constructor() { super({ id: 'boxteam.tools.revealAgent', title: { value: 'Reveal Agent Tools', original: 'Reveal Agent Tools' }, f1: false }); }
  async run(accessor: ServicesAccessor, payload: { gateway_id: string; workspace_id: string; gateway_connection_id?: string; agent_id: string }): Promise<void> {
    const views = accessor.get(IViewsService);
    if (!payload?.gateway_id || !payload.workspace_id || !payload.agent_id?.trim()) throw new Error('Agent 工具定位缺少目标工作区或 Agent ID');
    const request = { ...payload };
    const lifecycle = createManagementTargetLifecycle('Gateway 身份已变化，无法打开 Agent 工具');
    const owner: WorkspaceOwner = { gatewayId: request.gateway_id, workspaceId: request.workspace_id };
    const resolveTarget = () => {
      lifecycle.check();
      const current = workspaceContext.getSnapshot();
      if (current.loading) throw new Error('Gateway 工作区列表尚未就绪');
      if (current.error) throw new Error(current.error);
      const target = requireGatewayWorkspaceRequestTarget(current.workspaces, owner);
      if (target.status !== 'ready') throw new Error(`目标工作区不可用：${target.status}`);
      const connections = target.remote_gateways.filter(connection => connection.gateway_id === target.gateway_id);
      if (target.connection_kind === 'remote_gateway' && connections.length !== 1) throw new Error('目标 Gateway 连接未唯一登记');
      const connectionId = target.connection_kind === 'remote_gateway' ? connections[0].gateway_connection_id : undefined;
      if (connectionId !== request.gateway_connection_id) throw new Error('目标 Gateway 连接已变化');
      return { ...target, gateway_connection_id: connectionId };
    };
    try {
      lifecycle.check();
      await workspaceContext.ensureReady();
      resolveTarget();
      const view = await views.openView<WorkspacePanel>('boxteam.tools.view', true);
      if (!view) throw new Error('工作区工具面板不可用');
      view.revealAgentTools(resolveTarget(), request.agent_id);
    } finally {
      lifecycle.dispose();
    }
  }
});
import { McpServersListView } from '../upstream/src/vs/workbench/contrib/mcp/browser/mcpServersView.js';
import './workspacePanelState.js';
import './workspaceIdentityRecovery.js';
import './search/searchService.js';
import './settings/commands.js';

const views = Registry.as<IViewsRegistry>(ViewExtensions.ViewsRegistry);
// Agent 入口用会话树替换文件树；文件保留在对应会话编辑器的资源面板中。
const replaceFileViews = () => {
  const fileViews = views.getViews(VIEW_CONTAINER).filter(view => ['workbench.explorer.fileView', 'workbench.explorer.emptyView'].includes(view.id));
  if (fileViews.length) views.deregisterViews(fileViews, VIEW_CONTAINER);
};
replaceFileViews();
views.onViewsRegistered(events => { if (events.some(event => event.viewContainer.id === VIEW_CONTAINER.id)) queueMicrotask(replaceFileViews); });
views.registerViews([{
  id: 'boxteam.sessions', name: { value: t('workbench.view.sessions'), original: actionTitles['workbench.view.sessions'] }, ctorDescriptor: new SyncDescriptor(SessionNavigation),
  order: 0, canToggleVisibility: true, canMoveView: true, collapsed: false,
}], VIEW_CONTAINER);

const automation = Registry.as<IViewContainersRegistry>(ViewExtensions.ViewContainersRegistry).registerViewContainer({
  id: 'boxteam.automation', title: { value: t('workbench.view.automation'), original: actionTitles['workbench.view.automation'] }, icon: Codicon.gear,
  ctorDescriptor: new SyncDescriptor(ViewPaneContainer, ['boxteam.automation', { mergeViewWithContainerWhenSingleView: true }]),
  storageId: 'boxteam.automation', order: 6, hideIfEmpty: true,
}, ViewContainerLocation.Panel);
views.registerViews([{
  id: 'boxteam.automation.tasks', name: { value: t('workbench.view.automation'), original: actionTitles['workbench.view.automation'] },
  canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
  openCommandActionDescriptor: { id: 'boxteam.automation.focus', title: { value: t('workbench.view.openAutomation'), original: actionTitles['workbench.view.openAutomation'] } },
}], automation);

// 保留原版 Terminal/Output 视图和服务，在同一原生容器内贡献工作区业务内容。
const containers = Registry.as<IViewContainersRegistry>(ViewExtensions.ViewContainersRegistry);
for (const [kind, containerId, titleKey, openKey] of [
  ['terminal', 'terminal', 'workbench.view.workspaceTerminal', 'workbench.view.openWorkspaceTerminal'],
  ['output', 'workbench.panel.output', 'workbench.view.workspaceOutput', 'workbench.view.openWorkspaceOutput'],
] as const) {
  const container = containers.get(containerId);
  if (!container) throw new Error(t('workbench.view.upstreamPanelMissing', containerId));
  const original = views.getView(containerId);
  if (original) {
    // 初次进入 Agent 工作台以工作区内容为主；原版视图仍可由其命令和视图菜单打开。
    views.deregisterViews([original], container);
    views.registerViews([{ ...original, hideByDefault: true }], container);
  }
  views.registerViews([{
    id: `boxteam.workspace.${kind}`, name: { value: t(titleKey), original: actionTitles[titleKey] }, order: -1,
    canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
    openCommandActionDescriptor: { id: `boxteam.${kind}.focus`, title: { value: t(openKey), original: actionTitles[openKey] } },
  }], container);
}
for (const [kind, titleKey, openKey, icon, order] of [
  ['ports', 'workbench.view.ports', 'workbench.view.openPorts', Codicon.serverEnvironment, 4],
  ['mcp', 'workbench.view.mcp', 'workbench.view.openMcp', Codicon.plug, 7],
  ['browser', 'workbench.view.browser', 'workbench.view.openBrowser', Codicon.browser, 5],
] as const) {
  const id = `boxteam.${kind}`;
  const container = containers.registerViewContainer({
    id, title: { value: t(titleKey), original: actionTitles[titleKey] }, icon, order,
    ctorDescriptor: new SyncDescriptor(ViewPaneContainer, [id, { mergeViewWithContainerWhenSingleView: true }]),
    storageId: id, hideIfEmpty: true,
  }, ViewContainerLocation.Panel);
  views.registerViews([{
    id: `${id}.view`, name: { value: t(titleKey), original: actionTitles[titleKey] },
    canToggleVisibility: true, canMoveView: true,
    ctorDescriptor: kind === 'mcp' ? new SyncDescriptor(McpServersListView) : new SyncDescriptor(WorkspacePanel),
    openCommandActionDescriptor: { id: `${id}.focus`, title: { value: t(openKey), original: actionTitles[openKey] } },
  }], container);
}
const toolsContainerId = 'boxteam.tools';
const toolsContainer = containers.registerViewContainer({
  id: toolsContainerId, title: { value: t('workbench.view.tools'), original: actionTitles['workbench.view.tools'] },
  icon: Codicon.tools, order: 8, ctorDescriptor: new SyncDescriptor(ViewPaneContainer, [toolsContainerId, { mergeViewWithContainerWhenSingleView: true }]),
  storageId: toolsContainerId, hideIfEmpty: true,
}, ViewContainerLocation.Panel);
views.registerViews([{
  id: 'boxteam.tools.view', name: { value: t('workbench.view.tools'), original: actionTitles['workbench.view.tools'] },
  canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
  openCommandActionDescriptor: { id: 'boxteam.tools.focus', title: { value: t('workbench.view.openTools'), original: actionTitles['workbench.view.openTools'] } },
}],
toolsContainer);
