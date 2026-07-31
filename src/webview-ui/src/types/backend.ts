// 后端类型
import type {
  SessionExecutionEventDTO,
  SessionExecutionSseDTO,
} from '../../../web/src/types/gen/session_interaction';
import type { TraceEventDTO } from '../../../web/src/types/gen/trace';

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
  workspace_id: string;
  title: string;
  title_source: 'default' | 'user' | 'auto';
  current_agent_id: string;
  parent_session_id: string | null;
  status?: string;
  agent_id?: string;
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

export type SessionGoalStatus =
  | 'active'
  | 'paused'
  | 'blocked'
  | 'usage_limited'
  | 'budget_limited'
  | 'complete';

export interface SessionGoal {
  goal_id: string;
  session_id: string;
  objective: string;
  status: SessionGoalStatus;
  token_budget: number | null;
  tokens_used: number;
  time_used_seconds: number;
  created_at: string;
  updated_at: string;
}

export interface SessionGoalUpdateRequest {
  objective?: string;
  status?: SessionGoalStatus;
  token_budget?: number | null;
  replace?: boolean;
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

type TraceRaw = NonNullable<TraceEventDTO['raw']> & {
  payload?: Record<string, unknown>;
  agent_id?: string | null;
};

/** UI 时间线使用的 Trace 视图；协议字段均派生自生成 DTO。 */
export interface TraceEvent
  extends Omit<TraceEventDTO, 'session_id' | 'phase' | 'title' | 'content' | 'raw'> {
  session_id?: string;
  phase?: TraceEventDTO['phase'];
  title?: string;
  content?: string;
  agent_id: string | null;
  payload: Record<string, unknown>;
  raw?: TraceRaw;
}

export type ObservationEventType = SessionExecutionEventDTO['type'];
export type ObservationEvent = SessionExecutionEventDTO;
export type ObservationSseMessage = SessionExecutionSseDTO;
