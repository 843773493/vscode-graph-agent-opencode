import type React from 'react';
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { HostToWebviewMessageType, WebviewToHostMessageType } from '../../shared/protocol.js';
import type { ActiveJob, Message, Session, TraceEvent } from './types/backend';
import type { AppState, ConversationView } from './types/frontend';
import type { HostStateMessage, HostToWebviewMessage } from './types/protocol';
import { clearRuntimeLog, interceptConsoleToMessageSink, postMessage, setVsCodeState, writeRuntimeLog } from './vscode';

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function collectCurrentPageCss(): string {
  if (typeof document === 'undefined') {
    return '';
  }

  const pieces: string[] = [];

  for (const sheet of Array.from(document.styleSheets)) {
    try {
      const rules = sheet.cssRules;
      if (!rules) {
        continue;
      }

      const cssText = Array.from(rules).map((rule) => rule.cssText).join('\n');
      if (cssText) {
        pieces.push(cssText);
      }
    } catch {
      const owner = sheet.ownerNode as HTMLStyleElement | HTMLLinkElement | null;
      if (owner instanceof HTMLStyleElement && owner.textContent) {
        pieces.push(owner.textContent);
      }
    }
  }

  return pieces.join('\n');
}

function buildBundledPreviewHtml(): string {
  if (typeof document === 'undefined') {
    return '';
  }

  const domHtml = document.documentElement.outerHTML;
  const cssText = collectCurrentPageCss();
  const runtimeMeta = `<meta name="graph-agent-preview-bundled" content="true" />`;
  const inlineStyle = cssText ? `<style id="graph-agent-bundled-css">${escapeHtml(cssText)}</style>` : '';

  if (domHtml.includes('</head>')) {
    return domHtml.replace('</head>', `${runtimeMeta}${inlineStyle}</head>`);
  }

  return domHtml;
}

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

function isTraceEventLike(value: unknown): value is TraceEvent {
  if (!value || typeof value !== 'object') {
    return false;
  }

  const record = value as Record<string, unknown>;
  return typeof record.type === 'string' && typeof record.event_id === 'string' && typeof record.job_id === 'string' && 'payload' in record;
}

function isLegacyTraceEventLike(value: unknown): value is { event_type: string; data?: Record<string, unknown>; timestamp?: string | number | null } {
  if (!value || typeof value !== 'object') {
    return false;
  }

  const record = value as Record<string, unknown>;
  return typeof record.event_type === 'string' && ('timestamp' in record || 'data' in record);
}

function toTraceEvent(value: unknown): TraceEvent | null {
  if (isTraceEventLike(value)) {
    const record = value as TraceEvent;
    return {
      ...record,
      timestamp: typeof record.timestamp === 'string' ? record.timestamp : String(record.timestamp),
      step_id: record.step_id ?? null,
      agent_id: record.agent_id ?? null,
    };
  }

  if (!isLegacyTraceEventLike(value)) {
    return null;
  }

  const legacy = value;
  const payload = (legacy.data && typeof legacy.data === 'object' ? legacy.data : {}) as Record<string, unknown>;
  const type = String(legacy.event_type);
  const timestamp = legacy.timestamp == null ? new Date().toISOString() : (typeof legacy.timestamp === 'string' ? legacy.timestamp : String(legacy.timestamp));

  return {
    event_id: `legacy_${type}_${timestamp}`,
    job_id: String(payload.job_id ?? 'unknown_job'),
    step_id: typeof payload.step_id === 'string' ? payload.step_id : null,
    agent_id: typeof payload.agent_id === 'string' ? payload.agent_id : null,
    type: type as TraceEvent['type'],
    timestamp,
    payload: payload as never,
  } as TraceEvent;
}

function normalizeTraceEvents(value: unknown): TraceEvent[] {
  if (!Array.isArray(value)) {
    return [];
  }

  const normalized: TraceEvent[] = [];
  for (const item of value) {
    const normalizedItem = toTraceEvent(item);
    if (normalizedItem) {
      normalized.push(normalizedItem);
    }
  }

  return normalized;
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

function getBootDomElementSnapshot(): string {
  if (typeof document === 'undefined') {
    return '';
  }

  const bootEl = document.getElementById('graph-agent-boot');
  if (!bootEl) {
    return '';
  }

  return bootEl.outerHTML;
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
    workspaceRoot: boot.workspaceRoot ?? '',
    workspaceName: boot.workspaceName ?? 'workspace',
    sessions: (boot.sessions ?? []) as Session[],
    currentSession: (boot.currentSession ?? bootSession ?? null) as Session | null,
    messages: (boot.messages ?? []) as Message[],
    traceEvents: normalizeTraceEvents(boot.traceEvents),
    activeJob: (boot.activeJob ?? null) as ActiveJob | null,
    status: String(boot.status ?? '准备就绪'),
    expandDetails: Boolean(boot.expandDetails ?? true),
    historyPanelOpen: Boolean(boot.historyPanelOpen ?? false),
    pendingConversations,
  };
}

function persistCurrentState(state: AppState): void {
  const snapshot = getBootDomElementSnapshot();
  setVsCodeState({
    ...cloneMaps(state),
    bootDomElement: snapshot,
  });
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(() => {
    const bootState = readBootState();
    const persistedState = Object.keys(bootState).length > 0 ? {} : readPersistedState();
    return mergeState(bootState, persistedState);
  });

  useEffect(() => {
    clearRuntimeLog();
    interceptConsoleToMessageSink((line) => {
      writeRuntimeLog(`${line}\n`);
    });

    console.log('[webview-ui] webview ui 已启动');
  }, []);

  useEffect(() => {
    persistCurrentState(state);
  }, [state]);

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const sendMessage = useCallback((content: string) => {
    const activeSession = state.currentSession;
    console.log('[webview-ui] sendMessage 调用', {
      sessionId: activeSession?.session_id ?? null,
      contentLength: content.length,
      currentStatus: state.status,
      activeJob: state.activeJob?.jobId ?? null,
    });
    if (!activeSession) {
      console.warn('[webview-ui] 当前没有 activeSession，无法发送消息');
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
        source: 'pending',
      };
      next.pendingConversations.set(activeSession.session_id, conversation);
      next.status = '已发送，等待 SSE 事件';
      console.log('[webview-ui] 已创建本地 pending conversation', {
        sessionId: activeSession.session_id,
        conversationId: conversation.conversationId,
        userMessageId: conversation.userMessage?.message_id ?? null,
      });
      return next;
    });
    console.log('[webview-ui] 向宿主发送 sendMessage', {
      sessionId: activeSession.session_id,
      contentLength: content.length,
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
      console.log('[webview-ui] 收到宿主消息', msg);
      if (!msg?.type) return;
      if (msg.type === HostToWebviewMessageType.state) {
        const stateMsg = msg as HostStateMessage;
        console.log('[webview-ui] 处理 state 消息', {
          status: stateMsg.status,
          sessionId: stateMsg.state.session?.session_id ?? null,
          messages: stateMsg.state.messages?.length ?? 0,
          traces: stateMsg.state.traceEvents?.length ?? 0,
          activeJob: stateMsg.state.activeJob?.jobId ?? null,
        });
        setState(prev => {
          const next = cloneMaps(prev);
          next.workspaceRoot = stateMsg.state.workspaceRoot || '';
          next.workspaceName = stateMsg.state.workspaceName || 'workspace';
          next.sessions = stateMsg.state.sessions ?? [];
          next.currentSession = stateMsg.state.session ?? null;
          next.messages = stateMsg.state.messages ?? [];
          next.traceEvents = normalizeTraceEvents(stateMsg.state.traceEvents);
          next.activeJob = stateMsg.state.activeJob ?? null;
          next.status = stateMsg.status || '准备就绪';
          return next;
        });
        return;
      }
      if (msg.type === HostToWebviewMessageType.jobEvent) {
        const jobEvent = msg as Extract<HostToWebviewMessage, { type: 'jobEvent' }>;
        console.log('[webview-ui] 处理 jobEvent', {
          sessionId: jobEvent.sessionId,
          jobId: jobEvent.jobId,
          eventType: jobEvent.eventType,
          payloadKeys: Object.keys(jobEvent.payload ?? {}),
        });
        setState(prev => {
          const next = cloneMaps(prev);
          const pending = next.pendingConversations.get(jobEvent.sessionId);
          console.log('[webview-ui] 当前 pending 状态', {
            hasPending: Boolean(pending),
            pendingJobId: pending?.jobId ?? null,
            pendingStatus: pending?.status ?? null,
          });
          if (!pending || (pending.jobId && pending.jobId !== jobEvent.jobId)) return next;
          pending.jobId = jobEvent.jobId;
          pending.events = [
            ...pending.events,
            {
              event_id: `pending_${jobEvent.eventId ?? jobEvent.eventType}_${Date.now()}`,
              job_id: jobEvent.jobId ?? pending.jobId ?? 'unknown_job',
              step_id: jobEvent.payload?.step_id ?? null,
              agent_id: jobEvent.payload?.agent_id ?? null,
              type: jobEvent.eventType as TraceEvent['type'],
              timestamp: new Date().toISOString(),
              payload: (jobEvent.payload ?? {}) as never,
            } as TraceEvent,
          ];
          if (jobEvent.eventType === 'job_completed' || jobEvent.eventType === 'job_failed' || jobEvent.eventType === 'job_cancelled') {
            pending.status = jobEvent.eventType === 'job_completed' ? 'done' : 'error';
            pending.pending = false;
            console.log('[webview-ui] job 结束，pending 已标记完成', {
              sessionId: jobEvent.sessionId,
              jobId: jobEvent.jobId,
              status: pending.status,
            });
          }
          return next;
        });
        return;
      }
      if (msg.type === HostToWebviewMessageType.messageAccepted) {
        const accepted = msg as Extract<HostToWebviewMessage, { type: 'messageAccepted' }>;
        console.log('[webview-ui] 处理 messageAccepted', accepted);
        setState(prev => {
          const next = cloneMaps(prev);
          const pending = next.pendingConversations.get(accepted.sessionId) ?? {
            conversationId: `local_${Date.now()}`,
            sessionId: accepted.sessionId,
            userMessage: next.messages.filter(m => m.session_id === accepted.sessionId && m.role === 'user').slice(-1)[0] ?? null,
            assistantMessages: [],
            events: [],
            status: 'running',
            jobId: null,
            pending: true,
            source: 'pending',
          };
          pending.jobId = accepted.jobId;
          pending.pending = true;
          next.pendingConversations.set(accepted.sessionId, pending);
          next.activeJob = { jobId: accepted.jobId, sessionId: accepted.sessionId, status: 'running', messageId: accepted.messageId, content: accepted.content };
          next.status = '任务已接收';
          console.log('[webview-ui] activeJob 已更新', next.activeJob);
          return next;
        });
      }
      if (msg.type === HostToWebviewMessageType.sessionCreated) {
        const sessionCreated = msg as Extract<HostToWebviewMessage, { type: 'sessionCreated' }>;
        console.log('[webview-ui] 处理 sessionCreated', sessionCreated.session.session_id);
        setState(prev => ({ ...prev, currentSession: sessionCreated.session }));
      }
      if (msg.type === HostToWebviewMessageType.error) {
        const errorMsg = msg as Extract<HostToWebviewMessage, { type: 'error' }>;
        console.error('[webview-ui] 收到宿主错误', errorMsg.message);
        setStatus(errorMsg.message);
      }
    };
    window.addEventListener('message', handler);
    return () => window.removeEventListener('message', handler);
  }, [setStatus]);

  useEffect(() => {
    console.log('[webview-ui] 向宿主发送 ready');
    postMessage({ type: WebviewToHostMessageType.ready });
  }, []);

  useEffect(() => {
    setVsCodeState({
      workspaceRoot: state.workspaceRoot,
      workspaceName: state.workspaceName,
      sessions: state.sessions,
      currentSession: state.currentSession,
      messages: state.messages,
      traceEvents: normalizeTraceEvents(state.traceEvents),
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
