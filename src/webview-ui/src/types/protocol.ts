// webview 和 extension host 之间的数据类型
import type { ActiveJob, Message, Session, TraceEvent } from './backend';

export interface HostStatePayload {
  apiPort: number | null;
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
