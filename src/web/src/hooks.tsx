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
  createSession as apiCreateSession,
  DEFAULT_BACKEND_PORT,
  getAgentStateMessages as apiGetAgentStateMessages,
  interruptSession as apiInterruptSession,
  sendUserMessage as apiSendMessage,
  DEFAULT_SESSION_TITLE,
  getSessionTraces,
  getWorkspace,
  listMessages,
  listSessions,
  streamSessionEvents,
  type SessionStreamEvent,
} from "./api";
import type { Message, MessageRunAccepted, TraceEvent } from "./types/backend";
import type {
  AppState,
  ConversationContentView,
  ConversationView,
} from "./types/frontend";

function groupMessagesIntoConversations(
  messages: Message[],
): ConversationView[] {
  const conversations: ConversationView[] = [];
  let current: ConversationView | null = null;

  for (const message of messages) {
    if (message.role === "user") {
      current = {
        conversationId:
          message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: message,
        events: [],
        status: "done",
        jobId: String(message.metadata?.job_id ?? "") || null,
        pending: false,
        source: "messages",
      };
      conversations.push(current);
      continue;
    }

    if (!current) {
      // 助手消息出现在用户消息之前（理论上不应该发生），用助手消息 ID 作为 conversationId
      current = {
        conversationId:
          message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: null,
        events: [],
        status: "done",
        jobId: String(message.metadata?.job_id ?? "") || null,
        pending: false,
        source: "messages",
      };
      conversations.push(current);
    }
    // 助手消息内容由 ChatPanel 从 traceEvents 聚合得到；不再维护 assistantMessages 数组。
  }

  return conversations;
}

function attachTraceEventsToConversations(
  conversations: ConversationView[],
  traceEvents: TraceEvent[],
): ConversationView[] {
  if (conversations.length === 0 || traceEvents.length === 0) {
    return conversations;
  }

  // 按 event_id 去重（SSE 流 + job_completed 后 API 重新获取可能产生重复）
  const dedupedEvents = dedupeTraceEvents(traceEvents);

  // 从 trace 事件中提取 message_created，建立 message_id → job_id/时间戳映射
  // 后端 DTO 格式：真实 payload 嵌套在 raw.payload 中
  interface MessageBoundary {
    messageId: string;
    jobId: string;
    timestamp: number;
  }
  const boundaries: MessageBoundary[] = [];
  for (const event of dedupedEvents) {
    if (event.type === "message_created") {
      const innerPayload = event.raw?.payload ?? event.payload ?? {};
      const msgId =
        typeof innerPayload.message_id === "string"
          ? innerPayload.message_id
          : "";
      if (msgId) {
        boundaries.push({
          messageId: msgId,
          jobId: traceJobId(event),
          timestamp: new Date(event.timestamp).getTime(),
        });
      }
    }
  }

  // 为每个 conversation 按时间戳范围分配事件：
  // conversation[0] 得到 [boundary[0].ts, boundary[1].ts) 区间内的事件
  // conversation[N] 得到 [boundary[N].ts, +∞) 区间内的事件
  const boundaryTs = boundaries.map((b) => b.timestamp);

  return conversations.map((conversation) => {
    const userMsgId = conversation.userMessage?.message_id ?? "";
    // 找到该 conversation 对应的 message_created 边界索引
    const boundaryIndex = boundaries.findIndex(
      (b) => b.messageId === userMsgId,
    );
    if (boundaryIndex === -1) {
      return conversation;
    }

    const jobId = conversation.jobId ?? boundaries[boundaryIndex]?.jobId ?? "";
    if (jobId) {
      const convEvents = dedupedEvents.filter(
        (event) => traceJobId(event) === jobId,
      );
      return { ...conversation, jobId, events: convEvents };
    }

    const startTs = boundaryTs[boundaryIndex];
    const endTs =
      boundaryIndex + 1 < boundaryTs.length
        ? boundaryTs[boundaryIndex + 1]
        : Infinity;

    const convEvents = dedupedEvents.filter((event) => {
      const eventTs = new Date(event.timestamp).getTime();
      return eventTs >= startTs && eventTs < endTs;
    });

    return { ...conversation, events: convEvents };
  });
}

function rawTracePayload(event: TraceEvent): Record<string, unknown> {
  return event.raw?.payload ?? event.payload ?? {};
}

function traceJobId(event: TraceEvent): string {
  if (event.job_id && event.job_id !== "unknown_job") {
    return event.job_id;
  }
  return typeof event.raw?.job_id === "string" ? event.raw.job_id : "";
}

function tracePayloadString(event: TraceEvent, key: string): string {
  const payload = rawTracePayload(event);
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

function dedupeTraceEvents(events: TraceEvent[]): TraceEvent[] {
  const seenEventIds = new Set<string>();
  return events
    .filter((event) => {
      const id = event.event_id;
      if (!id) {
        return true;
      }
      if (seenEventIds.has(id)) {
        return false;
      }
      seenEventIds.add(id);
      return true;
    })
    .sort(
      (a, b) =>
        new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
    );
}

function isTerminalTraceType(eventType: string): boolean {
  return [
    "agent_end",
    "job_completed",
    "job_failed",
    "job_cancelled",
    "session_interrupted",
  ].includes(eventType);
}

function isJobTerminalTraceType(eventType: string): boolean {
  return [
    "job_completed",
    "job_failed",
    "job_cancelled",
    "session_interrupted",
  ].includes(eventType);
}

function terminalStatusForEvent(eventType: string): ConversationView["status"] {
  return eventType === "job_failed" ||
    eventType === "job_cancelled" ||
    eventType === "session_interrupted"
    ? "error"
    : "done";
}

function conversationStartTime(conversation: ConversationView): number {
  const messageTime = conversation.userMessage?.created_at;
  if (messageTime) {
    return new Date(messageTime).getTime();
  }
  const firstEvent = conversation.events[0];
  return firstEvent ? new Date(firstEvent.timestamp).getTime() : 0;
}

function conversationsMatch(
  left: ConversationView,
  right: ConversationView,
): boolean {
  const leftMessageId = left.userMessage?.message_id ?? "";
  const rightMessageId = right.userMessage?.message_id ?? "";
  if (leftMessageId && rightMessageId && leftMessageId === rightMessageId) {
    return true;
  }

  const leftJobId = left.jobId ?? "";
  const rightJobId = right.jobId ?? "";
  return Boolean(leftJobId && rightJobId && leftJobId === rightJobId);
}

function mergeConversation(
  persisted: ConversationView,
  pending: ConversationView,
): ConversationView {
  return {
    ...persisted,
    ...pending,
    userMessage: persisted.userMessage ?? pending.userMessage,
    events: dedupeTraceEvents([...persisted.events, ...pending.events]),
    source: persisted.source,
  };
}

function conversationMatchesTraceEvent(
  conversation: ConversationView,
  event: TraceEvent,
): boolean {
  const eventJobId = traceJobId(event);
  if (eventJobId && conversation.jobId === eventJobId) {
    return true;
  }

  const eventMessageId = tracePayloadString(event, "message_id");
  const conversationMessageId = conversation.userMessage?.message_id ?? "";
  return Boolean(eventMessageId && conversationMessageId === eventMessageId);
}

function traceEventsForConversation(
  traceEvents: TraceEvent[],
  conversation: ConversationView,
): TraceEvent[] {
  return traceEvents.filter((event) =>
    conversationMatchesTraceEvent(conversation, event),
  );
}

function statusForConversationEvents(
  events: TraceEvent[],
  fallback: ConversationView["status"],
): ConversationView["status"] {
  let status = fallback;
  for (const event of dedupeTraceEvents(events)) {
    if (event.type === "status_change") {
      status =
        tracePayloadString(event, "status") === "queued"
          ? "queued"
          : "running";
      continue;
    }

    if (
      [
        "job_created",
        "message_created",
        "job_started",
        "agent_start",
        "llm_request",
        "text_start",
        "text_delta",
        "text_end",
        "tool_call_start",
        "tool_call_end",
      ].includes(event.type)
    ) {
      status = "running";
      continue;
    }

    if (isTerminalTraceType(event.type)) {
      status = terminalStatusForEvent(event.type);
    }
  }
  return status;
}

function hasJobTerminalTraceEvent(events: TraceEvent[]): boolean {
  return events.some((event) => isJobTerminalTraceType(event.type));
}

function writePendingList(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  list: ConversationView[],
) {
  if (list.length === 0) {
    map.delete(sessionId);
    return;
  }
  map.set(sessionId, list);
}

function removePendingForTraceEvent(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  event: TraceEvent,
) {
  const pendingList = map.get(sessionId) ?? [];
  if (pendingList.length === 0) {
    return;
  }

  writePendingList(
    map,
    sessionId,
    pendingList.filter(
      (conversation) => !conversationMatchesTraceEvent(conversation, event),
    ),
  );
}

function buildTraceOnlyConversations(
  sessionId: string,
  traceEvents: TraceEvent[],
): ConversationView[] {
  const conversations: ConversationView[] = [];

  for (const event of traceEvents) {
    if (event.type !== "message_created") {
      continue;
    }

    const payload = rawTracePayload(event);
    const payloadSessionId =
      typeof payload.session_id === "string" ? payload.session_id : sessionId;
    const role = payload.role === "user" ? "user" : null;
    if (payloadSessionId !== sessionId || role !== "user") {
      continue;
    }

    const messageId =
      typeof payload.message_id === "string"
        ? payload.message_id
        : event.event_id;
    const content = typeof payload.content === "string" ? payload.content : "";
    const timestamp =
      typeof payload.created_at === "string"
        ? payload.created_at
        : event.timestamp;
    const hasFailure = traceEvents.some(
      (trace) =>
        trace.job_id === event.job_id &&
        ["job_failed", "job_cancelled", "session_interrupted"].includes(
          trace.type,
        ),
    );
    const hasCompletion = traceEvents.some(
      (trace) =>
        trace.job_id === event.job_id &&
        ["agent_end", "job_completed"].includes(trace.type),
    );

    conversations.push({
      conversationId: messageId,
      sessionId,
      userMessage: {
        message_id: messageId,
        session_id: sessionId,
        role,
        content,
        attachments: [],
        metadata: { source: "trace", job_id: event.job_id },
        created_at: timestamp,
        updated_at: timestamp,
      },
      events: [],
      status: hasFailure ? "error" : hasCompletion ? "done" : "running",
      jobId: event.job_id ?? null,
      pending: false,
      source: "messages",
    });
  }

  return conversations;
}

function cloneMaps(state: AppState): AppState {
  return {
    ...state,
    pendingConversations: new Map(state.pendingConversations),
  };
}

function buildTraceEvent(event: SessionStreamEvent): TraceEvent {
  // 后端 TraceEventDTO 把原始 payload 嵌套在 raw.payload 中
  const raw = event.raw;
  const rawPayload =
    raw &&
    typeof raw.payload === "object" &&
    raw.payload !== null &&
    !Array.isArray(raw.payload)
      ? (raw.payload as Record<string, unknown>)
      : {};
  const payload =
    Object.keys(rawPayload).length > 0 ? rawPayload : event.payload || {};
  const normalizedRaw: TraceEvent["raw"] | undefined = raw
    ? {
        event_id:
          typeof raw.event_id === "string" ? raw.event_id : event.event_id,
        job_id:
          typeof raw.job_id === "string"
            ? raw.job_id
            : (event.job_id ?? "unknown_job"),
        type: typeof raw.type === "string" ? raw.type : event.type,
        timestamp:
          typeof raw.timestamp === "string" ? raw.timestamp : event.timestamp,
        payload,
        session_id:
          typeof raw.session_id === "string" ? raw.session_id : undefined,
        agent_id:
          typeof raw.agent_id === "string" || raw.agent_id === null
            ? raw.agent_id
            : event.agent_id,
        step_id:
          typeof raw.step_id === "string" || raw.step_id === null
            ? raw.step_id
            : event.step_id,
      }
    : undefined;
  return {
    event_id: event.event_id,
    job_id: event.job_id ?? "unknown_job",
    step_id: event.step_id ?? null,
    agent_id: event.agent_id ?? null,
    timestamp: event.timestamp,
    type: event.type as TraceEvent["type"],
    payload,
    raw: normalizedRaw,
  };
}

export function getConversationsForSession(
  sessionId: string,
  state: AppState,
): ConversationView[] {
  const messageConversations = groupMessagesIntoConversations(
    state.messages.filter((message) => message.session_id === sessionId),
  );
  const conversations =
    messageConversations.length > 0
      ? messageConversations
      : buildTraceOnlyConversations(sessionId, state.traceEvents);
  const withTraceEvents = attachTraceEventsToConversations(
    conversations,
    state.traceEvents,
  );
  const pendingList = state.pendingConversations.get(sessionId) ?? [];

  if (pendingList.length === 0) {
    return withTraceEvents;
  }

  const merged = [...withTraceEvents];
  for (const pending of pendingList) {
    const matchedIndex = merged.findIndex((conversation) =>
      conversationsMatch(conversation, pending),
    );
    if (matchedIndex === -1) {
      merged.push({ ...pending, source: "pending" });
      continue;
    }

    merged[matchedIndex] = mergeConversation(merged[matchedIndex], pending);
  }

  return merged.sort(
    (a, b) => conversationStartTime(a) - conversationStartTime(b),
  );
}

const INITIAL_STATE: AppState = {
  apiPort: DEFAULT_BACKEND_PORT,
  workspaceRoot: null,
  workspaceName: null,
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
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
};

interface AppContextType {
  state: AppState;
  setStatus: (text: string) => void;
  sendMessage: (content: string) => Promise<void>;
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
  const streamAbortRef = useRef<AbortController | null>(null);
  const agentStateRequestIdRef = useRef(0);

  const setStatus = useCallback((text: string) => {
    setState((prev) => ({ ...prev, status: text }));
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const apiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      const [workspace, sessions] = await Promise.all([
        getWorkspace(apiPort),
        listSessions(apiPort),
      ]);
      setState((prev) => {
        // 保留当前已选中的会话，只有初次加载（无选中）才取列表第一个
        const nextCurrentSession =
          prev.currentSession ?? sessions.items[0] ?? null;
        return {
          ...prev,
          workspaceRoot: workspace.root_path,
          workspaceName: workspace.name,
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
    streamAbortRef.current?.abort();
    agentStateRequestIdRef.current += 1;
    setState((prev) => ({
      ...prev,
      currentSession:
        prev.sessions.find((session) => session.session_id === sessionId) ??
        prev.currentSession,
      contentView: "default",
      agentStateJsonl: "",
      agentStateMessageCount: 0,
      agentStateLoadedAt: null,
      agentStateLoading: false,
      agentStateError: null,
    }));
  }, []);

  const createSession = useCallback(
    async (title: string = DEFAULT_SESSION_TITLE) => {
      agentStateRequestIdRef.current += 1;
      const session = await apiCreateSession(
        state.apiPort ?? DEFAULT_BACKEND_PORT,
        title,
      );
      setState((prev) => ({
        ...prev,
        sessions: [session, ...prev.sessions],
        currentSession: session,
        traceEvents: [],
        status: "已创建会话",
        contentView: "default",
        agentStateJsonl: "",
        agentStateMessageCount: 0,
        agentStateLoadedAt: null,
        agentStateLoading: false,
        agentStateError: null,
      }));
    },
    [state.apiPort],
  );

  const sendMessage = useCallback(
    async (content: string) => {
      if (!state.currentSession) {
        throw new Error("当前没有可发送消息的会话");
      }

      const session = state.currentSession;
      setState((prev) => ({ ...prev, status: "正在发送消息" }));

      let accepted: MessageRunAccepted;
      try {
        accepted = await apiSendMessage(
          state.apiPort ?? DEFAULT_BACKEND_PORT,
          session.session_id,
          content,
          session.current_agent_id,
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `发送失败: ${message}` }));
        throw error;
      }
      const messageId = accepted.message_id ?? `local_user_${Date.now()}`;
      const jobId = accepted.job_id ?? null;

      setState((prev) => {
        const next = cloneMaps(prev);
        const now = new Date().toISOString();
        const conversation: ConversationView = {
          conversationId: messageId,
          sessionId: session.session_id,
          userMessage: {
            message_id: messageId,
            session_id: session.session_id,
            role: "user",
            content,
            metadata: { source: "optimistic" },
            attachments: [],
            created_at: now,
            updated_at: now,
          },
          events: [],
          status: accepted.status === "queued" ? "queued" : "running",
          jobId,
          pending: true,
          source: "pending",
        };
        const pendingList = next.pendingConversations.get(session.session_id) ?? [];
        next.pendingConversations.set(session.session_id, [
          ...pendingList,
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

  const switchContentView = useCallback(
    async (view: ConversationContentView) => {
      if (view === "default") {
        agentStateRequestIdRef.current += 1;
        setState((prev) => ({
          ...prev,
          contentView: "default",
          agentStateLoading: false,
          agentStateError: null,
          status: "默认视图",
        }));
        return;
      }

      const session = state.currentSession;
      if (!session) {
        agentStateRequestIdRef.current += 1;
        setState((prev) => ({
          ...prev,
          contentView: "agent",
          agentStateJsonl: "",
          agentStateMessageCount: 0,
          agentStateLoadedAt: new Date().toISOString(),
          agentStateLoading: false,
          agentStateError: "当前没有会话可读取 Agent State",
          status: "没有会话可读取 Agent State",
        }));
        return;
      }

      const apiPort = state.apiPort ?? DEFAULT_BACKEND_PORT;
      const sessionId = session.session_id;
      const requestId = agentStateRequestIdRef.current + 1;
      agentStateRequestIdRef.current = requestId;
      setState((prev) => ({
        ...prev,
        contentView: "agent",
        agentStateLoading: true,
        agentStateError: null,
        status: "正在读取 Agent State 快照",
      }));

      try {
        const snapshot = await apiGetAgentStateMessages(apiPort, sessionId);
        setState((prev) => {
          if (
            requestId !== agentStateRequestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "agent"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "agent",
            agentStateJsonl: snapshot.jsonl,
            agentStateMessageCount: snapshot.message_count,
            agentStateLoadedAt: new Date().toISOString(),
            agentStateLoading: false,
            agentStateError: null,
            status: `Agent State 快照已加载 (${snapshot.message_count} 条消息)`,
          };
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => {
          if (
            requestId !== agentStateRequestIdRef.current ||
            prev.currentSession?.session_id !== sessionId ||
            prev.contentView !== "agent"
          ) {
            return prev;
          }
          return {
            ...prev,
            contentView: "agent",
            agentStateLoading: false,
            agentStateError: message,
            status: `Agent State 加载失败: ${message}`,
          };
        });
      }
    },
    [state.apiPort, state.currentSession],
  );

  const currentSessionId = state.currentSession?.session_id ?? null;
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

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // 切换会话时加载 messages 和 traces（也处理初始加载）
  useEffect(() => {
    const sessionId = state.currentSession?.session_id;
    const apiPort = state.apiPort;

    if (!apiPort || !sessionId) return;

    let cancelled = false;

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
          return {
            ...prev,
            messages: messages.items ?? [],
            traceEvents: dedupeTraceEvents(traceEvents),
          };
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `加载失败: ${message}` }));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [state.currentSession?.session_id, state.apiPort]);

  // SSE 是实时路径；这个轮询是后端权威状态兜底，避免浏览器 fetch stream 卡住后 UI 停在 pending。
  useEffect(() => {
    const apiPort = state.apiPort;
    const sessionId = currentSessionId;
    if (!apiPort || !sessionId || !pendingPollKey) {
      return;
    }

    let cancelled = false;
    let timerId: number | null = null;

    const scheduleNext = () => {
      if (cancelled) {
        return;
      }
      timerId = window.setTimeout(refreshPendingFromBackend, 1000);
    };

    const refreshPendingFromBackend = async () => {
      try {
        const [messages, traceEvents] = await Promise.all([
          listMessages(apiPort, sessionId),
          getSessionTraces(apiPort, sessionId),
        ]);
        if (cancelled) {
          return;
        }

        const fetchedTraceEvents = dedupeTraceEvents(traceEvents);
        setState((prev) => {
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }

          const next = cloneMaps(prev);
          next.messages = messages.items ?? [];
          next.traceEvents = dedupeTraceEvents([
            ...next.traceEvents,
            ...fetchedTraceEvents,
          ]);

          const pendingList = next.pendingConversations.get(sessionId) ?? [];
          const updatedPendingList = pendingList
            .map((conversation) => {
              const conversationEvents = traceEventsForConversation(
                fetchedTraceEvents,
                conversation,
              );
              if (conversationEvents.length === 0) {
                return conversation;
              }

              const events = dedupeTraceEvents([
                ...conversation.events,
                ...conversationEvents,
              ]);
              return {
                ...conversation,
                events,
                status: statusForConversationEvents(
                  events,
                  conversation.status,
                ),
                pending: !hasJobTerminalTraceEvent(events),
              };
            })
            .filter((conversation) => conversation.pending);

          writePendingList(
            next.pendingConversations,
            sessionId,
            updatedPendingList,
          );

          if (updatedPendingList.length < pendingList.length) {
            next.status = "消息已更新";
          }

          return next;
        });
      } catch (error) {
        if (cancelled) {
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({
          ...prev,
          status: `刷新运行中消息失败: ${message}`,
        }));
      } finally {
        scheduleNext();
      }
    };

    timerId = window.setTimeout(refreshPendingFromBackend, 500);

    return () => {
      cancelled = true;
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    };
  }, [state.apiPort, currentSessionId, pendingPollKey]);

  useEffect(() => {
    const apiPort = state.apiPort;
    const sessionId = state.currentSession?.session_id ?? null;

    if (!apiPort || !sessionId) {
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
      return;
    }

    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;

    void streamSessionEvents(apiPort, sessionId, {
      signal: controller.signal,
      onEvent: (event: SessionStreamEvent) => {
        const traceEvent = buildTraceEvent(event);

        setState((prev) => {
          // 忽略非当前会话的事件（切换会话时旧 SSE 可能还有残留事件）
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.traceEvents = dedupeTraceEvents([...next.traceEvents, traceEvent]);

          const pendingList = next.pendingConversations.get(sessionId) ?? [];
          if (pendingList.length === 0) {
            return next;
          }

          let pendingIndex = pendingList.findIndex((conversation) =>
            conversationMatchesTraceEvent(conversation, traceEvent),
          );
          if (pendingIndex === -1 && pendingList.length === 1) {
            pendingIndex = 0;
          }
          if (pendingIndex === -1) {
            return next;
          }

          const pending = pendingList[pendingIndex];
          const updatedPending: ConversationView = {
            ...pending,
            events: dedupeTraceEvents([...pending.events, traceEvent]),
          };

          if (event.type === "status_change") {
            const status = tracePayloadString(traceEvent, "status");
            updatedPending.status = status === "queued" ? "queued" : "running";
          } else if (
            [
              "job_started",
              "text_start",
              "text_delta",
              "text_end",
              "tool_call_start",
              "tool_call_end",
            ].includes(event.type)
          ) {
            updatedPending.status = "running";
          } else if (isTerminalTraceType(event.type)) {
            updatedPending.status = terminalStatusForEvent(event.type);
            updatedPending.pending = false;
          }

          const updatedPendingList = [...pendingList];
          updatedPendingList[pendingIndex] = updatedPending;
          writePendingList(next.pendingConversations, sessionId, updatedPendingList);

          if (isJobTerminalTraceType(event.type)) {
            const terminalTraceEvent = traceEvent;

            void (async () => {
              try {
                const [messages, traceEvents] = await Promise.all([
                  listMessages(apiPort, sessionId),
                  getSessionTraces(apiPort, sessionId),
                ]);
                setState((latest) => {
                  const latestNext = cloneMaps(latest);
                  removePendingForTraceEvent(
                    latestNext.pendingConversations,
                    sessionId,
                    terminalTraceEvent,
                  );
                  if (latest.currentSession?.session_id !== sessionId) {
                    return latestNext;
                  }
                  latestNext.messages = messages.items;
                  latestNext.traceEvents = dedupeTraceEvents(traceEvents);
                  latestNext.status = "消息已更新";
                  return latestNext;
                });
              } catch (error) {
                const message =
                  error instanceof Error ? error.message : String(error);
                setState((latest) => ({
                  ...latest,
                  status: `刷新失败: ${message}`,
                }));
              }
            })();
          }

          return next;
        });
      },
      onError: (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `事件流错误: ${message}` }));
      },
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : String(error);
        setState((prev) => ({ ...prev, status: `事件流错误: ${message}` }));
      }
    });

    return () => {
      controller.abort();
    };
  }, [state.apiPort, state.currentSession?.session_id]);

  const value = useMemo(
    () => ({
      state,
      setStatus,
      sendMessage,
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
