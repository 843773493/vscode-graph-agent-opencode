import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { ConversationView } from "../../types/frontend";
import {
  SESSION_ID,
  SCOPE_KEY,
  WORKSPACE_ID,
  appState,
  bootstrap,
  installWindow,
  originalFetch,
  restoreTurnHistoryTestGlobals,
  wait,
} from "./testFixtures";
import { useSessionTurnHistory } from "./useSessionTurnHistory";

afterEach(() => {
  restoreTurnHistoryTestGlobals();
});

describe("useSessionTurnHistory pending bootstrap", () => {
  test("active Job 没有 queued request 时保留乐观 replay 提示", async () => {
    const apiPort = 9113;
    const activeJobId = "job_replay_active";
    installWindow(apiPort);
    let currentState = appState();
    const optimisticReplay: ConversationView = {
      conversationId: "msg_replay_active",
      displayMode: "live",
      sessionId: SESSION_ID,
      userMessage: {
        message_id: "msg_replay_active",
        session_id: SESSION_ID,
        role: "user",
        content: "请重新生成",
        attachments: [],
        metadata: {
          source: "optimistic_replay",
          replay_action: "regenerate",
          job_id: activeJobId,
        },
        created_at: "2026-07-28T00:00:04Z",
        updated_at: "2026-07-28T00:00:04Z",
      },
      assistantMessages: [],
      events: [],
      status: "running",
      jobId: activeJobId,
      pending: true,
      source: "pending",
    };
    currentState.pendingConversations.set(SCOPE_KEY, [optimisticReplay]);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-replay-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_replay",
            data: {
              ...bootstrap("ready", 1),
              latest_turn: null,
              active_job_id: activeJobId,
              active_jobs: [{
                job_id: activeJobId,
                message_id: "msg_replay_active",
                status: "running",
                updated_at: "2026-07-28T00:00:04Z",
                snapshot_version: 1,
              }],
              active_job_count: 1,
            },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/pending-requests`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_replay",
            data: {
              session_id: SESSION_ID,
              active_job_id: null,
              requests: [],
              snapshot_version: 1,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(60);
    });

    const replay = currentState.pendingConversations.get(SCOPE_KEY)?.[0];
    expect(replay?.userMessage?.metadata?.replay_action).toBe("regenerate");
    expect(replay?.jobId).toBe(activeJobId);
    expect(replay?.activeJobOverlay).toBe(true);
    expect(currentState.activeJobIdsBySession.get(SCOPE_KEY)).toBe(activeJobId);
    act(() => renderer!.unmount());
  });

  test("只有 queued request 时加载真实内容且不误标为 active Job", async () => {
    const apiPort = 9114;
    installWindow(apiPort);
    let currentState = appState();
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-queued-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_queued",
            data: {
              ...bootstrap("ready", 1),
              latest_turn: null,
              active_jobs: [{
                job_id: "job_queued_only",
                message_id: "msg_queued_only",
                status: "queued",
                updated_at: "2026-07-29T00:00:00Z",
                snapshot_version: 1,
              }],
              snapshot_version: 1,
              active_job_count: 1,
            },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/pending-requests`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_pending_queued",
            data: {
              session_id: SESSION_ID,
              active_job_id: null,
              requests: [{
                job_id: "job_queued_only",
                message_id: "msg_queued_only",
                session_id: SESSION_ID,
                content: "排队消息真实内容",
                attachments: [],
                delivery_policy: "after_turn",
                enqueue_sequence: 1,
                status: "queued",
                position: 1,
                agent_id: "default",
                message_created_at: "2026-07-29T00:00:00Z",
                message_metadata: {},
                created_at: "2026-07-29T00:00:00Z",
                updated_at: "2026-07-29T00:00:00Z",
                snapshot_version: 1,
              }],
              snapshot_version: 1,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(100);
    });

    expect(currentState.activeJobIdsBySession.has(SCOPE_KEY)).toBe(false);
    expect(currentState.pendingConversations.get(SCOPE_KEY)?.[0]?.userMessage?.content)
      .toBe("排队消息真实内容");
    expect(currentState.pendingConversations.get(SCOPE_KEY)?.[0]?.activeJobOverlay)
      .not.toBe(true);
    act(() => renderer!.unmount());
  });

  test("bootstrap 权威 count 为零时清除旧 queued 缓存", async () => {
    const apiPort = 9115;
    installWindow(apiPort);
    let currentState = appState();
    currentState.pendingConversations.set(SCOPE_KEY, [{
      conversationId: "old-queued",
      displayMode: "live",
      sessionId: SESSION_ID,
      userMessage: null,
      events: [],
      status: "queued",
      jobId: "old-queued-job",
      pending: true,
      source: "pending",
    }]);
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-empty-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_empty_pending",
            data: { ...bootstrap("ready", 1), latest_turn: null },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness />);
      await wait(30);
    });
    expect(currentState.pendingConversations.has(SCOPE_KEY)).toBe(false);
    act(() => renderer!.unmount());
  });
});
