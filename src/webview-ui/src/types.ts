// 全局应用状态
export interface Message {
  message_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  metadata: Record<string, unknown>;
  attachments: unknown[];
  created_at: string | null;
}

export interface TraceEvent {
  event_type: string;
  data: Record<string, unknown>;
  timestamp: string | null;
}

export interface Session {
  session_id: string;
  title: string;
  status: string;
  agent_id: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface PendingTurn {
  turnId: string;
  sessionId: string;
  userMessage: Message | null;
  assistantMessages: Message[];
  events: TraceEvent[];
  status: 'running' | 'done' | 'error';
  jobId: string | null;
  pending: boolean;
}

export interface ActiveJob {
  jobId: string | null;
  sessionId: string | null;
  status: string;
  messageId: string | null;
  content: string;
}

export interface AppState {
  workspaceRoot: string;
  workspaceName: string;
  sessions: Session[];
  currentSession: Session | null;
  messages: Message[];
  traceEvents: TraceEvent[];
  activeJob: ActiveJob | null;
  pendingTurns: Map<string, PendingTurn>;
  status: string;
  expandDetails: boolean;
  historyPanelOpen: boolean;
}

// 通信协议
export type HostToWebviewMessage =
  | { type: 'init' }
  | HostStateMessage
  | { type: 'error'; message: string }
  | { type: 'sessionCreated'; session: Session }
  | { type: 'messageAccepted'; sessionId: string; jobId: string | null; messageId: string | null; content: string }
  | { type: 'jobEvent'; sessionId: string; jobId: string; eventType: string; payload: Record<string, unknown> };

export interface HostStatePayload {
  workspaceRoot: string;
  workspaceName: string;
  sessions: Session[];
  session: Session | null;
  messages: Message[];
  traceEvents: TraceEvent[];
  activeJob: ActiveJob | null;
}

export interface HostStateMessage {
  type: 'state';
  status: string;
  state: HostStatePayload;
}

export type WebviewToHostMessage =
  | { type: 'ready' }
  | { type: 'refresh' }
  | { type: 'createSession'; title?: string }
  | { type: 'selectSession'; sessionId: string }
  | { type: 'sendMessage'; content: string }
  | { type: 'error'; message: string }
  | { type: 'updateSession'; sessionId: string; data: Record<string, unknown> };
