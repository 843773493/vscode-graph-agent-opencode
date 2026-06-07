// 前端内部类型
import type { ActiveJob, Message, ObservationSseMessage, PermissionRequest, QuestionRequest, Session, SessionObservationState, SessionStatusInfo, TraceEvent } from './backend';

export interface ConversationView {
  conversationId: string;
  sessionId: string;
  userMessage: Message | null;
  assistantMessages: Message[];
  events: TraceEvent[];
  observationEvents?: ObservationSseMessage[];
  status: 'running' | 'done' | 'error';
  jobId: string | null;
  pending: boolean;
  source: 'messages' | 'pending';
  sessionStatus?: SessionStatusInfo | null;
  observationState?: SessionObservationState | null;
  pendingQuestions?: QuestionRequest[];
  pendingPermissions?: PermissionRequest[];
}

export interface AppState {
  apiPort: number | null;
  workspaceRoot: string;
  workspaceName: string;
  sessions: Session[];
  currentSession: Session | null;
  messages: Message[];
  traceEvents: TraceEvent[];
  activeJob: ActiveJob | null;
  observationState: SessionObservationState | null;
  sessionStatus: SessionStatusInfo | null;
  pendingQuestions: QuestionRequest[];
  pendingPermissions: PermissionRequest[];
  pendingConversations: Map<string, ConversationView>;
  status: string;
  expandDetails: boolean;
  historyPanelOpen: boolean;
}
