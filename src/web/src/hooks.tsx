import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { createSession as apiCreateSession, sendUserMessage as apiSendMessage, DEFAULT_SESSION_TITLE, getSessionTraces, getWorkspace, listMessages, listSessions } from './api';
import type { Message, TraceEvent } from './types/backend';
import type { AppState, ConversationView } from './types/frontend';

function normalizeMessage(msg: Partial<Message> & { id?: string; createdAt?: string | null }): Message {
  return {
    message_id: msg.message_id ?? msg.id ?? '',
    session_id: msg.session_id ?? '',
    role: msg.role ?? 'assistant',
    content: msg.content ?? '',
    metadata: msg.metadata ?? {},
    attachments: msg.attachments ?? [],
    created_at: msg.created_at ?? msg.createdAt ?? null,
  };
}

function groupMessagesIntoConversations(messages: Message[]): ConversationView[] {
  const conversations: ConversationView[] = [];
  let current: ConversationView | null = null;

  for (const raw of messages) {
    const message = normalizeMessage(raw);
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
      events: traceEvents.filter(event => event.job_id === jobId),
    };
  });
}

export function getConversationsForSession(sessionId: string, state: AppState): ConversationView[] {
  const conversations = groupMessagesIntoConversations(state.messages.filter(message => message.session_id === sessionId));
  return attachTraceEventsToConversations(conversations, state.traceEvents);
}

const INITIAL_STATE: AppState = {
  apiPort: 8000,
  workspaceRoot: null,
  workspaceName: null,
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
  activeJob: null,
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

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      const [workspace, sessions] = await Promise.all([getWorkspace(state.apiPort ?? 8000), listSessions(state.apiPort ?? 8000)]);
      const nextCurrentSession = state.currentSession ?? sessions.items[0] ?? null;
      const traceEvents = nextCurrentSession ? await getSessionTraces(state.apiPort ?? 8000, nextCurrentSession.session_id) : [];
      setState(prev => ({
        ...prev,
        workspaceRoot: workspace.root_path,
        workspaceName: workspace.name,
        sessions: sessions.items,
        currentSession: nextCurrentSession,
        traceEvents,
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
  }, [state.apiPort]);

  const selectSession = useCallback((sessionId: string) => {
    void (async () => {
      const nextSession = state.sessions.find(session => session.session_id === sessionId) ?? state.currentSession;
      const traceEvents = nextSession ? await getSessionTraces(state.apiPort ?? 8000, nextSession.session_id) : [];
      setState(prev => ({
        ...prev,
        currentSession: prev.sessions.find(session => session.session_id === sessionId) ?? prev.currentSession,
        traceEvents,
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

    await apiSendMessage(state.apiPort ?? 8000, state.currentSession.session_id, content, state.currentSession.agent_id);
    const messages = await listMessages(state.apiPort ?? 8000, state.currentSession.session_id);
    const traceEvents = await getSessionTraces(state.apiPort ?? 8000, state.currentSession.session_id);
    setState(prev => ({
      ...prev,
      messages: messages.items,
      traceEvents,
      status: '消息已发送',
    }));
  }, [state.apiPort, state.currentSession]);

  const toggleHistoryPanel = useCallback(() => {
    setState(prev => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen }));
  }, []);

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState(prev => ({ ...prev, expandDetails: expand }));
  }, []);

  React.useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  const value = useMemo(() => ({ state, setStatus, sendMessage, selectSession, createSession, toggleHistoryPanel, toggleExpandDetails }), [state, setStatus, sendMessage, selectSession, createSession, toggleHistoryPanel, toggleExpandDetails]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
