import { afterEach, describe, expect, test } from "bun:test";
import type { Dispatch, SetStateAction } from "react";
import { getSessionTurnDetails } from "../api/sessionTurnHistory";

import type {
  AppState,
  ConversationView,
} from "../types/frontend";
import type {
  Job,
  Message,
  PendingRequestList,
  Session,
  TraceEvent,
  TurnDetailBatchRequest,
} from "../types/backend";
import { reconcileActiveJob } from "./sessionJobReconciliation";
import {
  applyTurnDetails,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  upsertTurn,
  writeTurnTimelineCache,
} from "../state/session/turnTimeline";

const originalFetch = globalThis.fetch;
const SESSION_ID = "ses_reconciliation";
const WORKSPACE_ID = "gw_reconciliation";
const SESSION_CACHE_KEY = `${WORKSPACE_ID}::${SESSION_ID}`;
const ACTIVE_JOB_ID = "job_reconciliation";

function session(): Session {
  return {
    session_id: SESSION_ID,
    workspace_id: "ws_local",
    title: "连接恢复测试",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:01:00Z",
  };
}

function assistantMessage(content: string = "恢复后的回复"): Message {
  return {
    message_id: "msg_assistant_recovered",
    session_id: SESSION_ID,
    role: "assistant",
    content,
    attachments: [],
    metadata: {},
    created_at: "2026-07-24T00:01:00Z",
    updated_at: "2026-07-24T00:01:00Z",
  };
}

function pendingConversation(): ConversationView {
  return {
    conversationId: "msg_user_active",
    sessionId: SESSION_ID,
    userMessage: null,
    events: [],
    status: "running",
    jobId: ACTIVE_JOB_ID,
    pending: true,
    source: "pending",
  };
}

function completedTrace(): TraceEvent {
  return {
    event_id: "evt_job_completed",
    session_id: SESSION_ID,
    job_id: ACTIVE_JOB_ID,
    type: "job_completed",
    phase: "job",
    title: "任务完成",
    content: "",
    status: "completed",
    timestamp: "2026-07-24T00:01:00Z",
    raw: { payload: { result: "ok" } },
  };
}

function textDeltaTrace(): TraceEvent {
  return {
    event_id: "evt_text_delta_recovered",
    part_id: "part_recovered",
    session_id: SESSION_ID,
    job_id: ACTIVE_JOB_ID,
    type: "text_delta",
    phase: "text",
    title: "文本流",
    content: "增量回复",
    timestamp: "2026-07-24T00:00:30Z",
    raw: {
      payload: { kind: "markdown", text: "增量回复" },
    },
  };
}

function appState(): AppState {
  const currentSession = session();
  const turnTimeline = upsertTurn(
    {
      ...createSessionTurnTimeline(SESSION_CACHE_KEY, 1),
      phase: "ready",
      projectionEpoch: 1,
    },
    {
      turn_id: ACTIVE_JOB_ID,
      job_id: ACTIVE_JOB_ID,
      session_id: SESSION_ID,
      ordinal: 1,
      revision: 1,
      status: "running",
      created_at: "2026-07-24T00:00:00Z",
      updated_at: "2026-07-24T00:00:30Z",
      items_view: "summary",
      user_messages: [],
      response_preview: "",
    },
  );
  return {
    apiPort: 49_100,
    gatewayWorkspaces: [],
    activeGatewayWorkspaceId: WORKSPACE_ID,
    sessionsByWorkspace: new Map([[WORKSPACE_ID, [currentSession]]]),
    sessionGatewayWorkspaceById: new Map([
      [SESSION_CACHE_KEY, WORKSPACE_ID],
    ]),
    removingGatewayWorkspaceIds: new Set(),
    sessionHistoryReloadNonce: 0,
    workspaceSwitching: false,
    gatewayError: null,
    uiSettings: {
      layout: {},
      session_sidebar: {
        filter_mode: "all",
        sort_mode: "updated",
        grouping_mode: "workspace",
        workspace_group_capped: false,
        collapsed_workspace_ids: [],
        collapsed_session_ids: [],
        expanded_root_tree_ids: [],
        collapsed_section_ids: [],
      },
      workspace_file_tree: { expanded_paths_by_workspace: {} },
      gateway_console: { view: "routing" },
      theme: { theme_id: "warm", background: null, resolved_theme: null },
      recent_local_workspace_paths: [],
    },
    uiSettingsLoaded: true,
    workspaceRoot: "/tmp/reconciliation-workspace",
    workspaceName: "reconciliation-workspace",
    agents: [],
    sessions: [currentSession],
    sessionAttachmentSummaries: new Map(),
    currentSession,
    currentSessionWorkspaceId: WORKSPACE_ID,
    turnTimelinesBySession: new Map([[SESSION_CACHE_KEY, turnTimeline]]),
    traceEvents: [],
    llmRequestLogs: [],
    llmRequestLogsLoadedAt: null,
    llmRequestLogsLoading: false,
    llmRequestLogsError: null,
    sessionChangesets: [],
    selectedChangesetId: null,
    activeChangeset: null,
    sessionChangesLoadedAt: null,
    sessionChangesLoading: false,
    sessionChangesError: null,
    sessionResources: [],
    sessionResourcesLoadedAt: null,
    sessionResourcesLoading: false,
    sessionResourcesError: null,
    eventQueuesBySession: new Map(),
    sessionTraceHistoryBySession: new Map(),
    pendingConversations: new Map([
      [SESSION_CACHE_KEY, [pendingConversation()]],
    ]),
    activeJobIdsBySession: new Map([
      [SESSION_CACHE_KEY, ACTIVE_JOB_ID],
    ]),
    unreadSessionKeys: new Set(),
    status: "正在处理",
    error: null,
    isBootstrapping: false,
    expandDetails: false,
    agentSessionsPanelOpen: true,
    contentView: "default",
    agentStateJsonl: "",
    agentStateMessageCount: 0,
    agentStateLoadedAt: null,
    agentStateLoading: false,
    agentStateError: null,
    compactLoading: false,
    lastCompactResult: null,
    currentGoal: null,
    currentGoalSessionId: null,
    goalLoading: false,
    goalError: null,
  };
}

function job(status: Job["status"], errorMessage: string | null = null): Job {
  return {
    job_id: ACTIVE_JOB_ID,
    message_id: "msg_user_active",
    session_id: SESSION_ID,
    mode: "single_agent",
    status,
    entry_agent: "default",
    progress: status === "running" ? 50 : 100,
    error_message: errorMessage,
    metadata: {},
    created_at: "2026-07-24T00:00:00Z",
    updated_at: "2026-07-24T00:01:00Z",
    ended_at: status === "running" ? null : "2026-07-24T00:01:00Z",
  };
}

function apiResponse(data: unknown): Response {
  return Response.json({
    code: 0,
    message: "ok",
    request_id: "req_reconciliation",
    data,
  });
}

function installMockBackend({
  port,
  activeJob,
  pendingSnapshot = {
    session_id: SESSION_ID,
    active_job_id: null,
    requests: [],
  },
  traces = [],
  messages = [assistantMessage()],
  turnProjectionEpoch = 1,
}: {
  port: number;
  activeJob: Job;
  pendingSnapshot?: PendingRequestList;
  traces?: TraceEvent[];
  messages?: Message[];
  turnProjectionEpoch?: number;
}): string[] {
  const businessRequests: string[] = [];
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const [input] = args;
      const url = new URL(String(input));
      if (url.pathname === "/api/gateway/auth/local-credential") {
        return apiResponse({ token: `token-${port}` });
      }
      businessRequests.push(`${url.pathname}${url.search}`);
      if (url.pathname === `/api/v1/jobs/${ACTIVE_JOB_ID}`) {
        return apiResponse(activeJob);
      }
      if (
        url.pathname ===
        `/api/v1/sessions/${SESSION_ID}/pending-requests`
      ) {
        return apiResponse(pendingSnapshot);
      }
      if (url.pathname === `/api/v1/sessions/${SESSION_ID}/traces`) {
        return apiResponse(traces);
      }
      if (url.pathname === `/api/v1/sessions/${SESSION_ID}/messages`) {
        return apiResponse({
          items: messages,
          next_cursor: null,
          has_more: false,
        });
      }
      if (url.pathname === `/api/v1/sessions/${SESSION_ID}/turns/details`) {
        return apiResponse({
          projection_epoch: turnProjectionEpoch,
          items: [{
            turn_id: ACTIVE_JOB_ID,
            job_id: ACTIVE_JOB_ID,
            session_id: SESSION_ID,
            ordinal: 1,
            revision: 2,
            status: activeJob.status,
            created_at: "2026-07-24T00:00:00Z",
            updated_at: "2026-07-24T00:01:00Z",
            completed_at: "2026-07-24T00:01:00Z",
            items_view: "full",
            user_messages: [],
            response_preview: messages[0]?.content ?? "",
            final_response: messages[0]?.content ?? "",
            items: traces,
          }],
        });
      }
      if (url.pathname === `/api/v1/sessions/${SESSION_ID}`) {
        return apiResponse(session());
      }
      throw new Error(`测试收到未预期请求: ${url.pathname}${url.search}`);
    },
    { preconnect: originalFetch.preconnect },
  );
  return businessRequests;
}

function stateHarness(initialState: AppState): {
  current: () => AppState;
  setState: Dispatch<SetStateAction<AppState>>;
} {
  let currentState = initialState;
  return {
    current: () => currentState,
    setState: (update) => {
      currentState =
        typeof update === "function" ? update(currentState) : update;
    },
  };
}

function turnDetailRefresher(
  port: number,
  harness: ReturnType<typeof stateHarness>,
): (turnIds: string[]) => Promise<void> {
  return async (turnIds) => {
    const batch = await getSessionTurnDetails(
      port,
      SESSION_ID,
      turnIds as TurnDetailBatchRequest["turn_ids"],
      WORKSPACE_ID,
    );
    harness.setState((latest) => {
      const timeline = latest.turnTimelinesBySession.get(SESSION_CACHE_KEY);
      if (!timeline) return latest;
      const epochDecision = decideTurnProjectionEpoch(
        timeline.projectionEpoch,
        batch.projection_epoch,
      );
      if (epochDecision === "discard_older") return latest;
      if (epochDecision === "refresh_bootstrap") {
        return {
          ...latest,
          sessionHistoryReloadNonce: latest.sessionHistoryReloadNonce + 1,
        };
      }
      return {
        ...latest,
        turnTimelinesBySession: writeTurnTimelineCache(
          latest.turnTimelinesBySession,
          SESSION_CACHE_KEY,
          applyTurnDetails(timeline, batch),
        ),
      };
    });
  };
}

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("运行中 Job 对账", () => {
  test("Job 终态以 Turn detail 原位更新并清除转圈", async () => {
    const port = 49_100;
    const requests = installMockBackend({
      port,
      activeJob: job("completed"),
      traces: [completedTrace()],
    });
    const harness = stateHarness(appState());

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    expect(harness.current().traceEvents).toEqual([]);
    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    const turn = harness.current().turnTimelinesBySession
      .get(SESSION_CACHE_KEY)?.turnsById[ACTIVE_JOB_ID];
    expect(turn?.revision).toBe(2);
    expect(turn && "final_response" in turn ? turn.final_response : null).toBe(
      "恢复后的回复",
    );
    expect(harness.current().status).toBe("任务已完成");
    expect(requests.filter((path) => path.endsWith("/turns/details"))).toHaveLength(1);
  });

  test("终态 Trace 丢失时仍以 completed Job 刷新 Turn 并清除转圈", async () => {
    const port = 49_101;
    const requests = installMockBackend({
      port,
      activeJob: job("completed"),
      traces: [],
    });
    const harness = stateHarness(appState());

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().pendingConversations.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    const turn = harness.current().turnTimelinesBySession
      .get(SESSION_CACHE_KEY)?.turnsById[ACTIVE_JOB_ID];
    expect(turn && "final_response" in turn ? turn.final_response : null).toBe(
      "恢复后的回复",
    );
    expect(harness.current().status).toBe("任务已完成");
    expect(requests).toEqual([
      `/api/v1/jobs/${ACTIVE_JOB_ID}`,
      `/api/v1/sessions/${SESSION_ID}/pending-requests`,
      `/api/v1/sessions/${SESSION_ID}`,
      `/api/v1/sessions/${SESSION_ID}/turns/details`,
    ]);
  });

  test("终态 detail 来自未来 epoch 时不合并并通过 reloadNonce 请求 bootstrap", async () => {
    const port = 49_106;
    installMockBackend({
      port,
      activeJob: job("completed"),
      turnProjectionEpoch: 2,
    });
    const harness = stateHarness(appState());

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    const turn = harness.current().turnTimelinesBySession
      .get(SESSION_CACHE_KEY)?.turnsById[ACTIVE_JOB_ID];
    expect(turn?.revision).toBe(1);
    expect(turn && "final_response" in turn).toBe(false);
    expect(harness.current().sessionHistoryReloadNonce).toBe(1);
    expect(harness.current().status).toBe("任务已完成");
  });

  test("后台会话任务完整结束后标记为未读", async () => {
    const port = 49_105;
    installMockBackend({
      port,
      activeJob: job("completed"),
      traces: [completedTrace()],
    });
    const initialState = appState();
    initialState.currentSession = null;
    const harness = stateHarness(initialState);

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().unreadSessionKeys.has(SESSION_CACHE_KEY)).toBe(true);
  });

  test("失败 Job 在缺少终态 Trace 时显示后端错误并清除转圈", async () => {
    const port = 49_102;
    installMockBackend({
      port,
      activeJob: job("failed", "上游模型连接失败"),
      traces: [],
      messages: [],
    });
    const harness = stateHarness(appState());

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().status).toBe("任务失败: 上游模型连接失败");
  });

  test("Job 仍在运行时只发一个轻量请求且不修改状态", async () => {
    const port = 49_103;
    const requests = installMockBackend({
      port,
      activeJob: job("running"),
    });
    const initialState = appState();
    const harness = stateHarness(initialState);

    await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
    );

    expect(requests).toEqual([`/api/v1/jobs/${ACTIVE_JOB_ID}`]);
    expect(harness.current()).toBe(initialState);
  });

  test("SSE 业务事件停滞时不回读完整或增量 Trace", async () => {
    const port = 49_106;
    const requests = installMockBackend({
      port,
      activeJob: job("running"),
      traces: [textDeltaTrace()],
    });
    const harness = stateHarness(appState());

    const result = await reconcileActiveJob(
      port,
      SESSION_ID,
      WORKSPACE_ID,
      SESSION_CACHE_KEY,
      ACTIVE_JOB_ID,
      turnDetailRefresher(port, harness),
      harness.setState,
      {
        afterCursor: "evt_previous",
      },
    );

    expect(requests).toEqual([
      `/api/v1/jobs/${ACTIVE_JOB_ID}`,
    ]);
    expect(result).toEqual({
      jobStatus: "running",
      lastEventCursor: "evt_previous",
      recoveredEventCount: 0,
    });
    expect(harness.current().traceEvents).toEqual([]);
    expect(harness.current().status).toBe("正在处理");
  });

  test("Job 已终止但 pending 仍标记运行中时透明报错", async () => {
    const port = 49_104;
    installMockBackend({
      port,
      activeJob: job("completed"),
      pendingSnapshot: {
        session_id: SESSION_ID,
        active_job_id: ACTIVE_JOB_ID,
        requests: [],
      },
    });
    const harness = stateHarness(appState());

    await expect(
      reconcileActiveJob(
        port,
        SESSION_ID,
        WORKSPACE_ID,
        SESSION_CACHE_KEY,
        ACTIVE_JOB_ID,
        turnDetailRefresher(port, harness),
        harness.setState,
      ),
    ).rejects.toThrow("Job 已终止但会话仍标记为运行中");
    expect(harness.current().activeJobIdsBySession.get(SESSION_CACHE_KEY)).toBe(
      ACTIVE_JOB_ID,
    );
  });
});
