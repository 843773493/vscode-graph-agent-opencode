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
  | 'llm_request';

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
  response: unknown;
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
  | (BaseTraceEvent & { type: 'llm_request'; payload: LLMRequestPayload });
