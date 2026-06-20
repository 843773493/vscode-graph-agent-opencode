// 前端内部类型
import type { Message, Session, TraceEvent } from './backend';

export interface ConversationView {
  conversationId: string;
  sessionId: string;
  userMessage: Message | null;
  // 助手消息内容由 ChatPanel 从 traceEvents 聚合得到，不再在 hooks 中维护。
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
