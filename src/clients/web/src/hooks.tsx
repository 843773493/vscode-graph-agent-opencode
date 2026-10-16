import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { DEFAULT_BACKEND_PORT } from "./api";
import type { AppState } from "./types/frontend";
import {
  messageStreamTurnIdForSession,
} from "./state/conversations";
import { useContentViewLoader } from "./hooks/content/useContentViewLoader";
import { useContentViewEffects } from "./hooks/content/useContentViewEffects";
import { useSessionTurnHistory } from "./hooks/sessionTurnHistory/useSessionTurnHistory";
import { useSessionEventStream } from "./hooks/sessionEventStream/useSessionEventStream";
import { useSessionMessageStream } from "./hooks/session/useSessionMessageStream";
import { useBackgroundSessionActivity } from "./hooks/session/useBackgroundSessionActivity";
import { useWorkspaceSessionActivity } from "./hooks/workspace/useWorkspaceSessionActivity";
import { useSessionInformationClipboard } from "./hooks/session/useSessionInformationClipboard";
import { useSessionActions } from "./hooks/session/useSessionActions";
import { useWorkspaceBootstrap } from "./hooks/workspace/useWorkspaceBootstrap";
import { useSessionViewState } from "./hooks/session/useSessionViewState";
import { useWorkspaceInformationClipboard } from "./hooks/workspace/useWorkspaceInformationClipboard";
import { useGatewayWorkspaceHierarchy } from "./hooks/gatewayWorkspace/useGatewayWorkspaceHierarchy";
import { useGatewayWorkspaceRuntimeLifecycle } from "./hooks/gatewayWorkspace/useGatewayWorkspaceRuntimeLifecycle";
import { useGatewayWorkspaceMutations } from "./hooks/gatewayWorkspace/useGatewayWorkspaceMutations";
import { useGatewayWorkspaceActivation } from "./hooks/gatewayWorkspace/useGatewayWorkspaceActivation";
import { useWorkspaceSessionSelection } from "./hooks/workspace/useWorkspaceSessionSelection";
import { useWorkspaceRefreshOrchestration } from "./hooks/workspace/useWorkspaceRefreshOrchestration";
import { useComposerStateProjection } from "./hooks/composer/useComposerStateProjection";
import { useUiSettingsController } from "./hooks/settings/useUiSettingsController";
import { sessionScopeKey } from "./state/session/sessionScope";
import { useSessionGoalController } from "./hooks/session/useSessionGoalController";
import { useUnreadSessionTracking } from "./hooks/session/useUnreadSessionTracking";
import { useSessionTraceHistory } from "./hooks/sessionTraceHistory/useSessionTraceHistory";
import {
  useSessionTurnTimeline,
  useSessionJobTurnLoader,
} from "./hooks/session/useSessionTurnTimeline";
import { errorMessage } from "./utils/errorMessage";
import { createSessionCatalogOutboxRuntime } from "./hooks/session/sessionCatalogOutboxRuntime";
import { refreshCurrentSessionCatalogOwner } from "./hooks/session/useSessionLifecycleActions";
import { requireGatewayWorkspaceRequestTarget } from "./api/workspaceIdentity";
// AppContext / ComposerContext 的契约与消费者已下沉到 hooks/app/appContext.tsx；
// 此处保持同名再导出，调用方（main.tsx、App.tsx、各组件与测试）不需要改动导入路径。
export {
  AppContext,
  ComposerContext,
  useAppState,
  useComposerState,
} from "./hooks/app/appContext";
export type {
  AppContextType,
  ComposerContextType,
} from "./hooks/app/appContext";
import { AppContext, ComposerContext } from "./hooks/app/appContext";
import { INITIAL_APP_STATE } from "./hooks/app/appStateSeed";
import { createSerialTaskQueue } from "./hooks/runtime/serialTaskQueue";

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(INITIAL_APP_STATE);
  const latestStateRef = useRef(state);
  const readState = useCallback(() => latestStateRef.current, []);
  const selectionIntentRef = useRef(0);
  const readSessionSelectionIntent = useCallback(() => selectionIntentRef.current, []);
  const gatewayListRequestRef = useRef(0);
  const gatewayListTaskQueueRef = useRef(createSerialTaskQueue());
  const runGatewayWorkspaceListTask = gatewayListTaskQueueRef.current.enqueue;
  latestStateRef.current = state;
  const currentSessionId = state.currentSession?.session_id ?? null;
  const defaultGatewayWorkspaceId =
    state.gatewayWorkspaces.find((workspace) => workspace.system_default)
      ?.workspace_id ?? state.activeGatewayWorkspaceId;
  const currentSessionGatewayWorkspaceId = currentSessionId
    ? state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId
    : state.activeGatewayWorkspaceId;
  const currentSessionWorkspaceRequestTarget = currentSessionGatewayWorkspaceId
    ? requireGatewayWorkspaceRequestTarget(
        state.gatewayWorkspaces,
        currentSessionGatewayWorkspaceId,
      )
    : null;
  const activeWorkspaceRequestTarget = state.activeGatewayWorkspaceId
    ? requireGatewayWorkspaceRequestTarget(
        state.gatewayWorkspaces,
        state.activeGatewayWorkspaceId,
      )
    : null;
  const currentSessionCacheKey =
    currentSessionId && currentSessionGatewayWorkspaceId
      ? sessionScopeKey(currentSessionGatewayWorkspaceId, currentSessionId)
      : currentSessionId;
  const { getCurrentTurnTimeline, currentTurnTimeline } = useSessionTurnTimeline(
    state,
    latestStateRef,
    currentSessionCacheKey,
  );
  const currentActiveJobId = currentSessionCacheKey
    ? state.activeJobIdsBySession.get(currentSessionCacheKey) ?? null
    : null;
  const currentMessageStreamTurnId = currentSessionId && currentSessionCacheKey
    ? messageStreamTurnIdForSession(
        currentSessionId,
        state,
        currentSessionCacheKey,
      )
    : null;
  const currentTraceHistory = currentSessionCacheKey
    ? state.sessionTraceHistoryBySession.get(currentSessionCacheKey) ?? null
    : null;
  const {
    loadOlder: loadOlderTraceHistory,
    refresh: refreshTraceHistory,
  } = useSessionTraceHistory({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSession: state.currentSession,
    workspaceId: currentSessionGatewayWorkspaceId,
    scopeKey: currentSessionCacheKey,
    active: state.contentView === "events",
    history: currentTraceHistory,
    setState,
  });
  const { refreshGoal, updateGoal, clearGoal } = useSessionGoalController({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSessionId,
    currentWorkspaceId: currentSessionGatewayWorkspaceId,
    setState,
  });
  const {
    invalidateAgentState,
    loadAgentStateMessageRawContent,
    loadSessionChangesets,
    refreshSessionResources,
    refreshSessionChanges,
    refreshAgentStateSnapshot,
    refreshLLMRequestLogs,
    reviewSessionChangeFile,
    controlSessionResource,
    switchContentView,
  } = useContentViewLoader({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSession: state.currentSession,
    currentSessionGatewayWorkspaceId,
    setState,
  });
  const {
    loadAroundTurn,
    loadNewerTurns: loadNewerMessages,
    loadOlderTurns: loadOlderMessages,
    loadTurnDetails,
    refreshTurnHistory,
  } = useSessionTurnHistory({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    getCurrentTimeline: getCurrentTurnTimeline,
    reloadNonce: state.sessionHistoryReloadNonce,
    setState,
  });
  const loadTerminalJobTurn = useSessionJobTurnLoader({
    state, loadTurnDetails, latestStateRef, sessionCacheKey: currentSessionCacheKey,
    refreshTurnHistory, setState,
  });
  const { abortCurrentStream } = useSessionEventStream({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    workspaceRequestTarget: currentSessionWorkspaceRequestTarget,
    sessionCacheKey: currentSessionCacheKey,
    activeJobId: currentActiveJobId,
    timelineReady:
      currentTurnTimeline?.phase === "ready"
      && currentTurnTimeline.projectionState === "ready",
    initialEventCursor: currentTurnTimeline?.eventCursor ?? null,
    refreshTurnHistory,
    loadTerminalJobTurn,
    setState,
  });
  useSessionMessageStream({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    turnId: currentMessageStreamTurnId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    setState,
  });
  useBackgroundSessionActivity({
    apiPort: state.apiPort,
    activeJobIdsBySession: state.activeJobIdsBySession,
    currentSessionCacheKey,
    setState,
  });
  useWorkspaceSessionActivity({
    apiPort: state.apiPort,
    workspaceTarget: activeWorkspaceRequestTarget,
    currentSessionCacheKey,
    setState,
  });

  useUnreadSessionTracking({
    unreadSessionKeys: state.unreadSessionKeys,
    currentSessionCacheKey,
    setState,
  });

  const copySessionInformation = useSessionInformationClipboard(
    state.apiPort ?? DEFAULT_BACKEND_PORT,
  );
  const copyWorkspaceInformation = useWorkspaceInformationClipboard(
    state.gatewayWorkspaces,
  );
  const setGatewayWorkspaceParent = useGatewayWorkspaceHierarchy(
    state.apiPort ?? DEFAULT_BACKEND_PORT,
    setState,
    gatewayListRequestRef,
    runGatewayWorkspaceListTask,
    selectionIntentRef,
  );

  const setStatus = useCallback((text: string) => {
    setState((prev) => ({ ...prev, status: text }));
  }, []);
  const sessionCatalogOutboxRuntime = useMemo(
    () => createSessionCatalogOutboxRuntime({
      apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
      onError: setStatus,
      refreshSessionOwner: (workspaceTarget, signal) => refreshCurrentSessionCatalogOwner(
        state.apiPort ?? DEFAULT_BACKEND_PORT, workspaceTarget,
        () => latestStateRef.current, setState, signal,
      ),
    }),
    [setStatus, state.apiPort],
  );
  useLayoutEffect(() => {
    sessionCatalogOutboxRuntime.activatePrincipal(state.gatewayUserAccess ?? null);
  }, [sessionCatalogOutboxRuntime, state.gatewayUserAccess]);
  const sessionCatalogOutboxSnapshot = useSyncExternalStore(
    sessionCatalogOutboxRuntime.subscribe,
    sessionCatalogOutboxRuntime.getSnapshot,
    sessionCatalogOutboxRuntime.getSnapshot,
  );

  const updateUiSettings = useUiSettingsController({
    apiPort: state.apiPort,
    setState,
    settings: state.uiSettings,
    isGuestView: state.gatewayUserAccess?.kind === "guest",
  });

  useEffect(() => {
    if (
      !state.uiSettingsLoaded ||
      state.uiSettings.layout.content_view === state.contentView
    ) {
      return;
    }
    void updateUiSettings({ layout: { content_view: state.contentView } });
  }, [state.contentView, state.uiSettings.layout.content_view, state.uiSettingsLoaded, updateUiSettings]);

  const {
    compactSession,
    createSession,
    forkSessionContext,
    deleteSession,
    interruptSession: interruptSessionCallback,
    renameSession,
    replayTurn,
    removePendingRequest,
    clearPendingRequests,
    updatePendingRequestPolicy,
    setSessionParent,
    selectSession: selectSessionCallback,
    selectWorkspaceSession: selectWorkspaceSessionCallback,
    sendMessage,
    switchAgent,
    switchModel,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
  } = useSessionActions({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSession: state.currentSession,
    activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    currentSessionGatewayWorkspaceId,
    workspaceRequestTargets: state.gatewayWorkspaces,
    currentSessionCacheKey,
    defaultGatewayWorkspaceId,
    contentView: state.contentView,
    readState,
    setState,
    abortCurrentStream,
    invalidateAgentState,
    refreshAgentStateSnapshot,
    sessionCatalogOutboxRuntime,
    gatewayUserAccess: state.gatewayUserAccess,
    selectionIntentRef,
  });

  const { loadSessionViewState, saveSessionViewState, toggleExpandDetails } = useSessionViewState({
    host: {
      apiPort: state.apiPort,
      currentWorkspaceId: state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId,
      currentSessionId: state.currentSession?.session_id ?? null,
      gatewayUserAccess: state.gatewayUserAccess,
      gatewayUserViewStates: state.gatewayUserViewStates,
      expandDetails: state.expandDetails,
    },
    setState,
    setStatus,
  });

  const toggleAgentSessionsPanel = useCallback(() => {
    let nextOpen: boolean | null = null;
    setState((prev) => {
      const resolvedNextOpen = !prev.agentSessionsPanelOpen;
      nextOpen = resolvedNextOpen;
      return { ...prev, agentSessionsPanelOpen: resolvedNextOpen };
    });
    if (nextOpen !== null) {
      void updateUiSettings({ layout: { agent_sessions_panel_open: nextOpen } }).catch(
        (error: unknown) => {
          const message = errorMessage(error);
          setState((prev) => ({ ...prev, status: `保存页面设置失败: ${message}` }));
        },
      );
    }
  }, [updateUiSettings]);

  const {
    invalidateWorkspaceRefreshes,
    refreshAgents,
    refreshGatewayWorkspaceStatuses,
    refreshSessions,
  } = useWorkspaceBootstrap({
    apiPort: state.apiPort,
    uiSettings: state.uiSettings,
    setState,
    selectionIntentRef,
    gatewayListRequestRef,
    runGatewayWorkspaceListTask,
  });
  useContentViewEffects({
    contentView: state.contentView,
    sessionId: currentSessionId,
    refreshLLMRequestLogs,
    refreshSessionChanges,
  });

  const {
    finishWorkspaceRefresh,
    refreshGatewayWorkspaceSessions,
    resetWorkspaceScopedState,
  } = useWorkspaceRefreshOrchestration({
    apiPort: state.apiPort,
    gatewayWorkspaces: state.gatewayWorkspaces,
    setState,
    refreshSessions,
    abortCurrentStream,
    selectionIntentRef,
  });

  const {
    activateGatewayWorkspace,
    activateGatewayWorkspaceInBackground,
    refreshGatewayState,
  } = useGatewayWorkspaceActivation({
    apiPort: state.apiPort,
    currentSessionId,
    latestStateRef,
    setState,
    invalidateWorkspaceRefreshes,
    refreshGatewayWorkspaceStatuses,
    resetWorkspaceScopedState,
    finishWorkspaceRefresh,
    selectionIntentRef,
    gatewayListRequestRef,
    runGatewayWorkspaceListTask,
  });

  const {
    selectSession,
    selectWorkspaceSession,
    openWorkspaceSession,
  } = useWorkspaceSessionSelection({
    apiPort: state.apiPort,
    latestStateRef,
    selectSession: selectSessionCallback,
    selectWorkspaceSession: selectWorkspaceSessionCallback,
    loadSessionViewState,
    activateGatewayWorkspaceInBackground,
    setStatus,
    selectionIntentRef,
  });

  const {
    reconnectGatewayWorkspace,
    safeRestartManagedGatewayWorkspaceBackend,
    startManagedGatewayWorkspaceBackend,
    stopManagedGatewayWorkspaceBackend,
    forceRestartManagedGatewayWorkspaceBackend,
    probeExternalGatewayWorkspace,
  } = useGatewayWorkspaceRuntimeLifecycle({
    apiPort: state.apiPort,
    currentSessionId,
    finishWorkspaceRefresh,
    refreshGatewayState,
    setState,
    gatewayListRequestRef,
    runGatewayWorkspaceListTask,
    selectionIntentRef,
  });

  const {
    addManagedGatewayWorkspace,
    addSshGatewayWorkspace,
    removeGatewayWorkspace,
    renameGatewayWorkspace,
    reorderGatewayWorkspaces,
  } = useGatewayWorkspaceMutations({
    apiPort: state.apiPort,
    activeGatewayWorkspaceId: state.activeGatewayWorkspaceId,
    recentLocalWorkspacePaths: state.uiSettings.recent_local_workspace_paths,
    setState,
    abortCurrentStream,
    invalidateWorkspaceRefreshes,
    finishWorkspaceRefresh,
    resetWorkspaceScopedState,
    updateUiSettings,
    gatewayListRequestRef,
    runGatewayWorkspaceListTask,
    selectionIntentRef,
  });

  const value = useMemo(
    () => ({
      state,
      sessionCatalogOutboxRuntime,
      sessionCatalogOutboxSnapshot,
      setStatus,
      sendMessage,
      replayTurn,
      removePendingRequest,
      clearPendingRequests,
      updatePendingRequestPolicy,
      loadAroundTurn,
      loadNewerMessages,
      loadOlderMessages,
      loadTurnDetails,
      loadAgentStateMessageRawContent,
      refreshTurnHistory,
      loadOlderTraceHistory,
      refreshTraceHistory,
      refreshLLMRequestLogs,
      compactSession,
      refreshGoal,
      updateGoal,
      clearGoal,
      switchAgent,
      switchModel,
      refreshAgents,
      setWorkspaceDefaultAgent,
      setWorkspaceDefaultProvider,
      interruptSession: interruptSessionCallback,
      selectSession,
      selectWorkspaceSession,
      openWorkspaceSession,
      readSessionSelectionIntent,
      createSession,
      forkSessionContext,
      renameSession,
      setSessionParent,
      deleteSession,
      refreshSessionResources,
      loadSessionChangesets,
      refreshSessionChanges,
      reviewSessionChangeFile,
      controlSessionResource,
      toggleAgentSessionsPanel,
      toggleExpandDetails,
      switchContentView,
      activateGatewayWorkspace,
      refreshGatewayState,
      reconnectGatewayWorkspace,
      startManagedGatewayWorkspaceBackend,
      stopManagedGatewayWorkspaceBackend,
      safeRestartManagedGatewayWorkspaceBackend,
      forceRestartManagedGatewayWorkspaceBackend,
      probeExternalGatewayWorkspace,
      addManagedGatewayWorkspace,
      addSshGatewayWorkspace,
      removeGatewayWorkspace,
      renameGatewayWorkspace,
      setGatewayWorkspaceParent,
      refreshGatewayWorkspaceSessions,
      reorderGatewayWorkspaces,
      copySessionInformation,
      copyWorkspaceInformation,
      updateUiSettings,
      saveSessionViewState,
    }),
    [
      state,
      sessionCatalogOutboxRuntime,
      sessionCatalogOutboxSnapshot,
      setStatus,
      sendMessage,
      replayTurn,
      removePendingRequest,
      clearPendingRequests,
      updatePendingRequestPolicy,
      loadAroundTurn,
      loadNewerMessages,
      loadOlderMessages,
      loadTurnDetails,
      loadAgentStateMessageRawContent,
      refreshTurnHistory,
      loadOlderTraceHistory,
      refreshTraceHistory,
      compactSession,
      refreshGoal,
      updateGoal,
      clearGoal,
      switchAgent,
      switchModel,
      refreshAgents,
      setWorkspaceDefaultAgent,
      setWorkspaceDefaultProvider,
      interruptSessionCallback,
      selectSession,
      selectWorkspaceSession,
      openWorkspaceSession,
      readSessionSelectionIntent,
      createSession,
      forkSessionContext,
      renameSession,
      setSessionParent,
      deleteSession,
      refreshSessionResources,
      loadSessionChangesets,
      refreshSessionChanges,
      reviewSessionChangeFile,
      controlSessionResource,
      toggleAgentSessionsPanel,
      toggleExpandDetails,
      refreshLLMRequestLogs,
      switchContentView,
      activateGatewayWorkspace,
      refreshGatewayState,
      reconnectGatewayWorkspace,
      startManagedGatewayWorkspaceBackend,
      stopManagedGatewayWorkspaceBackend,
      safeRestartManagedGatewayWorkspaceBackend,
      forceRestartManagedGatewayWorkspaceBackend,
      probeExternalGatewayWorkspace,
      addManagedGatewayWorkspace,
      addSshGatewayWorkspace,
      removeGatewayWorkspace,
      renameGatewayWorkspace,
      setGatewayWorkspaceParent,
      refreshGatewayWorkspaceSessions,
      reorderGatewayWorkspaces,
      copySessionInformation,
      copyWorkspaceInformation,
      updateUiSettings,
      saveSessionViewState,
    ],
  );

  const { composerValue } = useComposerStateProjection({
    state,
    currentSessionCacheKey,
    latestStateRef,
    setStatus,
    sendMessage,
    compactSession,
    refreshGoal,
    updateGoal,
    clearGoal,
    interruptSession: interruptSessionCallback,
    switchAgent,
    switchModel,
    refreshAgents,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
    switchContentView,
    createSession,
    renameSession,
    updateUiSettings,
  });

  return (
    <AppContext.Provider value={value}>
      <ComposerContext.Provider value={composerValue}>
        {children}
      </ComposerContext.Provider>
    </AppContext.Provider>
  );
}
