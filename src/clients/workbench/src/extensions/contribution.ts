import { t } from '../../../shared/core/i18n/index.js';
import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IWorkspaceEditingService } from '../../upstream/src/vs/workbench/services/workspaces/common/workspaceEditing.js';
import { gatewayWorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { loadSearchScope } from '../search/target.js';
import { workspaceFileUri } from '../workspaceFiles.js';
import type { PluginTarget } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { manageServicePluginSources } from './servicePluginManagement.js';

registerAction2(class extends Action2 {
  constructor() { super({ id: 'boxteam.plugins.manageSources', title: { value: t('plugins.manageSources'), original: actionTitles['plugins.manageSources'] }, f1: true }); }
  run(accessor: ServicesAccessor, target?: PluginTarget): Promise<void> { return manageServicePluginSources(accessor, target); }
});

registerAction2(class AttachExtensionWorkspaceViews extends Action2 {
  constructor() { super({ id: 'boxteam.extensions.attachWorkspaceViews', title: { value: t('plugins.attachWorkspaceTitle'), original: actionTitles['plugins.attachWorkspaceTitle'] }, f1: true }); }
  async run(accessor: ServicesAccessor): Promise<void> {
    const quickInput = accessor.get(IQuickInputService), editing = accessor.get(IWorkspaceEditingService);
    const routes = await gateway.workspaces(false);
    const selected = await quickInput.pick(routes.items.map(workspace => ({ label: workspace.name,
      description: workspace.gateway_id + ' / ' + workspace.workspace_id + ' · ' + workspace.status, workspace,
    })), { canPickMany: true, placeHolder: t('plugins.attachWorkspacePlaceholder') });
    if (!selected?.length) return;
    const scope = await loadSearchScope(selected.map(item => gatewayWorkspaceOwner(item.workspace)), new AbortController().signal);
    const folders = scope.workspaces.map(workspace => ({
      uri: workspaceFileUri(workspace, workspace.rootUri, workspace.rootUri), name: workspace.label,
    }));
    // 原 owner 持久化工作区文件并在当前窗口跳转；额外 reload 会抢先刷新旧地址。
    await editing.createAndEnterWorkspace(folders);
  }
});
