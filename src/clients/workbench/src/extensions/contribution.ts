import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IWorkspaceEditingService } from '../../upstream/src/vs/workbench/services/workspaces/common/workspaceEditing.js';
import { gatewayWorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { loadSearchScope } from '../search/target.js';
import { workspaceFileUri } from '../workspaceFiles.js';

registerAction2(class AttachExtensionWorkspaceViews extends Action2 {
  constructor() { super({ id: 'boxteam.extensions.attachWorkspaceViews', title: { value: '挂接窗口工作区并重新载入', original: 'Attach Window Workspaces and Reload' }, f1: true }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), editing = accessor.get(IWorkspaceEditingService);
    const routes = await gateway.workspaces(false);
    const selected = await quickInput.pick(routes.items.map(workspace => ({ label: workspace.name,
      description: workspace.gateway_id + ' / ' + workspace.workspace_id + ' · ' + workspace.status, workspace,
    })), { canPickMany: true, placeHolder: '明确选择本窗口的工作区集合；接受后重新载入，内置语言服务使用这一完整范围' });
    if (!selected?.length) return;
    const scope = await loadSearchScope(selected.map(item => gatewayWorkspaceOwner(item.workspace)), new AbortController().signal);
    const folders = scope.workspaces.map(workspace => ({
      uri: workspaceFileUri(workspace, workspace.rootUri, workspace.rootUri), name: workspace.label,
    }));
    // 原 owner 持久化工作区文件并在当前窗口跳转；额外 reload 会抢先刷新旧地址。
    await editing.createAndEnterWorkspace(folders);
  }
});
