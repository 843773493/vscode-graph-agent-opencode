import { extensionInstanceKey, installationTargetKey, type IExtensionWorkspaceView } from '../../upstream/src/vs/platform/extensions/common/extensions.js';
import { IProductService } from '../../upstream/src/vs/platform/product/common/productService.js';
import { ExtensionHostKind } from '../../upstream/src/vs/workbench/services/extensions/common/extensionHostKind.js';
import { IExtensionService, type IExtensionHost } from '../../upstream/src/vs/workbench/services/extensions/common/extensions.js';
import { registerHostCommand } from './commands.js';

export const extensionRuntimeDiagnosticsCommand = 'boxteam.extensions.getRuntimeDiagnostics';

interface ApiSupport {
  readonly id: string;
  readonly state: 'partial' | 'unsupported';
  readonly detail: string;
}

// 声明已接线的范围，不把类型、通道或单项通过当作整类 API 的支持证明。
const targetApiSupport: readonly ApiSupport[] = [
  { id: 'commands', state: 'partial', detail: '命令与激活按原宿主派发；完整菜单、快捷键及视图上下文仍待迁齐。' },
  { id: 'documentsLanguageSearch', state: 'partial', detail: '已有文档、语言、诊断和符号的目标投影；Notebook、其它编辑入口及完整搜索闭包仍待迁齐。' },
  { id: 'workspaceFiles', state: 'partial', detail: '工作区视图、文件读取、创建、保存及监听复用目标 owner；重命名、删除及完整原生路径往返仍待迁齐。' },
  { id: 'configuration', state: 'partial', detail: '配置按目标 profile/view/folder 接入原服务；完整多目标编辑和平台验收仍待完成。' },
  { id: 'terminal', state: 'unsupported', detail: '尚未接入目标终端服务；终端、shell、环境集合和 shell integration 明确拒绝。' },
  { id: 'tasks', state: 'unsupported', detail: '尚未接入目标任务执行服务；公开、废弃注册及内部任务入口明确拒绝。' },
  { id: 'debug', state: 'unsupported', detail: '尚未接入目标调试服务；调试、适配器和调试终端入口明确拒绝。' },
  { id: 'viewsWebviewsPorts', state: 'partial', detail: '静态贡献及已有资源通道可用；运行注册、Webview、端口和完整视图上下文仍待迁齐。' },
  { id: 'memento', state: 'partial', detail: 'globalState/workspaceState 已按目标和稳定视图隔离；完整多客户端与平台验收仍待完成。' },
  { id: 'secrets', state: 'unsupported', detail: '尚未接入目标秘密 owner；SecretStorage 在进入窗口秘密服务前明确拒绝。' },
  { id: 'stateSync', state: 'unsupported', detail: '目标跨设备同步尚未接通；setKeysForSync 明确拒绝。' },
  { id: 'dependenciesNativeModulesProposedApi', state: 'partial', detail: '依赖只解析合法目标实例；原生模块 ABI、提案授权及完整兼容矩阵仍待验证。' },
];

function sameView(host: IExtensionHost, view: IExtensionWorkspaceView): boolean {
  const location = host.runningLocation;
  return (location.kind === ExtensionHostKind.Remote || location.kind === ExtensionHostKind.LocalWebWorker)
    && !!location.installationTarget && installationTargetKey(location.installationTarget) === installationTargetKey(view.installationTarget)
    && location.viewId === view.viewId;
}

/** 每次读取原 owner 的当前快照；不发现目标、不启动宿主，也不缓存另一份运行事实。 */
export function extensionRuntimeDiagnostics(service: IExtensionService, product: Pick<IProductService, 'version' | 'commit'>, source?: IExtensionHost) {
  const hosts = service.getExtensionHosts();
  if (source && !hosts.includes(source)) throw new Error('发起能力查询的扩展宿主代际已经关闭');
  const views = service.getExtensionWorkspaceViews().filter(view => !source || sameView(source, view));
  const statuses = service.getExtensionsStatus();
  return {
    version: product.version, commit: product.commit, scope: source ? 'caller' as const : 'window' as const,
    apiSupport: targetApiSupport.map(entry => ({ ...entry })),
    targets: views.map(view => ({
      installationTarget: { ...view.installationTarget }, viewId: view.viewId, workspaceIds: [...view.workspaceIds],
      management: { ...view.capabilities },
      // 未请求 Node 发现与运行时缺失不同，不能用其中任一状态推断完整 API 可用。
      nodeChannel: view.nodeRuntime ? { state: view.nodeRuntime.channel_state, reason: view.nodeRuntime.reason ?? null,
        platform: view.nodeRuntime.platform, runtime: view.nodeRuntime.runtime ? { ...view.nodeRuntime.runtime } : null } : null,
      hosts: hosts.filter(host => sameView(host, view) && (!source || host === source)).map(host => {
        const context = host.targetContext;
        const binding = context?.connection;
        return {
          kind: host.runningLocation.kind === ExtensionHostKind.Remote ? 'node' as const : 'web' as const,
          phase: binding ? 'bound' as const : 'created' as const,
          binding: binding ? { clientId: binding.client_id, generation: binding.generation, requestId: binding.request_id } : null,
          processApi: context?.kind === 'node'
            ? { state: 'partial' as const, detail: '原 Node 代码在目标进程执行；不代表任务、调试或全部原生模块已可用。' }
            : host.runningLocation.kind === ExtensionHostKind.Remote
              ? { state: 'unavailable' as const, detail: 'Node 宿主尚未认证绑定，当前没有目标进程能力证明。' }
              : { state: 'unsupported' as const, detail: 'Web Worker 不提供目标 Node 进程 API。' },
          extensions: service.getHostExtensions(host).map(extension => {
            const status = statuses[extensionInstanceKey(extension)];
            if (!status) throw new Error('运行诊断缺少原实例状态：' + extension.identifier.value);
            return { id: extension.identifier.value, version: extension.version,
              activationStarted: status.activationStarted, activated: !!status.activationTimes,
              runtimeErrors: status.runtimeErrors.map(error => error.message) };
          }),
        };
      }),
    })),
  };
}

/** 原命令服务保留扩展调用来源；窗口查询不改变管理目标或发起运行操作。 */
export function registerExtensionRuntimeDiagnostics() {
  return registerHostCommand(extensionRuntimeDiagnosticsCommand, (accessor, _args, source) =>
    extensionRuntimeDiagnostics(accessor.get(IExtensionService), accessor.get(IProductService), source));
}
