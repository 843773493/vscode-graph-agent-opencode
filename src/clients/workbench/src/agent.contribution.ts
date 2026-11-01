import { Registry } from '../upstream/src/vs/platform/registry/common/platform.js';
import { SyncDescriptor } from '../upstream/src/vs/platform/instantiation/common/descriptors.js';
import { Extensions as ViewExtensions, IViewsRegistry, IViewContainersRegistry, ViewContainerLocation } from '../upstream/src/vs/workbench/common/views.js';
import { VIEW_CONTAINER } from '../upstream/src/vs/workbench/contrib/files/browser/explorerViewlet.js';
import { SessionNavigation } from './sessionNavigation.js';
import { ViewPaneContainer } from '../upstream/src/vs/workbench/browser/parts/views/viewPaneContainer.js';
import { Codicon } from '../upstream/src/vs/base/common/codicons.js';
import { WorkspacePanel } from './workspacePanel.js';
import './workspacePanelState.js';

const views = Registry.as<IViewsRegistry>(ViewExtensions.ViewsRegistry);
// Agent 入口用会话树替换文件树；文件保留在对应会话编辑器的资源面板中。
const replaceFileViews = () => {
  const fileViews = views.getViews(VIEW_CONTAINER).filter(view => ['workbench.explorer.fileView', 'workbench.explorer.emptyView'].includes(view.id));
  if (fileViews.length) views.deregisterViews(fileViews, VIEW_CONTAINER);
};
replaceFileViews();
views.onViewsRegistered(events => { if (events.some(event => event.viewContainer.id === VIEW_CONTAINER.id)) queueMicrotask(replaceFileViews); });
views.registerViews([{
  id: 'boxteam.sessions', name: { value: '会话', original: 'Sessions' }, ctorDescriptor: new SyncDescriptor(SessionNavigation),
  order: 0, canToggleVisibility: true, canMoveView: true, collapsed: false,
}], VIEW_CONTAINER);

const automation = Registry.as<IViewContainersRegistry>(ViewExtensions.ViewContainersRegistry).registerViewContainer({
  id: 'boxteam.automation', title: { value: '自动化', original: 'Automation' }, icon: Codicon.gear,
  ctorDescriptor: new SyncDescriptor(ViewPaneContainer, ['boxteam.automation', { mergeViewWithContainerWhenSingleView: true }]),
  storageId: 'boxteam.automation', order: 6, hideIfEmpty: true,
}, ViewContainerLocation.Panel);
views.registerViews([{
  id: 'boxteam.automation.tasks', name: { value: '自动化', original: 'Automation' },
  canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
  openCommandActionDescriptor: { id: 'boxteam.automation.focus', title: { value: '打开自动化', original: 'Open Automation' } },
}], automation);

// 保留原版 Terminal/Output 视图和服务，在同一原生容器内贡献工作区业务内容。
const containers = Registry.as<IViewContainersRegistry>(ViewExtensions.ViewContainersRegistry);
for (const [kind, containerId, title] of [
  ['terminal', 'terminal', '工作区终端'], ['output', 'workbench.panel.output', '工作区输出'],
] as const) {
  const container = containers.get(containerId);
  if (!container) throw new Error(`原版面板未注册：${containerId}`);
  const original = views.getView(containerId);
  if (original) {
    // 初次进入 Agent 工作台以工作区内容为主；原版视图仍可由其命令和视图菜单打开。
    views.deregisterViews([original], container);
    views.registerViews([{ ...original, hideByDefault: true }], container);
  }
  views.registerViews([{
    id: `boxteam.workspace.${kind}`, name: { value: title, original: `Workspace ${kind}` }, order: -1,
    canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
    openCommandActionDescriptor: { id: `boxteam.${kind}.focus`, title: { value: `打开${title}`, original: `Open workspace ${kind}` } },
  }], container);
}
for (const [kind, title, icon, order] of [
  ['ports', '端口', Codicon.serverEnvironment, 4], ['mcp', 'MCP 连接', Codicon.plug, 7],
  ['browser', '浏览器', Codicon.browser, 5],
] as const) {
  const id = `boxteam.${kind}`;
  const container = containers.registerViewContainer({
    id, title: { value: title, original: title }, icon, order,
    ctorDescriptor: new SyncDescriptor(ViewPaneContainer, [id, { mergeViewWithContainerWhenSingleView: true }]),
    storageId: id, hideIfEmpty: true,
  }, ViewContainerLocation.Panel);
  views.registerViews([{
    id: `${id}.view`, name: { value: title, original: title },
    canToggleVisibility: true, canMoveView: true, ctorDescriptor: new SyncDescriptor(WorkspacePanel),
    openCommandActionDescriptor: { id: `${id}.focus`, title: { value: `打开${title}`, original: `Open ${kind}` } },
  }], container);
}
