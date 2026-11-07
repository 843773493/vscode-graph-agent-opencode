import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IFileDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService, Severity } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IHostService } from '../../upstream/src/vs/workbench/services/host/browser/host.js';
import type { ExtensionInstallationInstallDTO, ExtensionTargetViewDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import type { WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gatewayWorkspaceKey, gatewayWorkspaceOwner, type WorkspaceOwner } from '../../../web/src/api/workspaceIdentity.js';
import { gateway } from '../agentApi.js';
import { extensionManagementTarget, managementGateway, refreshExtensionManagementTarget } from '../managementTarget.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource, workspaceFileUri } from '../workspaceFiles.js';

type InstallInput = { select: true } | { resources: URI[] | URI };

/** 安装域来自目标 Gateway；只有选择 VSIX 来源时才需要工作区，不读取窗口宿主视图。 */
export async function installTargetVsix(accessor: ServicesAccessor, input: InstallInput): Promise<void> {
  let resources = 'resources' in input ? (Array.isArray(input.resources) ? [...input.resources] : [input.resources]) : [];
  const quickInput = accessor.get(IQuickInputService), dialogs = accessor.get(IFileDialogService);
  const progress = accessor.get(IProgressService), notifications = accessor.get(INotificationService), host = accessor.get(IHostService);
  const initialGateway = workspaceContext.getSnapshot().gatewayId;
  if (!initialGateway) throw new Error('Gateway 身份尚未完成初始化，无法选择扩展安装目标');
  const selectedGateway = managementGateway('extensions');
  const controller = new AbortController(), cancellation = new CancellationTokenSource();
  let identityInvalidated = false;
  const invalidate = () => {
    identityInvalidated = true;
    controller.abort(new Error('Gateway 或用户身份已改变，请重新选择扩展安装目标'));
    cancellation.cancel();
  };
  const releaseUser = onGatewayUserSessionInvalidated(0, invalidate);
  const releaseContext = workspaceContext.subscribe(() => { if (workspaceContext.getSnapshot().gatewayId !== initialGateway) invalidate(); });
  const check = () => { controller.signal.throwIfAborted(); };
  const resolveSource = async (source: WorkspaceOwner, gatewayConnectionId: string | undefined, backendId: string) => {
    const target = { protocol_version: 1, gateway_id: source.gatewayId, workspace_ids: [source.workspaceId],
      gateway_connection_id: gatewayConnectionId, backend_id: backendId };
    const resolved = await gateway.request<ExtensionTargetViewDTO>('/api/gateway/extension-targets/resolve', {
      method: 'POST', body: JSON.stringify(target), signal: controller.signal,
    });
    check();
    if (resolved.protocol_version !== 1 || resolved.source !== 'live' || resolved.gateway_id !== source.gatewayId || !resolved.backend_id
      || resolved.backend_id !== backendId || JSON.stringify(resolved.workspace_ids) !== JSON.stringify(target.workspace_ids)) {
      throw new Error('VSIX 来源不属于本次选择的 Backend 安装域');
    }
    if (!resolved.capabilities?.installation_manage) throw new Error('所选目标未授权扩展安装管理');
    return { ...target, backend_id: resolved.backend_id };
  };
  const targets = new Map<string, Awaited<ReturnType<typeof resolveSource>>>();
  try {
    let selectedRoot: URI | undefined;
    // URI 只确定来源；整批准入后才读取冻结安装域，不能按来源改投目标。
    const readSources = () => resources.map(resource => {
      if (!URI.isUri(resource)) throw new Error('VSIX 安装需要明确的工作区文件 URI');
      if (resource.scheme !== 'boxteam-file') throw new Error('VSIX 来源必须是目标工作区的文件。请先将 VSIX 放入目标工作区；浏览器或中继机器的本机路径不能指定远端安装来源');
      const source = workspaceFileSource(resource);
      if (source.gatewayId !== selectedGateway.gateway_id) throw new Error('VSIX 来源不属于已选安装目标；当前不支持跨 Gateway 来源，请选择目标内文件或主动切换扩展管理目标');
      if (source.path === source.root || selectedRoot && workspaceFileUri(source, source.root, source.root).toString() !== selectedRoot.toString()) {
        throw new Error('VSIX 来源不属于本次选择的工作区文件根');
      }
      return source;
    });
    let sources = 'resources' in input ? readSources() : [];
    if ('resources' in input && !sources.length) return;
    const selected = await extensionManagementTarget(selectedGateway, controller.signal);
    check();
    const backendId = selected.backend_id;
    if ('select' in input) {
      const routes = await gateway.workspaces(false, controller.signal);
      check();
      const workspaces = routes.items.filter(workspace => workspace.gateway_id === selected.gateway_id);
      if (!workspaces.length) throw new Error('安装域已确定；当前 VSIX 文件来源需要该目标的已登记工作区，请先登记来源目录');
      const source = await quickInput.pick(workspaces.map(workspace => ({
        label: workspace.name, description: workspace.status, detail: workspace.workspace_id, workspace,
      })), { title: `选择 VSIX 来源工作区 · ${selected.name}`, placeHolder: '只选择来源文件范围，不改变已确定的安装域，也无需挂接窗口工作区' }, cancellation.token);
      check();
      if (!source) return;
      const owner = gatewayWorkspaceOwner(source.workspace);
      targets.set(gatewayWorkspaceKey(owner), await resolveSource(owner, selected.gateway_connection_id, backendId));
      const roots = await gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots', { signal: controller.signal }, gatewayWorkspaceKey(source.workspace));
      check();
      selectedRoot = workspaceFileUri(owner, roots.workspace_uri, roots.workspace_uri);
      resources = await dialogs.showOpenDialog({ title: `从目标工作区安装 VSIX · ${selected.name}`, defaultUri: selectedRoot,
        availableFileSystems: ['boxteam-file'], filters: [{ name: 'VSIX Extensions', extensions: ['vsix'] }],
        canSelectFiles: true, canSelectFolders: false, canSelectMany: true, openLabel: '安装' }) ?? [];
      check();
      sources = readSources();
    }
    check();
    if (!resources.length) return;
    for (const source of sources) {
      const key = gatewayWorkspaceKey(source);
      if (!targets.has(key)) targets.set(key, await resolveSource(source, selected.gateway_connection_id, backendId));
    }
    const receipts: string[] = [];
    try {
      await progress.withProgress({ location: ProgressLocation.Notification, title: '安装目标 VSIX' }, async report => {
        for (const source of sources) {
          check();
          const target = targets.get(gatewayWorkspaceKey(source))!;
          const label = `${target.gateway_id} · ${target.backend_id} · ${source.workspaceId}`;
          report.report({ message: `${label} · ${source.path}` });
          let result: ExtensionInstallationInstallDTO;
          try {
            result = await gateway.request<ExtensionInstallationInstallDTO>('/api/gateway/extension-installations/install', {
              method: 'POST', body: JSON.stringify({ ...target, source_uri: source.path }), signal: controller.signal,
            });
          } catch (error) {
            throw new Error(`目标 ${label} 未取得成功安装回执，请先查询原目标库存核实后再决定重试：${error instanceof Error ? error.message : String(error)}`, { cause: error });
          }
          if (result.protocol_version !== 1 || result.gateway_id !== target.gateway_id || result.backend_id !== target.backend_id
            || JSON.stringify(result.workspace_ids) !== JSON.stringify(target.workspace_ids) || result.profile_scope !== 'default'
            || !result.installed?.id || !result.installed.version || !result.inventory?.profile_id
            || result.installed.profile_id !== result.inventory.profile_id || typeof result.installed.reused !== 'boolean') {
            throw new Error(`目标 ${label} 的安装回执不匹配，请先查询原目标库存，不要自动重试`);
          }
          const outcome = result.installed.reused ? '复用已有版本，VSIX 正文未覆盖' : '已安装新制品';
          receipts.push(`${result.installed.id}@${result.installed.version} → ${label}（${result.installed.profile_id}；${outcome}）`);
          refreshExtensionManagementTarget({ gatewayId: target.gateway_id, backendId: target.backend_id });
          check();
        }
      });
    } finally {
      if (receipts.length && !identityInvalidated) notifications.prompt(Severity.Info,
        `安装结果：${receipts.join('；')}。重新加载后使用原宿主选择规则；当前运行版本尚未切换。`,
        [{ label: '重新加载窗口', run: () => host.reload() }]);
    }
  } finally { releaseUser(); releaseContext(); cancellation.dispose(); controller.abort(); }
}
