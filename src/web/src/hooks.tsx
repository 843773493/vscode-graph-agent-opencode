import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { createSession as apiCreateSession, sendMessage as apiSendMessage, DEFAULT_AGENT_ID, DEFAULT_SESSION_TITLE, getWorkspace, listMessages, listSessions } from './api';
import type { Message } from './types/backend';
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

export function getConversationsForSession(sessionId: string, state: AppState): ConversationView[] {
  return groupMessagesIntoConversations(state.messages.filter(message => message.session_id === sessionId));
}

const INITIAL_STATE: AppState = {
  apiPort: 8000,
  workspaceRoot: '',
  workspaceName: 'workspace',
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
  activeJob: null,
  pendingConversations: new Map(),
  status: '准备就绪',
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
    const [workspace, sessions] = await Promise.all([getWorkspace(state.apiPort ?? 8000), listSessions(state.apiPort ?? 8000)]);
    setState(prev => ({
      ...prev,
      workspaceRoot: workspace.root_path,
      workspaceName: workspace.name,
      sessions: sessions.items,
      currentSession: prev.currentSession ?? sessions.items[0] ?? null,
    }));
  }, [state.apiPort]);

  const selectSession = useCallback((sessionId: string) => {
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
      status: '已创建会话',
    }));
  }, [state.apiPort]);

  const sendMessage = useCallback(async (content: string) => {
    if (!state.currentSession) {
      throw new Error('当前没有可发送消息的会话');
    }

    await apiSendMessage(state.apiPort ?? 8000, state.currentSession.session_id, { content, agent_id: DEFAULT_AGENT_ID });
    const messages = await listMessages(state.apiPort ?? 8000, state.currentSession.session_id);
    setState(prev => ({
      ...prev,
      messages: messages.items,
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
