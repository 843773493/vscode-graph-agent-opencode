import {
    createSession as sharedCreateSession,
    getJob as sharedGetJob,
    getSessionTraces as sharedGetSessionTraces,
    getWorkspace as sharedGetWorkspace,
    listAgents as sharedListAgents,
    listMessages as sharedListMessages,
    listSessions as sharedListSessions,
    sendMessage as sharedSendMessage,
    streamSessionEvents as sharedStreamSessionEvents,
    TraceCursorGoneError,
    updateSession as sharedUpdateSession,
} from '../../shared/api.js';
import {
    DEFAULT_AGENT_ID,
    DEFAULT_BACKEND_HOST,
    DEFAULT_BACKEND_PORT,
    DEFAULT_BACKEND_TOKEN,
    DEFAULT_SESSION_TITLE,
} from '../../shared/constants.js';
import type { ActiveJob, Message, Session, TraceEvent } from './types/backend';

export type { ActiveJob, Message, Session, TraceEvent };

export interface WorkspaceInfo {
  root_path: string;
  name: string;
}

export interface PageResult<T> {
  items: T[];
}

export interface SessionAcceptResult {
  job_id: string | null;
  message_id: string | null;
}

export interface StreamEvent<TPayload = Record<string, unknown>> {
  eventType: string;
  eventId?: string;
  payload: TPayload;
  event?: TraceEvent;
}

function normalizePageResult<T>(value: unknown): PageResult<T> {
  if (!value || typeof value !== 'object') {
    return { items: [] };
  }

  const record = value as { items?: T[] };
  return { items: Array.isArray(record.items) ? record.items : [] };
}

export async function getWorkspace(port: number): Promise<WorkspaceInfo> {
  return (await sharedGetWorkspace(port)) as WorkspaceInfo;
}

export async function listAgents(port: number): Promise<unknown[]> {
  return (await sharedListAgents(port)) as unknown[];
}

export async function listSessions(port: number): Promise<PageResult<Session>> {
  return normalizePageResult<Session>(await sharedListSessions(port));
}

export async function createSession(port: number, title: string = DEFAULT_SESSION_TITLE): Promise<Session> {
  return (await sharedCreateSession(port, title)) as Session;
}

export async function updateSession(
  port: number,
  sessionId: string,
  payload: { parent_session_id: string | null },
): Promise<Session> {
  return (await sharedUpdateSession(port, sessionId, payload)) as Session;
}

export async function listMessages(port: number, sessionId: string): Promise<PageResult<Message>> {
  return normalizePageResult<Message>(await sharedListMessages(port, sessionId));
}

export async function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult> {
  return (await sharedSendMessage(port, sessionId, payload)) as SessionAcceptResult;
}

export async function getJob(port: number, jobId: string): Promise<ActiveJob | null> {
  return (await sharedGetJob(port, jobId)) as ActiveJob | null;
}

export async function getSessionTraces(port: number, sessionId: string, afterEventId?: string | null): Promise<TraceEvent[]> {
  return (await sharedGetSessionTraces(port, sessionId, afterEventId)) as TraceEvent[];
}

export async function streamSessionEvents(
  port: number,
  sessionId: string,
  options?: {
    afterEventId?: string | null;
    onEvent?: (event: StreamEvent) => void;
    onError?: (error: unknown) => void;
    signal?: AbortSignal;
  },
): Promise<void> {
  return sharedStreamSessionEvents(port, sessionId, options);
}

export { DEFAULT_AGENT_ID, DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT, DEFAULT_BACKEND_TOKEN, DEFAULT_SESSION_TITLE };
export { TraceCursorGoneError };
