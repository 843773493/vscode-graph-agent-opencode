import { describe, expect, test } from "bun:test";
import React from "react";
import { renderToString } from "react-dom/server";

import type { Session } from "../types/backend";
import type { AppState } from "../types/frontend";
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
});
