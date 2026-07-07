import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";
import {
  DEFAULT_BACKEND_PORT,
} from "./api";
import type {
  AttachmentRef,
  SessionResourceAction,
  SessionResourceKind,
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
import { usePendingConversationPoller } from "./hooks/usePendingConversationPoller";
import { useSessionHistoryLoader } from "./hooks/useSessionHistoryLoader";
import { useSessionEventStream } from "./hooks/useSessionEventStream";
import { useSessionActions } from "./hooks/useSessionActions";
import { useWorkspaceBootstrap } from "./hooks/useWorkspaceBootstrap";

export { getConversationsForSession } from "./state/conversations";
export { FRONTEND_EVENT_QUEUE_LIMIT } from "./state/traceEvents";

function defaultHistoryPanelOpen(): boolean {
  if (typeof window === "undefined") {
    return true;
  }
  return window.innerWidth > 640;
}

const INITIAL_STATE: AppState = {
  apiPort: DEFAULT_BACKEND_PORT,
  workspaceRoot: null,
  workspaceName: null,
  agents: [],
  sessions: [],
  sessionAttachmentSummaries: new Map(),
  currentSession: null,
  messages: [],
  traceEvents: [],
  llmRequestLogs: [],
  llmRequestLogsLoadedAt: null,
  llmRequestLogsLoading: false,
  llmRequestLogsError: null,
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
  historyPanelOpen: defaultHistoryPanelOpen(),
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
  compactSession: () => Promise<void>;
  switchAgent: (agentId: string) => Promise<void>;
  interruptSession: () => void;
  selectSession: (sessionId: string) => void;
  createSession: (title?: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;
  refreshSessionResources: (
    sessionId: string,
    options?: { silent?: boolean },
  ) => Promise<void>;
  controlSessionResource: (
    kind: SessionResourceKind,
    resourceId: string,
    action: SessionResourceAction,
  ) => Promise<void>;
  toggleHistoryPanel: () => void;
  toggleExpandDetails: (expand: boolean) => void;
  switchContentView: (view: ConversationContentView) => void;
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
  const {
    invalidateAgentState,
    refreshSessionResources,
    refreshAgentStateSnapshot,
    refreshLLMRequestLogs,
    controlSessionResource,
    switchContentView,
  } = useContentViewLoader({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSession: state.currentSession,
    setState,
  });
  const currentSessionId = state.currentSession?.session_id ?? null;
  const { abortCurrentStream } = useSessionEventStream({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    setState,
  });

  const setStatus = useCallback((text: string) => {
    setState((prev) => ({ ...prev, status: text }));
  }, []);

  const {
    compactSession,
    createSession,
    deleteSession,
    interruptSession: interruptSessionCallback,
    renameSession,
    selectSession,
    sendMessage,
    switchAgent,
  } = useSessionActions({
    apiPort: state.apiPort ?? DEFAULT_BACKEND_PORT,
    currentSession: state.currentSession,
    contentView: state.contentView,
    setState,
    abortCurrentStream,
    invalidateAgentState,
    refreshAgentStateSnapshot,
  });

  const toggleHistoryPanel = useCallback(() => {
    setState((prev) => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen }));
  }, []);

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState((prev) => ({ ...prev, expandDetails: expand }));
  }, []);

  const pendingPollKey = useMemo(() => {
    if (!currentSessionId) {
      return "";
    }

    const pendingList = state.pendingConversations.get(currentSessionId) ?? [];
    return pendingList
      .filter((conversation) => conversation.pending)
      .map(
        (conversation) =>
          `${conversation.conversationId}:${conversation.jobId ?? ""}`,
      )
      .join("|");
  }, [currentSessionId, state.pendingConversations]);

  usePendingConversationPoller({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    pendingPollKey,
    setState,
  });

  useWorkspaceBootstrap({ apiPort: state.apiPort, setState });
  useSessionHistoryLoader({
    apiPort: state.apiPort,
    sessionId: currentSessionId,
    setState,
  });
  useContentViewEffects({
    contentView: state.contentView,
    sessionId: currentSessionId,
    refreshLLMRequestLogs,
    refreshSessionResources,
  });

  const value = useMemo(
    () => ({
      state,
      setStatus,
      sendMessage,
      compactSession,
      switchAgent,
      interruptSession: interruptSessionCallback,
      selectSession,
      createSession,
      renameSession,
      deleteSession,
      refreshSessionResources,
      controlSessionResource,
      toggleHistoryPanel,
      toggleExpandDetails,
      switchContentView,
    }),
    [
      state,
      setStatus,
      sendMessage,
      compactSession,
      switchAgent,
      interruptSessionCallback,
      selectSession,
      createSession,
      renameSession,
      deleteSession,
      refreshSessionResources,
      controlSessionResource,
      toggleHistoryPanel,
      toggleExpandDetails,
      refreshLLMRequestLogs,
      switchContentView,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
