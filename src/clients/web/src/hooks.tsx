import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
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
  useTerminalTurnLoader,
} from "./hooks/session/useSessionTurnTimeline";
import { errorMessage } from "./utils/errorMessage";
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

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(INITIAL_APP_STATE);
  const latestStateRef = useRef(state);
  latestStateRef.current = state;
  const currentSessionId = state.currentSession?.session_id ?? null;
  const defaultGatewayWorkspaceId =
    state.gatewayWorkspaces.find((workspace) => workspace.system_default)
      ?.workspace_id ?? state.activeGatewayWorkspaceId;
  const currentSessionGatewayWorkspaceId = currentSessionId
    ? state.currentSessionWorkspaceId ?? state.activeGatewayWorkspaceId
    : state.activeGatewayWorkspaceId;
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
  const loadTerminalTurn = useTerminalTurnLoader(loadTurnDetails);
  const { abortCurrentStream } = useSessionEventStream({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    activeJobId: currentActiveJobId,
    timelineReady:
      currentTurnTimeline?.phase === "ready"
      && currentTurnTimeline.projectionState === "ready",
    initialEventCursor: currentTurnTimeline?.eventCursor ?? null,
    refreshTurnHistory,
    loadTerminalTurn,
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
    workspaceId: state.activeGatewayWorkspaceId,
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
  );

  const setStatus = useCallback((text: string) => {
    setState((prev) => ({ ...prev, status: text }));
  }, []);

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
    updatePendingRequest,
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
    currentSessionCacheKey,
    defaultGatewayWorkspaceId,
    contentView: state.contentView,
    setState,
    abortCurrentStream,
    invalidateAgentState,
    refreshAgentStateSnapshot,
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
    setState,
    refreshSessions,
    abortCurrentStream,
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
  });

  const value = useMemo(
    () => ({
      state,
      setStatus,
      sendMessage,
      replayTurn,
      updatePendingRequest,
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
      setStatus,
      sendMessage,
      replayTurn,
      updatePendingRequest,
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
