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
import type { Message, Session, TraceEvent } from './types/backend';
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
        assistantMessages: [],
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
      current = {
        conversationId: message.message_id || `conversation_${conversations.length}`,
        sessionId: message.session_id,
        userMessage: null,
        assistantMessages: [],
        events: [],
        status: 'done',
        jobId: String(message.metadata?.job_id ?? '') || null,
        pending: false,
        source: 'messages',
      };
      conversations.push(current);
    }

    current.assistantMessages.push(message);
  }

  return conversations;
}

function attachTraceEventsToConversations(conversations: ConversationView[], traceEvents: TraceEvent[]): ConversationView[] {
  if (conversations.length === 0 || traceEvents.length === 0) {
    return conversations;
  }

  return conversations.map((conversation) => {
    const jobId = conversation.jobId;
    if (!jobId) {
      return conversation;
    }

    return {
      ...conversation,
      events: traceEvents
        .filter(event => event.job_id === jobId)
        .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()),
    };
  });
}

function cloneMaps(state: AppState): AppState {
  return { ...state, pendingConversations: new Map(state.pendingConversations) };
}

function buildTraceEvent(event: SessionStreamEvent): TraceEvent {
  return {
    event_id: event.event_id,
    job_id: event.job_id ?? 'unknown_job',
    step_id: event.step_id ?? null,
    agent_id: event.agent_id ?? null,
    timestamp: event.timestamp,
    type: event.type as TraceEvent['type'],
    payload: event.payload,
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
      const nextCurrentSession = state.currentSession ?? sessions.items[0] ?? null;
      const traceEvents = nextCurrentSession ? await getSessionTraces(apiPort, nextCurrentSession.session_id) : [];
      setState(prev => ({
        ...prev,
        workspaceRoot: workspace.root_path,
        workspaceName: workspace.name,
        sessions: sessions.items,
        currentSession: nextCurrentSession,
        traceEvents: [...traceEvents].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()),
        error: null,
        isBootstrapping: false,
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setState(prev => ({
        ...prev,
        error: message,
        status: '初始化失败',
        isBootstrapping: false,
      }));
    }
  }, [state.apiPort, state.currentSession]);

  const selectSession = useCallback((sessionId: string) => {
    void (async () => {
      streamAbortRef.current?.abort();
      const nextSession = state.sessions.find(session => session.session_id === sessionId) ?? state.currentSession;
      const traceEvents = nextSession ? await getSessionTraces(state.apiPort ?? 8000, nextSession.session_id) : [];
      setState(prev => ({
        ...prev,
        currentSession: prev.sessions.find(session => session.session_id === sessionId) ?? prev.currentSession,
        traceEvents: [...traceEvents].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()),
      }));
    })();
  }, [state.apiPort, state.currentSession, state.sessions]);

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
        assistantMessages: [],
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
            const deltaText = typeof traceEvent.payload.text === 'string' ? traceEvent.payload.text : '';
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
