import type React from 'react';
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { HostToWebviewMessageType, WebviewToHostMessageType } from '../../shared/protocol.js';
import type {
    ActiveJob,
    AppState,
    ConversationView,
    HostStateMessage,
    HostToWebviewMessage,
    Message,
    Session,
    TraceEvent,
} from './types';
import { postMessage, setVsCodeState } from './vscode';

const INITIAL_STATE: AppState = {
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
  historyPanelOpen: false,
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
  if (!ctx) throw new Error('useAppState must be used within AppProvider');
  return ctx;
}

function cloneMaps(state: AppState): AppState {
  return { ...state, pendingConversations: new Map(state.pendingConversations) };
}

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

export function groupMessagesIntoConversations(messages: Message[]): ConversationView[] {
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
  const conversations = groupMessagesIntoConversations(state.messages.filter(m => m.session_id === sessionId));
  const pending = state.pendingConversations.get(sessionId);

  if (!pending) {
    return conversations;
  }

  const matchedConversationIndex = conversations.findIndex(conversation => {
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

  if (matchedConversationIndex >= 0) {
    const mergedConversation = conversations[matchedConversationIndex];
    const merged: ConversationView = {
      ...mergedConversation,
      assistantMessages: pending.assistantMessages.length > 0 ? pending.assistantMessages : mergedConversation.assistantMessages,
      events: [...mergedConversation.events, ...pending.events],
      status: pending.status,
      jobId: pending.jobId ?? mergedConversation.jobId,
      pending: pending.pending,
      source: mergedConversation.source,
    };
    return conversations.map((conversation, index) => (index === matchedConversationIndex ? merged : conversation));
  }

  return pending.userMessage ? [...conversations, { ...pending, source: 'pending' }] : conversations;
}

function readBootState(): Partial<AppState> {
  const bootEl = document.getElementById('graph-agent-boot');
  if (!bootEl?.textContent) return {};
  try {
    return JSON.parse(bootEl.textContent) as Partial<AppState>;
  } catch (error) {
    throw new Error(`读取 webview boot 数据失败: ${(error as Error).message}`);
  }
}

function readPersistedState(): Partial<AppState> {
  if (typeof window === 'undefined') {
    return {};
  }

  const acquire = (window as Window & { acquireVsCodeApi?: () => { getState: <T>() => T | undefined } }).acquireVsCodeApi;
  if (!acquire) {
    return {};
  }

  try {
    return acquire().getState<Partial<AppState>>() ?? {};
  } catch {
    return {};
  }
}

function mergeState(boot: Partial<AppState>, persisted: Partial<AppState>): AppState {
  const pendingConversations = new Map<string, ConversationView>();
  const bootPendingConversations = (boot as { pendingConversations?: ConversationView[] }).pendingConversations ?? (persisted as { pendingConversations?: ConversationView[] }).pendingConversations ?? [];
  const bootSession = (boot as { session?: Session | null }).session ?? null;
  const persistedSession = (persisted as { session?: Session | null }).session ?? null;
  bootPendingConversations.forEach(conversation => pendingConversations.set(conversation.sessionId, conversation));
  return {
    ...INITIAL_STATE,
    workspaceRoot: boot.workspaceRoot ?? persisted.workspaceRoot ?? '',
    workspaceName: boot.workspaceName ?? persisted.workspaceName ?? 'workspace',
    sessions: (boot.sessions ?? persisted.sessions ?? []) as Session[],
    currentSession: (boot.currentSession ?? bootSession ?? persisted.currentSession ?? persistedSession ?? null) as Session | null,
    messages: (boot.messages ?? persisted.messages ?? []) as Message[],
    traceEvents: (boot.traceEvents ?? persisted.traceEvents ?? []) as TraceEvent[],
    activeJob: (boot.activeJob ?? persisted.activeJob ?? null) as ActiveJob | null,
    status: String(boot.status ?? persisted.status ?? '准备就绪'),
    expandDetails: Boolean(boot.expandDetails ?? persisted.expandDetails ?? true),
    historyPanelOpen: Boolean(boot.historyPanelOpen ?? persisted.historyPanelOpen ?? false),
    pendingConversations,
  };
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(() => mergeState(readBootState(), readPersistedState()));

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const sendMessage = useCallback((content: string) => {
    const activeSession = state.currentSession;
    if (!activeSession) {
      setStatus('请先创建会话');
      return;
    }
    setState(prev => {
      const next = cloneMaps(prev);
      const conversation: ConversationView = {
        conversationId: `local_${Date.now()}`,
        sessionId: activeSession.session_id,
        userMessage: {
          message_id: `local_user_${Date.now()}`,
          session_id: activeSession.session_id,
          role: 'user',
          content,
          metadata: { source: 'optimistic' },
          attachments: [],
          created_at: new Date().toISOString(),
        },
        assistantMessages: [],
        events: [],
        status: 'running',
        jobId: null,
        pending: true,
      };
      next.pendingConversations.set(activeSession.session_id, conversation);
      next.status = '已发送，等待 SSE 事件';
      return next;
    });
    postMessage({ type: WebviewToHostMessageType.sendMessage, content });
  }, [state.currentSession, setStatus]);

  const selectSession = useCallback((sessionId: string) => postMessage({ type: WebviewToHostMessageType.selectSession, sessionId }), []);
  const createSession = useCallback((title = '新会话') => postMessage({ type: WebviewToHostMessageType.createSession, title }), []);
  const toggleHistoryPanel = useCallback(() => setState(prev => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen })), []);
  const toggleExpandDetails = useCallback((expand: boolean) => setState(prev => ({ ...prev, expandDetails: expand })), []);

  useEffect(() => {
    const handler = (event: MessageEvent<HostToWebviewMessage>) => {
      const msg = event.data;
      if (!msg?.type) return;
      if (msg.type === HostToWebviewMessageType.state) {
        const stateMsg = msg as HostStateMessage;
        setState(prev => {
          const next = cloneMaps(prev);
          next.workspaceRoot = stateMsg.state.workspaceRoot;
          next.workspaceName = stateMsg.state.workspaceName;
          next.sessions = stateMsg.state.sessions;
          next.currentSession = stateMsg.state.session;
          next.messages = stateMsg.state.messages;
          next.traceEvents = stateMsg.state.traceEvents;
          next.activeJob = stateMsg.state.activeJob;
          next.status = stateMsg.status;
          return next;
        });
        return;
      }
      if (msg.type === HostToWebviewMessageType.jobEvent) {
        const jobEvent = msg as Extract<HostToWebviewMessage, { type: 'jobEvent' }>;
        setState(prev => {
          const next = cloneMaps(prev);
          const pending = next.pendingConversations.get(jobEvent.sessionId);
          if (!pending || (pending.jobId && pending.jobId !== jobEvent.jobId)) return next;
          pending.jobId = jobEvent.jobId;
          pending.events = [...pending.events, { event_type: jobEvent.eventType, data: jobEvent.payload, timestamp: new Date().toISOString() }];
          if (jobEvent.eventType === 'job_completed' || jobEvent.eventType === 'job_failed' || jobEvent.eventType === 'job_cancelled') {
            pending.status = jobEvent.eventType === 'job_completed' ? 'done' : 'error';
            pending.pending = false;
          }
          return next;
        });
        return;
      }
      if (msg.type === HostToWebviewMessageType.messageAccepted) {
        const accepted = msg as Extract<HostToWebviewMessage, { type: 'messageAccepted' }>;
        setState(prev => {
          const next = cloneMaps(prev);
          const pending = next.pendingConversations.get(accepted.sessionId) ?? {
            conversationId: `local_${Date.now()}`,
            sessionId: accepted.sessionId,
            userMessage: next.messages.filter(m => m.session_id === accepted.sessionId && m.role === 'user').at(-1) ?? null,
            assistantMessages: [],
            events: [],
            status: 'running',
            jobId: null,
            pending: true,
          };
          pending.jobId = accepted.jobId;
          pending.pending = true;
          next.pendingConversations.set(accepted.sessionId, pending);
          next.activeJob = { jobId: accepted.jobId, sessionId: accepted.sessionId, status: 'running', messageId: accepted.messageId, content: accepted.content };
          next.status = '任务已接收';
          return next;
        });
      }
      if (msg.type === HostToWebviewMessageType.sessionCreated) {
        const sessionCreated = msg as Extract<HostToWebviewMessage, { type: 'sessionCreated' }>;
        setState(prev => ({ ...prev, currentSession: sessionCreated.session }));
      }
      if (msg.type === HostToWebviewMessageType.error) {
        const errorMsg = msg as Extract<HostToWebviewMessage, { type: 'error' }>;
        setStatus(errorMsg.message);
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [setStatus]);

  useEffect(() => {
    postMessage({ type: WebviewToHostMessageType.ready });
  }, []);

  useEffect(() => {
    setVsCodeState({
      workspaceRoot: state.workspaceRoot,
      workspaceName: state.workspaceName,
      sessions: state.sessions,
      currentSession: state.currentSession,
      messages: state.messages,
      traceEvents: state.traceEvents,
      activeJob: state.activeJob,
      status: state.status,
      expandDetails: state.expandDetails,
      historyPanelOpen: state.historyPanelOpen,
      pendingConversations: Array.from(state.pendingConversations.entries()),
    });
  }, [state]);

  const value = useMemo<AppContextType>(() => ({
    state,
    setStatus,
    sendMessage,
    selectSession,
    createSession,
    toggleHistoryPanel,
    toggleExpandDetails,
  }), [state, setStatus, sendMessage, selectSession, createSession, toggleHistoryPanel, toggleExpandDetails]);

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
