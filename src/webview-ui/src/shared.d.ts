declare module '../../shared/api.js' {
  export interface WorkspaceInfo {
    root_path: string;
    name: string;
  }

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

  export interface TraceEvent {
    event_id: string;
    job_id: string;
    step_id: string | null;
    agent_id: string | null;
    timestamp: string;
    type: string;
    payload: Record<string, unknown>;
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

  export interface StreamEvent<TPayload = Record<string, unknown>> {
    eventType: string;
    eventId?: string;
    payload: TPayload;
    event?: TraceEvent;
  }

  export class TraceCursorGoneError extends Error {
    readonly eventId: string;
    readonly status: 410;
  }

  export function getWorkspace(port: number): Promise<WorkspaceInfo>;
  export function listAgents(port: number): Promise<unknown[]>;
  export function listSessions(port: number): Promise<PageResult<Session>>;
  export function createSession(port: number, title?: string): Promise<Session>;
  export function updateSession(port: number, sessionId: string, payload: { parent_session_id: string | null }): Promise<Session>;
  export function listMessages(port: number, sessionId: string): Promise<PageResult<Message>>;
  export function sendMessage(port: number, sessionId: string, payload: unknown): Promise<SessionAcceptResult>;
  export function getJob(port: number, jobId: string): Promise<unknown>;
  export function getSessionTraces(port: number, sessionId: string, afterEventId?: string | null): Promise<TraceEvent[]>;
  export function streamSessionEvents(
    port: number,
    sessionId: string,
    options?: {
      afterEventId?: string | null;
      onEvent?: (event: StreamEvent) => void;
      onError?: (error: unknown) => void;
      signal?: AbortSignal;
    },
  ): Promise<void>;
}

declare module '../../shared/constants.js' {
  export const EXTENSION_ID: 'vscode-graph-agent';
  export const SIDEBAR_VIEW_ID: 'vscode-graph-agent.sidebar';
  export const OPEN_SIDEBAR_COMMAND: 'vscode-graph-agent.openSidebar';
  export const OPEN_UI_SHELL_DEBUG_COMMAND: 'graph-agent.openUiShellDebug';
  export const DEFAULT_BACKEND_HOST: '127.0.0.1';
  export const DEFAULT_BACKEND_PORT: 8000;
  export const DEFAULT_BACKEND_TOKEN: 'local-dev-token';
  export const API_PREFIX: '/api/v1';
  export const DEFAULT_SESSION_TITLE: '新会话';
  export const DEFAULT_AGENT_ID: 'default';
}
