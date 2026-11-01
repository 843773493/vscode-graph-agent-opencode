import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { getWorkspace } from '../../../web/src/api/workspace.js';
import type { GatewayWorkspace } from '../../../web/src/types/backend.js';
import type { GatewayResolvedThemeConfig } from '../../../web/src/theme/theme.js';
import type { IEditorService } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { TerminalContent } from '../../../web/src/components/panels/bottomPanel/TerminalContent.js';
import { BrowserContent } from '../../../web/src/components/panels/bottomPanel/BrowserContent.js';
import { GatewayLogContent } from '../../../web/src/components/panels/bottomPanel/GatewayLogContent.js';
import WorkspacePortForwardPanel from '../../../web/src/components/workspace/portForward/WorkspacePortForwardPanel.js';
import McpConnections from '../../../web/src/components/workspace/mcp/McpConnections.js';
import WarmConfirmProvider from '../../../web/src/components/shell/WarmConfirmProvider.js';
import { useWorkspaceResources } from '../../../web/src/hooks/workspace/useWorkspaceResources.js';
import { workspaceContext } from '../workspaceContext.js';
import { ThreadInput } from '../threadInput.js';
import { ReactSurface } from './surface.js';
import { TaskView } from './taskView.js';
import { productStyles } from './productStyles.js';
import style from './workspacePanelView.css?text';
import panelStyle from '../../../web/src/styles/foundation/workbenchPanel.css?text';
import portStyle from '../../../web/src/styles/workspace/portForwardPanel.css?text';

type PanelKind = 'terminal' | 'browser' | 'output' | 'ports' | 'automation' | 'mcp';
const panelKinds: Record<string, PanelKind> = {
  'boxteam.workspace.terminal': 'terminal', 'boxteam.workspace.output': 'output',
  'boxteam.browser.view': 'browser',
  'boxteam.ports.view': 'ports', 'boxteam.automation.tasks': 'automation', 'boxteam.mcp.view': 'mcp',
};
const toolbar = (tools: ReactNode) => <div className="native-workspace-panel-toolbar">{tools}</div>;

function WorkspaceConnections({ workspace, input, active, kind }: {
  workspace: GatewayWorkspace; input: ThreadInput | null; active: boolean; kind: 'terminal' | 'browser';
}) {
  const controller = useWorkspaceResources(0, workspace.workspace_id, kind, active);
  const [selected, setSelected] = useState<string | null>(null);
  const currentThread = input ? { session_id: input.owner.sessionId, thread_id: input.owner.threadId } : null;
  if (kind === 'browser') return <WarmConfirmProvider><BrowserContent controller={controller} workspaceId={workspace.workspace_id}
    currentThread={currentThread} renderToolbar={toolbar} /></WarmConfirmProvider>;
  return <section className="terminal-panel native-workspace-panel-product" data-testid="terminal-panel">
    <TerminalContent controller={controller} currentThread={currentThread}
      threadTargetUnavailable={false} workspaceId={workspace.workspace_id} workspaceName={workspace.name}
      selectedTerminalId={selected} onSelectTerminal={setSelected} renderToolbar={toolbar} />
  </section>;
}

function WorkspaceProduct({ kind, workspace, editors, active, input }: {
  kind: PanelKind; workspace: GatewayWorkspace; editors: IEditorService; active: boolean; input: ThreadInput | null;
}) {
  const [owner, setOwner] = useState<{ routeId: string; workspaceId: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let disposed = false;
    setOwner(null); setError(null);
    void getWorkspace(0, workspace.workspace_id).then(value => {
      if (workspace.connection_kind === 'local' && value.workspace_id !== workspace.backend_workspace_id) throw new Error('面板的工作区路由归属已改变');
      if (!disposed) setOwner({ routeId: workspace.workspace_id, workspaceId: value.workspace_id });
    }).catch((cause: unknown) => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [workspace.workspace_id, workspace.backend_workspace_id, workspace.connection_kind, revision]);
  if (error) return <div role="alert">工作区初始化失败：{error}<button onClick={() => setRevision(value => value + 1)}>重试工作区初始化</button></div>;
  if (!owner) return <p role="status">正在确认工作区…</p>;
  switch (kind) {
    case 'automation': return <TaskView owner={owner} editors={editors} active={active} goalSessionId={input?.owner.sessionId ?? null} />;
    case 'terminal': case 'browser': return <WorkspaceConnections workspace={workspace} input={input} active={active} kind={kind} />;
    case 'output': return <section className="gateway-log-panel native-workspace-panel-product" data-testid="gateway-log-panel">
      <GatewayLogContent apiPort={0} workspaceId={workspace.workspace_id} active={active} renderToolbar={toolbar} />
    </section>;
    case 'ports': return <WarmConfirmProvider><WorkspacePortForwardPanel apiPort={0} workspace={workspace} active={active} /></WarmConfirmProvider>;
    case 'mcp': return <McpConnections apiPort={0} target={workspace} active={active} />;
  }
}

function WorkspacePanelView({ editors, kind, visible, onTheme }: {
  editors: IEditorService; kind: PanelKind; visible: boolean; onTheme: (theme: GatewayResolvedThemeConfig) => void;
}) {
  const snapshot = useSyncExternalStore(workspaceContext.subscribe, workspaceContext.getSnapshot);
  const [visited, setVisited] = useState(new Map<string, GatewayWorkspace>());
  const [activeEditor, setActiveEditor] = useState(editors.activeEditor);
  useEffect(() => {
    const listener = editors.onDidActiveEditorChange(() => setActiveEditor(editors.activeEditor));
    return () => listener.dispose();
  }, [editors]);
  useEffect(() => { if (visible) void workspaceContext.refresh(); }, [visible]);
  const current = !snapshot.loading && !snapshot.error ? snapshot.workspaces.find(item => item.workspace_id === snapshot.activeId) : undefined;
  useEffect(() => {
    if (current) setVisited(previous => new Map(previous).set(current.workspace_id, current));
    // 已移除的工作区不再保留观察者或未保存表单。
    if (!snapshot.loading && !snapshot.error) setVisited(previous => new Map([...previous].filter(([id]) => snapshot.workspaces.some(item => item.workspace_id === id))));
  }, [current, snapshot.workspaces, snapshot.loading, snapshot.error]);
  useEffect(() => {
    const theme = snapshot.settings?.theme.resolved_theme;
    if (theme) onTheme(theme);
  }, [snapshot.settings, onTheme]);
  return <div className={`native-workspace-panel${kind === 'automation' ? ' native-workspace-automation' : ''}`} data-workspace-id={current?.workspace_id ?? ''} data-panel-kind={kind}>
    <header className="native-workspace-context"><span>{current?.name ?? '工作区'}</span>
      <button className="sidebar-icon-button" aria-label="刷新工作区状态" title="刷新工作区状态" onClick={() => void workspaceContext.refresh()}><span className="codicon codicon-refresh" /></button>
    </header>
    {snapshot.error ? <p role="alert">读取当前工作区失败：{snapshot.error}</p> : snapshot.loading ? <p role="status">正在切换工作区…</p>
      : !current ? <p>请先在主侧栏选择工作区。</p> : null}
    {[...visited.values()].map(workspace => {
      const selected = current?.workspace_id === workspace.workspace_id;
      const input = activeEditor instanceof ThreadInput && activeEditor.owner.routeId === workspace.workspace_id ? activeEditor : null;
      return <section key={workspace.workspace_id} className="native-workspace-panel-content" hidden={!selected} aria-label={`${kind}：${workspace.name}`}>
        <WorkspaceProduct kind={kind} workspace={workspace} editors={editors} active={visible && selected} input={input} />
      </section>;
    })}
  </div>;
}

/** 只装配原产品与原生视图生命周期，不维护第二套业务状态。 */
export class WorkspacePanelSurface {
  private readonly surface: ReactSurface;
  private readonly kind: PanelKind;
  private visible: boolean;
  constructor(container: HTMLElement, private readonly editors: IEditorService, id: string, visible: boolean,
    private readonly notifications: INotificationService) {
    const kind = panelKinds[id];
    if (!kind) throw new Error(`未注册的工作区面板：${id}`);
    this.kind = kind;
    this.surface = new ReactSurface(container, [...productStyles, panelStyle, portStyle, style]);
    this.visible = visible; this.render();
  }
  setVisible(visible: boolean): void { this.visible = visible; this.render(); }
  focus(): void { this.surface.focus('.native-workspace-panel-content:not([hidden]) :is(button,input,select)'); }
  dispose(): void { this.surface.dispose(); }
  private readonly onTheme = (theme: GatewayResolvedThemeConfig) => this.surface.applyTheme(theme, this.notifications);
  private render(): void { this.surface.render(<WorkspacePanelView editors={this.editors} kind={this.kind} visible={this.visible} onTheme={this.onTheme} />); }
}
