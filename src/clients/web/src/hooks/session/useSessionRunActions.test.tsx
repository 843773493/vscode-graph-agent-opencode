import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToString } from "react-dom/server";

import type { Session } from "../../types/backend";
import type { AppState } from "../../types/frontend";
import { useSessionRunActions } from "./useSessionRunActions";

const originalFetch = globalThis.fetch;

function session(): Session {
  return {
    session_id: "ses_send_regression",
    workspace_id: "workspace_send_regression",
    title: "发送回归",
    title_source: "user",
    current_agent_id: "default",
    parent_session_id: null,
    created_at: "2026-07-20T00:00:00Z",
    updated_at: "2026-07-20T00:00:00Z",
  };
}

function state(currentSession: Session): AppState {
  return {
    eventQueuesBySession: new Map(),
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    unreadSessionKeys: new Set(),
    sessionAttachmentSummaries: new Map(),
    sessionsByWorkspace: new Map(),
    sessionGatewayWorkspaceById: new Map(),
    currentSession,
    sessionHistoryReloadNonce: 0,
    status: "",
    contentView: "default",
  } as AppState;
}

describe("发送消息状态更新", () => {
  test("W9-d 发送失败后按后端队列快照校准而不是抹掉已接受的回合", async () => {
    const currentSession = session();
    const cacheKey = "gw_send_regression::ses_send_regression";
    let currentState = state(currentSession);
    let pendingCalls = 0;
    let sendMessage:
      | ReturnType<typeof useSessionRunActions>["sendMessage"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        const method = String(
          (args[1] as RequestInit | undefined)?.method ?? "GET",
        );
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_credential",
            data: { token: "test-pending-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (
          path === `/api/v1/sessions/${currentSession.session_id}/messages`
          && method === "POST"
        ) {
          // 网关拒绝：但后端其实已经收下并排进队列。
          return Response.json(
            { detail: "模型网关拒绝" },
            { status: 502 },
          );
        }
        if (
          path === `/api/v1/sessions/${currentSession.session_id}/pending-requests`
        ) {
          pendingCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_snapshot",
            data: {
              session_id: currentSession.session_id,
              snapshot_version: 9,
              active_job_id: "job_server_accepted",
              queue: [],
              requests: [],
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${method} ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: cacheKey,
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      sendMessage = actions.sendMessage;
      return null;
    }

    try {
      renderToString(<Harness />);
      if (!sendMessage) throw new Error("测试未获取 sendMessage");
      await expect(sendMessage("你好")).rejects.toThrow("模型网关拒绝");
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(pendingCalls).toBe(1);
    // 乐观回合被后端权威快照替换：后端已接受的 job 必须留在本地队列里。
    expect(currentState.activeJobIdsBySession.get(cacheKey))
      .toBe("job_server_accepted");
    expect(currentState.status).toContain("发送失败");
    expect(currentState.status).toContain("模型网关拒绝");
  });

  test("API 接受请求前的乐观更新不读取尚未返回的 accepted", async () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let sendMessage:
      | ReturnType<typeof useSessionRunActions>["sendMessage"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential",
            data: { token: "test-local-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (
          path ===
          `/api/v1/sessions/${currentSession.session_id}/messages`
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_send",
            data: {
              message_id: "msg_send_regression",
              job_id: "job_send_regression",
              status: "running",
              dispatch: {
                session_id: currentSession.session_id,
                job_id: "job_send_regression",
                job_status: "running",
                active_job_id: "job_send_regression",
                queued_jobs_ahead: 0,
                queued_job_count: 0,
                pending_job_count: 0,
              },
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: "gw_send_regression::ses_send_regression",
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      sendMessage = actions.sendMessage;
      return null;
    }

    try {
      renderToString(<Harness />);
      if (!sendMessage) {
        throw new Error("测试未获取 sendMessage");
      }
      await sendMessage("请只回复：收到");
    } finally {
      globalThis.fetch = originalFetch;
    }

    const cacheKey = "gw_send_regression::ses_send_regression";
    expect(currentState.status).toBe("已发送，等待生成");
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
    expect(currentState.activeJobIdsBySession.get(cacheKey)).toBe(
      "job_send_regression",
    );
    expect(
      currentState.pendingConversations.get(cacheKey)?.[0]?.conversationId,
    ).toBe("msg_send_regression");
  });

  test("后端已无运行任务时中断会清掉残留前端运行态并触发历史同步", async () => {
    const currentSession = session();
    let currentState = state(currentSession);
    const cacheKey = "gw_send_regression::ses_send_regression";
    currentState.activeJobIdsBySession.set(cacheKey, "job_stale_running");
    let interruptSession:
      | ReturnType<typeof useSessionRunActions>["interruptSession"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_interrupt_credential",
            data: { token: "test-interrupt-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_interrupt_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${currentSession.session_id}/interrupt`) {
          return Response.json(
            { detail: `Session ${currentSession.session_id} 当前没有正在运行的任务` },
            { status: 404 },
          );
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: cacheKey,
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      interruptSession = actions.interruptSession;
      return null;
    }

    try {
      renderToString(<Harness />);
      await interruptSession!();
    } finally {
      globalThis.fetch = originalFetch;
    }

    expect(currentState.activeJobIdsBySession.has(cacheKey)).toBe(false);
    expect(currentState.sessionHistoryReloadNonce).toBe(1);
    expect(currentState.status).toBe("运行任务已结束，正在同步会话历史");
  });

  test("中断遇到非 404 失败会恢复后端运行态并写入可见失败", async () => {
    const currentSession = session();
    let currentState = state(currentSession);
    const cacheKey = "gw_send_regression::ses_send_regression";
    currentState.activeJobIdsBySession.set(cacheKey, "job_stale_running");
    let interruptSession:
      | ReturnType<typeof useSessionRunActions>["interruptSession"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_interrupt_fail_credential",
            data: { token: "test-interrupt-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_interrupt_fail_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${currentSession.session_id}/interrupt`) {
          return Response.json(
            { detail: "中断执行器崩溃" },
            { status: 500 },
          );
        }
        // 后端真值：任务仍在运行。前端必须校准回这个 job，而不是乐观清空。
        if (
          path === `/api/v1/sessions/${currentSession.session_id}/pending-requests`
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_interrupt_fail_pending",
            data: {
              session_id: currentSession.session_id,
              active_job_id: "job_backend_running",
              requests: [],
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: cacheKey,
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      interruptSession = actions.interruptSession;
      return null;
    }

    let thrown: unknown = null;
    try {
      renderToString(<Harness />);
      await interruptSession!();
    } catch (error) {
      thrown = error;
    } finally {
      globalThis.fetch = originalFetch;
    }

    // 失败仍向上抛出，调用方必须接住；但状态与运行态已经收敛。
    expect(thrown instanceof Error ? thrown.message : String(thrown)).toContain(
      "中断执行器崩溃",
    );
    expect(currentState.status).toContain("中断生成失败");
    expect(currentState.status).toContain("中断执行器崩溃");
    // 后端仍在运行：activeJobId 必须回到后端真值，而不是被乐观清空。
    expect(currentState.activeJobIdsBySession.get(cacheKey)).toBe(
      "job_backend_running",
    );
    // 非 404 失败不得触发「任务已结束」的历史重载。
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
  });

  test("重新生成成功后保留可见的乐观运行回合", async () => {
    const currentSession = session();
    let currentState = state(currentSession);
    let replayTurn:
      | ReturnType<typeof useSessionRunActions>["replayTurn"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input)).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_replay_credential",
            data: { token: "test-replay-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_replay_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (
          path ===
          `/api/v1/sessions/${currentSession.session_id}/messages/msg_original/replay`
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_replay",
            data: {
              message_id: "msg_replay_new",
              job_id: "job_replay_new",
              session_id: currentSession.session_id,
              action: "regenerate",
              status: "running",
              replaced_message_id: "msg_original",
              removed_message_count: 1,
              notice: "已移除目标消息及其后的会话上下文；工作区文件修改不会被撤销。",
              dispatch: {
                session_id: currentSession.session_id,
                job_id: "job_replay_new",
                job_status: "running",
                active_job_id: "job_replay_new",
                queued_jobs_ahead: 0,
                queued_job_count: 0,
                pending_job_count: 0,
              },
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: "gw_send_regression::ses_send_regression",
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      replayTurn = actions.replayTurn;
      return null;
    }

    try {
      renderToString(<Harness />);
      if (!replayTurn) {
        throw new Error("测试未获取 replayTurn");
      }
      await replayTurn("msg_original", "regenerate", "原始回复");
    } finally {
      globalThis.fetch = originalFetch;
    }

    const replay = currentState.pendingConversations
      .get("gw_send_regression::ses_send_regression")?.[0];
    expect(replay?.conversationId).toBe("msg_replay_new");
    expect(replay?.activeJobOverlay).toBe(true);
    expect(replay?.userMessage?.metadata?.replay_action).toBe("regenerate");
    expect(currentState.activeJobIdsBySession.get("gw_send_regression::ses_send_regression"))
      .toBe("job_replay_new");
    expect(currentState.sessionHistoryReloadNonce).toBe(1);
  });

  test("W10-发送成功分支整体投影后端 dispatch 的全部队列事实", async () => {
    const currentSession = session();
    const cacheKey = "gw_send_regression::ses_send_regression";
    let currentState = state(currentSession);
    let sendMessage:
      | ReturnType<typeof useSessionRunActions>["sendMessage"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_credential",
            data: { token: "test-w10-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (
          path === `/api/v1/sessions/${currentSession.session_id}/messages`
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_send",
            data: {
              message_id: "msg_w10_projection",
              job_id: "job_w10_projection",
              status: "queued",
              dispatch: {
                session_id: currentSession.session_id,
                job_id: "job_w10_projection",
                job_status: "queued",
                active_job_id: "job_w10_active",
                blocked_by_job_id: "job_w10_active",
                queued_jobs_ahead: 2,
                queued_job_count: 3,
                pending_job_count: 4,
                delivery_policy: "after_interrupt",
                enqueue_sequence: 7,
                queue_snapshot_version: 11,
              },
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: cacheKey,
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      sendMessage = actions.sendMessage;
      return null;
    }

    try {
      renderToString(<Harness />);
      if (!sendMessage) throw new Error("测试未获取 sendMessage");
      await sendMessage("投递投影");
    } finally {
      globalThis.fetch = originalFetch;
    }

    const conversation = currentState.pendingConversations.get(cacheKey)?.[0];
    expect(conversation?.deliveryPolicy).toBe("after_interrupt");
    expect(conversation?.enqueueSequence).toBe(7);
    expect(conversation?.pendingPosition).toBe(2);
    expect(conversation?.queueSnapshotVersion).toBe(11);
    // 此前被逐字段手挑丢掉的队列计数与阻塞来源必须进入前端状态。
    expect(conversation?.queuedJobCount).toBe(3);
    expect(conversation?.pendingJobCount).toBe(4);
    expect(conversation?.blockedByJobId).toBe("job_w10_active");
  });

  test("W10-后端未提供 delivery_policy 时不伪造本地默认值", async () => {
    const currentSession = session();
    const cacheKey = "gw_send_regression::ses_send_regression";
    let currentState = state(currentSession);
    let sendMessage:
      | ReturnType<typeof useSessionRunActions>["sendMessage"]
      | undefined;

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0])).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_null_credential",
            data: { token: "test-w10-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_null_current_user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (
          path === `/api/v1/sessions/${currentSession.session_id}/messages`
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_w10_null_send",
            data: {
              message_id: "msg_w10_null",
              job_id: "job_w10_null",
              status: "running",
              dispatch: {
                session_id: currentSession.session_id,
                job_id: "job_w10_null",
                job_status: "running",
                active_job_id: "job_w10_null",
                queued_jobs_ahead: 0,
                queued_job_count: 0,
                pending_job_count: 1,
              },
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const actions = useSessionRunActions({
        apiPort: 8014,
        currentSession,
        activeGatewayWorkspaceId: "gw_send_regression",
        currentSessionGatewayWorkspaceId: "gw_send_regression",
        currentSessionCacheKey: cacheKey,
        defaultGatewayWorkspaceId: "gw_send_regression",
        contentView: "default",
        setState: (update) => {
          currentState =
            typeof update === "function" ? update(currentState) : update;
        },
        refreshAgentStateSnapshot: async () => undefined,
      });
      sendMessage = actions.sendMessage;
      return null;
    }

    try {
      renderToString(<Harness />);
      if (!sendMessage) throw new Error("测试未获取 sendMessage");
      // 请求参数带 after_turn，但后端拒绝提供 delivery_policy。
      await sendMessage("无投递策略", [], "after_turn");
    } finally {
      globalThis.fetch = originalFetch;
    }

    const conversation = currentState.pendingConversations.get(cacheKey)?.[0];
    expect(conversation?.deliveryPolicy).toBeUndefined();
    expect(conversation?.enqueueSequence).toBeUndefined();
    expect(conversation?.blockedByJobId).toBeUndefined();
    expect(conversation?.queuedJobCount).toBe(0);
    expect(conversation?.pendingJobCount).toBe(1);
  });
});
