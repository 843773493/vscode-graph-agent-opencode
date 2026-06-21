import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import {
    createSession as apiCreateSession,
    interruptSession as apiInterruptSession,
    sendUserMessage as apiSendMessage,
    DEFAULT_SESSION_TITLE,
    getSessionTraces,
    getWorkspace,
    listMessages,
    listSessions,
    streamSessionEvents,
    type SessionStreamEvent,
} from './api';
import type { Message, TraceEvent } from './types/backend';
import type { AppState, ConversationView } from './types/frontend';

function groupMessagesIntoConversations(messages: Message[]): ConversationView[] {
  const conversations: ConversationView[] = [];
  let current: ConversationView | null = null;

  for (const message of messages) {
    if (message.role === 'user') {
      current = {
        conversationId: message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: message,
        events: [],
        status: 'done',
        jobId: String(message.metadata?.job_id ?? '') || null,
        pending: false,
        source: 'messages',
      };
      conversations.push(current);
      continue;
    }

    if (!current) {
      // 助手消息出现在用户消息之前（理论上不应该发生），用助手消息 ID 作为 conversationId
      current = {
        conversationId: message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: null,
        events: [],
        status: 'done',
        jobId: String(message.metadata?.job_id ?? '') || null,
        pending: false,
        source: 'messages',
      };
      conversations.push(current);
    }
    // 助手消息内容由 ChatPanel 从 traceEvents 聚合得到；不再维护 assistantMessages 数组。
  }

  return conversations;
}

function attachTraceEventsToConversations(conversations: ConversationView[], traceEvents: TraceEvent[]): ConversationView[] {
  if (conversations.length === 0 || traceEvents.length === 0) {
    return conversations;
  }

  // 按 event_id 去重（SSE 流 + job_completed 后 API 重新获取可能产生重复）
  const seenEventIds = new Set<string>();
  const dedupedEvents = traceEvents.filter(event => {
    const id = event.event_id;
    if (!id || seenEventIds.has(id)) return false;
    seenEventIds.add(id);
    return true;
  }).sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

  // 从 trace 事件中提取 message_created，建立 message_id → 时间戳映射
  // 后端 DTO 格式：真实 payload 嵌套在 raw.payload 中
  interface MessageBoundary { messageId: string; timestamp: number; }
  const boundaries: MessageBoundary[] = [];
  for (const event of dedupedEvents) {
    if (event.type === 'message_created') {
      const rawPayload = (event as Record<string, unknown>).raw as Record<string, unknown> | undefined;
      const innerPayload = (rawPayload?.payload ?? event.payload ?? {}) as Record<string, unknown>;
      const msgId = typeof innerPayload.message_id === 'string' ? innerPayload.message_id : '';
      if (msgId) {
        boundaries.push({ messageId: msgId, timestamp: new Date(event.timestamp).getTime() });
      }
    }
  }

  // 为每个 conversation 按时间戳范围分配事件：
  // conversation[0] 得到 [boundary[0].ts, boundary[1].ts) 区间内的事件
  // conversation[N] 得到 [boundary[N].ts, +∞) 区间内的事件
  const boundaryTs = boundaries.map(b => b.timestamp);

  return conversations.map((conversation, convIndex) => {
    const userMsgId = conversation.userMessage?.message_id ?? '';
    // 找到该 conversation 对应的 message_created 边界索引
    const boundaryIndex = boundaries.findIndex(b => b.messageId === userMsgId);
    if (boundaryIndex === -1) {
      return conversation;
    }

    const startTs = boundaryTs[boundaryIndex];
    const endTs = boundaryIndex + 1 < boundaryTs.length ? boundaryTs[boundaryIndex + 1] : Infinity;

    const convEvents = dedupedEvents.filter(event => {
      const eventTs = new Date(event.timestamp).getTime();
      return eventTs >= startTs && eventTs < endTs;
    });

    return { ...conversation, events: convEvents };
  });
}

function cloneMaps(state: AppState): AppState {
  return { ...state, pendingConversations: new Map(state.pendingConversations) };
}

function buildTraceEvent(event: SessionStreamEvent): TraceEvent {
  // 后端 TraceEventDTO 把原始 payload 嵌套在 raw.payload 中
  const raw = (event.raw as Record<string, unknown> | undefined) || {};
  const rawPayload = (raw.payload as Record<string, unknown> | undefined) || {};
  return {
    event_id: event.event_id,
    job_id: event.job_id ?? 'unknown_job',
    step_id: event.step_id ?? null,
    agent_id: event.agent_id ?? null,
    timestamp: event.timestamp,
    type: event.type as TraceEvent['type'],
    payload: rawPayload,
    raw: event.raw,
  };
}

export function getConversationsForSession(sessionId: string, state: AppState): ConversationView[] {
  const conversations = groupMessagesIntoConversations(state.messages.filter(message => message.session_id === sessionId));
  const withTraceEvents = attachTraceEventsToConversations(conversations, state.traceEvents);
  const pending = state.pendingConversations.get(sessionId);

  if (!pending) {
    return withTraceEvents;
  }

  const matchedIndex = withTraceEvents.findIndex(conversation => {
    const userMessageId = conversation.userMessage?.message_id ?? '';
    const pendingUserMessageId = pending.userMessage?.message_id ?? '';
    const conversationJobId = conversation.jobId ?? '';
    const pendingJobId = pending.jobId ?? '';

    if (pendingUserMessageId && userMessageId && pendingUserMessageId === userMessageId) {
      return true;
    }

    if (pendingJobId && conversationJobId && pendingJobId === conversationJobId) {
      return true;
    }

    return false;
  });

  if (matchedIndex === -1) {
    // 防御：非 pending 状态的 pending 对话不应追加
    if (!pending.pending) {
      return withTraceEvents;
    }
    return [...withTraceEvents, { ...pending, source: 'pending' }];
  }

  return withTraceEvents.map((conversation, index) =>
    index === matchedIndex ? { ...conversation, ...pending, source: conversation.source } : conversation,
  );
}

const INITIAL_STATE: AppState = {
  apiPort: 8000,
  workspaceRoot: null,
  workspaceName: null,
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
  pendingConversations: new Map(),
  status: '准备就绪',
  error: null,
  isBootstrapping: true,
  expandDetails: true,
  historyPanelOpen: true,
};

interface AppContextType {
  state: AppState;
  setStatus: (text: string) => void;
  sendMessage: (content: string) => void;
  interruptSession: () => void;
  selectSession: (sessionId: string) => void;
  createSession: (title?: string) => void;
  toggleHistoryPanel: () => void;
  toggleExpandDetails: (expand: boolean) => void;
}

const AppContext = createContext<AppContextType | null>(null);

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) {
    throw new Error('useAppState must be used within AppProvider');
  }
  return ctx;
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(INITIAL_STATE);
  const streamAbortRef = useRef<AbortController | null>(null);

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const apiPort = state.apiPort ?? 8000;
      const [workspace, sessions] = await Promise.all([
        getWorkspace(apiPort),
        listSessions(apiPort),
      ]);
      setState(prev => {
        // 保留当前已选中的会话，只有初次加载（无选中）才取列表第一个
        const nextCurrentSession = prev.currentSession ?? sessions.items[0] ?? null;
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
      setState(prev => ({
        ...prev,
        error: message,
        status: '初始化失败',
        isBootstrapping: false,
      }));
    }
  }, [state.apiPort]);

  const selectSession = useCallback((sessionId: string) => {
    // 只需切换 currentSession 引用并中断旧 SSE；消息/trace 由 useEffect 统一加载
    streamAbortRef.current?.abort();
    setState(prev => ({
      ...prev,
      currentSession: prev.sessions.find(session => session.session_id === sessionId) ?? prev.currentSession,
    }));
  }, []);

  const createSession = useCallback(async (title: string = DEFAULT_SESSION_TITLE) => {
    const session = await apiCreateSession(state.apiPort ?? 8000, title);
    setState(prev => ({
      ...prev,
      sessions: [session, ...prev.sessions],
      currentSession: session,
      traceEvents: [],
      status: '已创建会话',
    }));
  }, [state.apiPort]);

  const sendMessage = useCallback(async (content: string) => {
    if (!state.currentSession) {
      throw new Error('当前没有可发送消息的会话');
    }

    const session = state.currentSession;
    const accepted = await apiSendMessage(state.apiPort ?? 8000, session.session_id, content, session.current_agent_id);
    const messageId = accepted.message_id ?? `local_user_${Date.now()}`;
    const jobId = accepted.job_id ?? null;

    setState(prev => {
      const next = cloneMaps(prev);
      const now = new Date().toISOString();
      const conversation: ConversationView = {
        conversationId: messageId,
        sessionId: session.session_id,
        userMessage: {
          message_id: messageId,
          session_id: session.session_id,
          role: 'user',
          content,
          metadata: { source: 'optimistic' },
          attachments: [],
          created_at: now,
          updated_at: now,
        },
        events: [],
        status: 'running',
        jobId,
        pending: true,
        source: 'pending',
        streamingText: '',
        streamingTextActive: false,
      };
      next.pendingConversations.set(session.session_id, conversation);
      next.status = '已发送，等待生成';
      return next;
    });
  }, [state.apiPort, state.currentSession]);

  const interruptSessionCallback = useCallback(async () => {
    if (!state.currentSession) {
      throw new Error('当前没有可中断的会话');
    }

    setState(prev => ({ ...prev, status: '正在中断生成...' }));
    const result = await apiInterruptSession(state.apiPort ?? 8000, state.currentSession.session_id);
    setState(prev => ({ ...prev, status: `已中断: ${result.phase}` }));
  }, [state.apiPort, state.currentSession]);

  const toggleHistoryPanel = useCallback(() => {
    setState(prev => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen }));
  }, []);

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState(prev => ({ ...prev, expandDetails: expand }));
  }, []);

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
        setState(prev => {
          // 如果在加载期间会话已切换，丢弃过期数据
          if (prev.currentSession?.session_id !== sessionId) return prev;
          return {
            ...prev,
            messages: messages.items ?? [],
            traceEvents: [...traceEvents].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()),
          };
        });
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setState(prev => ({ ...prev, status: `加载失败: ${message}` }));
      }
    })();

    return () => { cancelled = true; };
  }, [state.currentSession?.session_id, state.apiPort]);

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

        setState(prev => {
          // 忽略非当前会话的事件（切换会话时旧 SSE 可能还有残留事件）
          if (prev.currentSession?.session_id !== sessionId) {
            return prev;
          }
          const next = cloneMaps(prev);
          next.traceEvents = [...next.traceEvents, traceEvent];

          const pending = next.pendingConversations.get(sessionId);
          if (!pending) {
            return next;
          }

          const updatedPending: ConversationView = {
            ...pending,
            events: [...pending.events, traceEvent],
          };

          if (event.type === 'text_start') {
            updatedPending.streamingText = '';
            updatedPending.streamingTextActive = true;
          } else if (event.type === 'text_delta') {
            const deltaText = typeof traceEvent.payload?.text === 'string' ? traceEvent.payload.text : '';
            updatedPending.streamingText = (updatedPending.streamingText ?? '') + deltaText;
          } else if (event.type === 'text_end') {
            updatedPending.streamingTextActive = false;
          } else if (['job_completed', 'job_failed', 'job_cancelled', 'session_interrupted'].includes(event.type)) {
            updatedPending.status = event.type === 'job_completed' ? 'done' : 'error';
            updatedPending.pending = false;
            updatedPending.streamingTextActive = false;

            void (async () => {
              try {
                const [messages, traceEvents] = await Promise.all([
                  listMessages(apiPort, sessionId),
                  getSessionTraces(apiPort, sessionId),
                ]);
                setState(latest => {
                  const latestNext = cloneMaps(latest);
                  latestNext.pendingConversations.delete(sessionId);
                  if (latest.currentSession?.session_id !== sessionId) {
                    return latestNext;
                  }
                  latestNext.messages = messages.items;
                  latestNext.traceEvents = [...traceEvents].sort(
                    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime(),
                  );
                  latestNext.status = '消息已更新';
                  return latestNext;
                });
              } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                setState(latest => ({ ...latest, status: `刷新失败: ${message}` }));
              }
            })();
          }

          next.pendingConversations.set(sessionId, updatedPending);
          return next;
        });
      },
      onError: (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        setState(prev => ({ ...prev, status: `事件流错误: ${message}` }));
      },
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : String(error);
        setState(prev => ({ ...prev, status: `事件流错误: ${message}` }));
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
    }),
    [state, setStatus, sendMessage, interruptSessionCallback, selectSession, createSession, toggleHistoryPanel, toggleExpandDetails],
  );

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
