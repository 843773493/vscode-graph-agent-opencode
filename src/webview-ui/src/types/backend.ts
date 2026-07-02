// 后端类型
export interface Message {
  message_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  metadata: Record<string, unknown>;
  attachments: unknown[];
  created_at: string | null;
}

export interface Session {
  session_id: string;
  title: string;
  status: string;
  agent_id: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ActiveJob {
  jobId: string;
  sessionId: string;
  status: 'running' | 'done' | 'error' | 'job_completed' | 'job_failed' | 'job_cancelled' | string;
  messageId: string | null;
  content: string;
}

export interface QuestionOption {
  label: string;
  description: string;
  labelKey?: string;
  descriptionKey?: string;
  mode?: string;
}

export interface QuestionInfo {
  question: string;
  header: string;
  options: QuestionOption[];
  multiple?: boolean;
  questionKey?: string;
  headerKey?: string;
  custom?: boolean;
}

export interface QuestionRequest {
  id: string;
  sessionId: string;
  questions: QuestionInfo[];
  blocking?: boolean;
  tool?: {
    messageId: string;
    callId: string;
  };
}

export interface PermissionRequest {
  id: string;
  sessionId: string;
  permission: string;
  patterns: string[];
  metadata: Record<string, unknown>;
  always: string[];
  tool?: {
    messageId: string;
    callId: string;
  };
}

export type SessionStatus = 'idle' | 'busy' | 'question' | 'permission' | 'retry' | 'offline';

export interface SessionNetworkWait {
  id: string;
  sessionId: string;
  message: string;
  restored: boolean;
  time: {
    created: number;
    restored?: number;
  };
}

export interface SessionStatusInfo {
  sessionId: string;
  status: SessionStatus;
  message?: string;
  activeJobId?: string;
  waiting?: SessionNetworkWait;
}

export interface SessionObservationState {
  sessionId: string;
  activeJobId?: string | null;
  lastEventId?: string | null;
  isStreaming: boolean;
  isIdle: boolean;
  error?: string | null;
}

export type TraceEventType =
  | 'message_created'
  | 'job_created'
  | 'job_started'
  | 'job_completed'
  | 'job_cancelled'
  | 'job_failed'
  | 'status_change'
  | 'agent_start'
  | 'agent_step'
  | 'agent_end'
  | 'error'
  | 'llm_request'
  | 'tool_call_start'
  | 'tool_call_end'
  | 'text_start'
  | 'text_delta'
  | 'text_end';

interface BaseTraceEvent {
  event_id: string;
  job_id: string;
  step_id: string | null;
  agent_id: string | null;
  timestamp: string;
}

interface MessageCreatedPayload {
  message_id: string;
  session_id: string;
  role: string;
  content: string;
  attachments: unknown[];
  metadata: Record<string, unknown>;
  created_at: string;
}

interface JobCreatedPayload {
  session_id: string;
  message: string;
  agent_id: string;
}

interface JobStartedPayload {}

interface JobCompletedPayload {
  result: string;
}

interface JobCancelledPayload {}

interface JobFailedPayload {
  error: string;
}

interface StatusChangePayload {
  status: string;
  reason: string;
  blocked_by_job_id: string | null;
}

interface AgentStartPayload {
  message: string | null;
  agent_id: string;
}

interface AgentStepPayload {
  phase: string | null;
}

interface AgentEndPayload {
  final_text: string;
  agent_id: string;
}

interface ErrorPayload {
  error: string;
  phase: string;
}

interface LLMRequestPayload {
  model: string;
  timestamp: number;
}

interface TextStartPayload {}

interface TextDeltaPayload {
  text: string;
}

interface TextEndPayload {
  text: string;
}

export type TraceEvent =
  | (BaseTraceEvent & { type: 'message_created'; payload: MessageCreatedPayload })
  | (BaseTraceEvent & { type: 'job_created'; payload: JobCreatedPayload })
  | (BaseTraceEvent & { type: 'job_started'; payload: JobStartedPayload })
  | (BaseTraceEvent & { type: 'job_completed'; payload: JobCompletedPayload })
  | (BaseTraceEvent & { type: 'job_cancelled'; payload: JobCancelledPayload })
  | (BaseTraceEvent & { type: 'job_failed'; payload: JobFailedPayload })
  | (BaseTraceEvent & { type: 'status_change'; payload: StatusChangePayload })
  | (BaseTraceEvent & { type: 'agent_start'; payload: AgentStartPayload })
  | (BaseTraceEvent & { type: 'agent_step'; payload: AgentStepPayload })
  | (BaseTraceEvent & { type: 'agent_end'; payload: AgentEndPayload })
  | (BaseTraceEvent & { type: 'error'; payload: ErrorPayload })
  | (BaseTraceEvent & { type: 'llm_request'; payload: LLMRequestPayload })
  | (BaseTraceEvent & { type: 'tool_call_start'; payload: Record<string, unknown> })
  | (BaseTraceEvent & { type: 'tool_call_end'; payload: Record<string, unknown> })
  | (BaseTraceEvent & { type: 'text_start'; payload: TextStartPayload })
  | (BaseTraceEvent & { type: 'text_delta'; payload: TextDeltaPayload })
  | (BaseTraceEvent & { type: 'text_end'; payload: TextEndPayload });

export type ObservationEventType =
  | 'message.updated'
  | 'message.delta'
  | 'job.updated'
  | 'job.step.updated'
  | 'job.status.changed'
  | 'session.status.changed'
  | 'session.completed'
  | 'session.error'
  | 'question.requested'
  | 'permission.requested'
  | 'network.waiting';

export interface ObservationEvent<TPayload = unknown> {
  eventId: string;
  sessionId: string;
  jobId?: string | null;
  type: ObservationEventType;
  time: string;
  payload: TPayload;
}

export interface ObservationSseMessage<TPayload = unknown> {
  event: ObservationEvent<TPayload>;
  rawType: string;
  rawPayload: Record<string, unknown>;
}
