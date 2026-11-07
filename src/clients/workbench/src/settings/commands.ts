import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { workspaceContext } from '../workspaceContext.js';
import { managementGateway } from '../managementTarget.js';
import { manageDefaultAgentSource } from './defaultAgent.js';

export const defaultAgentSourceCommand = 'boxteam.settings.defaultAgentSource';

registerAction2(class extends Action2 {
  constructor() {
    super({ id: defaultAgentSourceCommand, title: { value: '编辑目标工作区默认 Agent 来源', original: 'Edit Target Workspace Default Agent Source' },
      category: { value: 'BoxTeam 设置', original: 'BoxTeam Settings' }, f1: true });
  }
  async run(accessor: ServicesAccessor, explicit?: WorkspaceOwner): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), notifications = accessor.get(INotificationService);
    const initial = workspaceContext.getSnapshot();
    if (initial.loading || initial.error || !initial.gatewayId) throw new Error(initial.error ?? '工作区目标尚未完成初始化');
    const controller = new AbortController(), cancellation = new CancellationTokenSource();
    const invalidate = () => { controller.abort(new Error('Gateway 或用户身份已改变，请重新选择配置目标')); cancellation.cancel(); };
    const release = onGatewayUserSessionInvalidated(0, invalidate);
    const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate(); });
    let selected: WorkspaceOwner | undefined;
    const check = () => {
      if (workspaceContext.getSnapshot().gatewayId !== initial.gatewayId) invalidate();
      controller.signal.throwIfAborted();
      if (selected && requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected).status !== 'ready') {
        throw new Error('原配置目标已离线，请先显式连接或启动 Backend');
      }
    };
    try {
      const target = managementGateway('settings', explicit?.gatewayId);
      selected = explicit ? { ...explicit } : (await quickInput.pick(initial.workspaces.filter(workspace => workspace.gateway_id === target.gateway_id).map(workspace => ({
        label: workspace.name, description: `${workspace.gateway_id} · ${workspace.status}`, detail: workspace.workspace_id,
        owner: gatewayWorkspaceOwner(workspace),
      })), { title: '选择业务配置目标', prompt: `管理目标：${target.name} · ${target.gateway_id}`,
        placeHolder: '选择当前设置目标内的工作区来源；未就绪工作区需先显式启动' }, cancellation.token))?.owner;
      if (!selected) return;
      check();
      const workspace = requireGatewayWorkspaceRequestTarget(workspaceContext.getSnapshot().workspaces, selected);
      await manageDefaultAgentSource(workspace, workspace.name, quickInput, notifications, check, controller.signal);
    } finally { release(); releaseContext(); cancellation.dispose(); controller.abort(); }
  }
});
