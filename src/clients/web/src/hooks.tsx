import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { DEFAULT_BACKEND_PORT } from "./api";
import type { TurnHistoryInclude } from "./api/sessionTurnHistory";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayRuntimeRestartResult,
  AttachmentRef,
  MessageReplayRequest,
  DeliveryPolicy,
  SessionResourceAction,
  SessionResourceKind,
  SessionFileChange,
  Session,
  SessionCompactResult,
  SessionGoal,
  SessionGoalUpdateRequest,
  SessionChangesetList,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "./types/backend";
import type {
  AppState,
  ConversationContentView,
} from "./types/frontend";
import {
  getConversationsForSession,
  messageStreamTurnIdForSession,
} from "./state/conversations";
import { useContentViewLoader } from "./hooks/useContentViewLoader";
import { useContentViewEffects } from "./hooks/useContentViewEffects";
import { useSessionTurnHistory } from "./hooks/sessionTurnHistory/useSessionTurnHistory";
import { useSessionEventStream } from "./hooks/sessionEventStream/useSessionEventStream";
import { useSessionMessageStream } from "./hooks/useSessionMessageStream";
import { useBackgroundSessionActivity } from "./hooks/useBackgroundSessionActivity";
import { useWorkspaceSessionActivity } from "./hooks/workspace/useWorkspaceSessionActivity";
import { useSessionInformationClipboard } from "./hooks/session/useSessionInformationClipboard";
import { useSessionActions } from "./hooks/session/useSessionActions";
import { useWorkspaceBootstrap } from "./hooks/workspace/useWorkspaceBootstrap";
import {
  useSessionViewState,
  type SessionViewStatePayload,
} from "./hooks/session/useSessionViewState";
import { useWorkspaceInformationClipboard } from "./hooks/workspace/useWorkspaceInformationClipboard";
import { useGatewayWorkspaceHierarchy } from "./hooks/gatewayWorkspace/useGatewayWorkspaceHierarchy";
import { useGatewayWorkspaceRuntimeLifecycle } from "./hooks/gatewayWorkspace/useGatewayWorkspaceRuntimeLifecycle";
import { useGatewayWorkspaceMutations } from "./hooks/gatewayWorkspace/useGatewayWorkspaceMutations";
import { useGatewayWorkspaceActivation } from "./hooks/gatewayWorkspace/useGatewayWorkspaceActivation";
import { useWorkspaceSessionSelection } from "./hooks/workspace/useWorkspaceSessionSelection";
import { useComposerStateProjection } from "./hooks/useComposerStateProjection";
import { useUiSettingsController } from "./hooks/useUiSettingsController";
import {
  readCachedUiSettings,
  readUnreadSessionKeys,
  writeUnreadSessionKeys,
} from "./state/storage";
import { sessionScopeKey } from "./state/session/sessionScope";
import { cloneMaps } from "./state/appStateMaps";
import { useSessionGoalController } from "./hooks/session/useSessionGoalController";
import { useSessionTraceHistory } from "./hooks/sessionTraceHistory/useSessionTraceHistory";
import type { ComposerStateSnapshot } from "./state/composerState";
import { refreshWorkspaceSessionList } from "./hooks/sessionEventStream/sessionRefresh";
import {
  useSessionTurnTimeline,
  useTerminalTurnLoader,
} from "./hooks/session/useSessionTurnTimeline";

export { getConversationsForSession } from "./state/conversations";
export { FRONTEND_EVENT_QUEUE_LIMIT } from "./state/traceEvents";

const CACHED_UI_SETTINGS = readCachedUiSettings();
const CACHED_UNREAD_SESSION_KEYS = readUnreadSessionKeys();

const INITIAL_STATE: AppState = {
  apiPort: DEFAULT_BACKEND_PORT,
  gatewayWorkspaces: [],
  activeGatewayWorkspaceId: null,
  sessionsByWorkspace: new Map(),
  sessionGatewayWorkspaceById: new Map(),
  removingGatewayWorkspaceIds: new Set(),
  sessionHistoryReloadNonce: 0,
  workspaceSwitching: false,
  gatewayError: null,
  gatewayUserAccess: null,
  gatewayUserViewStates: new Map(),
  uiSettings: CACHED_UI_SETTINGS,
  uiSettingsLoaded: false,
  workspaceRoot: null,
  workspaceName: null,
  agents: [],
  sessions: [],
  sessionAttachmentSummaries: new Map(),
  currentSession: null,
  currentSessionWorkspaceId: null,
  turnTimelinesBySession: new Map(),
  traceEvents: [],
  messageStreamsByTurnStream: new Map(),
  llmRequestLogs: [],
  llmRequestLogsLoadedAt: null,
  llmRequestLogsLoading: false,
  llmRequestLogsError: null,
  sessionChangesets: [],
  selectedChangesetId: null,
  activeChangeset: null,
  sessionChangesLoadedAt: null,
  sessionChangesLoading: false,
  sessionChangesError: null,
  sessionResources: [],
  sessionResourcesLoadedAt: null,
  sessionResourcesLoading: false,
  sessionResourcesError: null,
  eventQueuesBySession: new Map(),
  sessionTraceHistoryBySession: new Map(),
  pendingConversations: new Map(),
  activeJobIdsBySession: new Map(),
  unreadSessionKeys: CACHED_UNREAD_SESSION_KEYS,
  status: "准备就绪",
  error: null,
  isBootstrapping: true,
  expandDetails: false,
  agentSessionsPanelOpen: true,
  contentView: "default",
  agentStateJsonl: "",
  agentStateMessageCount: 0,
  agentStateLoadedAt: null,
  agentStateLoading: false,
  agentStateError: null,
  compactLoading: false,
  lastCompactResult: null,
  currentGoal: null,
  currentGoalSessionId: null,
  goalLoading: false,
  goalError: null,
};

interface AppContextType {
  state: AppState;
  setStatus: (text: string) => void;
  sendMessage: (
    content: string,
    attachments?: AttachmentRef[],
    deliveryPolicy?: DeliveryPolicy,
  ) => Promise<void>;
  updatePendingRequest: (
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  removePendingRequest: (messageId: string) => Promise<void>;
  clearPendingRequests: () => Promise<void>;
  updatePendingRequestPolicy: (
    messageId: string,
    deliveryPolicy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
  loadAroundTurn: (anchorTurnId: string) => Promise<void>;
  loadNewerMessages: () => Promise<void>;
  loadOlderMessages: () => Promise<void>;
  loadTurnDetails: (
    turnIds: string[],
    requestIdentity?: string | null,
    refreshAfterInFlight?: boolean,
    include?: TurnHistoryInclude[],
    toolCallIds?: string[],
  ) => Promise<void>;
  loadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  refreshTurnHistory: () => void;
  loadOlderTraceHistory: () => Promise<number>;
  refreshTraceHistory: () => Promise<void>;
  replayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  compactSession: () => Promise<SessionCompactResult>;
  refreshGoal: () => Promise<SessionGoal | null>;
  updateGoal: (
    payload: SessionGoalUpdateRequest,
    target?: { sessionId: string; workspaceId: string | null },
  ) => Promise<SessionGoal>;
  clearGoal: (
    target?: { sessionId: string; workspaceId: string | null },
  ) => Promise<void>;
  switchAgent: (agentId: string) => Promise<void>;
  switchModel: (providerId: string) => Promise<void>;
  refreshAgents: (workspaceId: string) => Promise<void>;
  setWorkspaceDefaultAgent: (agentId: string) => Promise<void>;
  setWorkspaceDefaultProvider: (
    agentId: string,
    providerId: string,
  ) => Promise<void>;
  interruptSession: () => void;
  selectSession: (sessionId: string) => void;
  selectWorkspaceSession: (
    workspaceId: string,
    sessionId: string,
    sessionOverride?: Session,
  ) => void;
  openWorkspaceSession: (workspaceId: string, sessionId: string) => Promise<void>;
  createSession: (
    title?: string,
    workspaceId?: string | null,
    folderId?: string | null,
  ) => Promise<Session>;
  forkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  renameSession: (
    sessionId: string,
    title: string,
    workspaceId?: string | null,
  ) => Promise<void>;
  deleteSession: (
    sessionId: string,
    workspaceId?: string | null,
  ) => Promise<void>;
  setSessionParent: (
    workspaceId: string,
    sessionId: string,
    parentSessionId: string | null,
  ) => Promise<void>;
  refreshSessionResources: (
    sessionId: string,
    options?: { silent?: boolean },
  ) => Promise<void>;
  controlSessionResource: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  loadSessionChangesets: (sessionId: string) => Promise<SessionChangesetList>;
  refreshSessionChanges: (
    sessionId: string,
    changesetId?: string | null,
    options?: { refreshList?: boolean },
  ) => Promise<void>;
  reviewSessionChangeFile: (
    file: SessionFileChange,
    reviewed: boolean,
  ) => Promise<void>;
  toggleAgentSessionsPanel: () => void;
  toggleExpandDetails: (expand: boolean) => void;
  switchContentView: (view: ConversationContentView) => void;
  activateGatewayWorkspace: (
    workspaceId: string,
    preferredSessionId?: string | null,
  ) => Promise<void>;
  refreshGatewayState: () => Promise<void>;
  reconnectGatewayWorkspace: (workspaceId: string) => Promise<void>;
  safeRestartManagedGatewayWorkspaceBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  forceRestartManagedGatewayWorkspaceBackend: (
    workspaceId: string,
  ) => Promise<GatewayRuntimeRestartResult>;
  startManagedGatewayWorkspaceBackend: (workspaceId: string) => Promise<void>;
  stopManagedGatewayWorkspaceBackend: (workspaceId: string) => Promise<void>;
  probeExternalGatewayWorkspace: (workspaceId: string) => Promise<void>;
  addManagedGatewayWorkspace: (
    payload: AddManagedGatewayWorkspaceRequest,
  ) => Promise<void>;
  addSshGatewayWorkspace: (
    payload: AddSshGatewayWorkspaceRequest,
  ) => Promise<void>;
  removeGatewayWorkspace: (workspaceId: string) => Promise<void>;
  renameGatewayWorkspace: (workspaceId: string, name: string) => Promise<string>;
  setGatewayWorkspaceParent: (
    workspaceId: string,
    parentWorkspaceId: string | null,
  ) => Promise<void>;
  refreshGatewayWorkspaceSessions: (workspaceId: string) => Promise<void>;
  reorderGatewayWorkspaces: (workspaceIds: string[]) => Promise<void>;
  copySessionInformation: (workspaceId: string, sessionId: string) => Promise<void>;
  copyWorkspaceInformation: (workspaceId: string) => Promise<void>;
  updateUiSettings: (
    input: WebUiSettingsUpdate | ((current: WebUiSettings) => WebUiSettingsUpdate),
  ) => Promise<void>;
  saveSessionViewState: (payload: SessionViewStatePayload) => void;
}

type HotReloadContextStore = {
  appContext?: React.Context<AppContextType | null>;
  composerContext?: React.Context<ComposerContextType | null>;
};

// Vite 热更新会分别重载 Provider 和消费者模块；复用 Context 身份，避免
// 旧 AppShell 读取到新 AppProvider 之外的 Context。生产构建不依赖这段状态。
const hotReloadContextStore = import.meta.hot?.data as HotReloadContextStore | undefined;
const AppContext = hotReloadContextStore?.appContext
  ?? createContext<AppContextType | null>(null);
if (hotReloadContextStore) {
  hotReloadContextStore.appContext = AppContext;
}

type ComposerContextActions = Pick<
  AppContextType,
  | "setStatus"
  | "sendMessage"
  | "compactSession"
  | "refreshGoal"
  | "updateGoal"
  | "clearGoal"
  | "interruptSession"
  | "switchAgent"
  | "switchModel"
  | "refreshAgents"
  | "setWorkspaceDefaultAgent"
  | "setWorkspaceDefaultProvider"
  | "switchContentView"
  | "createSession"
  | "renameSession"
  | "updateUiSettings"
>;

export interface ComposerContextType extends ComposerContextActions {
  state: ComposerStateSnapshot;
  getLatestAssistantContent: () => string | null;
}

export const ComposerContext = hotReloadContextStore?.composerContext
  ?? createContext<ComposerContextType | null>(null);
if (hotReloadContextStore) {
  hotReloadContextStore.composerContext = ComposerContext;
}

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useAppState must be used within AppProvider");
  }
  return ctx;
}

export function useComposerState() {
  const ctx = useContext(ComposerContext);
  if (!ctx) {
    throw new Error("useComposerState must be used within AppProvider");
  }
  return ctx;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(INITIAL_STATE);
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

  useEffect(() => {
    writeUnreadSessionKeys(state.unreadSessionKeys);
  }, [state.unreadSessionKeys]);

  useEffect(() => {
    const markCurrentSessionRead = () => {
      if (
        !currentSessionCacheKey
        || document.visibilityState !== "visible"
        || !document.hasFocus()
      ) {
        return;
      }
      setState((previous) => {
        if (!previous.unreadSessionKeys.has(currentSessionCacheKey)) {
          return previous;
        }
        const next = cloneMaps(previous);
        next.unreadSessionKeys.delete(currentSessionCacheKey);
        return next;
      });
    };
    markCurrentSessionRead();
    document.addEventListener("visibilitychange", markCurrentSessionRead);
    window.addEventListener("focus", markCurrentSessionRead);
    return () => {
      document.removeEventListener("visibilitychange", markCurrentSessionRead);
      window.removeEventListener("focus", markCurrentSessionRead);
    };
  }, [currentSessionCacheKey]);
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

  const refreshGatewayWorkspaceSessions = useCallback(async (workspaceId: string) => {
    await refreshWorkspaceSessionList(
      state.apiPort ?? DEFAULT_BACKEND_PORT,
      workspaceId,
      setState,
      { force: true },
    );
  }, [setState, state.apiPort]);

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
    onApplyViewState: ({ workspaceId, sessionId, viewState, toolDetailsExpanded }) => {
      setState((previous) => {
        const next = cloneMaps(previous);
        const cacheKey = sessionScopeKey(workspaceId, sessionId);
        if (viewState) next.gatewayUserViewStates.set(cacheKey, viewState);
        else next.gatewayUserViewStates.delete(cacheKey);
        if (
          toolDetailsExpanded !== undefined
          && previous.currentSession?.session_id === sessionId
          && previous.currentSessionWorkspaceId === workspaceId
        ) {
          next.expandDetails = toolDetailsExpanded;
        }
        return next;
      });
    },
    onSetExpandDetails: (expand) => {
      setState((previous) => ({ ...previous, expandDetails: expand }));
    },
    onStatusChange: setStatus,
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
          const message = error instanceof Error ? error.message : String(error);
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

  const resetWorkspaceScopedState = useCallback(() => {
    abortCurrentStream();
    setState((prev) => ({
      ...prev,
      workspaceSwitching: true,
      error: null,
      status: "正在切换工作区",
    }));
  }, [abortCurrentStream]);

  const finishWorkspaceRefresh = useCallback(async (
    preferredSessionId?: string | null,
    options: {
      checkGatewayWorkspaceHealth?: boolean;
      reuseCurrentUiSettings?: boolean;
    } = {},
  ): Promise<string | null> => {
    // 返回本轮刷新真正生效的活动工作区 id：刷新被作废时 refreshSessions 返回
    // null。调用方必须比对它是否等于自己请求的 workspaceId，不能只看真值——
    // 自动健康回退可能把活动工作区切到别的 id，那不算请求的那个工作区生效。
    const appliedWorkspaceId = await refreshSessions(preferredSessionId, options);
    if (appliedWorkspaceId === null) {
      return null;
    }
    setState((prev) => ({
      ...prev,
      workspaceSwitching: false,
      error: null,
      status: "工作区已就绪",
    }));
    return appliedWorkspaceId;
  }, [refreshSessions]);

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
