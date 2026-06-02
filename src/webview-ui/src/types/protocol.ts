// webview 和 extension host 之间的数据类型
import type { ActiveJob, Message, Session, TraceEvent } from './backend';

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

export type HostToWebviewMessage =
  | { type: 'init' }
  | HostStateMessage
  | { type: 'error'; message: string }
  | { type: 'sessionCreated'; session: Session }
  | { type: 'messageAccepted'; sessionId: string; jobId: string | null; messageId: string | null; content: string }
  | { type: 'jobEvent'; sessionId: string; jobId: string; eventType: string; payload: Record<string, unknown> };

export type WebviewToHostMessage =
  | { type: 'ready' }
  | { type: 'refresh' }
  | { type: 'writeWebviewPreview'; content: string }
  | { type: 'writeRuntimeWebviewUiLog'; content: string }
  | { type: 'createSession'; title?: string }
  | { type: 'selectSession'; sessionId: string }
  | { type: 'sendMessage'; content: string }
  | { type: 'error'; message: string }
  | { type: 'updateSession'; sessionId: string; data: Record<string, unknown> };
