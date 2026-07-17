import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  DEFAULT_BACKEND_PORT,
} from "./api";
import {
  activateGatewayWorkspace as apiActivateGatewayWorkspace,
  addLocalGatewayWorkspace as apiAddLocalGatewayWorkspace,
  addSshGatewayWorkspace as apiAddSshGatewayWorkspace,
  listGatewayWorkspaces as apiListGatewayWorkspaces,
  reconnectGatewayWorkspace as apiReconnectGatewayWorkspace,
  removeGatewayWorkspace as apiRemoveGatewayWorkspace,
  renameGatewayWorkspace as apiRenameGatewayWorkspace,
  reorderGatewayWorkspaces as apiReorderGatewayWorkspaces,
  updateGatewayUiSettings as apiUpdateGatewayUiSettings,
} from "./gatewayApi";
import type {
  AddLocalGatewayWorkspaceRequest,
  AddSshGatewayWorkspaceRequest,
  AttachmentRef,
  MessageReplayRequest,
  SessionResourceAction,
  SessionResourceKind,
  SessionFileChange,
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
import { useSessionHistoryLoader } from "./hooks/useSessionHistoryLoader";
import { useSessionEventStream } from "./hooks/useSessionEventStream";
import { useSessionInformationClipboard } from "./hooks/useSessionInformationClipboard";
import { useSessionActions } from "./hooks/useSessionActions";
import { useWorkspaceBootstrap } from "./hooks/useWorkspaceBootstrap";
import {
  readCachedUiSettings,
  writeCachedUiSettings,
} from "./state/storage";
import { sessionScopeKey } from "./state/session/sessionScope";
import { applyGatewayWorkspaceListAfterRemoval } from "./state/gatewayWorkspaceState";

export { getConversationsForSession } from "./state/conversations";
export { FRONTEND_EVENT_QUEUE_LIMIT } from "./state/traceEvents";

const CACHED_UI_SETTINGS = readCachedUiSettings();

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
  messages: [],
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
  pendingConversations: new Map(),
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
};

interface AppContextType {
  state: AppState;
  setStatus: (text: string) => void;
  sendMessage: (content: string, attachments?: AttachmentRef[]) => Promise<void>;
  replayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  compactSession: () => Promise<void>;
  switchAgent: (agentId: string) => Promise<void>;
  interruptSession: () => void;
  selectSession: (sessionId: string) => void;
  selectWorkspaceSession: (workspaceId: string, sessionId: string) => void;
  createSession: (title?: string) => Promise<void>;
  forkSessionContext: (
    workspaceId: string,
    sourceSessionId: string,
  ) => Promise<void>;
  startNewSessionDraft: (workspaceId?: string | null) => void;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
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
  addLocalGatewayWorkspace: (
    payload: AddLocalGatewayWorkspaceRequest,
  ) => Promise<void>;
  addSshGatewayWorkspace: (
    payload: AddSshGatewayWorkspaceRequest,
  ) => Promise<void>;
  removeGatewayWorkspace: (workspaceId: string) => Promise<void>;
  renameGatewayWorkspace: (workspaceId: string, name: string) => Promise<string>;
  reorderGatewayWorkspaces: (workspaceIds: string[]) => Promise<void>;
  copySessionInformation: (workspaceId: string, sessionId: string) => Promise<void>;
  updateUiSettings: (payload: WebUiSettingsUpdate) => Promise<void>;
}

const AppContext = createContext<AppContextType | null>(null);

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error("useAppState must be used within AppProvider");
  }
  return ctx;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(INITIAL_STATE);
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
  const { abortCurrentStream } = useSessionEventStream({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    setState,
  });
  const copySessionInformation = useSessionInformationClipboard(
    state.apiPort ?? DEFAULT_BACKEND_PORT,
  );

  const setStatus = useCallback((text: string) => {
    setState((prev) => ({ ...prev, status: text }));
  }, []);

  const updateUiSettings = useCallback(
    async (payload: WebUiSettingsUpdate) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      const settings = await apiUpdateGatewayUiSettings(resolvedApiPort, payload);
      writeCachedUiSettings(settings);
      setState((prev) => ({
        ...prev,
        uiSettings: settings,
        uiSettingsLoaded: true,
      }));
    },
    [state.apiPort],
  );

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
    setSessionParent,
    selectSession,
    selectWorkspaceSession,
    sendMessage,
    startNewSessionDraft,
    switchAgent,
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
  useSessionHistoryLoader({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    workspaceId: currentSessionGatewayWorkspaceId,
    sessionCacheKey: currentSessionCacheKey,
    reloadNonce: state.sessionHistoryReloadNonce,
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

  const addLocalGatewayWorkspace = useCallback(
    async (payload: AddLocalGatewayWorkspaceRequest) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      try {
        await apiAddLocalGatewayWorkspace(resolvedApiPort, payload);
        const normalizedPath = payload.root_path.trim();
        if (normalizedPath) {
          const recentPaths = [
            normalizedPath,
            ...state.uiSettings.recent_local_workspace_paths,
          ].filter(
            (path, index, paths) =>
              path.trim() && paths.findIndex((item) => item === path) === index,
          );
          const settings = await apiUpdateGatewayUiSettings(resolvedApiPort, {
            recent_local_workspace_paths: recentPaths,
          });
          writeCachedUiSettings(settings);
        }
        await finishWorkspaceRefresh();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: "添加本机工作区失败",
          isBootstrapping: false,
        }));
        throw error;
      }
    },
    [finishWorkspaceRefresh, resetWorkspaceScopedState, state.apiPort],
  );

  const addSshGatewayWorkspace = useCallback(
    async (payload: AddSshGatewayWorkspaceRequest) => {
      const resolvedApiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      resetWorkspaceScopedState();
      try {
        await apiAddSshGatewayWorkspace(resolvedApiPort, payload);
        await finishWorkspaceRefresh();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          workspaceSwitching: false,
          gatewayError: message,
          error: message,
          status: "添加 SSH 工作区失败",
          isBootstrapping: false,
        }));
        throw error;
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
            messages: [],
            traceEvents: [],
            llmRequestLogs: [],
            sessionResources: [],
            agentStateJsonl: "",
            agentStateMessageCount: 0,
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
      compactSession,
      switchAgent,
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
      addLocalGatewayWorkspace,
      addSshGatewayWorkspace,
      removeGatewayWorkspace,
      renameGatewayWorkspace,
      reorderGatewayWorkspaces,
      copySessionInformation,
      updateUiSettings,
    }),
    [
      state,
      setStatus,
      sendMessage,
      replayTurn,
      compactSession,
      switchAgent,
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
      addLocalGatewayWorkspace,
      addSshGatewayWorkspace,
      removeGatewayWorkspace,
      renameGatewayWorkspace,
      reorderGatewayWorkspaces,
      copySessionInformation,
      updateUiSettings,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
