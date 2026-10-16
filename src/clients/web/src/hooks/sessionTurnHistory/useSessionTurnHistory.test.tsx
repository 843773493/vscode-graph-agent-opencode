import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { partialBootstrapPollDelay } from "./bootstrap";
import {
  createSessionTurnTimeline,
  upsertTurns,
} from "../../state/session/turnTimeline";
import {
  HISTORY_SOURCE_BINDING,
  SESSION_ID,
  THREAD_ID,
  SCOPE_KEY,
  WORKSPACE_ID,
  appState,
  bootstrap,
  emptyHistoryPageResponse,
  installWindow,
  originalFetch,
  restoreTurnHistoryTestGlobals,
  turnDetail,
  wait,
} from "./testFixtures";
import { useSessionTurnHistory } from "./useSessionTurnHistory";
import { getConversationsForSession } from "../../state/conversations";

afterEach(() => {
  restoreTurnHistoryTestGlobals();
});

describe("useSessionTurnHistory partial bootstrap", () => {
  test.each([THREAD_ID, "thr_foreign_page"])("真实 bootstrap/page owner 经 %s 响应进入投影或明确拒绝", async (pageOwner) => {
    const apiPort = 9120;
    installWindow(apiPort);
    let current = appState();
    const paths: string[] = [];
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
      if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "history-owner-token" } });
      paths.push(path);
      if (path.endsWith("/bootstrap")) return Response.json({ code: 0, message: "ok", request_id: "req_owner_bootstrap",
        data: { ...bootstrap("ready", 1), latest_turn: null } });
      if (path.endsWith("/history")) return Response.json({ code: 0, message: "ok", request_id: "req_owner_page",
        data: { thread_id: pageOwner, ...HISTORY_SOURCE_BINDING,
          projection_epoch: 1, items: [turnDetail(1)], internal_executions: [] } });
      throw new Error(`未声明的 owner 验证请求: ${path}`);
    }, { preconnect: originalFetch.preconnect });
    function Harness(): React.ReactNode {
      useSessionTurnHistory({ apiPort, sessionId: SESSION_ID, workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY, getCurrentTimeline: () => current.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
        reloadNonce: 0, setState: (update) => { current = typeof update === "function" ? update(current) : update; } });
      return null;
    }
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<Harness />); await wait(60); });
    const timeline = current.turnTimelinesBySession.get(SCOPE_KEY)!;
    expect(paths).toContain(`/api/v1/sessions/${SESSION_ID}/bootstrap`);
    expect(paths).toContain(`/api/v1/sessions/${SESSION_ID}/history`);
    expect(timeline.threadId).toBe(THREAD_ID);
    if (pageOwner === THREAD_ID) {
      expect(getConversationsForSession(SESSION_ID, current, SCOPE_KEY)[0]?.assistantMessages?.[0]?.thread_id).toBe(THREAD_ID);
      expect(timeline.error).toBeNull();
    } else {
      expect(timeline.orderedTurnIds).toEqual([]);
      expect(timeline.error).toContain("thread owner");
    }
    act(() => renderer.unmount());
  });

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
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_internal_only_history",
            data: {
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [],
              internal_executions: [{
                execution_id: "execution-internal-only",
                job_id: "job-internal-only",
                session_id: SESSION_ID,
                thread_id: THREAD_ID,
                status: "completed",
                created_at: "2026-07-28T00:00:00Z",
                updated_at: "2026-07-28T00:00:00Z",
                display_parts: [{
                  kind: "generated_session_result",
                  text: "子会话结果",
                  item_id: "item-internal-only",
                  item_sequence: 1,
                  logical_item_ordinal: 0,
                  created_at: "2026-07-28T00:00:00Z",
                  provenance: {},
                }],
                response_parts: [],
              }],
              projection_epoch: 1,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness({ reloadNonce = 0 }: { reloadNonce?: number }): React.ReactNode {
      useSessionTurnHistory({
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
      renderer = create(<Harness />);
      await wait(30);
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(timeline?.orderedTurnIds).toEqual([]);
    expect(timeline?.turnsById.job_latest).toBeUndefined();
    expect(timeline?.internalExecutions.map(({ execution_id }) => execution_id)).toEqual([
      "execution-internal-only",
    ]);

    // 显式刷新也必须从空 context window 开始，不能把已经失效的旧 Turn
    // 带回下一次详情水合。
    currentState.turnTimelinesBySession.set(
      SCOPE_KEY,
      upsertTurns(createSessionTurnTimeline(SCOPE_KEY), [turnDetail(1)]),
    );
    await act(async () => {
      renderer!.update(<Harness reloadNonce={1} />);
      await wait(30);
    });
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.orderedTurnIds)
      .toEqual([]);
    act(() => renderer!.unmount());
  });

  test("切换到已就绪的历史缓存不再请求 bootstrap", async () => {
    const apiPort = 9116;
    const cachedWorkspaceId = "workspace_cached_history";
    const cachedScopeKey = `${cachedWorkspaceId}::${SESSION_ID}`;
    installWindow(apiPort);
    let currentScopeKey = SCOPE_KEY;
    let currentState = appState();
    let bootstrapCalls = 0;
    currentState.turnTimelinesBySession.set(
      cachedScopeKey,
      {
        ...upsertTurns(
          createSessionTurnTimeline(cachedScopeKey),
          [turnDetail(1)],
        ),
        phase: "ready",
        eventCursor: "event-cached",
        projectionEpoch: 1,
        projectionState: "ready",
      },
    );
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_credential_cached_history",
            data: { token: "turn-history-cached-token" },
          });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_current_cached_history",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
          bootstrapCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_bootstrap_cached_history",
            data: { ...bootstrap("ready", 1), latest_turn: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return emptyHistoryPageResponse("req_history_cached_history");
        }
        throw new Error(`缓存命中测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness({ scopeKey, workspaceId }: {
      scopeKey: string;
      workspaceId: string;
    }): React.ReactNode {
      useSessionTurnHistory({
        apiPort,
        sessionId: SESSION_ID,
        workspaceId,
        sessionCacheKey: scopeKey,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(currentScopeKey) ?? null,
        reloadNonce: 0,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    await act(async () => {
      renderer = create(<Harness scopeKey={SCOPE_KEY} workspaceId={WORKSPACE_ID} />);
      await wait(80);
    });
    expect(bootstrapCalls).toBe(1);

    currentScopeKey = cachedScopeKey;
    await act(async () => {
      renderer!.update(
        <Harness scopeKey={cachedScopeKey} workspaceId={cachedWorkspaceId} />,
      );
      await wait(80);
    });

    expect(bootstrapCalls).toBe(1);
    expect(currentState.turnTimelinesBySession.get(cachedScopeKey)?.phase).toBe("ready");
    expect(currentState.status).toBe("已从本地缓存恢复会话");
    act(() => renderer!.unmount());
  });

  test.each([true, false])("around取消%s时切owner再回访缓存仍可双向加载，旧响应不清新请求", async (settlesOnAbort) => {
    const apiPort = 9150;
    const otherWorkspaceId = "workspace-return-history";
    const otherScopeKey = `${otherWorkspaceId}::${SESSION_ID}`;
    installWindow(apiPort);
    let currentScopeKey = SCOPE_KEY;
    let currentState = appState();
    currentState.turnTimelinesBySession.set(otherScopeKey, {
      ...createSessionTurnTimeline(otherScopeKey), threadId: "thread-other-cached",
      phase: "ready", projectionEpoch: 1,
    });
    let history!: ReturnType<typeof useSessionTurnHistory>;
    let bootstrapCalls = 0;
    let resolveAround!: (response: Response) => void;
    let markAroundReached!: () => void;
    const aroundReached = new Promise<void>((resolve) => { markAroundReached = resolve; });
    let aroundSignal: AbortSignal | null | undefined;
    let resolveAfter!: (response: Response) => void;
    let markAfterReached!: () => void;
    const afterReached = new Promise<void>((resolve) => { markAfterReached = resolve; });
    const directionalRequests: Array<Record<string, unknown>> = [];
    const pageResponse = () => Response.json({ code: 0, message: "ok", request_id: "request-return-page", data: {
      thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING, items: [], internal_executions: [], projection_epoch: 1,
    } });
    globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
      const [input, init] = args;
      const path = new URL(String(input), `http://127.0.0.1:${apiPort}`).pathname;
      if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "history-return-token" } });
      if (path === `/api/v1/sessions/${SESSION_ID}/bootstrap`) {
        bootstrapCalls += 1;
        return Response.json({ code: 0, message: "ok", request_id: "request-return-bootstrap", data: {
          ...bootstrap("ready", 1), latest_turn: null,
        } });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/threads/${THREAD_ID}/history`) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        if (body.anchor_turn_id === "job_latest") return Response.json({ code: 0, message: "ok", request_id: "request-return-around", data: {
          thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING, items: [turnDetail(1)], internal_executions: [], projection_epoch: 1,
          has_before: true, before_cursor: "before-return", has_after: true, after_cursor: "after-return",
        } });
        aroundSignal = init?.signal;
        return new Promise<Response>((resolve, reject) => {
          resolveAround = resolve;
          if (settlesOnAbort) aroundSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
          markAroundReached();
        });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        if (body.direction === "tail") return Response.json({ code: 0, message: "ok", request_id: "request-return-tail", data: {
          thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING, items: [turnDetail(1)], internal_executions: [], projection_epoch: 1,
          has_before: true, before_cursor: "before-return",
        } });
        directionalRequests.push(body);
        if (body.direction === "after") return new Promise<Response>((resolve) => { resolveAfter = resolve; markAfterReached(); });
        if (body.direction === "before") return pageResponse();
      }
      throw new Error(`回访缓存测试收到未预期请求: ${path}`);
    }, { preconnect: originalFetch.preconnect });
    function Harness({ scopeKey, workspaceId }: { scopeKey: string; workspaceId: string }): React.ReactNode {
      history = useSessionTurnHistory({ apiPort, sessionId: SESSION_ID, workspaceId, sessionCacheKey: scopeKey,
        getCurrentTimeline: () => currentState.turnTimelinesBySession.get(currentScopeKey) ?? null,
        reloadNonce: 0, setState: (update) => { currentState = typeof update === "function" ? update(currentState) : update; } });
      return null;
    }
    let renderer!: ReactTestRenderer;
    await act(async () => { renderer = create(<Harness scopeKey={SCOPE_KEY} workspaceId={WORKSPACE_ID} />); await wait(60); });
    await act(async () => { await history.loadAroundTurn("job_latest"); });
    const before = currentState.turnTimelinesBySession.get(SCOPE_KEY)!;
    expect(before.phase).toBe("ready");
    expect(before.orderedTurnIds).toEqual(["job_latest"]);
    let aroundOutcome!: Promise<unknown>;
    await act(async () => {
      aroundOutcome = history.loadAroundTurn("turn-old-target").then(() => null, (error: unknown) => error);
      await aroundReached;
    });
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(true);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingAfter).toBe(true);
    currentScopeKey = otherScopeKey;
    currentState.currentSessionWorkspaceId = otherWorkspaceId;
    await act(async () => { renderer.update(<Harness scopeKey={otherScopeKey} workspaceId={otherWorkspaceId} />); await wait(0); });
    expect(aroundSignal?.aborted).toBe(true);
    expect(currentState.turnTimelinesBySession.get(otherScopeKey)?.generation).toBe(2);
    if (settlesOnAbort) {
      expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(false);
      expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingAfter).toBe(false);
    }
    currentScopeKey = SCOPE_KEY;
    currentState.currentSessionWorkspaceId = WORKSPACE_ID;
    await act(async () => { renderer.update(<Harness scopeKey={SCOPE_KEY} workspaceId={WORKSPACE_ID} />); await wait(0); });
    const returned = currentState.turnTimelinesBySession.get(SCOPE_KEY)!;
    expect(bootstrapCalls).toBe(1);
    expect(returned.generation).toBe(3);
    expect(returned.phase).toBe("ready");
    expect(returned.loadingBefore).toBe(false);
    expect(returned.loadingAfter).toBe(false);
    expect(returned.turnsById).toBe(before.turnsById);
    expect(returned.summaryTurnsById).toBe(before.summaryTurnsById);
    expect(returned.detailAccessOrder).toBe(before.detailAccessOrder);
    let afterOutcome!: Promise<void>;
    await act(async () => { afterOutcome = history.loadNewerTurns(); await afterReached; });
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingAfter).toBe(true);
    await act(async () => { resolveAround(pageResponse()); await aroundOutcome; });
    expect(await aroundOutcome).toBeInstanceOf(Error);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.generation).toBe(3);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingAfter).toBe(true);
    await act(async () => { resolveAfter(pageResponse()); await afterOutcome; await history.loadOlderTurns(); });
    expect(directionalRequests).toEqual([
      { direction: "after", cursor: "after-return", turns: 3 },
      { direction: "before", cursor: "before-return", turns: 3 },
    ]);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(false);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.loadingAfter).toBe(false);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.orderedTurnIds).toEqual(before.orderedTurnIds);
    act(() => renderer.unmount());
  });

  test("轮询直到 ready，且 epoch 切换会重新水合相同 revision 的最新 Turn", async () => {
    const apiPort = 9107;
    installWindow(apiPort);
    let currentState = appState();
    let bootstrapCalls = 0;
    let bootstrapInFlight = 0;
    let maxBootstrapInFlight = 0;
    let detailCalls = 0;
    let messageStreamAvailabilityCalls = 0;
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
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_user",
            data: { user_id: "user_turn_history", display_name: "Turn History" },
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
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [turnDetail(detailRequestNumber)],
              projection_epoch: detailRequestNumber,
            },
          });
        }
        if (path.endsWith("/message-streams/availability")) {
          messageStreamAvailabilityCalls += 1;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_availability",
            data: {},
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
      await wait(360);
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(bootstrapCalls).toBe(2);
    expect(maxBootstrapInFlight).toBe(1);
    expect(detailCalls).toBe(2);
    expect(messageStreamAvailabilityCalls).toBe(0);
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
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          return emptyHistoryPageResponse("req_history_partial_cleanup");
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
          && JSON.parse(String(init?.body ?? "{}")).direction === "tail"
        ) {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "req_abort_initial_history",
            data: {
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [],
              internal_executions: [],
              before_cursor: "cursor_abort_before",
              has_before: true,
              projection_epoch: 1,
            },
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
