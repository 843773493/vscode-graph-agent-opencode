// 前端内部类型
import type { Message, Session, TraceEvent } from './backend';

export interface ConversationView {
  conversationId: string;
  sessionId: string;
  userMessage: Message | null;
  assistantMessages: Message[];
  events: TraceEvent[];
  status: 'running' | 'done' | 'error';
  jobId: string | null;
  pending: boolean;
  source: 'messages' | 'pending';
  streamingText?: string;
  streamingTextActive?: boolean;
}

export interface AppState {
  apiPort: number | null;
  workspaceRoot: string | null;
  workspaceName: string | null;
  sessions: Session[];
  currentSession: Session | null;
  messages: Message[];
  traceEvents: TraceEvent[];
  pendingConversations: Map<string, ConversationView>;
  status: string;
  error: string | null;
  isBootstrapping: boolean;
  expandDetails: boolean;
  historyPanelOpen: boolean;
}
