import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { partialBootstrapPollDelay } from "./bootstrap";
import {
  createSessionTurnTimeline,
  upsertTurns,
} from "../../state/session/turnTimeline";
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

describe("useSessionTurnHistory partial bootstrap", () => {
  test("进入新的会话 scope 会先丢弃旧缓存，避免水合已失效 Turn", async () => {
    const apiPort = 9114;
    installWindow(apiPort);
    let currentState = appState();
    currentState.turnTimelinesBySession.set(
      SCOPE_KEY,
      upsertTurns(
        createSessionTurnTimeline(SCOPE_KEY),
        [turnDetail(1)],
      ),
    );
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential_scope_reset",
            data: { token: "turn-history-scope-reset-token" },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_scope_reset",
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

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(timeline?.orderedTurnIds).toEqual([]);
    expect(timeline?.turnsById.job_latest).toBeUndefined();
    act(() => renderer!.unmount());
  });

  test("轮询直到 ready，且 epoch 切换会重新水合相同 revision 的最新 Turn", async () => {
    const apiPort = 9107;
    installWindow(apiPort);
    let currentState = appState();
    let bootstrapCalls = 0;
    let bootstrapInFlight = 0;
    let maxBootstrapInFlight = 0;
    let detailCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential",
            data: { token: "turn-history-test-token" },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          bootstrapCalls += 1;
          bootstrapInFlight += 1;
          maxBootstrapInFlight = Math.max(maxBootstrapInFlight, bootstrapInFlight);
          await wait(10);
          bootstrapInFlight -= 1;
          const payload = bootstrapCalls === 1
            ? bootstrap("partial", 1)
            : bootstrap("ready", 2);
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_bootstrap_${bootstrapCalls}`,
            data: payload,
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          detailCalls += 1;
          const detailRequestNumber = detailCalls;
          if (detailRequestNumber === 1) {
            await wait(330);
          }
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_details_${detailRequestNumber}`,
            data: {
              items: [turnDetail(detailRequestNumber)],
              projection_epoch: detailRequestNumber,
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
      await wait(360);
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(bootstrapCalls).toBe(2);
    expect(maxBootstrapInFlight).toBe(1);
    expect(detailCalls).toBe(2);
    expect(timeline?.projectionState).toBe("ready");
    expect(timeline?.projectionEpoch).toBe(2);
    expect(timeline?.turnsById.job_latest.items_view).toBe("full");
    expect(
      timeline?.turnsById.job_latest
      && "final_response" in timeline.turnsById.job_latest
        ? timeline.turnsById.job_latest.final_response
        : null,
    ).toBe("epoch 2 完整回复");
    expect(currentState.status).toBe("最新 Turn 已加载");
    act(() => renderer!.unmount());
  });

  test("卸载会清除 partial 的下一次轮询 timer", async () => {
    const apiPort = 9108;
    installWindow(apiPort);
    let currentState = appState();
    let bootstrapCalls = 0;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential_cleanup",
            data: { token: "turn-history-cleanup-token" },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          bootstrapCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_partial_${bootstrapCalls}`,
            data: { ...bootstrap("partial", 1), latest_turn: null },
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
    act(() => renderer!.unmount());
    await wait(partialBootstrapPollDelay(0) + 50);

    expect(bootstrapCalls).toBe(1);
  });

  test("重新加载会取消旧 scope 的详情和历史分页请求", async () => {
    const apiPort = 9113;
    installWindow(apiPort);
    let currentState = appState();
    let history: ReturnType<typeof useSessionTurnHistory> | null = null;
    let bootstrapCalls = 0;
    const requestSignals: {
      detail: AbortSignal | null;
      page: AbortSignal | null;
    } = { detail: null, page: null };
    let detailAbortObserved = false;
    let pageAbortObserved = false;

    const pendingUntilAbort = (
      signal: AbortSignal | null,
      onAbort: () => void,
    ): Promise<Response> => {
      if (!signal) throw new Error("Turn 历史请求缺少 AbortSignal");
      return new Promise<Response>((_resolve, reject) => {
        const rejectAbort = () => {
          onAbort();
          reject(new DOMException("aborted", "AbortError"));
        };
        if (signal.aborted) {
          rejectAbort();
          return;
        }
        signal.addEventListener("abort", rejectAbort, { once: true });
      });
    };

    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential_abort",
            data: { token: "turn-history-abort-token" },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          bootstrapCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: `req_bootstrap_abort_${bootstrapCalls}`,
            data: { ...bootstrap("ready", 1), latest_turn: null },
          });
        }
        if (
          path === `/api/v1/sessions/${SESSION_ID}/history`
          && JSON.parse(String(init?.body ?? "{}")).direction === "before"
        ) {
          requestSignals.page = init?.signal ?? null;
          return pendingUntilAbort(requestSignals.page, () => {
            pageAbortObserved = true;
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          requestSignals.detail = init?.signal ?? null;
          return pendingUntilAbort(requestSignals.detail, () => {
            detailAbortObserved = true;
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness({ reloadNonce }: { reloadNonce: number }): React.ReactNode {
      history = useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness reloadNonce={0} />);
      await wait(30);
    });
    let detailRequest: Promise<void>;
    let pageRequest: Promise<void>;
    await act(async () => {
      detailRequest = history!.loadTurnDetails(["job_latest"]);
      pageRequest = history!.loadOlderTurns();
      await wait(20);
    });
    expect(requestSignals.detail?.aborted).toBe(false);
    expect(requestSignals.page?.aborted).toBe(false);

    await act(async () => {
      renderer!.update(<Harness reloadNonce={1} />);
      await wait(30);
    });
    await Promise.all([detailRequest!, pageRequest!]);

    expect(requestSignals.detail?.aborted).toBe(true);
    expect(requestSignals.page?.aborted).toBe(true);
    expect(detailAbortObserved).toBe(true);
    expect(pageAbortObserved).toBe(true);
    expect(bootstrapCalls).toBe(2);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.error).toBeNull();
    act(() => renderer!.unmount());
  });

  test("partial 轮询采用封顶的指数退避", () => {
    expect(partialBootstrapPollDelay(0)).toBe(250);
    expect(partialBootstrapPollDelay(1)).toBe(500);
    expect(partialBootstrapPollDelay(2)).toBe(1_000);
    expect(partialBootstrapPollDelay(20)).toBe(2_000);
  });
});
