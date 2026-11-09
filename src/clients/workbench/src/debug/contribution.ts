import { DisposableStore } from '../../upstream/src/vs/base/common/lifecycle.js';
import type { IJSONSchema } from '../../upstream/src/vs/base/common/jsonSchema.js';
import { Action2, registerAction2 } from '../../upstream/src/vs/platform/actions/common/actions.js';
import type { ServicesAccessor } from '../../upstream/src/vs/platform/instantiation/common/instantiation.js';
import { IQuickInputService } from '../../upstream/src/vs/platform/quickinput/common/quickInput.js';
import { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { IDebugService, type IDebugAdapterFactory } from '../../upstream/src/vs/workbench/contrib/debug/common/debug.js';
import type { NodeDebugConfigurationDTO, NodeDebugDebuggerContributionsDTO, NodeDebugStartRequest, WorkspaceFileRootsDTO } from '../../../web/src/types/protocol_generated/boxteam/workspace/v2/public.js';
import { onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import { gateway, type ThreadOwner } from '../agentApi.js';
import { ThreadPane } from '../threadPane.js';
import { PluginDebugAdapter, launchContext, type PluginDebugConfiguration } from './adapter.js';

/** 所有逻辑类型复用此 dispatcher，目标只来自原 session 的冻结启动上下文。 */
const adapterFactory: IDebugAdapterFactory = {
  createDebugAdapter: session => new PluginDebugAdapter(session),
  async substituteVariables(_folder, config) { launchContext(config); return config; },
  async runInTerminal() { throw new Error('当前调试器未声明 runInTerminal 能力'); },
};

registerAction2(class StartPluginDebug extends Action2 {
  constructor() { super({ id: 'boxteam.debug.start', title: { value: '调试：在 Thread 中启动 Node 脚本…', original: 'Debug: Start Node Script in Thread…' }, f1: true }); }

  async run(accessor: ServicesAccessor): Promise<void> {
    const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService), debug = accessor.get(IDebugService);
    // 必须在第一个 await 前捕获实际发起 Thread；其后不重新使用当前焦点。
    const active = editors.activeEditorPane;
    const threads = editors.visibleEditorPanes.filter(pane => pane instanceof ThreadPane)
      .flatMap(pane => { const target = pane.threadTarget; return target ? [{ pane, target, owner: Object.freeze({ ...target.input.owner }) }] : []; });
    const originating = active instanceof ThreadPane ? threads.find(item => item.pane === active) : undefined;
    if (active instanceof ThreadPane && !originating) throw new Error('发起 Thread 尚未就绪');
    if (!threads.length) throw new Error('请先打开调试所属的 Thread');
    let invalidated = false;
    const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    try {
      const selected = originating ?? await quick.pick(threads.map(item => ({ ...item, label: item.owner.title,
        description: `${item.owner.gatewayId} · ${item.owner.workspaceId}`, detail: `${item.owner.sessionId} / ${item.owner.threadId}` })), { title: '选择调试所属的 Thread' });
      if (!selected) return;
      const owner: Readonly<ThreadOwner> = selected.owner;
      const assertOwner = () => {
        if (invalidated || selected.pane.threadTarget?.conversation !== selected.target.conversation) throw new Error('发起 Thread 或 Gateway 凭据已失效，请重新启动调试');
      };
      assertOwner();
      const published = await gateway.request<NodeDebugDebuggerContributionsDTO>(`/api/v1/debug/node/debugger-contributions?workspace_id=${encodeURIComponent(owner.workspaceId)}`, {}, owner.workspaceKey);
      if (published.gateway_id !== owner.gatewayId || published.workspace_id !== owner.workspaceId) throw new Error('调试声明响应目标与发起 Thread 不一致');
      if (!published.debuggers.length) throw new Error('目标工作区没有已就绪的调试插件；请先在目标插件管理中启用');
      const chosen = published.debuggers.length === 1 ? published.debuggers[0] : (await quick.pick(published.debuggers.map(binding => ({ binding,
        label: binding.label, description: `${binding.package_id}/${binding.plugin_id} · ${binding.package_version}` })), { title: '选择此目标的调试器' }))?.binding;
      if (!chosen) return;
      // 在异步配置读取、选择或变量解析前冻结完整声明及目标。
      const binding = Object.freeze(structuredClone(chosen));
      if (binding.gateway_id !== owner.gatewayId || binding.workspace_id !== owner.workspaceId) throw new Error('调试器声明不属于发起 Thread');
      assertOwner();
      const query = new URLSearchParams({ session_id: owner.sessionId, thread_id: owner.threadId });
      const [configurations, roots] = await Promise.all([
        gateway.request<NodeDebugConfigurationDTO[]>(`/api/v1/debug/node/configurations?${query}`, {}, owner.workspaceKey),
        gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots', {}, owner.workspaceKey),
      ]);
      const picked = await quick.pick([
        ...configurations.map(configuration => ({ label: configuration.name, description: configuration.script_path, configuration })),
        { label: '输入目标脚本路径…', description: '由目标工作区解释相对路径、绝对路径或资源 URI', configuration: undefined },
      ], { title: `${binding.label} · ${owner.title}`, placeHolder: '使用此 Thread 的原调试方案或指定脚本' });
      if (!picked) return;
      const configuration = picked.configuration;
      const path = configuration?.script_path ?? await quick.input({ title: '目标 Node 脚本', prompt: `${owner.gatewayId} · ${owner.workspaceId}`, placeHolder: 'main.js / 目标机器绝对路径 / 资源 URI', validateInput: async value => value.trim() ? undefined : '请输入脚本路径' });
      if (!path) return;
      assertOwner();
      const start: NodeDebugStartRequest = { session_id: owner.sessionId, thread_id: owner.threadId, configuration_id: configuration?.configuration_id,
        path, working_directory: configuration?.working_directory, launch_profile_name: configuration?.launch_profile_name,
        args: configuration?.args ?? [], breakpoints: [], debugger_registration_id: binding.registration_id, defer_configuration_done: true };
      const config: PluginDebugConfiguration = { type: `boxteam-plugin:${binding.package_id}/${binding.plugin_id}/${binding.type}`, request: 'launch',
        name: `${configuration?.name ?? path} · ${owner.title}`, pluginDebuggerRegistration: binding.registration_id,
        pluginDebug: { launchId: crypto.randomUUID(), owner, debugger: binding, rootUris: [roots.workspace_uri, roots.filesystem_uri], start: Object.freeze(start) } };
      const resources = new DisposableStore();
      resources.add(debug.getAdapterManager().registerPluginDebugger(binding.registration_id, {
        type: config.type, label: binding.label, languages: ['javascript', 'javascriptreact'],
        configurationAttributes: { launch: structuredClone(binding.configuration_schema) as IJSONSchema },
      }, adapterFactory));
      resources.add(debug.onDidEndSession(event => {
        const ended = (event.session.configuration as Partial<PluginDebugConfiguration>).pluginDebug;
        if (ended?.launchId === config.pluginDebug.launchId && !event.restart) resources.dispose();
      }));
      try { if (!await debug.startDebugging(undefined, config)) resources.dispose(); }
      catch (error) { resources.dispose(); throw error; }
    } finally { releaseToken(); }
  }
});
