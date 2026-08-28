import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  SESSION_ID,
  SCOPE_KEY,
  WORKSPACE_ID,
  appState,
  bootstrap,
  installWindow,
  originalFetch,
  restoreTurnHistoryTestGlobals,
  turnDetail,
  wait,
} from "./testFixtures";
import { useSessionTurnHistory } from "./useSessionTurnHistory";

afterEach(() => {
  restoreTurnHistoryTestGlobals();
});

describe("useSessionTurnHistory detail invalidation", () => {
  test("连续失效会在每个悬挂详情请求后追取更高 revision", async () => {
    const apiPort = 9116;
    installWindow(apiPort);
    let currentState = appState();
    let history: ReturnType<typeof useSessionTurnHistory> | null = null;
    let detailCalls = 0;
    let releaseFirst: () => void = () => undefined;
    let releaseSecond: () => void = () => undefined;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const secondGate = new Promise<void>((resolve) => {
      releaseSecond = resolve;
    });
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-dirty-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_dirty",
            data: bootstrap("ready", 1),
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          detailCalls += 1;
          const requestNumber = detailCalls;
          if (requestNumber === 1) await firstGate;
          if (requestNumber === 2) await secondGate;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_details_dirty_${requestNumber}`,
            data: {
              projection_epoch: 1,
              items: [{
                ...turnDetail(1),
                revision: requestNumber,
                final_response: `revision ${requestNumber}`,
              }],
            },
          });
        }
        if (path.endsWith("/message-stream/snapshot")) {
          return Response.json({ detail: "message stream not found" }, { status: 404 });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      history = useSessionTurnHistory({
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
    expect(detailCalls).toBe(1);
    const firstInvalidation = history!.loadTurnDetails(["job_latest"], null, true);
    await act(async () => {
      releaseFirst();
      await wait(30);
    });
    expect(detailCalls).toBe(2);
    const secondInvalidation = history!.loadTurnDetails(["job_latest"], null, true);
    await act(async () => {
      releaseSecond();
      await Promise.all([firstInvalidation, secondInvalidation]);
    });

    expect(detailCalls).toBe(3);
    const finalTurn = currentState.turnTimelinesBySession
      .get(SCOPE_KEY)?.turnsById.job_latest;
    expect(finalTurn?.revision).toBe(3);
    expect(finalTurn && "final_response" in finalTurn
      ? finalTurn.final_response
      : null).toBe("revision 3");
    act(() => renderer!.unmount());
  });

  test("详情响应来自未来 epoch 时不合并并递增 reloadNonce", async () => {
    const apiPort = 9109;
    installWindow(apiPort);
    let currentState = appState();
    let history: ReturnType<typeof useSessionTurnHistory> | null = null;
    let detailCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential_future",
            data: { token: "turn-history-future-token" },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_current",
            data: { ...bootstrap("ready", 1), latest_turn: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          detailCalls += 1;
          await wait(10);
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_details_future",
            data: { items: [turnDetail(2)], projection_epoch: 2 },
          });
        }
        if (path.endsWith("/message-stream/snapshot")) {
          return Response.json({ detail: "message stream not found" }, { status: 404 });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      history = useSessionTurnHistory({
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
    await act(async () => {
      await Promise.all([
        history!.loadTurnDetails(["job_latest"]),
        history!.loadTurnDetails(["job_latest"]),
      ]);
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(timeline?.projectionEpoch).toBe(1);
    expect(timeline?.turnsById.job_latest).toBeUndefined();
    expect(currentState.sessionHistoryReloadNonce).toBe(1);
    expect(currentState.status).toBe("Turn 投影已更新，正在重新加载");
    expect(detailCalls).toBe(1);
    act(() => renderer!.unmount());
  });

  test("bootstrap 返回已失效的最新 Turn 时不会重复请求详情", async () => {
    const apiPort = 9117;
    installWindow(apiPort);
    let currentState = appState();
    let history: ReturnType<typeof useSessionTurnHistory> | null = null;
    let historyCalls = 0;
    let bootstrapCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(
          String(args[0]),
          `http://127.0.0.1:${apiPort}`,
        ).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-stale-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          bootstrapCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_bootstrap_stale_${bootstrapCalls}`,
            data: bootstrap("ready", 1),
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          historyCalls += 1;
          return Response.json(
            {
              detail: {
                code: "stale_turn_reference",
                session_id: SESSION_ID,
                turn_ids: ["job_latest"],
                message: "Turn 已不属于当前上下文视图",
              },
            },
            { status: 409 },
          );
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      history = useSessionTurnHistory({
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
      await wait(250);
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(historyCalls).toBe(1);
    expect(bootstrapCalls).toBe(2);
    expect(timeline?.orderedTurnIds).not.toContain("job_latest");
    expect(timeline?.invalidatedTurnIds).toContain("job_latest");
    await history!.loadTurnDetails(["job_latest"], null, true);
    expect(historyCalls).toBe(1);
    act(() => renderer!.unmount());
  });

  test("Job 终态早于 rollout 提交时会短暂重试 Turn 详情", async () => {
    const apiPort = 9110;
    installWindow(apiPort);
    let currentState = appState();
    let history: ReturnType<typeof useSessionTurnHistory> | null = null;
    let detailCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const path = new URL(
          String(args[0]),
          `http://127.0.0.1:${apiPort}`,
        ).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: "turn-history-retry-token" } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_retry",
            data: bootstrap("ready", 1),
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          detailCalls += 1;
          if (detailCalls < 3) {
            return Response.json(
              { detail: "rollout Turn 不存在: ['job_latest']" },
              { status: 404 },
            );
          }
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_details_retry",
            data: { projection_epoch: 1, items: [turnDetail(1)] },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      history = useSessionTurnHistory({
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
      await wait(500);
    });

    expect(detailCalls).toBe(3);
    expect(currentState.sessionHistoryReloadNonce).toBe(0);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.error).toBeNull();
    act(() => renderer!.unmount());
  });
});
