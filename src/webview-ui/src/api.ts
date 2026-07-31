import {
    createSession as sharedCreateSession,
    getJob as sharedGetJob,
    getSessionGoal as sharedGetSessionGoal,
    getSessionTraces as sharedGetSessionTraces,
    getWorkspace as sharedGetWorkspace,
    listAgents as sharedListAgents,
    listMessages as sharedListMessages,
    listSessions as sharedListSessions,
    moveSessionParent as sharedMoveSessionParent,
    sendMessage as sharedSendMessage,
    streamSessionEvents as sharedStreamSessionEvents,
    updateSessionGoal as sharedUpdateSessionGoal,
    clearSessionGoal as sharedClearSessionGoal,
    TraceCursorGoneError,
} from '../../shared/api.js';
import {
    DEFAULT_AGENT_ID,
    DEFAULT_BACKEND_HOST,
    DEFAULT_BACKEND_PORT,
    DEFAULT_BACKEND_TOKEN,
    DEFAULT_SESSION_TITLE,
} from '../../shared/constants.js';
import type {
    StreamEvent as SharedStreamEvent,
} from '../../shared/api.js';
import type { TraceEventDTO } from '../../web/src/types/gen/trace';
import type { ActiveJob, Message, Session, SessionGoal, SessionGoalUpdateRequest, TraceEvent } from './types/backend';

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

export type StreamEvent = SharedStreamEvent;

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

export async function moveSessionParent(
  port: number,
  sessionId: string,
  parentNodeId: string | null,
): Promise<Session> {
  return (await sharedMoveSessionParent(port, sessionId, parentNodeId)) as Session;
}

export async function listMessages(port: number, sessionId: string): Promise<PageResult<Message>> {
  return normalizePageResult<Message>(await sharedListMessages(port, sessionId));
}

export async function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult> {
  return (await sharedSendMessage(port, sessionId, payload)) as SessionAcceptResult;
}

export async function getJob(port: number, jobId: string): Promise<ActiveJob | null> {
  const job = await sharedGetJob(port, jobId);
  return {
    jobId: job.job_id,
    sessionId: job.session_id,
    status: job.status,
    messageId: job.message_id,
    content: '',
  };
}

export async function getSessionGoal(port: number, sessionId: string): Promise<SessionGoal | null> {
  return (await sharedGetSessionGoal(port, sessionId)) as SessionGoal | null;
}

export async function updateSessionGoal(
  port: number,
  sessionId: string,
  payload: SessionGoalUpdateRequest,
): Promise<SessionGoal> {
  return (await sharedUpdateSessionGoal(port, sessionId, payload)) as SessionGoal;
}

export async function clearSessionGoal(port: number, sessionId: string): Promise<void> {
  await sharedClearSessionGoal(port, sessionId);
}

export async function getSessionTraces(port: number, sessionId: string, afterEventId?: string | null): Promise<TraceEventDTO[]> {
  return sharedGetSessionTraces(port, sessionId, afterEventId);
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
