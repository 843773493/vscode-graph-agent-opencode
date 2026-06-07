// webview 和 extension host 之间的数据类型
import type { ActiveJob, Message, ObservationSseMessage, PermissionRequest, QuestionRequest, Session, SessionObservationState, SessionStatusInfo, TraceEvent } from './backend';

export interface HostStatePayload {
  apiPort: number | null;
  workspaceRoot: string;
  workspaceName: string;
  sessions: Session[];
  session: Session | null;
  messages: Message[];
  traceEvents: TraceEvent[];
  activeJob: ActiveJob | null;
  observationEvents?: Array<ObservationSseMessage>;
  observationState?: SessionObservationState | null;
  sessionStatus?: SessionStatusInfo | null;
  pendingQuestions?: QuestionRequest[];
  pendingPermissions?: PermissionRequest[];
}

export interface HostStateMessage {
  type: 'state';
  status: string;
  state: HostStatePayload;
}
