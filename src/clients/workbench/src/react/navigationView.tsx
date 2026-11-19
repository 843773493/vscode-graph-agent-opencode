import { t } from '../../../shared/core/i18n/index.js';
import { gatewayWorkspaceKey, workspaceOwnerFromKey } from "../../../web/src/api/workspaceIdentity.js";
import { useCallback, useEffect, useRef, useState } from 'react';
import AgentSessionsPanel from '../../../web/src/components/panels/sessionPanels/AgentSessionsPanel.js';
import WarmConfirmProvider, { useWarmConfirm } from '../../../web/src/components/shell/WarmConfirmProvider.js';
import SessionNameDialog from '../../../web/src/components/overlays/SessionNameDialog.js';
import SessionContextForkDialog from '../../../web/src/components/overlays/SessionContextForkDialog.js';
import type { WorkspaceDetailsTarget } from '../../../web/src/components/workspace/WorkspaceDetailsDialog.js';
import { useSessionCatalogActions } from '../../../web/src/hooks/shell/useSessionCatalogActions.js';
import { useSessionInformationClipboard } from '../../../web/src/hooks/session/useSessionInformationClipboard.js';
import { useWorkspaceInformationClipboard } from '../../../web/src/hooks/workspace/useWorkspaceInformationClipboard.js';
import { resolveAgentSessionsPreferences } from '../../../web/src/state/uiSettings/preferences.js';
import { gatewayWorkspaceOwner, requireGatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import type { GatewayWorkspaceRequestTarget } from '../../../web/src/api/workspaceIdentity.js';
import { getSession } from '../../../web/src/api.js';
import { HttpRequestError, onGatewayTokenInvalidated } from '../../../web/src/api/http.js';
import type { AddRemoteGatewayConnectionRequest, GatewayRuntimeGroupRequest, GatewayWorkspace, Session } from '../../../web/src/types/backend.js';
import { IEditorService, SIDE_GROUP } from '../../upstream/src/vs/workbench/services/editor/common/editorService.js';
import { EditorsOrder, type IEditorIdentifier } from '../../upstream/src/vs/workbench/common/editor.js';
import type { IEditorGroupsService } from '../../upstream/src/vs/workbench/services/editor/common/editorGroupsService.js';
import type { INotificationService } from '../../upstream/src/vs/platform/notification/common/notification.js';
import { IStorageService, WillSaveStateReason } from '../../upstream/src/vs/platform/storage/common/storage.js';
import type { ICommandService } from '../../upstream/src/vs/platform/commands/common/commands.js';
import { DeferredPromise } from '../../upstream/src/vs/base/common/async.js';
import { ThreadInput } from '../threadInput.js';
import { ThreadPane, type ThreadEditorOptions } from '../threadPane.js';
import { workspaceContext } from '../workspaceContext.js';
import { ReactSurface } from './surface.js';
import { productStyles } from './productStyles.js';
import { useNavigationRuntime } from './useNavigationRuntime.js';
import { useManagedBackendConfirmation } from '../../../web/src/hooks/gatewayWorkspace/useManagedBackendConfirmation.js';
import { sessionScopeKey } from '../../../web/src/state/session/sessionScope.js';
import style from './navigationView.css?text';
import type { CatalogRevealRequest } from '../../../web/src/hooks/sessionResourceExplorer/useCatalogReveal.js';
import { workspaceActionCommands, type WorkspaceAction, type WorkspaceActionHandlers } from '../search/workspaceActions.js';
import { sessionActionCommands, type SessionAction, type SessionActionHandlers } from '../search/sessionActions.js';
import type { SessionForkSourceSelection } from '../../../web/src/hooks/shell/useSessionForkDialog.js';

function NavigationView({ editors, groups, notifications, storage, commands, bindWorkspaceActions, bindSessionActions, bindGatewayActions, catalogReveal }: {
  editors: IEditorService; groups: IEditorGroupsService; notifications: INotificationService;
  storage: IStorageService;
  commands: ICommandService;
  bindWorkspaceActions: (actions?: WorkspaceActionHandlers) => void;
  bindSessionActions: (actions?: SessionActionHandlers) => void;
  bindGatewayActions: (actions?: GatewayNavigationActions) => void;
  catalogReveal?: CatalogRevealRequest;
}) {
  const confirm = useWarmConfirm();
  const [workspaceDetails, setWorkspaceDetails] = useState<WorkspaceDetailsTarget | null>(null);
  const [diagnostic, setDiagnostic] = useState<{ title: string; detail: string } | null>(null);
  const [activeEditor, setActiveEditor] = useState(() => editors.activeEditor);
  const gesture = useRef({ pinned: false, sideBySide: false });
  const openRequests = useRef(new Map<number, number>());
  const created = useCallback(async (session: Session, workspaceKey: string | null) => {
    if (!workspaceKey || !session.thread_id) throw new Error(t('navigation.sessions.createdReceiptMissing'));
    await editors.openEditor(new ThreadInput({ ...workspaceContext.ownerFor(workspaceKey, session.workspace_id),
      sessionId: session.session_id, threadId: session.thread_id, title: session.title }), { pinned: true });
  }, [editors]);
  const notifyError = useCallback((message: string) => { notifications.error(message); }, [notifications]);
  const refreshEditors = useCallback(async (target: GatewayWorkspaceRequestTarget, signal: AbortSignal) => {
    const snapshot = workspaceContext.getSnapshot();
    const workspace = snapshot.workspaces.find(item => gatewayWorkspaceKey(item) === gatewayWorkspaceKey(target));
    if (!workspace || !snapshot.gatewayId) throw new Error(t('navigation.catalog.editorOwnerMissing'));
    const owner = gatewayWorkspaceOwner(workspace);
    const matchingEditors = () => editors.getEditors(EditorsOrder.SEQUENTIAL).filter((entry): entry is IEditorIdentifier & { editor: ThreadInput } =>
      entry.editor instanceof ThreadInput && entry.editor.owner.gatewayId === owner.gatewayId
        && entry.editor.owner.workspaceId === owner.workspaceId);
    const sessionIds = new Set(matchingEditors().map(({ editor }) => editor.owner.sessionId));
    await Promise.all([...sessionIds].map(async sessionId => {
      let session: Session;
      try {
        session = await getSession(0, sessionId, gatewayWorkspaceKey(target), { signal });
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
        throw new Error(t('navigation.catalog.editorOwnerMismatch', target.workspace_id, sessionId));
      }
      for (const { editor } of matchingEditors()) {
        if (editor.owner.sessionId === sessionId && editor.owner.threadId === session.thread_id) editor.setTitle(session.title);
      }
    }));
  }, [editors, storage]);
  const activeSessionCacheKey = activeEditor instanceof ThreadInput && activeEditor.owner.workspaceKey
    ? sessionScopeKey(activeEditor.owner.workspaceKey, activeEditor.owner.sessionId) : null;
  const runtime = useNavigationRuntime(created, notifyError, refreshEditors, activeSessionCacheKey);
  const { state, setStatus, reportError, setActiveSession, clearActiveSession } = runtime;
  const showWorkspaceDetails = useCallback(async (workspaceKey: string) => {
    requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, workspaceKey);
    if (!state.browserViewScope) throw new Error(t('workbench.managementTarget.gatewayNotReady'));
    setWorkspaceDetails({ workspaceKey, entryGatewayId: state.browserViewScope });
  }, [state.gatewayWorkspaces, state.browserViewScope]);
  useEffect(() => onGatewayTokenInvalidated(0, () => {
    setWorkspaceDetails(null);
  }), []);
  useEffect(() => {
    if (workspaceDetails && state.browserViewScope !== workspaceDetails.entryGatewayId) setWorkspaceDetails(null);
  }, [state.browserViewScope, workspaceDetails]);
  const backendActions = useManagedBackendConfirmation({ apiPort: 0,
    onStart: runtime.startManagedGatewayWorkspaceBackend, onStop: runtime.stopManagedGatewayWorkspaceBackend,
    onSafeRestart: runtime.safeRestartManagedGatewayWorkspaceBackend, onForceRestart: runtime.forceRestartManagedGatewayWorkspaceBackend,
    onRefresh: runtime.refreshGatewayState });
  useEffect(() => {
    bindWorkspaceActions({ activate: id => runtime.activateGatewayWorkspace(id), reconnect: runtime.reconnectGatewayWorkspace, start: backendActions.start,
      stop: backendActions.stop, safeRestart: backendActions.safeRestart, forceRestart: backendActions.forceRestart, details: showWorkspaceDetails });
    return () => bindWorkspaceActions();
  }, [bindWorkspaceActions, runtime.activateGatewayWorkspace, runtime.reconnectGatewayWorkspace, backendActions.start, backendActions.stop, backendActions.safeRestart, backendActions.forceRestart, showWorkspaceDetails]);
  useEffect(() => {
    bindGatewayActions({ addRemoteGatewayConnection: runtime.addRemoteGatewayConnection, refresh: runtime.refreshGatewayState });
    return () => bindGatewayActions();
  }, [bindGatewayActions, runtime.addRemoteGatewayConnection, runtime.refreshGatewayState]);
  const activateWorkspace = useCallback((id: string) => commands.executeCommand<void>(workspaceActionCommands.activate, workspaceOwnerFromKey(id)), [commands]);
  const reconnectWorkspace = useCallback((id: string) => commands.executeCommand<void>(workspaceActionCommands.reconnect, workspaceOwnerFromKey(id)), [commands]);
  const startWorkspace = useCallback((id: string) => commands.executeCommand<void>(workspaceActionCommands.start, workspaceOwnerFromKey(id)), [commands]);
  const detailsWorkspace = useCallback((id: string) => commands.executeCommand<void>(workspaceActionCommands.details, workspaceOwnerFromKey(id)), [commands]);
  const openWorkspaceSettings = async (workspace: GatewayWorkspace, connectionId?: string) => {
    const current = requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, gatewayWorkspaceOwner(workspace));
    if (current.connection_kind === 'remote_gateway' && !current.remote_gateways.some(connection => connection.gateway_connection_id === connectionId)) {
      throw new Error(t('workbench.workspaceAction.identityChanged'));
    }
    await commands.executeCommand('boxteam.settings.openTarget', { gateway_id: current.gateway_id, name: current.name,
      connection_kind: current.connection_kind, gateway_connection_id: connectionId }, { workspace_id: current.workspace_id });
  };
  const openWorkspaceLogs = async (workspace: GatewayWorkspace, connectionId?: string) => {
    const current = requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, gatewayWorkspaceOwner(workspace));
    if (current.connection_kind === 'remote_gateway' && !current.remote_gateways.some(connection => connection.gateway_connection_id === connectionId)) {
      throw new Error(t('workbench.workspaceAction.identityChanged'));
    }
    await commands.executeCommand('boxteam.gateway.diagnostics', { gateway_id: current.gateway_id, gateway_connection_id: connectionId,
      name: current.name, connection_kind: current.connection_kind }, current.workspace_id);
  };
  useEffect(() => { if (state.status) console.info(t('navigation.statusPrefix'), state.status); }, [state.status]);
  const openSession = useCallback(async (workspaceKey: string, sessionId: string, options = gesture.current) => {
    // 点击时冻结目标组和打开意图，不能让异步完成后的焦点改变会话归属。
    const group = options.sideBySide ? SIDE_GROUP : groups.activeGroup.id;
    const request = (openRequests.current.get(group) ?? 0) + 1;
    openRequests.current.set(group, request);
    const session = await setActiveSession(workspaceKey, sessionId);
    // Session DTO 已携带 catalog 冻结的 main Thread，不再串行查询同一身份。
    if (session.session_id !== sessionId || !session.thread_id) throw new Error(t('workbench.session.mainThreadOwnerMismatch'));
    if (openRequests.current.get(group) !== request) return;
    const editorOptions: ThreadEditorOptions = { pinned: options.pinned, boxteamSession: session };
    await editors.openEditor(new ThreadInput({ ...workspaceContext.ownerFor(workspaceKey, session.workspace_id),
      sessionId, threadId: session.thread_id, title: session.title }), editorOptions, group);
  }, [editors, groups, setActiveSession]);
  useEffect(() => {
    let selection: { group: number; editor: typeof editors.activeEditor } | undefined;
    const update = () => {
      const input = editors.activeEditor;
      const group = groups.activeGroup.id;
      // 恢复编辑器也会再次发出活动事件；只有实际 Tab／分组改变才取消目录打开。
      if (selection?.group === group && selection.editor === input) return;
      selection = undefined;
      setActiveEditor(input);
      openRequests.current.clear();
      clearActiveSession();
      const pane = editors.activeEditorPane;
      // 冷恢复先撤销旧打开意图，等所属 Pane 完成后才使用已解析的路由。
      if (input instanceof ThreadInput && (!(pane instanceof ThreadPane) || pane.threadTarget?.input !== input)) return;
      selection = { group, editor: input };
      // 原版在 ThreadPane.setInput 完成后发布活动事件，owner 已由所属 Pane 解析。
      if (input instanceof ThreadInput) void setActiveSession(input.owner.workspaceKey, input.owner.sessionId).catch(cause => {
        if (editors.activeEditor === input && groups.activeGroup.id === group) reportError(String(cause));
      });
    };
    update();
    const listener = editors.onDidActiveEditorChange(update);
    return () => listener.dispose();
  }, [editors, groups, setActiveSession, clearActiveSession, reportError]);
  const actions = useSessionCatalogActions({ apiPort: 0, activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    activeSessionId: state.currentSession?.session_id ?? null,
    gatewayWorkspaces: state.gatewayWorkspaces, confirm, setStatus, onError: reportError, sessionCatalogOutboxRuntime: runtime.catalog, activateGatewayWorkspace: runtime.activateGatewayWorkspace,
    createSession: runtime.createSession, openWorkspaceSession: openSession, removeGatewayWorkspace: runtime.removeGatewayWorkspace,
    deleteSession: runtime.deleteSession, renameSession: runtime.renameSession, forkSessionContext: runtime.forkSessionContext,
    readSessionSelectionIntent: () => runtime.selectionIntentRef.current, setSessionParent: runtime.setSessionParent,
    onForkCreated: async (_source, result) => {
      const target = requireGatewayWorkspaceRequestTarget(state.gatewayWorkspaces, { gatewayId: result.target_gateway_id, workspaceId: result.session.workspace_id });
      await editors.openEditor(new ThreadInput({ gatewayId: result.target_gateway_id, workspaceKey: gatewayWorkspaceKey(target), workspaceId: result.session.workspace_id,
        sessionId: result.session.session_id, threadId: result.thread_id, title: result.session.title }), { pinned: true });
    },
  });
  const copySession = useSessionInformationClipboard(0);
  const copyWorkspace = useWorkspaceInformationClipboard(state.gatewayWorkspaces);
  useEffect(() => {
    bindSessionActions({ rename: actions.openRenameDialog, delete: actions.removeSession,
      fork: (id, title, key, assertAccess, source) => actions.forkSession(key, id, source, { title, assertAccess }) });
    return () => bindSessionActions();
  }, [bindSessionActions, actions.openRenameDialog, actions.removeSession, actions.forkSession]);
  const runSessionAction = (action: SessionAction, sessionId: string, _title: string, workspaceKey: string) => {
    void commands.executeCommand(sessionActionCommands[action], { owner: workspaceOwnerFromKey(workspaceKey), sessionId })
      .catch(cause => reportError(String(cause)));
  };
  return <div className="native-session-navigation" onClickCapture={event => { gesture.current = { pinned: event.detail > 1, sideBySide: event.altKey }; }}>
    <AgentSessionsPanel catalogReveal={catalogReveal} apiPort={0} sessions={state.sessions} currentSessionId={state.currentSession?.session_id ?? ''}
      currentSessionWorkspaceId={state.currentSessionWorkspaceId}
      onSelectSession={id => { if (state.activeGatewayWorkspaceId) void openSession(state.activeGatewayWorkspaceId, id).catch(cause => reportError(String(cause))); }}
      onRenameSession={(...args) => runSessionAction('rename', ...args)} onDeleteSession={(...args) => runSessionAction('delete', ...args)} onSetSessionParent={actions.changeSessionParent}
      onForkSessionContext={(workspaceKey, sessionId) => commands.executeCommand(sessionActionCommands.fork, { owner: workspaceOwnerFromKey(workspaceKey), sessionId })}
      onStatusChange={setStatus} onError={reportError} onOpenDiagnostic={(title, detail) => setDiagnostic({ title, detail })}
      isOpen workspaceName={state.workspaceName ?? ''} gatewayWorkspaces={state.gatewayWorkspaces} gatewayWorkspacesStale={state.gatewayWorkspacesStale}
      activeGatewayWorkspaceId={state.activeGatewayWorkspaceId} removingGatewayWorkspaceIds={state.removingGatewayWorkspaceIds}
      workspaceSwitching={state.workspaceSwitching} onActivateWorkspace={activateWorkspace} onSetWorkspaceParent={runtime.setWorkspaceParent}
      onRefreshWorkspaceSessions={runtime.refreshGatewayWorkspaceSessions} onRemoveWorkspace={actions.removeWorkspace}
      onAddWorkspace={runtime.addManagedGatewayWorkspace} onOpenGatewayControl={() => {
        void commands.executeCommand('boxteam.gateway.connections').catch(cause => reportError(String(cause)));
      }}
      workspaceDetails={workspaceDetails} onCloseWorkspaceDetails={() => setWorkspaceDetails(current => current === workspaceDetails ? null : current)}
      onOpenWorkspaceDetails={detailsWorkspace} onOpenWorkspaceSettings={openWorkspaceSettings} onOpenWorkspaceLogs={openWorkspaceLogs}
      onReconnectWorkspace={reconnectWorkspace} onStartWorkspace={startWorkspace}
      onStopWorkspace={backendActions.stop} onRenameWorkspace={runtime.renameGatewayWorkspace}
      onCopySessionInformation={copySession} onCopyWorkspaceInformation={copyWorkspace} onSelectWorkspaceSession={openSession}
      onPinSession={(workspaceKey, sessionId) => { void openSession(workspaceKey, sessionId, { pinned: true, sideBySide: gesture.current.sideBySide }).catch(cause => reportError(String(cause))); }}
      activeSession={state.currentSession} sessionAttachmentSummaries={state.sessionAttachmentSummaries}
      activeJobIdsBySession={state.activeJobIdsBySession} unreadSessionKeys={state.unreadSessionKeys}
      onCreateSession={actions.createSessionInCatalog} onCreateSessionInFolder={actions.createSessionInFolder}
      onCreateSessionFolder={actions.createSessionFolder} onSessionFolderDeleted={async workspaceKey => {
        await runtime.refreshGatewayWorkspaceSessions(workspaceKey); actions.invalidateSessionCatalog(workspaceKey);
      }} onInvalidateSessionCatalog={actions.invalidateSessionCatalog}
      catalogRefreshVersions={actions.sessionCatalogRefreshVersions} sessionCatalogOutboxRuntime={runtime.catalog}
      sessionCatalogOutboxSnapshot={runtime.catalogSnapshot} flexRatio={1}
      preferences={resolveAgentSessionsPreferences(state.uiSettings)} onPreferencesChange={updater => {
        void runtime.updateUiSettings(current => ({ session_sidebar: updater(current.session_sidebar) })).catch(cause => reportError(String(cause)));
      }} />
    <SessionNameDialog open={actions.nameDialog !== null} title={t('navigation.sessions.rename')} label={t('navigation.sessions.name')} initialValue={actions.nameDialog?.initialTitle ?? ''}
      confirmText={t('navigation.save')} submitting={actions.nameDialogSubmitting} error={actions.nameDialogError} onCancel={actions.closeNameDialog} onSubmit={actions.submitNameDialog} />
    <SessionContextForkDialog state={actions.forkDialog} onCancel={actions.closeForkDialog} onSubmit={actions.submitForkDialog} onSelectionChange={actions.updateForkDialogSelection} />
    {diagnostic ? <div className="native-navigation-dialog" role="dialog" aria-label={diagnostic.title}>
      <button type="button" className="sidebar-icon-button" aria-label={t('navigation.diagnostic.close')} onClick={() => setDiagnostic(null)}><span className="codicon codicon-close" /></button>
      <h3>{diagnostic.title}</h3><pre>{diagnostic.detail}</pre>
    </div> : null}
  </div>;
}

export interface GatewayNavigationActions {
  addRemoteGatewayConnection: (payload: AddRemoteGatewayConnectionRequest) => Promise<void>;
  refresh: () => Promise<void>;
}

export class NavigationSurface {
  private catalogReveal?: CatalogRevealRequest;
  private readonly surface: ReactSurface;
  private readonly render: (request?: CatalogRevealRequest) => void;
  private gatewayActions?: GatewayNavigationActions;
  private gatewayReady?: DeferredPromise<GatewayNavigationActions>;
  private reveal?: AbortController;
  private workspaceActions?: WorkspaceActionHandlers;
  private actionsReady?: DeferredPromise<WorkspaceActionHandlers>;
  private sessionActions?: SessionActionHandlers;
  private sessionActionsReady?: DeferredPromise<SessionActionHandlers>;
  constructor(container: HTMLElement, editors: IEditorService, groups: IEditorGroupsService, notifications: INotificationService, storage: IStorageService, commands: ICommandService) {
    this.surface = new ReactSurface(container, [...productStyles, style]);
    const bindWorkspaceActions = (actions?: WorkspaceActionHandlers) => {
      this.workspaceActions = actions;
      if (actions && this.actionsReady) { void this.actionsReady.complete(actions); this.actionsReady = undefined; }
    };
    const bindSessionActions = (actions?: SessionActionHandlers) => {
      this.sessionActions = actions;
      if (actions && this.sessionActionsReady) { void this.sessionActionsReady.complete(actions); this.sessionActionsReady = undefined; }
    };
    const bindGatewayActions = (actions?: GatewayNavigationActions) => {
      this.gatewayActions = actions;
      if (actions && this.gatewayReady) { void this.gatewayReady.complete(actions); this.gatewayReady = undefined; }
    };
    this.render = (catalogReveal = this.catalogReveal) => { this.catalogReveal = catalogReveal; this.surface.render(<WarmConfirmProvider><NavigationView editors={editors} groups={groups}
      notifications={notifications} storage={storage} commands={commands} bindWorkspaceActions={bindWorkspaceActions} bindSessionActions={bindSessionActions}
      bindGatewayActions={bindGatewayActions} catalogReveal={catalogReveal} /></WarmConfirmProvider>); };
    this.render();
  }
  focus(): void { this.surface.focus('.session-resource-main-action, button'); }
  async runWorkspaceAction(action: WorkspaceAction, workspaceKey: string, assertAccess: () => void, request?: GatewayRuntimeGroupRequest): Promise<void> {
    if (!this.workspaceActions) await (this.actionsReady ??= new DeferredPromise<WorkspaceActionHandlers>()).p;
    assertAccess();
    if (!this.workspaceActions) throw new Error(t('navigation.workspace.navigationClosedAction'));
    await this.workspaceActions[action](workspaceKey, request);
  }
  async runGatewayAction(action: 'refresh' | AddRemoteGatewayConnectionRequest, assertAccess: () => void): Promise<void> {
    if (!this.gatewayActions) await (this.gatewayReady ??= new DeferredPromise<GatewayNavigationActions>()).p;
    assertAccess();
    if (!this.gatewayActions) throw new Error(t('workbench.workspaceAction.navigationUnavailable'));
    if (action === 'refresh') await this.gatewayActions.refresh();
    else await this.gatewayActions.addRemoteGatewayConnection(action);
  }
  async runSessionAction(action: SessionAction, sessionId: string, title: string, workspaceKey: string, assertAccess: () => void, source?: SessionForkSourceSelection): Promise<void> {
    if (!this.sessionActions) await (this.sessionActionsReady ??= new DeferredPromise<SessionActionHandlers>()).p;
    assertAccess();
    if (!this.sessionActions) throw new Error(t('navigation.sessions.navigationClosedAction'));
    await this.sessionActions[action](sessionId, title, workspaceKey, assertAccess, source);
  }
  revealFolder(target: GatewayWorkspaceRequestTarget, nodeId: string, focus: boolean): Promise<void> {
    this.reveal?.abort(new Error(t('navigation.catalog.revealSuperseded')));
    const controller = this.reveal = new AbortController();
    return new Promise<void>((complete, fail) => {
      controller.signal.addEventListener('abort', () => fail(controller.signal.reason), { once: true });
      this.render({ target, nodeId, focus, signal: controller.signal, complete, fail });
    }).finally(() => {
      controller.abort();
      if (this.reveal === controller) this.reveal = undefined;
    });
  }
  dispose(): void {
    this.reveal?.abort(new Error(t('navigation.sessions.navigationClosed')));
    if (this.actionsReady) void this.actionsReady.error(new Error(t('navigation.workspace.navigationClosedAction')));
    if (this.sessionActionsReady) void this.sessionActionsReady.error(new Error(t('navigation.sessions.navigationClosedAction')));
    if (this.gatewayReady) void this.gatewayReady.error(new Error(t('workbench.workspaceAction.navigationUnavailable')));
    this.gatewayActions = undefined;
    this.sessionActions = undefined;
    this.workspaceActions = undefined;
    this.surface.dispose();
  }
}
