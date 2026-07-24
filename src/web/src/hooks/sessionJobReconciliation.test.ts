import { afterEach, describe, expect, test } from "bun:test";
import type { Dispatch, SetStateAction } from "react";

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
} from "../types/backend";
import { reconcileActiveJob } from "./sessionJobReconciliation";

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

function appState(): AppState {
  const currentSession = session();
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
    messages: [],
    messageHistoryNextCursor: null,
    messageHistoryHasMore: false,
    messageHistoryLoadingOlder: false,
    messageHistoryError: null,
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
    pendingConversations: new Map([
      [SESSION_CACHE_KEY, [pendingConversation()]],
    ]),
    activeJobIdsBySession: new Map([
      [SESSION_CACHE_KEY, ACTIVE_JOB_ID],
    ]),
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
}: {
  port: number;
  activeJob: Job;
  pendingSnapshot?: PendingRequestList;
  traces?: TraceEvent[];
  messages?: Message[];
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

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("运行中 Job 对账", () => {
  test("Job 与终态 Trace 一致时恢复 Trace、消息并清除转圈", async () => {
    const port = 49_100;
    installMockBackend({
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
      harness.setState,
    );

    expect(harness.current().traceEvents.map((event) => event.event_id)).toEqual(
      ["evt_job_completed"],
    );
    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().messages).toHaveLength(1);
    expect(harness.current().status).toBe("回复已完成");
  });

  test("终态 Trace 丢失时仍以 completed Job 刷新消息并清除转圈", async () => {
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
      harness.setState,
    );

    expect(harness.current().activeJobIdsBySession.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().pendingConversations.has(SESSION_CACHE_KEY)).toBe(
      false,
    );
    expect(harness.current().messages.map((item) => item.content)).toEqual([
      "恢复后的回复",
    ]);
    expect(harness.current().status).toBe("任务已完成");
    expect(requests).toEqual([
      `/api/v1/jobs/${ACTIVE_JOB_ID}`,
      `/api/v1/sessions/${SESSION_ID}/pending-requests`,
      `/api/v1/sessions/${SESSION_ID}/traces?tail_limit=2000`,
      `/api/v1/sessions/${SESSION_ID}/messages?limit=40`,
      `/api/v1/sessions/${SESSION_ID}`,
    ]);
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
      harness.setState,
    );

    expect(requests).toEqual([`/api/v1/jobs/${ACTIVE_JOB_ID}`]);
    expect(harness.current()).toBe(initialState);
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
        harness.setState,
      ),
    ).rejects.toThrow("Job 已终止但会话仍标记为运行中");
    expect(harness.current().activeJobIdsBySession.get(SESSION_CACHE_KEY)).toBe(
      ACTIVE_JOB_ID,
    );
  });
});
