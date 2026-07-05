import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  compactSessionContext as apiCompactSessionContext,
  createSession as apiCreateSession,
  DEFAULT_BACKEND_PORT,
  interruptSession as apiInterruptSession,
  listAgents as apiListAgents,
  sendUserMessage as apiSendMessage,
  DEFAULT_SESSION_TITLE,
  getSessionTraces,
  getWorkspace,
  listMessages,
  listSessions,
  updateSessionAgent as apiUpdateSessionAgent,
} from "./api";
import type {
  AttachmentRef,
  MessageRunAccepted,
} from "./types/backend";
import type {
  AppState,
  ConversationContentView,
  ConversationView,
} from "./types/frontend";
import { cloneMaps } from "./state/appStateMaps";
import {
  updateAttachmentSummariesFromMessages,
  updateAttachmentSummariesFromTraces,
  updateSessionAttachmentSummary,
} from "./state/attachments";
import {
  getConversationsForSession,
  writePendingList,
} from "./state/conversations";
import { readLastSessionId, writeLastSessionId } from "./state/storage";
import {
  appendFrontendEvent,
  appendReceivedEvents,
  dedupeTraceEvents,
} from "./state/traceEvents";
import {
  resetAgentStateFields,
  useAgentStateLoader,
} from "./hooks/useAgentStateLoader";
import { usePendingConversationPoller } from "./hooks/usePendingConversationPoller";
import { useSessionEventStream } from "./hooks/useSessionEventStream";

export { getConversationsForSession } from "./state/conversations";
export { FRONTEND_EVENT_QUEUE_LIMIT } from "./state/traceEvents";

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
  eventQueuesBySession: new Map(),
  pendingConversations: new Map(),
  status: "准备就绪",
  error: null,
  isBootstrapping: true,
  expandDetails: true,
  historyPanelOpen: true,
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
  createSession: (title?: string) => void;
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
    refreshAgentStateSnapshot,
    switchContentView,
  } = useAgentStateLoader({
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

  const refreshSessions = useCallback(async () => {
    try {
      const apiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      const [workspace, sessions, agents] = await Promise.all([
        getWorkspace(apiPort),
        listSessions(apiPort),
        apiListAgents(apiPort),
      ]);
      setState((prev) => {
        const preferredSessionId =
          prev.currentSession?.session_id ?? readLastSessionId();
        const nextCurrentSession =
          sessions.items.find(
            (session) => session.session_id === preferredSessionId,
          ) ??
          sessions.items[0] ??
          null;
        return {
          ...prev,
          workspaceRoot: workspace.root_path,
          workspaceName: workspace.name,
          agents,
          sessions: sessions.items,
          currentSession: nextCurrentSession,
          error: null,
          isBootstrapping: false,
        };
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        error: message,
        status: "初始化失败",
        isBootstrapping: false,
      }));
    }
  }, [state.apiPort]);

  const selectSession = useCallback((sessionId: string) => {
    // 只需切换 currentSession 引用并中断旧 SSE；消息/trace 由 useEffect 统一加载
    abortCurrentStream();
    invalidateAgentState();
    setState((prev) => {
      const next = cloneMaps(prev);
      const selected =
        prev.sessions.find((session) => session.session_id === sessionId) ??
        prev.currentSession;
      next.currentSession = selected;
      next.contentView = "default";
      Object.assign(next, resetAgentStateFields(next));
      if (selected) {
        writeLastSessionId(selected.session_id);
        appendFrontendEvent(
          next.eventQueuesBySession,
          selected.session_id,
          "session_selected",
          "切换会话",
          {
            session_id: selected.session_id,
            title: selected.title,
          },
          selected.title,
        );
      }
      return next;
    });
  }, [abortCurrentStream, invalidateAgentState]);

  const createSession = useCallback(
    async (title: string = DEFAULT_SESSION_TITLE) => {
      invalidateAgentState();
      const session = await apiCreateSession(
        state.apiPort ?? DEFAULT_BACKEND_PORT,
        title,
      );
      setState((prev) => {
        const next = cloneMaps(prev);
        next.sessions = [session, ...prev.sessions];
        next.currentSession = session;
        writeLastSessionId(session.session_id);
        next.traceEvents = [];
        next.status = "已创建会话";
        next.contentView = "default";
        Object.assign(next, resetAgentStateFields(next));
        appendFrontendEvent(
          next.eventQueuesBySession,
          session.session_id,
          "session_created",
          "创建会话",
          {
            session_id: session.session_id,
            title: session.title,
          },
          session.title,
        );
        return next;
      });
    },
    [invalidateAgentState, state.apiPort],
  );

  const sendMessage = useCallback(
    async (content: string, attachments: AttachmentRef[] = []) => {
      if (!state.currentSession) {
        throw new Error("当前没有可发送消息的会话");
      }

      const session = state.currentSession;
      const pendingSubmissionId = `pending_submission_${Date.now()}`;
      const submittedAt = new Date().toISOString();
      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: pendingSubmissionId,
          sessionId: session.session_id,
          userMessage: {
            message_id: pendingSubmissionId,
            session_id: session.session_id,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          events: [],
          status: "running",
          jobId: null,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        updateSessionAttachmentSummary(
          next.sessionAttachmentSummaries,
          session.session_id,
          attachments,
          submittedAt,
        );
        const pendingList =
          next.pendingConversations.get(session.session_id) ?? [];
        next.pendingConversations.set(session.session_id, [
          ...pendingList,
          conversation,
        ]);
        next.status = "正在发送消息";
        next.contentView = "default";
        return next;
      });

      let accepted: MessageRunAccepted;
      try {
        accepted = await apiSendMessage(
          state.apiPort ?? DEFAULT_BACKEND_PORT,
          session.session_id,
          content,
          session.current_agent_id,
          attachments,
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          const next = cloneMaps(prev);
          const pendingList =
            next.pendingConversations.get(session.session_id) ?? [];
          writePendingList(
            next.pendingConversations,
            session.session_id,
            pendingList.filter(
              (conversation) =>
                conversation.pendingSubmissionId !== pendingSubmissionId,
            ),
          );
          next.status = `发送失败: ${message}`;
          return next;
        });
        throw error;
      }
      const messageId = accepted.message_id ?? `local_user_${Date.now()}`;
      const jobId = accepted.job_id ?? null;

      setState((prev) => {
        const next = cloneMaps(prev);
        const conversation: ConversationView = {
          conversationId: messageId,
          sessionId: session.session_id,
          userMessage: {
            message_id: messageId,
            session_id: session.session_id,
            role: "user",
            content,
            metadata: {
              source: "optimistic",
              job_id: jobId,
              pending_submission_id: pendingSubmissionId,
            },
            attachments,
            created_at: submittedAt,
            updated_at: submittedAt,
          },
          events: [],
          status: accepted.status === "queued" ? "queued" : "running",
          jobId,
          pending: true,
          pendingSubmissionId,
          source: "pending",
        };
        const pendingList = next.pendingConversations.get(session.session_id) ?? [];
        next.pendingConversations.set(session.session_id, [
          ...pendingList.filter(
            (item) => item.pendingSubmissionId !== pendingSubmissionId,
          ),
          conversation,
        ]);
        next.status =
          accepted.status === "queued" ? "已排队，等待当前任务结束" : "已发送，等待生成";
        next.contentView = "default";
        return next;
      });
    },
    [state.apiPort, state.currentSession],
  );

  const compactSession = useCallback(async () => {
    const session = state.currentSession;
    if (!session) {
      throw new Error("当前没有可压缩上下文的会话");
    }

    const apiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
    setState((prev) => ({
      ...prev,
      compactLoading: true,
      status: "正在压缩上下文",
    }));

    try {
      const result = await apiCompactSessionContext(apiPort, session.session_id);
      setState((prev) => {
        const next = cloneMaps(prev);
        next.compactLoading = false;
        next.lastCompactResult = result;
        next.status =
          result.status === "compacted"
            ? `已压缩上下文: ${result.summarized_message_count} 条`
            : `上下文未压缩: ${result.message}`;
        appendFrontendEvent(
          next.eventQueuesBySession,
          result.session_id,
          "context_compacted",
          result.status === "compacted" ? "上下文已压缩" : "上下文未压缩",
          {
            session_id: result.session_id,
            status: result.status,
            before_message_count: result.before_message_count,
            effective_message_count_before: result.effective_message_count_before,
            effective_message_count_after: result.effective_message_count_after,
            summarized_message_count: result.summarized_message_count,
            retained_message_count: result.retained_message_count,
            history_file_path: result.history_file_path,
          },
          result.message,
        );
        return next;
      });

      if (state.contentView === "agent") {
        await refreshAgentStateSnapshot(session.session_id);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState((prev) => ({
        ...prev,
        compactLoading: false,
        status: `上下文压缩失败: ${message}`,
      }));
      throw error;
    }
  }, [
    refreshAgentStateSnapshot,
    state.apiPort,
    state.contentView,
    state.currentSession,
  ]);

  const switchAgent = useCallback(
    async (agentId: string) => {
      const session = state.currentSession;
      if (!session) {
        throw new Error("当前没有可切换 Agent 的会话");
      }

      if (agentId === session.current_agent_id) {
        setState((prev) => ({ ...prev, status: `当前已是 Agent: ${agentId}` }));
        return;
      }

      setState((prev) => ({ ...prev, status: `正在切换 Agent: ${agentId}` }));

      try {
        const updatedSession = await apiUpdateSessionAgent(
          state.apiPort ?? DEFAULT_BACKEND_PORT,
          session.session_id,
          agentId,
        );
        setState((prev) => {
          const next = cloneMaps(prev);
          next.currentSession = updatedSession;
          next.sessions = prev.sessions.map((item) =>
            item.session_id === updatedSession.session_id
              ? updatedSession
              : item,
          );
          if (
            !next.sessions.some(
              (item) => item.session_id === updatedSession.session_id,
            )
          ) {
            next.sessions = [updatedSession, ...next.sessions];
          }
          next.status = `已切换 Agent: ${updatedSession.current_agent_id}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            updatedSession.session_id,
            "agent_switched",
            "切换 Agent",
            {
              session_id: updatedSession.session_id,
              agent_id: updatedSession.current_agent_id,
            },
            updatedSession.current_agent_id,
          );
          return next;
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `Agent 切换失败: ${message}` }));
        throw error;
      }
    },
    [state.apiPort, state.currentSession],
  );

  const interruptSessionCallback = useCallback(async () => {
    if (!state.currentSession) {
      throw new Error("当前没有可中断的会话");
    }

    setState((prev) => ({ ...prev, status: "正在中断生成..." }));
    const result = await apiInterruptSession(
      state.apiPort ?? DEFAULT_BACKEND_PORT,
      state.currentSession.session_id,
    );
    setState((prev) => ({ ...prev, status: `已中断: ${result.phase}` }));
  }, [state.apiPort, state.currentSession]);

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

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // 切换会话时加载 messages 和 traces（也处理初始加载）
  useEffect(() => {
    const sessionId = state.currentSession?.session_id;
    const apiPort = state.apiPort;

    if (!apiPort || !sessionId) return;

    let cancelled = false;
    setState((prev) => {
      if (prev.currentSession?.session_id !== sessionId) return prev;
      const next = cloneMaps(prev);
      appendFrontendEvent(
        next.eventQueuesBySession,
        sessionId,
        "session_load_started",
        "开始加载会话历史",
        { session_id: sessionId },
      );
      return next;
    });

    void (async () => {
      try {
        const [messages, traceEvents] = await Promise.all([
          listMessages(apiPort, sessionId),
          getSessionTraces(apiPort, sessionId),
        ]);
        if (cancelled) return;
        setState((prev) => {
          // 如果在加载期间会话已切换，丢弃过期数据
          if (prev.currentSession?.session_id !== sessionId) return prev;
          const next = cloneMaps(prev);
          const fetchedTraceEvents = dedupeTraceEvents(traceEvents);
          next.messages = messages.items ?? [];
          next.traceEvents = fetchedTraceEvents;
          updateAttachmentSummariesFromMessages(
            next.sessionAttachmentSummaries,
            next.messages,
          );
          updateAttachmentSummariesFromTraces(
            next.sessionAttachmentSummaries,
            sessionId,
            fetchedTraceEvents,
          );
          appendReceivedEvents(
            next.eventQueuesBySession,
            sessionId,
            fetchedTraceEvents,
            "initial_load",
          );
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_completed",
            "会话历史加载完成",
            {
              session_id: sessionId,
              message_count: messages.items?.length ?? 0,
              trace_event_count: fetchedTraceEvents.length,
            },
          );
          return {
            ...next,
          };
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.status = `加载失败: ${message}`;
          appendFrontendEvent(
            next.eventQueuesBySession,
            sessionId,
            "session_load_failed",
            "会话历史加载失败",
            { session_id: sessionId, error: message },
            message,
          );
          return next;
        });
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.currentSession?.session_id, state.apiPort]);

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
      toggleHistoryPanel,
      toggleExpandDetails,
      switchContentView,
    ],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
