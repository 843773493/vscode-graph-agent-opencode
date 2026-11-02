import { useCallback, useEffect, useRef, useState } from 'react';
import AgentSessionsPanel from '../../../web/src/components/panels/sessionPanels/AgentSessionsPanel.js';
import WarmConfirmProvider, { useWarmConfirm } from '../../../web/src/components/shell/WarmConfirmProvider.js';
import SessionNameDialog from '../../../web/src/components/overlays/SessionNameDialog.js';
import SessionContextForkDialog from '../../../web/src/components/overlays/SessionContextForkDialog.js';
import GatewayControlCenter from '../../../web/src/components/workspace/gateway/GatewayControlCenter.js';
import { useSessionCatalogActions } from '../../../web/src/hooks/shell/useSessionCatalogActions.js';
import { useSessionInformationClipboard } from '../../../web/src/hooks/session/useSessionInformationClipboard.js';
import { useWorkspaceInformationClipboard } from '../../../web/src/hooks/workspace/useWorkspaceInformationClipboard.js';
import { resolveAgentSessionsPreferences } from '../../../web/src/state/uiSettings/preferences.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRoute } from '../../../web/src/api/workspaceIdentity.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { getGatewayHealth } from '../../../web/src/gatewayApi.js';
import { getSession } from '../../../web/src/api.js';
import { HttpRequestError } from '../../../web/src/api/http.js';
import type { Session } from '../../../web/src/types/backend.js';
import type { GatewayResolvedThemeConfig } from '../../../web/src/theme/theme.js';
import { IEditorService, SIDE_GROUP } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { EditorsOrder, type IEditorIdentifier } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IStorageService, WillSaveStateReason } from '../../upstream/src/vs/platform/storage/common/storage.js';
import { ThreadInput } from '../threadInput.js';
import { gateway } from '../agentApi.js';
import { workspaceContext } from '../workspaceContext.js';
import { ReactSurface } from './surface.js';
import { productStyles } from './productStyles.js';
import { useNavigationRuntime } from './useNavigationRuntime.js';
import { useManagedBackendConfirmation } from '../../../web/src/hooks/gatewayWorkspace/useManagedBackendConfirmation.js';
import { sessionScopeKey } from '../../../web/src/state/session/sessionScope.js';
import style from './navigationView.css?text';

function NavigationView({ editors, groups, notifications, storage, onTheme }: {
  editors: IEditorService; groups: IEditorGroupsService; notifications: INotificationService;
  storage: IStorageService;
  onTheme: (theme: GatewayResolvedThemeConfig) => void;
}) {
  const confirm = useWarmConfirm();
  const [gatewayOpen, setGatewayOpen] = useState(false);
  const [diagnostic, setDiagnostic] = useState<{ title: string; detail: string } | null>(null);
  const [activeEditor, setActiveEditor] = useState(() => editors.activeEditor);
  const gesture = useRef({ pinned: false, sideBySide: false });
  const openRequests = useRef(new Map<number, number>());
  const created = useCallback(async (session: Session, routeId: string | null) => {
    if (!routeId || !session.thread_id) throw new Error('新建会话回执缺少工作区路由或主 Thread');
    await editors.openEditor(new ThreadInput({ ...workspaceContext.ownerFor(routeId, session.workspace_id),
      sessionId: session.session_id, threadId: session.thread_id, title: session.title }), { pinned: true });
  }, [editors]);
  const notifyError = useCallback((message: string) => { notifications.error(message); }, [notifications]);
  const refreshEditors = useCallback(async (target: GatewayWorkspaceRequestTarget, signal: AbortSignal) => {
    const snapshot = workspaceContext.getSnapshot();
    const workspace = snapshot.workspaces.find(item => item.workspace_id === target.workspace_id);
    if (!workspace || !snapshot.gatewayId) throw new Error('编辑器目录对账缺少工作区登记身份');
    const owner = gatewayWorkspaceOwner(workspace, snapshot.gatewayId);
    const matchingEditors = () => editors.getEditors(EditorsOrder.SEQUENTIAL).filter((entry): entry is IEditorIdentifier & { editor: ThreadInput } =>
      entry.editor instanceof ThreadInput && entry.editor.owner.gatewayId === owner.gatewayId
        && entry.editor.owner.workspaceId === owner.workspaceId);
    const sessionIds = new Set(matchingEditors().map(({ editor }) => editor.owner.sessionId));
    await Promise.all([...sessionIds].map(async sessionId => {
      let session: Session;
      try {
        session = await getSession(0, sessionId, target.workspace_id, { signal });
      } catch (cause) {
        signal.throwIfAborted();
        if (!(cause instanceof HttpRequestError) || cause.status !== 404) throw cause;
        // deleting 的完整 DTO 仍然可读；只有后端确认消失才关闭全部所属 Thread 标签。
        const deleted = matchingEditors().filter(({ editor }) => editor.owner.sessionId === sessionId);
        // 先把已删除 owner 从原版恢复布局中排除并提交，才能关闭可见标签；卸载时无法等待 IndexedDB。
        for (const { editor } of deleted) editor.restorable = false;
        await storage.flush(WillSaveStateReason.SHUTDOWN);
        await editors.closeEditors(deleted);
        return;
      }
      signal.throwIfAborted();
      if (session.session_id !== sessionId || session.workspace_id !== owner.workspaceId) {
        throw new Error(`编辑器目录对账返回了其它会话身份：${target.workspace_id}/${sessionId}`);
      }
      for (const { editor } of matchingEditors()) {
        if (editor.owner.sessionId === sessionId && editor.owner.threadId === session.thread_id) editor.setTitle(session.title);
      }
    }));
  }, [editors, storage]);
  const activeSessionCacheKey = activeEditor instanceof ThreadInput && activeEditor.owner.routeId
    ? sessionScopeKey(activeEditor.owner.routeId, activeEditor.owner.sessionId) : null;
  const runtime = useNavigationRuntime(created, notifyError, refreshEditors, activeSessionCacheKey);
  const { state, setStatus, reportError, setActiveSession, clearActiveSession } = runtime;
  const backendActions = useManagedBackendConfirmation({ apiPort: 0,
    onStart: runtime.startManagedGatewayWorkspaceBackend, onStop: runtime.stopManagedGatewayWorkspaceBackend,
    onSafeRestart: runtime.safeRestartManagedGatewayWorkspaceBackend, onForceRestart: runtime.forceRestartManagedGatewayWorkspaceBackend,
    onRefresh: runtime.refreshGatewayState });
  useEffect(() => { if (state.status) console.info('导航状态：', state.status); }, [state.status]);
  const openSession = useCallback(async (routeId: string, sessionId: string, options = gesture.current) => {
    // 点击时冻结目标组和打开意图，不能让异步完成后的焦点改变会话归属。
    const group = options.sideBySide ? SIDE_GROUP : groups.activeGroup.id;
    const request = (openRequests.current.get(group) ?? 0) + 1;
    openRequests.current.set(group, request);
    const session = await setActiveSession(routeId, sessionId);
    const main = await gateway.mainThread(routeId, sessionId);
    if (openRequests.current.get(group) !== request) return;
    await editors.openEditor(new ThreadInput({ ...workspaceContext.ownerFor(routeId, session.workspace_id),
      sessionId, threadId: main.thread_id, title: session.title }), { pinned: options.pinned }, group);
  }, [editors, groups, setActiveSession]);
  useEffect(() => {
    let selection: { group: number; editor: typeof editors.activeEditor } | undefined;
    const update = () => {
      const input = editors.activeEditor;
      const group = groups.activeGroup.id;
      // 恢复编辑器也会再次发出活动事件；只有实际 Tab／分组改变才取消目录打开。
      if (selection?.group === group && selection.editor === input) return;
      selection = { group, editor: input };
      setActiveEditor(input);
      openRequests.current.clear();
      clearActiveSession();
      if (input instanceof ThreadInput) void input.resolveOwner().then(owner => {
        if (editors.activeEditor !== input || groups.activeGroup.id !== group || selection?.editor !== input || selection.group !== group) return;
        return setActiveSession(owner.routeId, owner.sessionId);
      }).catch(cause => {
        if (editors.activeEditor === input && groups.activeGroup.id === group) reportError(String(cause));
      });
    };
    update();
    const listener = editors.onDidActiveEditorChange(update);
    return () => listener.dispose();
  }, [editors, groups, setActiveSession, clearActiveSession, reportError]);
  useEffect(() => { if (state.uiSettings.theme.resolved_theme) onTheme(state.uiSettings.theme.resolved_theme); }, [onTheme, state.uiSettings]);
  const actions = useSessionCatalogActions({ apiPort: 0, activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    activeSessionId: state.currentSession?.session_id ?? null, sessionsByWorkspace: state.sessionsByWorkspace,
    gatewayWorkspaces: state.gatewayWorkspaces, confirm, setStatus, onError: reportError, sessionCatalogOutboxRuntime: runtime.catalog,
    gatewayUserAccess: state.gatewayUserAccess, activateGatewayWorkspace: runtime.activateGatewayWorkspace,
    createSession: runtime.createSession, openWorkspaceSession: openSession, removeGatewayWorkspace: runtime.removeGatewayWorkspace,
    deleteSession: runtime.deleteSession, renameSession: runtime.renameSession, forkSessionContext: runtime.forkSessionContext,
    readSessionSelectionIntent: () => runtime.selectionIntentRef.current, setSessionParent: runtime.setSessionParent,
    onForkCreated: async (_source, result) => {
      const health = await getGatewayHealth(0);
      const target = requireGatewayWorkspaceRoute(state.gatewayWorkspaces, health.gateway_id, { gatewayId: result.target_gateway_id, workspaceId: result.session.workspace_id });
      await editors.openEditor(new ThreadInput({ gatewayId: result.target_gateway_id, routeId: target.workspace_id, workspaceId: result.session.workspace_id,
        sessionId: result.session.session_id, threadId: result.thread_id, title: result.session.title }), { pinned: true });
    },
  });
  const copySession = useSessionInformationClipboard(0);
  const copyWorkspace = useWorkspaceInformationClipboard(state.gatewayWorkspaces);
  return <div className="native-session-navigation" onClickCapture={event => { gesture.current = { pinned: event.detail > 1, sideBySide: event.altKey }; }}>
    <AgentSessionsPanel apiPort={0} sessions={state.sessions} currentSessionId={state.currentSession?.session_id ?? ''}
      onSelectSession={id => { if (state.activeGatewayWorkspaceId) void openSession(state.activeGatewayWorkspaceId, id).catch(cause => reportError(String(cause))); }}
      onRenameSession={actions.openRenameDialog} onDeleteSession={actions.removeSession} onSetSessionParent={actions.changeSessionParent}
      onForkSessionContext={actions.forkSession} onStatusChange={setStatus} onError={reportError} onOpenDiagnostic={(title, detail) => setDiagnostic({ title, detail })}
      isOpen workspaceName={state.workspaceName ?? ''} gatewayWorkspaces={state.gatewayWorkspaces} gatewayWorkspacesStale={state.gatewayWorkspacesStale}
      activeGatewayWorkspaceId={state.activeGatewayWorkspaceId} removingGatewayWorkspaceIds={state.removingGatewayWorkspaceIds}
      workspaceSwitching={state.workspaceSwitching} onActivateWorkspace={runtime.activateGatewayWorkspace} onSetWorkspaceParent={runtime.setWorkspaceParent}
      onRefreshWorkspaceSessions={runtime.refreshGatewayWorkspaceSessions} onRemoveWorkspace={actions.removeWorkspace}
      onAddWorkspace={runtime.addManagedGatewayWorkspace} onOpenGatewayControl={() => setGatewayOpen(true)}
      onReconnectWorkspace={runtime.reconnectGatewayWorkspace} onStartWorkspace={backendActions.start}
      onStopWorkspace={backendActions.stop} onRenameWorkspace={runtime.renameGatewayWorkspace}
      onCopySessionInformation={copySession} onCopyWorkspaceInformation={copyWorkspace} onSelectWorkspaceSession={openSession}
      onPinSession={(routeId, sessionId) => { void openSession(routeId, sessionId, { pinned: true, sideBySide: gesture.current.sideBySide }).catch(cause => reportError(String(cause))); }}
      activeSession={state.currentSession} sessionAttachmentSummaries={state.sessionAttachmentSummaries}
      activeJobIdsBySession={state.activeJobIdsBySession} unreadSessionKeys={state.unreadSessionKeys}
      onCreateSession={actions.createSessionInCatalog} onCreateSessionInFolder={actions.createSessionInFolder}
      onCreateSessionFolder={actions.createSessionFolder} onSessionFolderDeleted={async routeId => {
        await runtime.refreshGatewayWorkspaceSessions(routeId); actions.invalidateSessionCatalog(routeId);
      }} onInvalidateSessionCatalog={actions.invalidateSessionCatalog} catalogSyncKeys={actions.sessionCatalogSyncKeys}
      catalogRefreshVersions={actions.sessionCatalogRefreshVersions} sessionCatalogOutboxRuntime={runtime.catalog}
      sessionCatalogOutboxSnapshot={runtime.catalogSnapshot} gatewayUserAccess={state.gatewayUserAccess} flexRatio={1}
      preferences={resolveAgentSessionsPreferences(state.uiSettings)} onPreferencesChange={updater => {
        void runtime.updateUiSettings(current => ({ session_sidebar: updater(current.session_sidebar) })).catch(cause => reportError(String(cause)));
      }} />
    <SessionNameDialog open={actions.nameDialog !== null} title="重命名会话" label="会话名称" initialValue={actions.nameDialog?.initialTitle ?? ''}
      confirmText="保存" submitting={actions.nameDialogSubmitting} error={actions.nameDialogError} onCancel={actions.closeNameDialog} onSubmit={actions.submitNameDialog} />
    <SessionContextForkDialog state={actions.forkDialog} onCancel={actions.closeForkDialog} onSubmit={actions.submitForkDialog} onSelectionChange={actions.updateForkDialogSelection} />
    {gatewayOpen ? <div className="native-navigation-dialog" role="dialog" aria-label="Gateway 控制中心">
      <button type="button" className="sidebar-icon-button" aria-label="关闭 Gateway 控制中心" onClick={() => setGatewayOpen(false)}><span className="codicon codicon-close" /></button>
      <GatewayControlCenter apiPort={0} workspaces={state.gatewayWorkspaces} gatewayError={state.gatewayError}
        onAddSsh={runtime.addSshGatewayWorkspace} onRefresh={runtime.refreshGatewayState} onReconnect={runtime.reconnectGatewayWorkspace}
        onStart={backendActions.start} onStop={backendActions.stop}
        onSafeRestart={backendActions.safeRestart} onForceRestart={backendActions.forceRestart}
        uiSettings={state.uiSettings} onUpdateUiSettings={runtime.updateUiSettings} />
    </div> : null}
    {diagnostic ? <div className="native-navigation-dialog" role="dialog" aria-label={diagnostic.title}>
      <button type="button" className="sidebar-icon-button" aria-label="关闭诊断" onClick={() => setDiagnostic(null)}><span className="codicon codicon-close" /></button>
      <h3>{diagnostic.title}</h3><pre>{diagnostic.detail}</pre>
    </div> : null}
  </div>;
}

export class NavigationSurface {
  private readonly surface: ReactSurface;
  constructor(container: HTMLElement, editors: IEditorService, groups: IEditorGroupsService, notifications: INotificationService, storage: IStorageService) {
    this.surface = new ReactSurface(container, [...productStyles, style]);
    this.surface.render(<WarmConfirmProvider><NavigationView editors={editors} groups={groups} notifications={notifications} storage={storage} onTheme={theme => this.surface.applyTheme(theme, notifications)} /></WarmConfirmProvider>);
  }
  focus(): void { this.surface.focus('.session-resource-main-action, button'); }
  dispose(): void { this.surface.dispose(); }
}
