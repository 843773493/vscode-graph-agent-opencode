import { createContext, useContext, useEffect, useState, useCallback } from 'react';
import type { AppState, Message, Session, TraceEvent, ActiveJob, PendingTurn, HostToWebviewMessage } from './types';
import { postMessage, postDebug } from './vscode';

const INITIAL_STATE: AppState = {
  workspaceRoot: '',
  workspaceName: 'workspace',
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
  activeJob: null,
  pendingTurns: new Map(),
  status: '准备就绪',
  expandDetails: true,
  autoContinueEnabled: new Map(),
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
  setAutoContinue: (sessionId: string, enabled: boolean) => void;
}

const AppContext = createContext<AppContextType | null>(null);

export function useAppState() {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppState must be used within AppProvider');
  return ctx;
}

/** 深度复制 Map/Set 等高阶类型 */
function cloneMaps(state: AppState): AppState {
  return {
    ...state,
    pendingTurns: new Map(state.pendingTurns),
    autoContinueEnabled: new Map(state.autoContinueEnabled),
  };
}

/* ---- Message/Turn helpers ---- */

export function normalizeMessage(msg: Partial<Message>): Required<Pick<Message, 'message_id' | 'session_id' | 'role' | 'content' | 'metadata' | 'attachments' | 'created_at'>> {
  return {
    message_id: msg.message_id ?? (msg as any)?.id ?? '',
    session_id: msg.session_id ?? '',
    role: msg.role ?? 'assistant',
    content: msg.content ?? '',
    metadata: (msg.metadata as Record<string, unknown>) ?? {},
    attachments: (msg.attachments as unknown[]) ?? [],
    created_at: msg.created_at ?? (msg as any)?.createdAt ?? null,
  };
}

export interface Turn {
  turnId: string;
  sessionId: string;
  userMessage: Message | null;
  assistantMessages: Message[];
  events: TraceEvent[];
  status: 'running' | 'done' | 'error';
  jobId: string | null;
  pending?: boolean;
}

export function splitMessagesIntoTurns(messages: ReturnType<typeof normalizeMessage>[]): Turn[] {
  const turns: Turn[] = [];
  let currentTurn: Turn | null = null;
  for (const rawMessage of messages) {
    const message = normalizeMessage(rawMessage);
    if (message.role === 'user') {
      currentTurn = {
        turnId: message.message_id || `turn_${turns.length}_${message.created_at ?? Date.now()}`,
        sessionId: message.session_id,
        userMessage: message,
        assistantMessages: [],
        events: [],
        status: 'done',
        jobId: (message.metadata as Record<string, unknown>)?.job_id ?? null,
      };
      turns.push(currentTurn);
      continue;
    }
    if (!currentTurn) {
      currentTurn = {
        turnId: message.message_id || `turn_${turns.length}_${Date.now()}`,
        sessionId: message.session_id,
        userMessage: null,
        assistantMessages: [],
        events: [],
        status: 'done',
        jobId: (message.metadata as Record<string, unknown>)?.job_id ?? null,
      };
      turns.push(currentTurn);
    }
    currentTurn.assistantMessages.push(message);
  }
  return turns;
}

export function getTurnsForSession(sessionId: string, state: AppState): Turn[] {
  const backendTurns = splitMessagesIntoTurns(
    state.messages
      .filter(m => m.session_id === sessionId)
      .map(normalizeMessage)
  );
  const pendingTurn = state.pendingTurns.get(sessionId);
  if (!pendingTurn) return backendTurns;
  const lastBackendTurn = backendTurns[backendTurns.length - 1];
  const pendingIsConfirmed =
    pendingTurn.status === 'done' &&
    Boolean(lastBackendTurn?.userMessage) &&
    lastBackendTurn!.userMessage!.content === pendingTurn.userMessage?.content &&
    lastBackendTurn!.assistantMessages.length > 0;
  if (pendingIsConfirmed) {
    return backendTurns;
  }
  return [...backendTurns, pendingTurn];
}

/* ---- Main context/provider ---- */

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(() => {
    const bootEl = document.getElementById('graph-agent-boot');
    let boot: Record<string, unknown> = {};
    try { boot = bootEl ? JSON.parse(bootEl.textContent || '{}') : {}; } catch { /* ignore */ }
    const persisted = (window as any).acquireVsCodeApi?.().getState?.() ?? {};
    return {
      ...INITIAL_STATE,
      workspaceRoot: String(boot.workspaceRoot ?? persisted.workspaceRoot ?? ''),
      workspaceName: String(boot.workspaceName ?? persisted.workspaceName ?? 'workspace'),
      sessions: Array.isArray(boot.sessions)
        ? boot.sessions
        : Array.isArray(persisted.sessions)
          ? persisted.sessions
          : [],
      currentSession: boot.session ?? persisted.currentSession ?? null,
      messages: Array.isArray(boot.messages)
        ? (boot.messages as Message[])
        : Array.isArray(persisted.messages)
          ? (persisted.messages as Message[])
          : [],
      traceEvents: Array.isArray(boot.traceEvents)
        ? (boot.traceEvents as TraceEvent[])
        : Array.isArray(persisted.traceEvents)
          ? (persisted.traceEvents as TraceEvent[])
          : [],
      activeJob: boot.activeJob != null
        ? ({ jobId: (boot.activeJob as any).jobId ?? null, sessionId: (boot.activeJob as any).sessionId ?? null, status: String((boot.activeJob as any).status), messageId: (boot.activeJob as any).messageId ?? null, content: String((boot.activeJob as any).content) })
        : null,
      status: String(boot.status ?? persisted.status ?? '准备就绪'),
      expandDetails: boot.expandDetails != null ? Boolean(boot.expandDetails) : true,
    };
  });

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const sendMessage = useCallback((content: string) => {
    postDebug('sendMessage 调用');
    const activeSession = state.currentSession;
    if (!activeSession) {
      setStatus('请先创建 session');
      return;
    }
    setState(prev => {
      const next = cloneMaps(prev);
      let pendingTurn = next.pendingTurns.get(activeSession.session_id);
      if (!pendingTurn) {
        pendingTurn = {
          turnId: `local_${Date.now()}_${Math.random().toString(16).slice(2)}`,
          sessionId: activeSession.session_id,
          userMessage: null,
          assistantMessages: [],
          events: [],
          status: 'running',
          jobId: null,
          pending: true,
        };
        next.pendingTurns.set(activeSession.session_id, pendingTurn);
      }
      pendingTurn.userMessage = {
        message_id: `local_user_${Date.now()}`,
        session_id: activeSession.session_id,
        role: 'user',
        content,
        metadata: { source: 'local_optimistic' },
        attachments: [],
        created_at: new Date().toISOString(),
      };
      pendingTurn.assistantMessages = [];
      pendingTurn.events = [];
      pendingTurn.status = 'running';
      pendingTurn.jobId = null;
      pendingTurn.pending = true;
      next.status = '已发送，正在等待模型响应...';
      return next;
    });
    postMessage({ type: 'sendMessage', content });
  }, [state.currentSession, setStatus]);

  const selectSession = useCallback((sessionId: string) => {
    postDebug(`selectSession: ${sessionId}`);
    postMessage({ type: 'selectSession', sessionId });
  }, []);

  const createSession = useCallback((title = '新会话') => {
    postDebug(`createSession: ${title}`);
    postMessage({ type: 'createSession', title });
  }, []);

  const toggleHistoryPanel = useCallback(() => {
    setState(prev => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen }));
  }, []);

  const toggleExpandDetails = useCallback((expand: boolean) => {
    setState(prev => ({ ...prev, expandDetails: expand }));
  }, []);

  const setAutoContinue = useCallback((sessionId: string, enabled: boolean) => {
    setState(prev => {
      const next = cloneMaps(prev);
      next.autoContinueEnabled.set(sessionId, enabled);
      return next;
    });
    postMessage({ type: 'debug', detail: `setAutoContinue: ${sessionId} = ${enabled}` } satisfies any as any);
  }, []);

  // 处理 Host 消息
  useEffect(() => {
    const handler = (event: MessageEvent) => {
      const msg = event.data;
      if (!msg || !msg.type) return;
      switch (msg.type) {
        case 'state':
          setState(prev => {
            const next = cloneMaps(prev);
            if (msg.state.sessions) next.sessions = msg.state.sessions;
            if (msg.state.workspaceRoot) next.workspaceRoot = msg.state.workspaceRoot;
            if (msg.state.workspaceName) next.workspaceName = msg.state.workspaceName;
            if (msg.state.session) next.currentSession = msg.state.session;
            if (msg.state.messages) next.messages = msg.state.messages;
            if (msg.state.traceEvents) next.traceEvents = msg.state.traceEvents;
            if (msg.state.activeJob) next.activeJob = msg.state.activeJob;
            next.status = msg.status ?? next.status;
            return next;
          } as any);
          break;
        case 'messageAccepted':
          setState(prev => {
            const next = cloneMaps(prev);
            const pending = next.pendingTurns.get(msg.sessionId) ?? (() => {
              const t: PendingTurn = { turnId: '', sessionId: msg.sessionId, userMessage: null, assistantMessages: [], events: [], status: 'running', jobId: null, pending: true };
              next.pendingTurns.set(msg.sessionId, t);
              return t;
            })();
            pending.jobId = msg.jobId ?? null;
            pending.status = 'running';
            pending.pending = true;
            next.activeJob = { jobId: msg.jobId ?? null, sessionId: msg.sessionId, status: 'running', messageId: msg.messageId ?? null, content: msg.content ?? '' };
            next.status = '任务已提交，开始接收思考过程...';
            return next;
          } as any);
          break;
        case 'jobEvent':
          setState(prev => {
            const next = cloneMaps(prev);
            const pending = next.pendingTurns.get(msg.sessionId);
            if (!pending) return next;
            if (pending.jobId && pending.jobId !== msg.jobId) return next;
            pending.jobId = msg.jobId;
            pending.events = [...(pending.events ?? []), {
              event_type: msg.eventType ?? 'event',
              data: msg.payload ?? {},
              timestamp: (msg.payload as Record<string, unknown>)?.timestamp
                ? String((msg.payload as Record<string, unknown>)!.timestamp)
                : new Date().toISOString(),
            }];
            if (['job_completed', 'job_failed', 'job_cancelled'].includes(String(msg.eventType ?? '').toLowerCase())) {
              pending.status = msg.eventType === 'job_completed' ? 'done' : 'error';
              pending.pending = false;
              next.activeJob = { ...(next.activeJob ?? { jobId: null, sessionId: null, status: '', messageId: null, content: '' }), jobId: msg.jobId, sessionId: msg.sessionId, status: msg.eventType };
              const autoContinueEnabled = next.autoContinueEnabled.get(msg.sessionId) ?? false;
              if (autoContinueEnabled && msg.eventType === 'job_completed') {
                setTimeout(() => postMessage({ type: 'sendMessage', content: '继续' }), 500);
              }
            }
            return next;
          } as any);
          break;
        case 'sessionCreated':
          setState(prev => ({ ...prev, currentSession: (msg as any).session ?? prev.currentSession }));
          break;
        case 'error':
          if (msg.message) setStatus(String(msg.message));
          break;
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [setStatus]);

  // 初始化
  useEffect(() => {
    postDebug('webview React 已启动');
    postMessage({ type: 'ready' });
  }, []);

  // persist to VSCode state
  useEffect(() => {
    (window as any).acquireVsCodeApi?.()?.setState?.({
      workspaceRoot: state.workspaceRoot,
      workspaceName: state.workspaceName,
      sessions: state.sessions,
      currentSession: state.currentSession,
      messages: state.messages,
      traceEvents: state.traceEvents,
      activeJob: state.activeJob,
      status: state.status,
      expandDetails: state.expandDetails,
      pendingTurns: Array.from(state.pendingTurns.values()),
      autoContinueEnabled: Array.from(state.autoContinueEnabled.entries()),
    });
  }, [state]);

  const handleCodeAction = useCallback((action: string, _code: string) => {
    postDebug(`代码块操作: ${action}`);
  }, []);

  const handleMessageAction = useCallback((action: string, _messageId?: string) => {
    postDebug(`消息操作: ${action}`);
  }, []);

  const value: AppContextType = {
    state,
    setStatus,
    sendMessage,
    selectSession,
    createSession,
    toggleHistoryPanel,
    toggleExpandDetails,
    setAutoContinue,
  };

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
