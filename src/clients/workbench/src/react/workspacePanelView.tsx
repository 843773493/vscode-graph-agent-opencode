import { failureSummary } from "../../../shared/core/failurePresentation";
import { gatewayWorkspaceKey, type GatewayWorkspaceRequestTarget, type WorkspaceOwner } from "../../../web/src/api/workspaceIdentity.js";
import { t } from '../../../shared/core/i18n/index.js';
import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { listAgents } from '../../../web/src/api/workspace.js';
import type { GatewayWorkspace } from '../../../web/src/types/backend.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import type { IContextMenuService } from '../../upstream/src/vs/platform/contextview/browser/contextView.js';
import { toAction } from '../../upstream/src/vs/base/common/actions.js';
import { TerminalContent } from '../../../web/src/components/panels/bottomPanel/TerminalContent.js';
import { TerminalThemeContext } from '../../../web/src/components/panels/bottomPanel/TerminalOutput.js';
import type { IColorTheme } from '../../upstream/src/vs/platform/theme/common/themeService.js';
import { getTerminalTheme } from '../theme.js';
import { BrowserContent } from '../../../web/src/components/panels/bottomPanel/BrowserContent.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { useWorkspaceResources } from '../../../web/src/hooks/workspace/useWorkspaceResources.js';
import type { DispatchWorkspaceResourceAction } from '../../../web/src/hooks/workspace/resourceActions.js';
import type { WorkspaceResourceDTO } from '../../../web/src/api/workspaceResources.js';
import { watchPluginRuntimeStatus } from '../../../web/src/api/pluginRuntimeStatus.js';
import { DEFAULT_API_REQUEST_TIMEOUT_MS } from '../../../web/src/api/http.js';
import type { PluginRuntimeStatusDTO } from '../../../web/src/types/protocol_generated/boxteam/gateway/v2/service_plugin.js';
import { pluginStateLabel } from '../extensions/pluginScope.js';
import { resourceActionCommand } from '../resources/runtimeActions.js';
import { workspaceContext } from '../workspaceContext.js';
import { managementGateway, managementWorkspace, onDidChangePanelManagementTarget } from '../managementTarget.js';
import { selectPanelWorkspace } from '../workspacePanelState.js';
import { publishPanelToolbar, releasePanelToolbar } from '../panelToolbar.js';
import type { PanelToolbarControl } from '../../../web/src/components/panels/bottomPanel/PanelToolbar.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThreadInput } from '../threadInput.js';
import ComposerToolControl from '../../../web/src/components/composer/ComposerToolControl.js';
import type { Agent } from '../../../web/src/types/backend.js';
import { ReactSurface } from './surface.js';
import { TaskView } from './taskView.js';
import { productStyles } from './productStyles.js';
import style from './workspacePanelView.css?text';
import panelStyle from '../../../web/src/styles/foundation/workbenchPanel.css?text';

type PanelKind = 'terminal' | 'browser' | 'automation' | 'tools';
const panelKinds: Record<string, PanelKind> = {
  'boxteam.workspace.terminal': 'terminal',
  'boxteam.browser.view': 'browser',
  'boxteam.automation.tasks': 'automation',
  'boxteam.tools.view': 'tools',
};
const panelViewIds = Object.fromEntries(Object.entries(panelKinds).map(([id, kind]) => [kind, id])) as Record<PanelKind, string>;
const panelLabelKeys: Record<PanelKind, string> = {
  terminal: 'workbench.view.workspaceTerminal', browser: 'workbench.view.browser',
  automation: 'workbench.view.automation',
  tools: 'workbench.view.tools',
};
const ToolbarView = createContext<string | null>(null);
const TerminalMenus = createContext<IContextMenuService | null>(null);
function NativeProductToolbar({ controls }: { controls: readonly PanelToolbarControl[] }) {
  const viewId = useContext(ToolbarView);
  const owner = useRef({});
  useLayoutEffect(() => { if (viewId) publishPanelToolbar(owner.current, viewId, controls); }, [viewId, controls]);
  useLayoutEffect(() => () => releasePanelToolbar(owner.current), [viewId]);
  return null;
}
const toolbar = (controls: readonly PanelToolbarControl[]) => <NativeProductToolbar controls={controls} />;
interface ResourceReveal { owner: WorkspaceOwner; resourceId: string }
export interface AgentToolReveal { target: GatewayWorkspaceRequestTarget; agentId: string }

function WorkspaceConnections({ workspace, input, active, kind, commands, notifications, reveal }: {
  workspace: GatewayWorkspace; input: ThreadInput | null; active: boolean; kind: 'terminal' | 'browser';
  commands: ICommandService; notifications: INotificationService; reveal?: ResourceReveal;
}) {
  const menus = useContext(TerminalMenus)!;
  const connections = workspace.remote_gateways.filter(item => item.gateway_id === workspace.gateway_id);
  const gatewayConnectionId = connections.length === 1 ? connections[0].gateway_connection_id : undefined;
  // 同一 owner 的目录刷新或面板显隐不重建正文；仅路由身份变化才淘汰旧快照。
  const scope = useMemo(() => ({}), [workspace.gateway_id, workspace.workspace_id, workspace.connection_kind, gatewayConnectionId, connections.length, kind]);
  const [retry, setRetry] = useState(0);
  const [status, setStatus] = useState<{ scope: typeof scope; snapshot?: PluginRuntimeStatusDTO; error?: string }>();
  useEffect(() => {
    if (!active) return;
    const abort = new AbortController();
    const timer = window.setTimeout(() => {
      setStatus(previous => ({ scope, snapshot: previous?.scope === scope ? previous.snapshot : undefined, error: t('plugins.runtimeSnapshotTimeout') }));
      abort.abort();
    }, DEFAULT_API_REQUEST_TIMEOUT_MS);
    if (workspace.connection_kind === 'remote_gateway' && connections.length !== 1) {
      window.clearTimeout(timer);
      setStatus({ scope, error: t('workbench.resource.gatewayNotLoaded') });
      return () => abort.abort();
    }
    void watchPluginRuntimeStatus({ gateway_id: workspace.gateway_id,
      gateway_connection_id: workspace.connection_kind === 'remote_gateway' && connections.length === 1 ? connections[0].gateway_connection_id : undefined,
    }, abort.signal, snapshot => {
      window.clearTimeout(timer);
      if (!abort.signal.aborted) setStatus({ scope, snapshot });
    }).catch((cause: unknown) => {
      window.clearTimeout(timer);
      if (!abort.signal.aborted) setStatus(previous => ({ scope, snapshot: previous?.scope === scope ? previous.snapshot : undefined, error: cause instanceof Error ? cause.message : String(cause) }));
    });
    return () => { window.clearTimeout(timer); abort.abort(); };
  }, [scope, active, retry]);
  const currentStatus = status?.scope === scope ? status : undefined;
  const slot = currentStatus?.snapshot?.plugins.find(item => item.binding?.package_id === 'boxteam.developer-tools'
    && item.binding.plugin_id === kind && item.binding.workspace_id === workspace.workspace_id);
  const binding = currentStatus?.snapshot?.configured_bindings.find(item => item.package_id === 'boxteam.developer-tools'
    && item.plugin_id === kind && item.workspace_id === workspace.workspace_id);
  const configuration = currentStatus?.snapshot?.workspace_configurations.find(item => item.workspace_id === workspace.workspace_id);
  const pluginError = currentStatus?.error ?? configuration?.error ?? slot?.error;
  const ready = !configuration?.error && !slot?.error && configuration?.state === 'ready' && binding?.enabled === true
    && slot?.binding?.enabled === true && slot.binding.package_version === binding.package_version && slot.state === 'ready';
  const dispatch = useCallback<DispatchWorkspaceResourceAction>((action, source, thread) => commands.executeCommand<WorkspaceResourceDTO | undefined>(
    resourceActionCommand(action), { owner: { gatewayId: workspace.gateway_id, workspaceId: workspace.workspace_id }, resource: source.resource, source, thread },
  ).catch((cause: unknown) => { notifications.error(cause instanceof Error ? cause : String(cause)); throw cause; }), [commands, notifications, workspace.gateway_id, workspace.workspace_id]);
  const controller = useWorkspaceResources(0, gatewayWorkspaceKey(workspace), kind, active && ready, dispatch);
  const [selection, setSelection] = useState<{ reveal?: ResourceReveal; id: string | null }>({ reveal, id: reveal?.resourceId ?? null });
  // 新的显式定位在本次 render 即生效，不能先短暂连接上次选中或清单首项。
  const selected = reveal && selection.reveal !== reveal ? reveal.resourceId : selection.id;
  const setSelected = (id: string | null) => setSelection({ reveal, id });
  const container = useRef<HTMLDivElement>(null);
  const found = controller.items.some(item => item.resource_id === selected);
  useEffect(() => {
    if (active && reveal?.resourceId === selected && found) {
      container.current?.querySelector<HTMLElement>('.terminal-panel-list button.active, .resource-tree-item.is-selected .resource-tree-main')?.focus();
    }
  }, [active, reveal, selected, found]);
  const currentThread = input ? { session_id: input.owner.sessionId, thread_id: input.owner.threadId } : null;
  if (!ready) return <div className="native-workspace-panel-product panel-feedback-host" aria-busy={!currentStatus}>
    {currentStatus ? <div className="panel-feedback" role={pluginError ? 'alert' : 'status'}>
      {pluginError ? failureSummary(pluginError) : (currentStatus?.snapshot
        ? `${t(kind === 'terminal' ? 'resources.kind.terminal' : 'resources.kind.browser')} · ${!binding ? t('plugins.unbound')
          : !binding.enabled ? t('plugins.disabled') : pluginStateLabel(slot?.state ?? 'waiting')}`
        : t('plugins.loading'))}
    {currentStatus.error ? <button type="button" onClick={() => setRetry(value => value + 1)}>{t('workbench.panel.retryInitialize')}</button>
      : <button type="button" onClick={() => void commands.executeCommand('workbench.view.extensions').catch((cause: unknown) => notifications.error(String(cause)))}>{t('workbench.plugins.managePluginPackages')}</button>}
    </div> : null}
  </div>;
  return <div ref={container} className="native-workspace-panel-product panel-feedback-host">
    {currentStatus?.error ? <div className="panel-feedback" role="alert">{failureSummary(currentStatus.error)}<button type="button" onClick={() => setRetry(value => value + 1)}>{t('workbench.panel.retryInitialize')}</button></div> : null}
    {kind === 'browser' ? <WarmConfirmProvider><BrowserContent controller={controller} workspaceId={gatewayWorkspaceKey(workspace)}
      currentThread={currentThread} selectedId={selected} onSelect={setSelected} renderToolbar={toolbar} /></WarmConfirmProvider>
    : <section className="terminal-panel native-workspace-panel-product" data-testid="terminal-panel">
    <TerminalContent apiPort={0} controller={controller} currentThread={currentThread}
      threadTargetUnavailable={false} workspaceId={gatewayWorkspaceKey(workspace)} workspaceName={workspace.name}
      selectedTerminalId={selected} onSelectTerminal={setSelected} renderToolbar={toolbar}
      showTerminalMenu={(anchor, controls) => {
        const actions = controls.map(control => toAction({ id: `boxteam.terminal.${control.id}`, label: control.label,
          enabled: !control.disabled, run: async () => { await control.run(); } }));
        menus.showContextMenu({ getAnchor: () => anchor, getActions: () => actions });
      }} />
    </section>}
  </div>;
}

function WorkspaceProduct({ kind, workspace, editors, active, input, commands, notifications, reveal }: {
  kind: PanelKind; workspace: GatewayWorkspace; editors: IEditorService; active: boolean; input: ThreadInput | null;
  commands: ICommandService; notifications: INotificationService; reveal?: ResourceReveal | AgentToolReveal;
}) {
  const owner = { workspaceKey: gatewayWorkspaceKey(workspace), workspaceId: workspace.workspace_id };
  switch (kind) {
    case 'automation': return <TaskView owner={owner} editors={editors} active={active} goalSessionId={input?.owner.sessionId ?? null} />;
    case 'terminal': case 'browser': return <WorkspaceConnections workspace={workspace} input={input} active={active} kind={kind} commands={commands} notifications={notifications} reveal={reveal && 'owner' in reveal ? reveal : undefined} />;
    case 'tools': return <WorkspaceToolManager workspace={workspace} active={active} reveal={reveal && 'target' in reveal ? reveal : undefined} />;
  }
}

function WorkspaceToolManager({ workspace, active, reveal }: { workspace: GatewayWorkspace; active: boolean; reveal?: AgentToolReveal }) {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  useEffect(() => {
    let disposed = false;
    setAgents(null); setError(null); setSelected('');
    void listAgents(0, gatewayWorkspaceKey(workspace)).then(value => {
      if (disposed) return;
      setAgents(value);
      setSelected(reveal?.agentId ?? value.find(agent => agent.workspace_default)?.agent_id ?? value[0]?.agent_id ?? '');
    }).catch(cause => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [workspace.gateway_id, workspace.workspace_id, reveal?.agentId]);
  if (error) return <div className="native-workspace-panel-product panel-feedback-host"><p className="panel-feedback" role="alert">{failureSummary(error)}</p></div>;
  if (!agents) return <div className="native-workspace-panel-product" aria-busy="true" />;
  if (!agents.length) return <p role="status">{t('composer.tools.noAgents')}</p>;
  const agent = agents.find(item => item.agent_id === selected);
  if (reveal && !agent) return <p role="alert">找不到指定工作区中的 Agent：{reveal.agentId}</p>;
  return <section className="native-workspace-panel-product native-workspace-tools panel-feedback-host" data-agent-id={selected}>
    {toolbar([{ id: 'agent', kind: 'select', label: t('composer.tools.agentOwner'), icon: 'account', value: selected,
      options: agents.map(item => ({ value: item.agent_id, label: item.name })), onChange: setSelected }])}
    <p>{t('composer.tools.workspaceAgentImpact')}</p>
    {agent ? <ComposerToolControl apiPort={0} agentId={agent.agent_id} workspaceId={gatewayWorkspaceKey(workspace)}
      inline active={active} onStatus={setStatus} models={agent.models} /> : null}
    {status ? <p className="panel-feedback" role="status">{status}</p> : null}
  </section>;
}

function WorkspacePanelView({ editors, kind, visible, commands, notifications, reveal }: {
  editors: IEditorService; kind: PanelKind; visible: boolean;
  commands: ICommandService; notifications: INotificationService; reveal?: ResourceReveal | AgentToolReveal;
}) {
  const snapshot = useSyncExternalStore(workspaceContext.subscribe, workspaceContext.getSnapshot);
  const [visited, setVisited] = useState(new Map<string, GatewayWorkspace>());
  const [activeEditor, setActiveEditor] = useState(editors.activeEditor);
  useEffect(() => {
    const listener = editors.onDidActiveEditorChange(() => setActiveEditor(editors.activeEditor));
    return () => listener.dispose();
  }, [editors]);
  useEffect(() => { if (visible) void workspaceContext.refresh(); }, [visible]);
  let current: GatewayWorkspace | undefined, scopeError: string | undefined;
  if (snapshot.gatewayId) {
    try {
      const target = managementGateway('panel');
      current = managementWorkspace('panel', target);
      const connections = current.remote_gateways.filter(item => item.gateway_id === target.gateway_id);
      if (current.connection_kind === 'remote_gateway'
        && (connections.length !== 1 || connections[0].gateway_connection_id !== target.gateway_connection_id)) {
        throw new Error(t('workbench.resource.gatewayNotLoaded'));
      }
      if (current.status !== 'ready') scopeError = t('workbench.panel.workspaceNotReady', current.status);
    } catch (cause) { scopeError = cause instanceof Error ? cause.message : String(cause); }
  }
  useEffect(() => {
    if (current && !scopeError) setVisited(previous => new Map(previous).set(gatewayWorkspaceKey(current), current));
    // 已移除的工作区不再保留观察者或未保存表单。
    if (!snapshot.loading && !snapshot.error) setVisited(previous => new Map([...previous].filter(([id]) => snapshot.workspaces.some(item => gatewayWorkspaceKey(item) === id))));
  }, [current, scopeError, snapshot.workspaces, snapshot.loading, snapshot.error]);
  const selectedWorkspace = current;
  return <div className={`native-workspace-panel panel-feedback-host${kind === 'automation' ? ' native-workspace-automation' : ''}`} data-workspace-id={selectedWorkspace?.workspace_id ?? ''} data-panel-kind={kind}>
    {snapshot.error ? <p className="panel-feedback" role="alert">{t('workbench.panel.readFailed', snapshot.error)}</p> : snapshot.loading ? null
      : scopeError ? <p className="panel-feedback" role="alert">{failureSummary(scopeError)}{selectedWorkspace ? <button type="button"
        onClick={() => void commands.executeCommand('boxteam.workspace.details', {
          gatewayId: selectedWorkspace.gateway_id, workspaceId: selectedWorkspace.workspace_id,
        }).catch((cause: unknown) => notifications.error(String(cause)))}>{t('workbench.workspaceAction.details')}</button> : null}</p>
      : !selectedWorkspace ? <p>{t('workbench.panel.chooseWorkspace')}</p> : null}
    {[...visited.values()].map(workspace => {
      const selected = !scopeError && selectedWorkspace?.gateway_id === workspace.gateway_id && selectedWorkspace.workspace_id === workspace.workspace_id;
      const input = activeEditor instanceof ThreadInput && activeEditor.owner.workspaceKey === gatewayWorkspaceKey(workspace) ? activeEditor : null;
      return <section key={gatewayWorkspaceKey(workspace)} className="native-workspace-panel-content" hidden={!selected} aria-label={t('workbench.panel.label', t(panelLabelKeys[kind]), workspace.name)}>
        <ToolbarView.Provider value={visible && selected ? panelViewIds[kind] : null}><WorkspaceProduct kind={kind} workspace={workspace} editors={editors} active={visible && selected} input={input} commands={commands} notifications={notifications}
          reveal={reveal && 'target' in reveal ? reveal.target.gateway_id === workspace.gateway_id && reveal.target.workspace_id === workspace.workspace_id ? reveal : undefined
            : reveal?.owner.gatewayId === workspace.gateway_id && reveal.owner.workspaceId === workspace.workspace_id ? reveal : undefined} /></ToolbarView.Provider>
      </section>;
    })}
  </div>;
}

/** 只装配原产品与原生视图生命周期，不维护第二套业务状态。 */
export class WorkspacePanelSurface {
  private readonly surface: ReactSurface;
  private readonly kind: PanelKind;
  private visible: boolean;
  private reveal?: ResourceReveal | AgentToolReveal;
  private readonly selectionListener: IDisposable;
  constructor(container: HTMLElement, private readonly editors: IEditorService, id: string, visible: boolean,
    private readonly notifications: INotificationService, private readonly commands: ICommandService,
    private readonly menus: IContextMenuService, private theme: IColorTheme, onDiagnostic?: (message: string) => void) {
    const kind = panelKinds[id];
    if (!kind) throw new Error(t('workbench.panel.unregistered', id));
    this.kind = kind;
    this.surface = new ReactSurface(container, [...productStyles, panelStyle, style], onDiagnostic);
    this.selectionListener = onDidChangePanelManagementTarget(() => {
        this.reveal = undefined;
        this.render();
    });
    this.visible = visible; this.render();
  }
  setVisible(visible: boolean): void { this.visible = visible; this.render(); }
  setTheme(theme: IColorTheme): void { this.theme = theme; this.render(); }
  revealResource(owner: WorkspaceOwner, resourceId: string): void {
    selectPanelWorkspace(owner);
    this.reveal = { owner: { ...owner }, resourceId };
    this.render();
  }
  revealAgentTools(target: GatewayWorkspaceRequestTarget, agentId: string): void {
    selectPanelWorkspace({ gatewayId: target.gateway_id, workspaceId: target.workspace_id });
    this.reveal = { target: { ...target }, agentId };
    this.render();
  }
  focus(): void { this.surface.focus('.native-workspace-panel-content:not([hidden]) :is(button,input,select)'); }
  dispose(): void { this.selectionListener.dispose(); this.surface.dispose(); }
  private render(): void { this.surface.render(<TerminalThemeContext.Provider value={getTerminalTheme(this.theme)}><TerminalMenus.Provider value={this.menus}><WorkspacePanelView editors={this.editors} kind={this.kind} visible={this.visible} commands={this.commands} notifications={this.notifications} reveal={this.reveal} /></TerminalMenus.Provider></TerminalThemeContext.Provider>); }
}
