import type { Session, SessionTurnBootstrap, TurnDetail } from "../../types/backend";
import type { AppState } from "../../types/frontend";

export const SESSION_ID = "ses_partial_projection";
export const WORKSPACE_ID = "workspace_partial_projection";
export const SCOPE_KEY = `${WORKSPACE_ID}::${SESSION_ID}`;
export const originalFetch = globalThis.fetch;

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(
  globalThis,
  "window",
);

export function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

export function session(): Session {
  return {
    session_id: SESSION_ID,
    workspace_id: WORKSPACE_ID,
    title: "渐进 Turn 历史",
    current_agent_id: "default",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
  };
}

export function latestTurn(): NonNullable<SessionTurnBootstrap["latest_turn"]> {
  return {
    turn_id: "job_latest",
    job_id: "job_latest",
    session_id: SESSION_ID,
    ordinal: 3,
    revision: 1,
    status: "completed",
    created_at: "2026-07-28T00:00:03Z",
    updated_at: "2026-07-28T00:00:03Z",
    completed_at: "2026-07-28T00:00:03Z",
    items_view: "summary",
    source_message_ids: ["msg_latest"],
    source_message_count: 1,
    merged_job_ids: [],
    merged_job_count: 0,
    sources_truncated: false,
    user_messages: [],
    user_message_count: 0,
    user_messages_truncated: false,
    response_preview: "最新回复",
    preview_truncated: false,
    item_count: 1,
  };
}

export function turnDetail(epoch: number): TurnDetail {
  return {
    turn_id: "job_latest",
    job_id: "job_latest",
    session_id: SESSION_ID,
    ordinal: 3,
    revision: 1,
    status: "completed",
    created_at: "2026-07-28T00:00:03Z",
    updated_at: "2026-07-28T00:00:03Z",
    completed_at: "2026-07-28T00:00:03Z",
    items_view: "full",
    source_message_ids: ["msg_latest"],
    merged_job_ids: [],
    user_messages: [],
    response_preview: "最新回复",
    preview_truncated: false,
    final_response: `epoch ${epoch} 完整回复`,
    items: [],
  };
}

function turnBootstrapSession(): SessionTurnBootstrap["session"] {
  return {
    session_id: SESSION_ID,
    workspace_id: WORKSPACE_ID,
    title: "渐进 Turn 历史",
    current_agent_id: "default",
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
  };
}

export function bootstrap(
  projectionState: "partial" | "ready",
  projectionEpoch: number,
): SessionTurnBootstrap {
  return {
    session: turnBootstrapSession(),
    latest_turn: latestTurn(),
    active_job_id: null,
    active_jobs: [],
    active_job_count: 0,
    active_jobs_truncated: false,
    projection_state: projectionState,
    older_cursor: projectionState === "ready" ? "older-ready" : null,
    event_cursor: `event-${projectionEpoch}`,
    projection_epoch: projectionEpoch,
  };
}

export function appState(): AppState {
  const currentSession = session();
  return {
    sessions: [currentSession],
    sessionsByWorkspace: new Map([[WORKSPACE_ID, [currentSession]]]),
    sessionGatewayWorkspaceById: new Map(),
    currentSession,
    currentSessionWorkspaceId: WORKSPACE_ID,
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    sessionAttachmentSummaries: new Map(),
    turnTimelinesBySession: new Map(),
    sessionHistoryReloadNonce: 0,
    status: "",
  } as AppState;
}

export function installWindow(apiPort: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(apiPort) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

export function restoreTurnHistoryTestGlobals(): void {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
}
