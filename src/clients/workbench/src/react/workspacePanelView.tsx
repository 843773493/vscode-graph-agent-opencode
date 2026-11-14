import { gatewayWorkspaceKey, requireGatewayWorkspaceRequestTarget, type GatewayWorkspaceRequestTarget, type WorkspaceOwner } from "../../../web/src/api/workspaceIdentity.js";
import { t } from '../../../shared/core/i18n/index.js';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { getWorkspace, listAgents } from '../../../web/src/api/workspace.js';
import type { GatewayWorkspace } from '../../../web/src/types/backend.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import type { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { TerminalContent } from '../../../web/src/components/panels/bottomPanel/TerminalContent.js';
import { BrowserContent } from '../../../web/src/components/panels/bottomPanel/BrowserContent.js';
import { GatewayLogContent } from '../../../web/src/components/panels/bottomPanel/GatewayLogContent.js';
import WorkspacePortForwardPanel from '../../../web/src/components/workspace/portForward/WorkspacePortForwardPanel.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { useWorkspaceResources } from '../../../web/src/hooks/workspace/useWorkspaceResources.js';
import type { DispatchWorkspaceResourceAction } from '../../../web/src/hooks/workspace/resourceActions.js';
import type { WorkspaceResourceDTO } from '../../../web/src/api/workspaceResources.js';
import { resourceActionCommand } from '../resources/runtimeActions.js';
import { workspaceContext } from '../workspaceContext.js';
import { managementGateway, managementWorkspace, managementTargetDisplayName, onDidChangeToolManagementTarget,
  selectManagementTarget, selectToolManagementTargetCommand, type ManagementTarget } from '../managementTarget.js';
import type { IDisposable } from '../../upstream/src/vs/base/common/lifecycle.js';
import { ThreadInput } from '../threadInput.js';
import ComposerToolControl from '../../../web/src/components/composer/ComposerToolControl.js';
import type { Agent } from '../../../web/src/types/backend.js';
import { ReactSurface } from './surface.js';
import { TaskView } from './taskView.js';
import { productStyles } from './productStyles.js';
import style from './workspacePanelView.css?text';
import panelStyle from '../../../web/src/styles/foundation/workbenchPanel.css?text';
import portStyle from '../../../web/src/styles/workspace/portForwardPanel.css?text';

type PanelKind = 'terminal' | 'browser' | 'output' | 'ports' | 'automation' | 'tools';
const panelKinds: Record<string, PanelKind> = {
  'boxteam.workspace.terminal': 'terminal', 'boxteam.workspace.output': 'output',
  'boxteam.browser.view': 'browser',
  'boxteam.ports.view': 'ports', 'boxteam.automation.tasks': 'automation',
  'boxteam.tools.view': 'tools',
};
const panelLabelKeys: Record<PanelKind, string> = {
  terminal: 'workbench.view.workspaceTerminal', browser: 'workbench.view.browser',
  output: 'workbench.view.workspaceOutput', ports: 'workbench.view.ports', automation: 'workbench.view.automation',
  tools: 'workbench.view.tools',
};
const toolbar = (tools: ReactNode) => <div className="native-workspace-panel-toolbar">{tools}</div>;
interface ResourceReveal { owner: WorkspaceOwner; resourceId: string }
export interface AgentToolReveal { target: GatewayWorkspaceRequestTarget; agentId: string }

function WorkspaceConnections({ workspace, input, active, kind, commands, notifications, reveal }: {
  workspace: GatewayWorkspace; input: ThreadInput | null; active: boolean; kind: 'terminal' | 'browser';
  commands: ICommandService; notifications: INotificationService; reveal?: ResourceReveal;
}) {
  const dispatch = useCallback<DispatchWorkspaceResourceAction>((action, source, thread) => commands.executeCommand<WorkspaceResourceDTO | undefined>(
    resourceActionCommand(action), { owner: { gatewayId: workspace.gateway_id, workspaceId: workspace.workspace_id }, resource: source.resource, source, thread },
  ).catch((cause: unknown) => { notifications.error(cause instanceof Error ? cause : String(cause)); throw cause; }), [commands, notifications, workspace.gateway_id, workspace.workspace_id]);
  const controller = useWorkspaceResources(0, gatewayWorkspaceKey(workspace), kind, active, dispatch);
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
  return <div ref={container} className="native-workspace-panel-product">
    {kind === 'browser' ? <WarmConfirmProvider><BrowserContent controller={controller} workspaceId={gatewayWorkspaceKey(workspace)}
      currentThread={currentThread} selectedId={selected} onSelect={setSelected} renderToolbar={toolbar} /></WarmConfirmProvider>
    : <section className="terminal-panel native-workspace-panel-product" data-testid="terminal-panel">
    <TerminalContent apiPort={0} controller={controller} currentThread={currentThread}
      threadTargetUnavailable={false} workspaceId={gatewayWorkspaceKey(workspace)} workspaceName={workspace.name}
      selectedTerminalId={selected} onSelectTerminal={setSelected} renderToolbar={toolbar} />
    </section>}
  </div>;
}

function WorkspaceProduct({ kind, workspace, editors, active, input, commands, notifications, reveal }: {
  kind: PanelKind; workspace: GatewayWorkspace; editors: IEditorService; active: boolean; input: ThreadInput | null;
  commands: ICommandService; notifications: INotificationService; reveal?: ResourceReveal | AgentToolReveal;
}) {
  const [owner, setOwner] = useState<{ workspaceKey: string; workspaceId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let disposed = false;
    setOwner(null); setError(null);
    void getWorkspace(0, gatewayWorkspaceKey(workspace)).then(value => {
      if (value.workspace_id !== workspace.workspace_id) throw new Error(t('workbench.panel.ownerChanged'));
      if (!disposed) setOwner({ workspaceKey: gatewayWorkspaceKey(workspace), workspaceId: value.workspace_id });
    }).catch((cause: unknown) => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [workspace.gateway_id, workspace.workspace_id, revision]);
  if (error) return <div role="alert">{t('workbench.panel.initializeFailed', error)}<button onClick={() => setRevision(value => value + 1)}>{t('workbench.panel.retryInitialize')}</button></div>;
  if (!owner) return <p role="status">{t('workbench.panel.confirmingWorkspace')}</p>;
  switch (kind) {
    case 'automation': return <TaskView owner={owner} editors={editors} active={active} goalSessionId={input?.owner.sessionId ?? null} />;
    case 'terminal': case 'browser': return <WorkspaceConnections workspace={workspace} input={input} active={active} kind={kind} commands={commands} notifications={notifications} reveal={reveal && 'owner' in reveal ? reveal : undefined} />;
    case 'output': return <section className="gateway-log-panel native-workspace-panel-product" data-testid="gateway-log-panel">
      <GatewayLogContent apiPort={0} workspaceId={gatewayWorkspaceKey(workspace)} active={active} renderToolbar={toolbar} />
    </section>;
    case 'tools': return <WorkspaceToolManager workspace={workspace} active={active} reveal={reveal && 'target' in reveal ? reveal : undefined} />;
    case 'ports': return <WarmConfirmProvider><WorkspacePortForwardPanel apiPort={0} workspace={workspace} active={active} /></WarmConfirmProvider>;
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
  if (error) return <p role="alert">{error}</p>;
  if (!agents) return <p role="status">{t('composer.tools.loading')}</p>;
  if (!agents.length) return <p role="status">{t('composer.tools.noAgents')}</p>;
  const agent = agents.find(item => item.agent_id === selected);
  if (reveal && !agent) return <p role="alert">找不到指定工作区中的 Agent：{reveal.agentId}</p>;
  return <section className="native-workspace-panel-product native-workspace-tools" data-agent-id={selected}>
    <header><label>{t('composer.tools.agentOwner')}
      <select value={selected} onChange={event => setSelected(event.target.value)} aria-label={t('composer.tools.agentOwner')}>
        {agents.map(item => <option key={item.agent_id} value={item.agent_id}>{item.name} · {item.agent_id}</option>)}
      </select>
    </label><span>{t('composer.tools.workspaceAgentImpact')}</span></header>
    {agent ? <ComposerToolControl apiPort={0} agentId={agent.agent_id} workspaceId={gatewayWorkspaceKey(workspace)}
      inline active={active} onStatus={setStatus} providers={agent.providers} /> : null}
    {status ? <p role="status">{status}</p> : null}
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
  let current = kind !== 'tools' && !snapshot.loading && !snapshot.error
    ? snapshot.workspaces.find(item => gatewayWorkspaceKey(item) === snapshot.activeId) : undefined;
  let toolTarget: ManagementTarget | undefined, toolError: string | undefined;
  if (kind === 'tools' && !snapshot.loading && !snapshot.error) {
    try {
      const explicit = reveal && 'target' in reveal ? reveal.target : undefined;
      toolTarget = managementGateway('tools', explicit?.gateway_id);
      if (explicit) toolTarget = { ...toolTarget, gateway_connection_id: explicit.gateway_connection_id };
      current = explicit ? requireGatewayWorkspaceRequestTarget(snapshot.workspaces,
        { gatewayId: explicit.gateway_id, workspaceId: explicit.workspace_id }) : managementWorkspace('tools', toolTarget);
      const gatewayId = current.gateway_id;
      const connections = current.remote_gateways.filter(item => item.gateway_id === gatewayId);
      const expectedConnection = explicit ? explicit.gateway_connection_id : toolTarget.gateway_connection_id;
      if (current.connection_kind === 'remote_gateway'
        && (connections.length !== 1 || connections[0].gateway_connection_id !== expectedConnection)) {
        throw new Error(t('workbench.resource.gatewayNotLoaded'));
      }
      if (current.status !== 'ready') toolError = t('workbench.panel.toolsWorkspaceNotReady', current.status);
    } catch (cause) { toolError = cause instanceof Error ? cause.message : String(cause); current = undefined; }
  }
  useEffect(() => {
    if (current && !toolError) setVisited(previous => new Map(previous).set(gatewayWorkspaceKey(current), current));
    // 已移除的工作区不再保留观察者或未保存表单。
    if (!snapshot.loading && !snapshot.error) setVisited(previous => new Map([...previous].filter(([id]) => snapshot.workspaces.some(item => gatewayWorkspaceKey(item) === id))));
  }, [current, toolError, snapshot.workspaces, snapshot.loading, snapshot.error]);
  const selectedWorkspace = current;
  return <div className={`native-workspace-panel${kind === 'automation' ? ' native-workspace-automation' : ''}`} data-workspace-id={selectedWorkspace?.workspace_id ?? ''} data-panel-kind={kind}>
    <header className="native-workspace-context">{kind === 'tools' ? <>
      <button type="button" aria-label={t('workbench.managementTarget.selectTools')} disabled={snapshot.loading || !snapshot.gatewayId}
        onClick={() => void commands.executeCommand(selectToolManagementTargetCommand).catch((cause: unknown) => notifications.error(String(cause)))}>
        {t('workbench.managementTarget.title', managementTargetDisplayName(toolTarget))} <span className="codicon codicon-chevron-down" />
      </button>
      <label>{t('workbench.panel.workspace')} <select aria-label={t('workbench.panel.toolsWorkspace')}
        disabled={!toolTarget || snapshot.loading || !!snapshot.error} value={selectedWorkspace?.workspace_id ?? ''}
        onChange={event => {
          if (!toolTarget) return;
          managementWorkspace('tools', toolTarget, event.target.value);
          selectManagementTarget('tools', toolTarget);
        }}>
        {!selectedWorkspace ? <option value="">{t('plugins.noWorkspaces')}</option> : null}
        {snapshot.workspaces.filter(item => item.gateway_id === toolTarget?.gateway_id).map(item =>
          <option key={item.workspace_id} value={item.workspace_id}>{item.name} · {item.status}</option>)}
      </select></label>
    </> : <span>{selectedWorkspace?.name ?? t('workbench.panel.workspace')}</span>}
      <button className="sidebar-icon-button" aria-label={t('workbench.panel.refresh')} title={t('workbench.panel.refresh')} onClick={() => void workspaceContext.refresh()}><span className="codicon codicon-refresh" /></button>
    </header>
    {snapshot.error ? <p role="alert">{t('workbench.panel.readFailed', snapshot.error)}</p> : snapshot.loading ? <p role="status">{t('workbench.panel.switching')}</p>
      : toolError ? <p role="alert">{toolError}{selectedWorkspace ? <button type="button"
        onClick={() => void commands.executeCommand('boxteam.workspace.details', {
          gatewayId: selectedWorkspace.gateway_id, workspaceId: selectedWorkspace.workspace_id,
        }).catch((cause: unknown) => notifications.error(String(cause)))}>{t('workbench.workspaceAction.details')}</button> : null}</p>
      : !selectedWorkspace ? <p>{t('workbench.panel.chooseWorkspace')}</p> : null}
    {[...visited.values()].map(workspace => {
      const selected = !toolError && selectedWorkspace?.gateway_id === workspace.gateway_id && selectedWorkspace.workspace_id === workspace.workspace_id;
      const input = activeEditor instanceof ThreadInput && activeEditor.owner.workspaceKey === gatewayWorkspaceKey(workspace) ? activeEditor : null;
      return <section key={gatewayWorkspaceKey(workspace)} className="native-workspace-panel-content" hidden={!selected} aria-label={t('workbench.panel.label', t(panelLabelKeys[kind]), workspace.name)}>
        <WorkspaceProduct kind={kind} workspace={workspace} editors={editors} active={visible && selected} input={input} commands={commands} notifications={notifications}
          reveal={reveal && 'target' in reveal ? reveal.target.gateway_id === workspace.gateway_id && reveal.target.workspace_id === workspace.workspace_id ? reveal : undefined
            : reveal?.owner.gatewayId === workspace.gateway_id && reveal.owner.workspaceId === workspace.workspace_id ? reveal : undefined} />
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
  private readonly toolSelectionListener?: IDisposable;
  constructor(container: HTMLElement, private readonly editors: IEditorService, id: string, visible: boolean,
    private readonly notifications: INotificationService, private readonly commands: ICommandService) {
    const kind = panelKinds[id];
    if (!kind) throw new Error(t('workbench.panel.unregistered', id));
    this.kind = kind;
    this.surface = new ReactSurface(container, [...productStyles, panelStyle, portStyle, style]);
    if (kind === 'tools') this.toolSelectionListener = onDidChangeToolManagementTarget(() => {
        this.reveal = undefined;
        this.render();
    });
    this.visible = visible; this.render();
  }
  setVisible(visible: boolean): void { this.visible = visible; this.render(); }
  revealResource(owner: WorkspaceOwner, resourceId: string): void {
    this.reveal = { owner: { ...owner }, resourceId };
    this.render();
  }
  revealAgentTools(target: GatewayWorkspaceRequestTarget, agentId: string): void {
    this.reveal = { target: { ...target }, agentId };
    this.render();
  }
  focus(): void { this.surface.focus('.native-workspace-panel-content:not([hidden]) :is(button,input,select)'); }
  dispose(): void { this.toolSelectionListener?.dispose(); this.surface.dispose(); }
  private render(): void { this.surface.render(<WorkspacePanelView editors={this.editors} kind={this.kind} visible={this.visible} commands={this.commands} notifications={this.notifications} reveal={this.reveal} />); }
}
