import { CancellationTokenSource } from '../../upstream/src/vs/base/common/cancellation.js';
import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import { URI } from '../../upstream/src/vs/base/common/uri.js';
import { IFileDialogService } from '../../upstream/src/vs/platform/dialogs/common/dialogs.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { INotificationService, Severity } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IProgressService, ProgressLocation } from '../../upstream/src/vs/platform/progress/common/progress.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IWorkspaceContextService } from '../../upstream/src/vs/platform/workspace/common/workspace.js';
import { IExtensionService } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { IHostService } from '../../upstream/src/vs/workbench/services/host/browser/host.js';
import type { ExtensionInstallationInstallDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/public.js';
import { onGatewayUserSessionInvalidated } from '../../../web/src/api/http.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { workspaceFileSource } from '../workspaceFiles.js';

type InstallInput = { select: true } | { resources: URI[] | URI };

/** 两条原 VSIX action 共用目标准入和唯一安装 owner；安装不改变当前运行实例。 */
export async function installTargetVsix(accessor: ServicesAccessor, input: InstallInput): Promise<void> {
  let resources = 'resources' in input ? (Array.isArray(input.resources) ? [...input.resources] : [input.resources]) : [];
  const context = accessor.get(IWorkspaceContextService), quickInput = accessor.get(IQuickInputService);
  const dialogs = accessor.get(IFileDialogService), progress = accessor.get(IProgressService);
  const notifications = accessor.get(INotificationService), host = accessor.get(IHostService);
  const extensions = accessor.get(IExtensionService);
  const initialGateway = workspaceContext.getSnapshot().gatewayId;
  if (!initialGateway) throw new Error('Gateway 身份尚未完成初始化，无法选择扩展安装目标');
  const controller = new AbortController(), cancellation = new CancellationTokenSource(), subscriptions = new DisposableStore();
  const invalidate = () => {
    controller.abort(new Error('Gateway、用户身份或原工作区根已改变，请重新选择扩展安装目标'));
    cancellation.cancel();
  };
  let identityInvalidated = false;
  const invalidateIdentity = () => { identityInvalidated = true; invalidate(); };
  const releaseUser = onGatewayUserSessionInvalidated(0, invalidateIdentity);
  const releaseContext = workspaceContext.subscribe(() => {
    if (workspaceContext.getSnapshot().gatewayId !== initialGateway) invalidateIdentity();
  });
  let requiredRoots: readonly URI[] = [];
  const rootsRemoved = () => requiredRoots.some(root => !context.getWorkspace().folders.some(folder => folder.uri.toString() === root.toString()));
  const check = () => {
    if (workspaceContext.getSnapshot().gatewayId !== initialGateway) invalidateIdentity();
    if (rootsRemoved()) invalidate();
    controller.signal.throwIfAborted();
  };
  subscriptions.add(context.onDidChangeWorkspaceFolders(() => { if (rootsRemoved()) invalidate(); }));
  try {
    // TODO(1.4/4.3)：原窗口扫描仍要求宿主管理权限；仅安装权限的初始化须迁齐原目标发现 owner。
    // 本入口目前只消费已经建立的明确视图，不能据此声称两权限已在窗口初始化中完全解耦。
    await extensions.whenInstalledExtensionsRegistered();
    const workspace = await context.getCompleteWorkspace();
    check();
    // 只读原运行位置 owner 的安装目标与完整视图；安装权限不依赖宿主发现请求。
    const views = extensions.getExtensionWorkspaceViews();
    const roots = workspace.folders.filter(folder => folder.uri.scheme === 'boxteam-file').map(folder => {
      const source = workspaceFileSource(folder.uri);
      if (source.path !== source.root) throw new Error('扩展安装只能使用明确挂接的工作区文件根');
      return { folder, source };
    });
    const targetFor = (source: ReturnType<typeof workspaceFileSource>) => {
      const view = views.find(view => view.installationTarget.gatewayId === source.gatewayId && view.workspaceIds.includes(source.workspaceId));
      if (!view) throw new Error('工作区尚无明确 Backend 安装视图，请重新加载目标工作区');
      const target = { protocol_version: 1, gateway_id: view.installationTarget.gatewayId,
        backend_id: view.installationTarget.backendId, workspace_ids: [...view.workspaceIds] };
      const viewRoots = target.workspace_ids.map(id => {
        const root = roots.find(({ source }) => source.gatewayId === target.gateway_id && source.workspaceId === id);
        if (!root) throw new Error('原扩展安装视图的工作区集合已改变，请重新加载窗口');
        return root.folder.uri;
      });
      return { target, viewRoots };
    };
    let selectedRoot: URI | undefined;
    if ('select' in input) {
      if (!roots.length) throw new Error('当前窗口未挂接可安装 VSIX 的工作区，请先打开目标工作区文件根');
      const choices = roots.map(({ folder, source }) => {
        const target = targetFor(source);
        return { ...target, root: folder.uri, label: folder.name, description: `${source.gatewayId} · ${source.workspaceId}`,
          detail: `Backend ${target.target.backend_id} · 默认 profile · ${source.root}` };
      });
      const selected = await quickInput.pick(choices,
        { title: '选择 VSIX 来源工作区与安装目标', placeHolder: '安装到所选 Backend 默认 profile，影响其工作区视图；不会启动或切换工作区' }, cancellation.token);
      check();
      if (!selected) return;
      selectedRoot = selected.root;
      requiredRoots = selected.viewRoots;
      check();
      // 目标在打开原文件选择器前已冻结，目录读取失败不能改投其它目标。
      resources = await dialogs.showOpenDialog({ title: '从目标工作区安装 VSIX', defaultUri: selectedRoot,
        availableFileSystems: ['boxteam-file'], filters: [{ name: 'VSIX Extensions', extensions: ['vsix'] }],
        canSelectFiles: true, canSelectFolders: false, canSelectMany: true, openLabel: '安装' }) ?? [];
    }
    check();
    if (!resources.length) return;
    // 整批核来源与目标；非法后项不能在合法前项安装后才被发现。
    const sources = resources.map(resource => {
      if (!URI.isUri(resource)) throw new Error('VSIX 安装需要明确的工作区文件 URI');
      if (resource.scheme !== 'boxteam-file') throw new Error('VSIX 来源必须是已挂接目标工作区的文件。请先将 VSIX 放入目标工作区，再从目标文件入口安装；浏览器或中继机器的本机路径不能指定远端安装来源');
      const source = workspaceFileSource(resource);
      const attached = roots.find(({ source: root }) => root.gatewayId === source.gatewayId
        && root.workspaceId === source.workspaceId && root.root === source.root);
      if (!attached || source.path === source.root || selectedRoot && attached.folder.uri.toString() !== selectedRoot.toString()) {
        throw new Error('VSIX 来源不属于本次选择的已挂接工作区文件根');
      }
      return { ...source, ...targetFor(source) };
    });
    requiredRoots = sources.flatMap(source => source.viewRoots);
    check();
    const receipts: string[] = [];
    try {
      await progress.withProgress({ location: ProgressLocation.Notification, title: '安装目标 VSIX' }, async report => {
        for (const source of sources) {
          check();
          const target = source.target;
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
            || result.installed.profile_id !== result.inventory.profile_id) {
            throw new Error(`目标 ${label} 的安装回执不匹配，请先查询原目标库存，不要自动重试`);
          }
          receipts.push(`${result.installed.id}@${result.installed.version} → ${label}（${result.installed.profile_id}）`);
          check();
        }
      });
    } finally {
      // 中途失败不回滚已完成安装；仅发布已核实回执，保留原错误和未执行后项。
      if (receipts.length && !identityInvalidated) notifications.prompt(Severity.Info,
        `已安装：${receipts.join('；')}。重新加载后使用原宿主选择规则；当前运行版本尚未切换。`,
        [{ label: '重新加载窗口', run: () => host.reload() }]);
    }
  } finally {
    releaseUser(); releaseContext(); subscriptions.dispose(); cancellation.dispose(); controller.abort();
  }
}
