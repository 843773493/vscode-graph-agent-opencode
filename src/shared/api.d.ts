import type { SessionExecutionEvent } from '../clients/web/src/types/protocol_buf_generated/boxteam/workspace/v2/session_interaction_pb';
import type { TraceEventDTO } from '../clients/web/src/protocol/jsonTypes';

type SessionExecutionEventDTO = SessionExecutionEvent;

export interface WorkspaceInfo {
  root_path: string;
  name: string;
}

export interface PageResult<T> {
  items?: T[];
  [key: string]: unknown;
}

export interface SessionAcceptResult {
  job_id?: string | null;
  message_id?: string | null;
  [key: string]: unknown;
}

export interface JobResult {
  job_id: string;
  message_id: string;
  session_id: string;
  status: string;
  error_message?: string | null;
  [key: string]: unknown;
}

export interface StreamEvent {
  eventType: TraceEventDTO['type'];
  eventId: string;
  payload: Record<string, unknown>;
  event: TraceEventDTO;
}

export interface JobStreamEvent {
  eventType: SessionExecutionEventDTO['type'];
  eventId: string;
  payload: SessionExecutionEventDTO['payload'];
  event: SessionExecutionEventDTO;
  rawType: string;
  rawPayload: Record<string, unknown>;
}

export interface StreamOptions<TEvent> {
  afterEventId?: string | null;
  onEvent?: (event: TEvent) => void;
  onError?: (error: unknown) => void;
  signal?: AbortSignal;
}

export class TraceCursorGoneError extends Error {
  readonly eventId: string;
  readonly status: 410;
}

export class JobEventCursorGoneError extends Error {
  readonly eventId: string;
  readonly status: 410;
}

export function getWorkspace(port: number): Promise<WorkspaceInfo>;
export function listAgents(port: number): Promise<unknown[]>;
export function listSessions(port: number): Promise<PageResult<unknown>>;
export function createSession(port: number, title?: string): Promise<unknown>;
export function updateSession(port: number, sessionId: string, payload: unknown): Promise<unknown>;
export function getSession(port: number, sessionId: string): Promise<unknown>;
export function getSessionGoal(port: number, sessionId: string): Promise<unknown>;
export function updateSessionGoal(port: number, sessionId: string, payload: unknown): Promise<unknown>;
export function clearSessionGoal(port: number, sessionId: string): Promise<unknown>;
export function moveSessionParent(port: number, sessionId: string, parentNodeId: string | null): Promise<unknown>;
export function listMessages(port: number, sessionId: string): Promise<PageResult<unknown>>;
export function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult>;
export function getJob(port: number, jobId: string): Promise<JobResult>;
export function getSessionTraces(
  port: number,
  sessionId: string,
  afterEventId?: string | null,
): Promise<TraceEventDTO[]>;
export function streamSessionEvents(
  port: number,
  sessionId: string,
  options?: StreamOptions<StreamEvent>,
): Promise<void>;
export function streamJobEvents(
  port: number,
  jobId: string,
  options?: StreamOptions<JobStreamEvent>,
): Promise<void>;
