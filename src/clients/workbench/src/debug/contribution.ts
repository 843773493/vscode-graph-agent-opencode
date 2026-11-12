import actionTitles from '../../../shared/core/i18n/actionTitles.en.json';
import { t } from '../../../shared/core/i18n/index.js';
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
  async runInTerminal() { throw new Error(t('workbench.debug.terminalUnavailable')); },
};

registerAction2(class StartPluginDebug extends Action2 {
  constructor() { super({ id: 'boxteam.debug.start', title: { value: t('workbench.debug.startScript'), original: actionTitles['workbench.debug.startScript'] }, f1: true }); }

  async run(accessor: ServicesAccessor): Promise<void> {
    const editors = accessor.get(IEditorService), quick = accessor.get(IQuickInputService), debug = accessor.get(IDebugService);
    // 必须在第一个 await 前捕获实际发起 Thread；其后不重新使用当前焦点。
    const active = editors.activeEditorPane;
    const threads = editors.visibleEditorPanes.filter(pane => pane instanceof ThreadPane)
      .flatMap(pane => { const target = pane.threadTarget; return target ? [{ pane, target, owner: Object.freeze({ ...target.input.owner }) }] : []; });
    const originating = active instanceof ThreadPane ? threads.find(item => item.pane === active) : undefined;
    if (active instanceof ThreadPane && !originating) throw new Error(t('workbench.debug.originNotReady'));
    if (!threads.length) throw new Error(t('workbench.debug.openThreadFirst'));
    let invalidated = false;
    const releaseToken = onGatewayTokenInvalidated(0, () => { invalidated = true; });
    try {
      const selected = originating ?? await quick.pick(threads.map(item => ({ ...item, label: item.owner.title,
        description: `${item.owner.gatewayId} · ${item.owner.workspaceId}`, detail: `${item.owner.sessionId} / ${item.owner.threadId}` })), { title: t('workbench.debug.chooseThread') });
      if (!selected) return;
      const owner: Readonly<ThreadOwner> = selected.owner;
      const assertOwner = () => {
        if (invalidated || selected.pane.threadTarget?.conversation !== selected.target.conversation) throw new Error(t('workbench.debug.originExpired'));
      };
      assertOwner();
      const published = await gateway.request<NodeDebugDebuggerContributionsDTO>(`/api/v1/debug/node/debugger-contributions?workspace_id=${encodeURIComponent(owner.workspaceId)}`, {}, owner.workspaceKey);
      if (published.gateway_id !== owner.gatewayId || published.workspace_id !== owner.workspaceId) throw new Error(t('workbench.debug.declarationOwnerMismatch'));
      if (!published.debuggers.length) throw new Error(t('workbench.debug.noReadyPlugin'));
      const chosen = published.debuggers.length === 1 ? published.debuggers[0] : (await quick.pick(published.debuggers.map(binding => ({ binding,
        label: binding.label, description: `${binding.package_id}/${binding.plugin_id} · ${binding.package_version}` })), { title: t('workbench.debug.chooseDebugger') }))?.binding;
      if (!chosen) return;
      // 在异步配置读取、选择或变量解析前冻结完整声明及目标。
      const binding = Object.freeze(structuredClone(chosen));
      if (binding.gateway_id !== owner.gatewayId || binding.workspace_id !== owner.workspaceId) throw new Error(t('workbench.debug.debuggerOwnerMismatch'));
      assertOwner();
      const query = new URLSearchParams({ session_id: owner.sessionId, thread_id: owner.threadId });
      const [configurations, roots] = await Promise.all([
        gateway.request<NodeDebugConfigurationDTO[]>(`/api/v1/debug/node/configurations?${query}`, {}, owner.workspaceKey),
        gateway.request<WorkspaceFileRootsDTO>('/api/v1/workspace/files/roots', {}, owner.workspaceKey),
      ]);
      const picked = await quick.pick([
        ...configurations.map(configuration => ({ label: configuration.name, description: configuration.script_path, configuration })),
        { label: t('workbench.debug.enterScript'), description: t('workbench.debug.scriptInputDescription'), configuration: undefined },
      ], { title: `${binding.label} · ${owner.title}`, placeHolder: t('workbench.debug.configurationHint') });
      if (!picked) return;
      const configuration = picked.configuration;
      const path = configuration?.script_path ?? await quick.input({ title: t('workbench.debug.targetScript'), prompt: `${owner.gatewayId} · ${owner.workspaceId}`, placeHolder: t('workbench.debug.scriptPathHint'), validateInput: async value => value.trim() ? undefined : t('workbench.debug.scriptPathRequired') });
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
