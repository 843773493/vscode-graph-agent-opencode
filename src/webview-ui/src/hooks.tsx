import type React from 'react';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { createSession as apiCreateSession, sendMessage as apiSendMessage, DEFAULT_AGENT_ID, DEFAULT_SESSION_TITLE, getSessionTraces, getWorkspace, listMessages, listSessions, streamJobEvents } from './api';
import type { ActiveJob, Message, Session, TraceEvent } from './types/backend';
import type { AppState, ConversationView } from './types/frontend';
import { clearRuntimeLog, getVsCodeState, interceptConsoleToMessageSink, setVsCodeState, writeRuntimeLog } from './vscode';

type StreamEvent = {
  eventType: string;
  payload: Record<string, unknown>;
};

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

function attachObservationEvents(conversations: ConversationView[], observationEvents: unknown): ConversationView[] {
  if (!Array.isArray(observationEvents) || conversations.length === 0) {
    return conversations;
  }

  return conversations.map((conversation) => ({
    ...conversation,
    observationEvents: conversation.observationEvents ?? [],
  }));
}

function attachTraceEvents(conversations: ConversationView[], traceEvents: TraceEvent[]): ConversationView[] {
  if (!Array.isArray(traceEvents) || conversations.length === 0) {
    return conversations;
  }

  return conversations.map((conversation) => ({
    ...conversation,
    events: traceEvents.filter((event) => {
      const payload = event.payload as Record<string, unknown>;
      const eventSessionId = typeof payload.session_id === 'string' ? payload.session_id : null;
      const eventJobId = typeof event.job_id === 'string' ? event.job_id : null;

      if (eventSessionId && eventSessionId === conversation.sessionId) {
        return true;
      }

      if (eventJobId && conversation.jobId && eventJobId === conversation.jobId) {
        return true;
      }

      return false;
    }),
  }));
}

export function getConversationsForSession(sessionId: string, state: AppState): ConversationView[] {
  const conversations = groupMessagesIntoConversations(state.messages.filter(m => m.session_id === sessionId));
  const pending = state.pendingConversations.get(sessionId);
  const withObservationEvents = attachObservationEvents(conversations, (state as { observationEvents?: unknown }).observationEvents);
  const withTraceEvents = attachTraceEvents(withObservationEvents, normalizeTraceEventList(state.traceEvents).filter(event => {
    const payload = event.payload as Record<string, unknown>;
    return typeof payload.session_id === 'string' ? payload.session_id === sessionId : true;
  }));

  if (!pending) {
    return withTraceEvents;
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

  if (matchedConversationIndex === -1) {
    return pending.userMessage ? [...withTraceEvents, { ...pending, source: 'pending' }] : withTraceEvents;
  }

  const merged = {
    ...withTraceEvents[matchedConversationIndex],
    ...pending,
    source: withTraceEvents[matchedConversationIndex].source,
  };
  return withTraceEvents.map((conversation, index) => (index === matchedConversationIndex ? merged : conversation));
}

const INITIAL_STATE: AppState = {
  apiPort: null,
  workspaceRoot: '',
  workspaceName: 'workspace',
  sessions: [],
  currentSession: null,
  messages: [],
  traceEvents: [],
  activeJob: null,
  observationState: null,
  sessionStatus: null,
  pendingQuestions: [],
  pendingPermissions: [],
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

function persistCurrentState(state: AppState): void {
  const snapshot = typeof document === 'undefined' ? '' : document.getElementById('graph-agent-boot')?.outerHTML ?? '';
  setVsCodeState({
    ...cloneMaps(state),
    bootDomElement: snapshot,
  });
}

function readBootState(): Partial<AppState> {
  if (typeof document === 'undefined') {
    return {};
  }

  const bootEl = document.getElementById('graph-agent-boot');
  if (!bootEl?.textContent) {
    return {};
  }

  try {
    return JSON.parse(bootEl.textContent) as Partial<AppState>;
  } catch (error) {
    throw new Error(`读取 webview boot 数据失败: ${(error as Error).message}`);
  }
}

function readPersistedState(): Partial<AppState> {
  return getVsCodeState<Partial<AppState>>() ?? {};
}

function normalizeSessionList(value: unknown): Session[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.filter((item): item is Session => Boolean(item) && typeof item === 'object' && 'session_id' in item && 'title' in item && 'status' in item && 'agent_id' in item) as Session[];
}

function normalizeMessageList(value: unknown): Message[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.filter((item): item is Message => Boolean(item) && typeof item === 'object' && 'message_id' in item && 'session_id' in item && 'role' in item && 'content' in item) as Message[];
}

function normalizeTraceEventList(value: unknown): TraceEvent[] {
  if (!Array.isArray(value)) {
    return [];
  }

  return value.filter((item): item is TraceEvent => Boolean(item) && typeof item === 'object' && 'event_id' in item && 'job_id' in item && 'payload' in item) as TraceEvent[];
}

function mergeState(boot: Partial<AppState>, persisted: Partial<AppState>): AppState {
  const pendingConversations = new Map<string, ConversationView>();
  const bootPendingConversations = (boot as { pendingConversations?: ConversationView[] }).pendingConversations ?? (persisted as { pendingConversations?: ConversationView[] }).pendingConversations ?? [];
  const bootSession = (boot as { session?: Session | null }).session ?? null;
  const persistedSession = (persisted as { session?: Session | null }).session ?? null;
  const bootObservationState = (boot as { observationState?: AppState['observationState'] }).observationState ?? (persisted as { observationState?: AppState['observationState'] }).observationState ?? null;
  const bootSessionStatus = (boot as { sessionStatus?: AppState['sessionStatus'] }).sessionStatus ?? (persisted as { sessionStatus?: AppState['sessionStatus'] }).sessionStatus ?? null;
  const bootPendingQuestions = (boot as { pendingQuestions?: AppState['pendingQuestions'] }).pendingQuestions ?? (persisted as { pendingQuestions?: AppState['pendingQuestions'] }).pendingQuestions ?? [];
  const bootPendingPermissions = (boot as { pendingPermissions?: AppState['pendingPermissions'] }).pendingPermissions ?? (persisted as { pendingPermissions?: AppState['pendingPermissions'] }).pendingPermissions ?? [];
  bootPendingConversations.forEach(conversation => pendingConversations.set(conversation.sessionId, conversation));
  return {
    ...INITIAL_STATE,
    apiPort: boot.apiPort ?? persisted.apiPort ?? null,
    workspaceRoot: boot.workspaceRoot ?? '',
    workspaceName: boot.workspaceName ?? 'workspace',
    sessions: normalizeSessionList(boot.sessions ?? []),
    currentSession: (boot.currentSession ?? bootSession ?? null) as Session | null,
    messages: normalizeMessageList(boot.messages ?? []),
    traceEvents: normalizeTraceEventList(boot.traceEvents),
    activeJob: (boot.activeJob ?? null) as ActiveJob | null,
    observationState: bootObservationState,
    sessionStatus: bootSessionStatus,
    pendingQuestions: bootPendingQuestions,
    pendingPermissions: bootPendingPermissions,
    status: String(boot.status ?? '准备就绪'),
    expandDetails: Boolean(boot.expandDetails ?? true),
    historyPanelOpen: Boolean(boot.historyPanelOpen ?? false),
    pendingConversations,
  };
}

async function requestInitialState(port: number): Promise<Partial<AppState>> {
  const workspace = await getWorkspace(port);
  const sessionsPage = await listSessions(port);
  const sessions = normalizeSessionList(sessionsPage.items ?? []);
  const currentSession = sessions[0] ?? null;
  const messagesPage = currentSession ? await listMessages(port, currentSession.session_id) : { items: [] as Message[] };
  const traces = currentSession ? await getSessionTraces(port, currentSession.session_id) : [] as TraceEvent[];

  return {
    apiPort: port,
    workspaceRoot: workspace?.root_path ?? '',
    workspaceName: workspace?.name ?? 'workspace',
    sessions,
    currentSession,
    messages: messagesPage.items ?? [],
    traceEvents: traces,
    activeJob: null,
    status: '准备就绪',
    expandDetails: true,
    historyPanelOpen: false,
  };
}

export function AppProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AppState>(() => {
    const bootState = readBootState();
    const persistedState = Object.keys(bootState).length > 0 ? {} : readPersistedState();
    return mergeState(bootState, persistedState);
  });
  const streamAbortRef = useRef<AbortController | null>(null);
  const activeSessionIdRef = useRef<string | null>(null);

  const refreshFromBackend = useCallback(async () => {
    if (state.apiPort == null) {
      throw new Error('后端端口未初始化');
    }

    const workspace = await getWorkspace(state.apiPort);
    const sessionsPage = await listSessions(state.apiPort);
    const sessions = normalizeSessionList(sessionsPage.items ?? []);
    const currentSession = state.currentSession ? sessions.find(session => session.session_id === state.currentSession?.session_id) ?? sessions[0] ?? null : sessions[0] ?? null;
    const messagesPage = currentSession ? await listMessages(state.apiPort, currentSession.session_id) : { items: [] as Message[] };
    const traces = currentSession ? await getSessionTraces(state.apiPort, currentSession.session_id) : [] as TraceEvent[];

    setState(prev => {
      const next = cloneMaps(prev);
      next.workspaceRoot = workspace?.root_path ?? '';
      next.workspaceName = workspace?.name ?? 'workspace';
      next.sessions = sessions;
      next.currentSession = currentSession;
      next.messages = normalizeMessageList(messagesPage.items ?? []);
      next.traceEvents = normalizeTraceEventList(traces);
      next.status = '已刷新消息';
      return next;
    });
  }, [state.apiPort, state.currentSession]);

  useEffect(() => {
    clearRuntimeLog();
    interceptConsoleToMessageSink((line) => {
      writeRuntimeLog(`${line}\n`);
    });
  }, []);

  useEffect(() => {
    persistCurrentState(state);
  }, [state]);

  useEffect(() => {
    if (state.apiPort == null) {
      return;
    }

    let cancelled = false;
    void (async () => {
      const initial = await requestInitialState(state.apiPort as number);
      if (cancelled) {
        return;
      }

      setState(prev => {
        const next = cloneMaps(prev);
        next.apiPort = initial.apiPort ?? prev.apiPort;
        next.workspaceRoot = initial.workspaceRoot ?? prev.workspaceRoot;
        next.workspaceName = initial.workspaceName ?? prev.workspaceName;
        next.sessions = normalizeSessionList(initial.sessions ?? prev.sessions);
        next.currentSession = (initial.currentSession ?? prev.currentSession) as Session | null;
        next.messages = normalizeMessageList(initial.messages ?? prev.messages);
        next.traceEvents = normalizeTraceEventList(initial.traceEvents ?? prev.traceEvents);
        next.activeJob = (initial.activeJob ?? prev.activeJob) as ActiveJob | null;
        next.status = String(initial.status ?? prev.status);
        next.expandDetails = Boolean(initial.expandDetails ?? prev.expandDetails);
        next.historyPanelOpen = Boolean(initial.historyPanelOpen ?? prev.historyPanelOpen);
        return next;
      });
    })().catch((error) => {
      setState(prev => ({ ...prev, status: error instanceof Error ? error.message : String(error) }));
    });

    return () => {
      cancelled = true;
    };
  }, [state.apiPort]);

  useEffect(() => {
    const currentSessionId = state.currentSession?.session_id ?? null;
    activeSessionIdRef.current = currentSessionId;
    const jobId = state.activeJob?.jobId ?? null;
    if (!state.apiPort || !currentSessionId || !jobId) {
      return;
    }

    streamAbortRef.current?.abort();
    const controller = new AbortController();
    streamAbortRef.current = controller;

    void streamJobEvents(state.apiPort, jobId, {
      signal: controller.signal,
      onEvent: ({ eventType, payload }: StreamEvent) => {
        if (!activeSessionIdRef.current) {
          return;
        }

        setState(prev => {
          const next = cloneMaps(prev);
          const sessionId = activeSessionIdRef.current ?? '';
          const pending = next.pendingConversations.get(sessionId);
          if (pending) {
            pending.events = [
              ...pending.events,
              {
                event_id: `pending_${eventType}_${Date.now()}`,
                job_id: next.activeJob?.jobId ?? pending.jobId ?? 'unknown_job',
                step_id: typeof payload.step_id === 'string' ? payload.step_id : null,
                agent_id: typeof payload.agent_id === 'string' ? payload.agent_id : null,
                type: eventType as TraceEvent['type'],
                timestamp: new Date().toISOString(),
                payload: (payload ?? {}) as never,
              } as TraceEvent,
            ];
            if (['job_completed', 'job_failed', 'job_cancelled'].includes(eventType)) {
              pending.status = eventType === 'job_completed' ? 'done' : 'error';
              pending.pending = false;
              next.activeJob = null;
              void refreshFromBackend();
            }
          }
          return next;
        });
      },
      onError: (error: unknown) => {
        setState(prev => ({ ...prev, status: error instanceof Error ? error.message : String(error) }));
      },
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) {
        setState(prev => ({ ...prev, status: error instanceof Error ? error.message : String(error) }));
      }
    });

    return () => {
      controller.abort();
    };
  }, [refreshFromBackend, state.activeJob?.jobId, state.apiPort, state.currentSession?.session_id]);

  const setStatus = useCallback((text: string) => {
    setState(prev => ({ ...prev, status: text }));
  }, []);

  const sendMessage = useCallback(async (content: string) => {
    if (!content.trim()) {
      return;
    }
    if (state.apiPort == null) {
      throw new Error('后端端口未初始化');
    }

    const activeSession: Session = state.currentSession ?? (await apiCreateSession(state.apiPort, DEFAULT_SESSION_TITLE));
    if (!state.currentSession) {
      setState(prev => ({ ...prev, currentSession: activeSession, sessions: [activeSession, ...prev.sessions] }));
    }

    const defaultAgent = state.currentSession?.agent_id || DEFAULT_AGENT_ID;
    const payload = {
      message: { role: 'user', content, attachments: [], metadata: {} },
      run: {
        mode: 'single_agent',
        agent_id: defaultAgent,
        response_mode: 'stream',
        async: true,
        max_steps: 20,
        timeout_seconds: 600,
        context: {
          workspace_root: state.workspaceRoot,
          workspace_name: state.workspaceName,
        },
      },
    };

    setState(prev => {
      const next = cloneMaps(prev);
      const sessionId = activeSession.session_id;
      const conversation: ConversationView = {
        conversationId: `local_${Date.now()}`,
        sessionId,
        userMessage: {
          message_id: `local_user_${Date.now()}`,
          session_id: sessionId,
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
      next.pendingConversations.set(sessionId, conversation);
      next.status = '已发送，等待 SSE 事件';
      return next;
    });

    const accepted = await apiSendMessage(state.apiPort, activeSession.session_id, payload);
    const jobId = accepted?.job_id;
    if (!jobId) {
      throw new Error('后端未返回 job_id');
    }

    setState(prev => ({
      ...prev,
      activeJob: {
        jobId,
        sessionId: activeSession.session_id,
        status: 'running',
        messageId: accepted.message_id ?? null,
        content,
      },
    }));
  }, [state.apiPort, state.currentSession, state.workspaceName, state.workspaceRoot]);

  const selectSession = useCallback(async (sessionId: string) => {
    if (!state.apiPort) {
      throw new Error('后端端口未初始化');
    }

    const selected = state.sessions.find((session: Session) => session.session_id === sessionId) ?? null;
    if (!selected) {
      throw new Error(`未找到 session: ${sessionId}`);
    }

    const messagesPage = await listMessages(state.apiPort, sessionId);
    const traces = await getSessionTraces(state.apiPort, sessionId);

    setState(prev => {
      const next = cloneMaps(prev);
      next.currentSession = selected;
      next.messages = normalizeMessageList(messagesPage.items ?? []);
      next.traceEvents = normalizeTraceEventList(traces);
      next.activeJob = null;
      next.status = '已切换 session';
      return next;
    });
  }, [state.apiPort, state.sessions]);

  const createSession = useCallback(async (title: string = DEFAULT_SESSION_TITLE) => {
    if (!state.apiPort) {
      throw new Error('后端端口未初始化');
    }

    const session: Session = await apiCreateSession(state.apiPort, title);
    setState(prev => ({
      ...prev,
      currentSession: session,
      sessions: [session, ...prev.sessions.filter(item => item.session_id !== session.session_id)],
      messages: [],
      traceEvents: [],
      activeJob: null,
      status: '已创建新 session',
    }));
  }, [state.apiPort]);

  const toggleHistoryPanel = useCallback(() => setState(prev => ({ ...prev, historyPanelOpen: !prev.historyPanelOpen })), []);
  const toggleExpandDetails = useCallback((expand: boolean) => setState(prev => ({ ...prev, expandDetails: expand })), []);

  const value = useMemo<AppContextType>(() => ({
    state,
    setStatus,
    sendMessage,
    selectSession,
    createSession,
    toggleHistoryPanel,
    toggleExpandDetails,
  }), [state, setStatus, sendMessage, selectSession, createSession, toggleHistoryPanel, toggleExpandDetails]);

  useEffect(() => {
    if (state.apiPort == null) {
      return;
    }

    setVsCodeState({
      ...cloneMaps(state),
      apiPort: state.apiPort,
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

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>;
}
