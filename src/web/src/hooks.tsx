import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  DEFAULT_BACKEND_PORT,
  listSessions as apiListSessions,
} from "./api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
  addManagedGatewayWorkspace as apiAddManagedGatewayWorkspace,
  addSshGatewayWorkspace as apiAddSshGatewayWorkspace,
  listGatewayWorkspaces as apiListGatewayWorkspaces,
  probeExternalGatewayWorkspace as apiProbeExternalGatewayWorkspace,
  reconnectGatewayWorkspace as apiReconnectGatewayWorkspace,
  removeGatewayWorkspace as apiRemoveGatewayWorkspace,
  renameGatewayWorkspace as apiRenameGatewayWorkspace,
  reorderGatewayWorkspaces as apiReorderGatewayWorkspaces,
  forceRestartManagedGatewayWorkspaceBackend as apiForceRestartManagedGatewayWorkspaceBackend,
  safeRestartManagedGatewayWorkspaceBackend as apiSafeRestartManagedGatewayWorkspaceBackend,
  startManagedGatewayWorkspaceBackend as apiStartManagedGatewayWorkspaceBackend,
  stopManagedGatewayWorkspaceBackend as apiStopManagedGatewayWorkspaceBackend,
} from "./gatewayApi";
import type {
  AddManagedGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  GatewayRuntimeRestartResult,
  AttachmentRef,
  MessageReplayRequest,
  PendingRequestKind,
  PendingRequestOrderItem,
  SessionResourceAction,
  SessionResourceKind,
  SessionFileChange,
  Session,
  SessionGoal,
  SessionGoalUpdateRequest,
  WebUiSettings,
  WebUiSettingsUpdate,
} from "./types/backend";
import type {
  AppState,
  ConversationContentView,
} from "./types/frontend";
import {
  getConversationsForSession,
} from "./state/conversations";
import { useContentViewLoader } from "./hooks/useContentViewLoader";
import { useContentViewEffects } from "./hooks/useContentViewEffects";
import { useSessionTurnHistory } from "./hooks/sessionTurnHistory/useSessionTurnHistory";
import { useSessionEventStream } from "./hooks/useSessionEventStream";
import { useBackgroundSessionActivity } from "./hooks/useBackgroundSessionActivity";
import { useSessionInformationClipboard } from "./hooks/useSessionInformationClipboard";
import { useSessionActions } from "./hooks/useSessionActions";
import { useWorkspaceBootstrap } from "./hooks/useWorkspaceBootstrap";
import { useWorkspaceInformationClipboard } from "./hooks/useWorkspaceInformationClipboard";
import { useGatewayWorkspaceHierarchy } from "./hooks/useGatewayWorkspaceHierarchy";
import { useUiSettingsController } from "./hooks/useUiSettingsController";
import {
  readCachedUiSettings,
  readUnreadSessionKeys,
  writeUnreadSessionKeys,
} from "./state/storage";
import { sessionScopeKey } from "./state/session/sessionScope";
import { applyGatewayWorkspaceListAfterRemoval } from "./state/gatewayWorkspaceState";
import { cloneMaps } from "./state/appStateMaps";
import { useSessionGoalController } from "./hooks/useSessionGoalController";
import { useSessionTraceHistory } from "./hooks/sessionTraceHistory/useSessionTraceHistory";
import {
  reuseComposerStateSnapshot,
  selectComposerState,
  type ComposerStateSnapshot,
} from "./state/composerState";

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
    queue?: PendingRequestKind | null,
  ) => Promise<void>;
  updatePendingRequest: (
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  removePendingRequest: (messageId: string) => Promise<void>;
  clearPendingRequests: () => Promise<void>;
  reorderPendingRequests: (
    requests: PendingRequestOrderItem[],
  ) => Promise<void>;
  sendPendingRequestImmediately: (messageId: string) => Promise<void>;
  loadOlderMessages: () => Promise<void>;
  loadTurnDetails: (turnIds: string[]) => Promise<void>;
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
  compactSession: () => Promise<void>;
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
  setWorkspaceDefaultAgent: (agentId: string) => Promise<void>;
  setWorkspaceDefaultProvider: (
    agentId: string,
    providerId: string,
  ) => Promise<void>;
  interruptSession: () => void;
  selectSession: (sessionId: string) => void;
  selectWorkspaceSession: (workspaceId: string, sessionId: string) => void;
  createSession: (
    title?: string,
    workspaceId?: string | null,
    folderId?: string | null,
  ) => Promise<Session>;
  forkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  startNewSessionDraft: (workspaceId?: string | null) => void;
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
  refreshSessionChanges: (sessionId: string, changesetId?: string | null) => Promise<void>;
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
}

const AppContext = createContext<AppContextType | null>(null);

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
  | "setWorkspaceDefaultAgent"
  | "setWorkspaceDefaultProvider"
  | "switchContentView"
  | "createSession"
  | "startNewSessionDraft"
  | "renameSession"
  | "activateGatewayWorkspace"
  | "updateUiSettings"
>;

export interface ComposerContextType extends ComposerContextActions {
  state: ComposerStateSnapshot;
  getLatestAssistantContent: () => string | null;
}

export const ComposerContext = createContext<ComposerContextType | null>(null);

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
  const currentActiveJobId = currentSessionCacheKey
    ? state.activeJobIdsBySession.get(currentSessionCacheKey) ?? null
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
    currentActiveJobId,
    setState,
  });
  const {
    invalidateAgentState,
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
    loadOlderTurns: loadOlderMessages,
    loadTurnDetails,
    refreshTurnHistory,
  } = useSessionTurnHistory({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    reloadNonce: state.sessionHistoryReloadNonce,
    setState,
  });
  const currentTurnTimeline = currentSessionCacheKey
    ? state.turnTimelinesBySession.get(currentSessionCacheKey) ?? null
    : null;
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
    refreshTurnDetails: loadTurnDetails,
    refreshTurnHistory,
    setState,
  });
  useBackgroundSessionActivity({
    apiPort: state.apiPort,
    activeJobIdsBySession: state.activeJobIdsBySession,
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

  const getLatestAssistantContent = useCallback((): string | null => {
    const latest = latestStateRef.current;
    const latestSessionId = latest.currentSession?.session_id ?? null;
    const latestWorkspaceId =
      latest.currentSessionWorkspaceId ?? latest.activeGatewayWorkspaceId;
    const scopeKey = latestSessionId && latestWorkspaceId
      ? sessionScopeKey(latestWorkspaceId, latestSessionId)
      : latestSessionId;
    const timeline = scopeKey
      ? latest.turnTimelinesBySession.get(scopeKey)
      : null;
    if (timeline) {
      for (let index = timeline.orderedTurnIds.length - 1; index >= 0; index -= 1) {
        const turn = timeline.turnsById[timeline.orderedTurnIds[index]];
        if (!turn) {
          continue;
        }
        const content = "final_response" in turn
          ? turn.final_response ?? turn.response_preview ?? ""
          : turn.response_preview ?? "";
        if (content.trim()) {
          return content;
        }
      }
    }
    return null;
  }, []);

  const refreshGatewayWorkspaceSessions = useCallback(async (workspaceId: string) => {
    const page = await apiListSessions(
      state.apiPort ?? DEFAULT_BACKEND_PORT,
      workspaceId,
    );
    setState((previous) => {
      const next = cloneMaps(previous);
      next.sessionsByWorkspace.set(workspaceId, page.items);
      for (const session of page.items) {
        next.sessionGatewayWorkspaceById.set(
          sessionScopeKey(workspaceId, session.session_id),
          workspaceId,
        );
      }
      if (
        previous.activeGatewayWorkspaceId === workspaceId ||
        previous.currentSessionWorkspaceId === workspaceId
      ) {
        next.sessions = page.items;
        const currentSessionId = previous.currentSession?.session_id;
        next.currentSession = currentSessionId
          ? page.items.find((session) => session.session_id === currentSessionId) ?? null
          : null;
      }
      return next;
    });
  }, [state.apiPort]);

  const updateUiSettings = useUiSettingsController({
    apiPort: state.apiPort,
    setState,
    settings: state.uiSettings,
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
    reorderPendingRequests,
    sendPendingRequestImmediately,
    setSessionParent,
    selectSession,
    selectWorkspaceSession,
    sendMessage,
    startNewSessionDraft,
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

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState((prev) => ({ ...prev, expandDetails: expand }));
  }, []);

  const { invalidateWorkspaceRefreshes, refreshSessions } = useWorkspaceBootstrap({
    apiPort: state.apiPort,
    setState,
  });
  useContentViewEffects({
    contentView: state.contentView,
    sessionId: currentSessionId,
    refreshLLMRequestLogs,
    refreshSessionChanges,
    refreshSessionResources,
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

  const finishWorkspaceRefresh = useCallback(async (preferredSessionId?: string | null) => {
    const applied = await refreshSessions(preferredSessionId);
    if (!applied) {
      return;
    }
    setState((prev) => ({
      ...prev,
      workspaceSwitching: false,
      status: "工作区已就绪",
    }));
  }, [refreshSessions]);

  const activateGatewayWorkspace = useCallback(
    async (workspaceId: string, preferredSessionId?: string | null) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      try {
        await apiActivateGatewayWorkspace(resolvedApiPort, workspaceId);
        await finishWorkspaceRefresh(preferredSessionId);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: "工作区切换失败",
          isBootstrapping: false,
        }));
        throw error;
      }
    },
    [
      finishWorkspaceRefresh,
      resetWorkspaceScopedState,
      state.apiPort,
    ],
  );

  const refreshGatewayState = useCallback(async () => {
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      status: "正在刷新 Gateway 状态",
    }));
    try {
      await finishWorkspaceRefresh(currentSessionId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        gatewayError: message,
        error: message,
        status: `刷新 Gateway 状态失败: ${message}`,
      }));
      throw error;
    }
  }, [currentSessionId, finishWorkspaceRefresh]);

  const reconnectGatewayWorkspace = useCallback(async (workspaceId: string) => {
    const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
    setState((prev) => ({
      ...prev,
      gatewayError: null,
      status: "正在重新连接工作区",
    }));
    try {
      await apiReconnectGatewayWorkspace(resolvedApiPort, workspaceId);
      await finishWorkspaceRefresh(currentSessionId);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        gatewayError: message,
        error: message,
        status: `重新连接工作区失败: ${message}`,
      }));
      throw error;
    }
  }, [currentSessionId, finishWorkspaceRefresh, state.apiPort]);

  const safeRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({
        ...prev,
        gatewayError: null,
        status: "正在安全排空并重启 Workspace 后端",
      }));
      try {
        const result = await apiSafeRestartManagedGatewayWorkspaceBackend(
          resolvedApiPort,
          workspaceId,
        );
        await finishWorkspaceRefresh(currentSessionId);
        return result;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `安全重启 Workspace 后端失败: ${message}`,
        }));
        throw error;
      }
    },
    [currentSessionId, finishWorkspaceRefresh, state.apiPort],
  );

  const startManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({ ...prev, gatewayError: null, status: "正在启动工作区" }));
      try {
        const result = await apiStartManagedGatewayWorkspaceBackend(
          resolvedApiPort,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          gatewayWorkspaces: result.workspaces.items,
          activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
          status: "工作区已启动",
        }));
        await finishWorkspaceRefresh(currentSessionId);
      } catch (error) {
        try {
          await refreshGatewayState();
        } catch {
          // refreshGatewayState 已将二次读取失败完整写入界面状态。
        }
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, gatewayError: message, error: message, status: `启动工作区失败: ${message}` }));
        throw error;
      }
    },
    [currentSessionId, finishWorkspaceRefresh, refreshGatewayState, state.apiPort],
  );

  const stopManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({ ...prev, gatewayError: null, status: "正在关闭工作区" }));
      try {
        const result = await apiStopManagedGatewayWorkspaceBackend(
          resolvedApiPort,
          workspaceId,
        );
        setState((prev) => ({
          ...prev,
          gatewayWorkspaces: result.workspaces.items,
          activeGatewayWorkspaceId: result.workspaces.active_workspace_id,
          status: result.status === "blocked" ? "工作区仍有活动任务，未关闭" : "工作区已关闭",
        }));
        if (result.status === "blocked") {
          const details = result.blockers
            .map((blocker) => `${blocker.kind}:${blocker.resource_id}`)
            .join("、");
          throw new Error(`工作区仍有 ${result.blockers.length} 个活动任务，未关闭${details ? `（${details}）` : ""}`);
        }
        await finishWorkspaceRefresh(currentSessionId);
      } catch (error) {
        try {
          await refreshGatewayState();
        } catch {
          // refreshGatewayState 已将二次读取失败完整写入界面状态。
        }
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, gatewayError: message, error: message, status: `关闭工作区失败: ${message}` }));
        throw error;
      }
    },
    [currentSessionId, finishWorkspaceRefresh, refreshGatewayState, state.apiPort],
  );

  const forceRestartManagedGatewayWorkspaceBackend = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({
        ...prev,
        gatewayError: null,
        status: "正在中断活动任务并强制重启 Workspace 后端",
      }));
      try {
        const result = await apiForceRestartManagedGatewayWorkspaceBackend(
          resolvedApiPort,
          workspaceId,
        );
        await finishWorkspaceRefresh(currentSessionId);
        return result;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `强制重启 Workspace 后端失败: ${message}`,
        }));
        throw error;
      }
    },
    [currentSessionId, finishWorkspaceRefresh, state.apiPort],
  );

  const probeExternalGatewayWorkspace = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      setState((prev) => ({
        ...prev,
        gatewayError: null,
        status: "正在重新探测外部后端",
      }));
      try {
        await apiProbeExternalGatewayWorkspace(resolvedApiPort, workspaceId);
        await finishWorkspaceRefresh(currentSessionId);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `重新探测外部后端失败: ${message}`,
        }));
        throw error;
      }
    },
    [currentSessionId, finishWorkspaceRefresh, state.apiPort],
  );

  const addManagedGatewayWorkspace = useCallback(
    async (payload: AddManagedGatewayWorkspaceRequest) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        await apiAddManagedGatewayWorkspace(resolvedApiPort, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: `添加工作区失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }

      const reconciliationErrors: string[] = [];
      const normalizedPath = payload.root_path.trim();
      if (normalizedPath && !payload.gateway_connection_id) {
        try {
          const recentPaths = [
            normalizedPath,
            ...state.uiSettings.recent_local_workspace_paths,
          ].filter(
            (path, index, paths) =>
              path.trim() && paths.findIndex((item) => item === path) === index,
          );
          await updateUiSettings({
            recent_local_workspace_paths: recentPaths,
          });
        } catch (error) {
          reconciliationErrors.push(
            `保存最近路径失败: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
      try {
        await finishWorkspaceRefresh();
      } catch (error) {
        reconciliationErrors.push(
          `刷新工作区列表失败: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      if (reconciliationErrors.length > 0) {
        const message = `工作区已添加，但界面同步失败: ${reconciliationErrors.join("；")}`;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: message,
          isBootstrapping: false,
        }));
        throw new Error(message);
      }
    },
    [finishWorkspaceRefresh, state.apiPort, state.uiSettings.recent_local_workspace_paths, updateUiSettings],
  );

  const addSshGatewayWorkspace = useCallback(
    async (payload: AddSshGatewayWorkspaceRequest) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      try {
        await apiAddSshGatewayWorkspace(resolvedApiPort, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: `连接远程 Gateway 失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }
      try {
        await finishWorkspaceRefresh();
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        const message = `远程 Gateway 已连接，但界面同步失败: ${detail}`;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: message,
          isBootstrapping: false,
        }));
        throw new Error(message);
      }
    },
    [finishWorkspaceRefresh, resetWorkspaceScopedState, state.apiPort],
  );

  const removeGatewayWorkspace = useCallback(
    async (workspaceId: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      const removedActiveWorkspace =
        workspaceId === state.activeGatewayWorkspaceId;
      let workspaceRemoved = false;
      invalidateWorkspaceRefreshes();
      setState((prev) => ({
        ...prev,
        removingGatewayWorkspaceIds: new Set([
          ...prev.removingGatewayWorkspaceIds,
          workspaceId,
        ]),
        gatewayError: null,
        error: null,
        status: "正在删除工作区",
      }));
      try {
        const workspaceList = await apiRemoveGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
        );
        workspaceRemoved = true;
        const activeWorkspaceChanged =
          workspaceList.active_workspace_id !== state.activeGatewayWorkspaceId;
        if (removedActiveWorkspace || activeWorkspaceChanged) {
          abortCurrentStream();
        }
        setState((prev) => {
          const reconciledState = applyGatewayWorkspaceListAfterRemoval(
            prev,
            workspaceId,
            workspaceList,
          );
          if (!removedActiveWorkspace && !activeWorkspaceChanged) {
            return reconciledState;
          }
          const activeWorkspace = workspaceList.items.find(
            (workspace) =>
              workspace.workspace_id === workspaceList.active_workspace_id,
          );
          return {
            ...reconciledState,
            workspaceSwitching: true,
            workspaceRoot: activeWorkspace?.root_path ?? null,
            workspaceName: activeWorkspace?.name ?? null,
            sessions: workspaceList.active_workspace_id
              ? reconciledState.sessionsByWorkspace.get(
                  workspaceList.active_workspace_id,
                ) ?? []
              : [],
            currentSession: null,
            currentSessionWorkspaceId: null,
            traceEvents: [],
            llmRequestLogs: [],
            sessionResources: [],
            agentStateJsonl: "",
            agentStateMessageCount: 0,
          };
        });
        await updateUiSettings((current) => {
          const expandedPathsByWorkspace = {
            ...current.workspace_file_tree.expanded_paths_by_workspace,
          };
          delete expandedPathsByWorkspace[workspaceId];
          return {
            session_sidebar: {
              collapsed_workspace_ids:
                current.session_sidebar.collapsed_workspace_ids.filter(
                  (collapsedId) => collapsedId !== workspaceId,
                ),
              expanded_root_tree_ids:
                current.session_sidebar.expanded_root_tree_ids.filter(
                  (treeId) => treeId !== `workspace:${workspaceId}`,
                ),
            },
            workspace_file_tree: {
              expanded_paths_by_workspace: expandedPathsByWorkspace,
            },
          };
        });
        if (removedActiveWorkspace || activeWorkspaceChanged) {
          await finishWorkspaceRefresh();
        }
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : String(error);
        const operationMessage = workspaceRemoved
          ? `工作区已删除，但新活动工作区加载失败: ${errorMessage}`
          : errorMessage;
        let reconciliationMessage: string | null = null;
        try {
          const workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          setState((prev) => {
            const removingGatewayWorkspaceIds = new Set(
              prev.removingGatewayWorkspaceIds,
            );
            removingGatewayWorkspaceIds.delete(workspaceId);
            return {
              ...prev,
              gatewayWorkspaces: workspaceList.items,
              activeGatewayWorkspaceId: workspaceList.active_workspace_id,
              removingGatewayWorkspaceIds,
            };
          });
        } catch (reconciliationError) {
          reconciliationMessage =
            reconciliationError instanceof Error
              ? reconciliationError.message
              : String(reconciliationError);
        }
        const message = reconciliationMessage
          ? `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`
          : operationMessage;
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          removingGatewayWorkspaceIds: new Set(
            [...prev.removingGatewayWorkspaceIds].filter(
              (removingId) => removingId !== workspaceId,
            ),
          ),
          gatewayError: message,
          error: message,
          status: workspaceRemoved
            ? message
            : `删除工作区失败: ${message}`,
          isBootstrapping: false,
        }));
        throw error;
      }
    },
    [
      abortCurrentStream,
      finishWorkspaceRefresh,
      invalidateWorkspaceRefreshes,
      state.activeGatewayWorkspaceId,
      state.apiPort,
      updateUiSettings,
    ],
  );

  const reorderGatewayWorkspaces = useCallback(
    async (workspaceIds: string[]) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        const workspaceList = await apiReorderGatewayWorkspaces(resolvedApiPort, {
          workspace_ids: workspaceIds,
        });
        setState((prev) => {
          const activeWorkspaceId =
            workspaceList.active_workspace_id ?? prev.activeGatewayWorkspaceId;
          const activeWorkspace = workspaceList.items.find(
            (workspace) => workspace.workspace_id === activeWorkspaceId,
          );
          return {
            ...prev,
            gatewayWorkspaces: workspaceList.items,
            activeGatewayWorkspaceId: activeWorkspaceId,
            workspaceRoot: activeWorkspace?.root_path ?? prev.workspaceRoot,
            workspaceName: activeWorkspace?.name ?? prev.workspaceName,
            status: "工作区顺序已更新",
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `工作区排序失败: ${message}`,
        }));
        throw error;
      }
    },
    [state.apiPort],
  );

  const renameGatewayWorkspace = useCallback(
    async (workspaceId: string, name: string) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      try {
        const workspaceList = await apiRenameGatewayWorkspace(
          resolvedApiPort,
          workspaceId,
          { name },
        );
        const renamedWorkspace = workspaceList.items.find(
          (workspace) => workspace.workspace_id === workspaceId,
        );
        if (!renamedWorkspace) {
          throw new Error(`Gateway 重命名响应缺少工作区: ${workspaceId}`);
        }
        setState((prev) => {
          const activeWorkspace = workspaceList.items.find(
            (workspace) =>
              workspace.workspace_id === workspaceList.active_workspace_id,
          );
          return {
            ...prev,
            gatewayWorkspaces: workspaceList.items,
            activeGatewayWorkspaceId: workspaceList.active_workspace_id,
            workspaceRoot: activeWorkspace?.root_path ?? null,
            workspaceName: activeWorkspace?.name ?? null,
            gatewayError: null,
            error: null,
            status: `工作区已重命名为「${renamedWorkspace.name}」`,
          };
        });
        return renamedWorkspace.name;
      } catch (error) {
        const operationMessage =
          error instanceof Error ? error.message : String(error);
        let message = operationMessage;
        try {
          const workspaceList = await apiListGatewayWorkspaces(resolvedApiPort);
          setState((prev) => {
            const activeWorkspace = workspaceList.items.find(
              (workspace) =>
                workspace.workspace_id === workspaceList.active_workspace_id,
            );
            return {
              ...prev,
              gatewayWorkspaces: workspaceList.items,
              activeGatewayWorkspaceId: workspaceList.active_workspace_id,
              workspaceRoot: activeWorkspace?.root_path ?? null,
              workspaceName: activeWorkspace?.name ?? null,
            };
          });
        } catch (reconciliationError) {
          const reconciliationMessage = reconciliationError instanceof Error
            ? reconciliationError.message
            : String(reconciliationError);
          message = `${operationMessage}；重新读取工作区列表也失败: ${reconciliationMessage}`;
        }
        setState((prev) => ({
          ...prev,
          gatewayError: message,
          error: message,
          status: `重命名工作区失败: ${message}`,
        }));
        throw new Error(message);
      }
    },
    [state.apiPort],
  );

  const value = useMemo(
    () => ({
      state,
      setStatus,
      sendMessage,
      replayTurn,
      updatePendingRequest,
      removePendingRequest,
      clearPendingRequests,
      reorderPendingRequests,
      sendPendingRequestImmediately,
      loadOlderMessages,
      loadTurnDetails,
      refreshTurnHistory,
      loadOlderTraceHistory,
      refreshTraceHistory,
      compactSession,
      refreshGoal,
      updateGoal,
      clearGoal,
      switchAgent,
      switchModel,
      setWorkspaceDefaultAgent,
      setWorkspaceDefaultProvider,
      interruptSession: interruptSessionCallback,
      selectSession,
      selectWorkspaceSession,
      createSession,
      forkSessionContext,
      startNewSessionDraft,
      renameSession,
      setSessionParent,
      deleteSession,
      refreshSessionResources,
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
    }),
    [
      state,
      setStatus,
      sendMessage,
      replayTurn,
      updatePendingRequest,
      removePendingRequest,
      clearPendingRequests,
      reorderPendingRequests,
      sendPendingRequestImmediately,
      loadOlderMessages,
      loadTurnDetails,
      refreshTurnHistory,
      loadOlderTraceHistory,
      refreshTraceHistory,
      compactSession,
      refreshGoal,
      updateGoal,
      clearGoal,
      switchAgent,
      switchModel,
      setWorkspaceDefaultAgent,
      setWorkspaceDefaultProvider,
      interruptSessionCallback,
      selectSession,
      selectWorkspaceSession,
      createSession,
      forkSessionContext,
      startNewSessionDraft,
      renameSession,
      setSessionParent,
      deleteSession,
      refreshSessionResources,
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
    ],
  );

  const composerStateRef = useRef<ComposerStateSnapshot | null>(null);
  const selectedComposerState = selectComposerState(state, currentSessionCacheKey);
  const composerState = reuseComposerStateSnapshot(
    composerStateRef.current,
    selectedComposerState,
  );
  composerStateRef.current = composerState;
  const composerValue = useMemo<ComposerContextType>(() => ({
    state: composerState,
    getLatestAssistantContent,
    setStatus,
    sendMessage,
    compactSession,
    refreshGoal,
    updateGoal,
    clearGoal,
    interruptSession: interruptSessionCallback,
    switchAgent,
    switchModel,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
    switchContentView,
    createSession,
    startNewSessionDraft,
    renameSession,
    activateGatewayWorkspace,
    updateUiSettings,
  }), [
    activateGatewayWorkspace,
    clearGoal,
    compactSession,
    composerState,
    createSession,
    getLatestAssistantContent,
    interruptSessionCallback,
    refreshGoal,
    renameSession,
    sendMessage,
    setStatus,
    setWorkspaceDefaultAgent,
    setWorkspaceDefaultProvider,
    startNewSessionDraft,
    switchAgent,
    switchContentView,
    switchModel,
    updateGoal,
    updateUiSettings,
  ]);

  return (
    <AppContext.Provider value={value}>
      <ComposerContext.Provider value={composerValue}>
        {children}
      </ComposerContext.Provider>
    </AppContext.Provider>
  );
}
