import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IViewsService } from '../../upstream/src/vs/workbench/services/views/common/viewsService.js';
import { EditorResourceAccessor, SideBySideEditor } from '../../upstream/src/vs/workbench/common/editor.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceKey, gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { listGatewayWorkspaces } from '../../../web/src/gatewayApi.js';
import { workspaceContext } from '../workspaceContext.js';
import type { SessionNavigation } from '../sessionNavigation.js';
import { captureSearchOwner } from './target.js';

export const workspaceActionCommands = {
  activate: 'boxteam.workspace.activate', reconnect: 'boxteam.workspace.reconnect', start: 'boxteam.workspace.start',
} as const;
export type WorkspaceAction = keyof typeof workspaceActionCommands;
export type WorkspaceActionHandlers = Record<WorkspaceAction, (workspaceKey: string) => Promise<void>>;

/** 三个命令只把冻结目标交给原导航 owner；确认、队列与权威回读仍由原 hooks 负责。 */
async function runWorkspaceAction(accessor: ServicesAccessor, action: WorkspaceAction, explicit?: WorkspaceOwner): Promise<void> {
  // ServiceAccessor 只在同步命令入口有效，异步阶段仅使用已取得的服务句柄。
  const editors = accessor.get(IEditorService), quickInput = accessor.get(IQuickInputService), views = accessor.get(IViewsService);
  const initial = workspaceContext.getSnapshot();
  if (!initial.gatewayId) throw new Error('Gateway 登记身份尚未加载，请稍后重试');
  let invalidated = false;
  const release = onGatewayTokenInvalidated(0, () => { invalidated = true; });
  const assertAccess = () => {
    if (invalidated || workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) throw new Error('Gateway 或访问凭据已改变，请重新选择工作区');
  };
  try {
    let owner = explicit;
    if (!owner) {
      if (initial.loading || initial.error) throw new Error(initial.error ?? '工作区登记信息尚未加载');
      const resource = EditorResourceAccessor.getCanonicalUri(editors.activeEditor, { supportSideBySide: SideBySideEditor.PRIMARY });
      if (resource?.scheme.startsWith('boxteam-') || initial.activeId !== null) owner = captureSearchOwner(editors);
      else {
        // 无明确发起目标时只让用户选择，不猜同名或最近使用的工作区。
        const selected = await quickInput.pick(initial.workspaces.map(workspace => ({
          label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
          owner: gatewayWorkspaceOwner(workspace),
        })), { title: '选择工作区动作目标', placeHolder: '浏览候选不会连接、启动或激活工作区' });
        if (!selected) return;
        owner = selected.owner;
      }
    }
    // 在任何 await 前复制稳定 owner，调用方随后改变活动工作区或参数对象都不能改投。
    const targetOwner = { ...owner };
    const latest = await listGatewayWorkspaces(0, { checkHealth: false });
    assertAccess();
    const target = requireGatewayWorkspaceRequestTarget(latest.items, targetOwner);
    if (action === 'start' && !target.managed) throw new Error('原工作区不是托管目标，不能启动 Backend');
    if (action === 'reconnect' && target.connection_kind !== 'remote_gateway') throw new Error('原工作区没有远程 Gateway 连接动作');
    const view = await views.openView<SessionNavigation>('boxteam.sessions', false);
    assertAccess();
    if (!view) throw new Error('工作区导航不可用');
    await view.runWorkspaceAction(action, gatewayWorkspaceKey(targetOwner), assertAccess);
  } finally { release(); }
}

for (const [action, title] of [
  ['activate', '切换工作区'], ['reconnect', '连接工作区 Gateway'], ['start', '启动工作区 Backend'],
] as const) registerAction2(class extends Action2 {
  constructor() { super({ id: workspaceActionCommands[action], title: { value: title, original: title }, f1: true }); }
  run(accessor: ServicesAccessor, owner?: WorkspaceOwner): Promise<void> { return runWorkspaceAction(accessor, action, owner); }
});
