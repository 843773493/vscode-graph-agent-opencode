import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createSessionTurnTimeline } from "../../state/session/turnTimeline";
import {
  applyTurnBootstrap,
  applyTurnHistoryPage,
  beginTurnBootstrap,
} from "../../state/session/turnTimeline";
import type { AppState } from "../../types/frontend";
import type { TurnHistoryPage } from "../../types/backend";
import { useAroundTurnLoader, useInitialTurnLoader, useOlderTurnLoader } from "./page";
import { turnDetail } from "./testFixtures";
import { restoreGlobalDescriptor } from "../../tests/testGlobals";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const SESSION_ID = "session-page-epoch";
const THREAD_ID = "thr_page_current_target";
const WORKSPACE_ID = "workspace-page-epoch";
const SCOPE_KEY = `${WORKSPACE_ID}::${SESSION_ID}`;
const HISTORY_SOURCE_BINDING = { source_view_id: "view-page-current", source_view_revision: 1 } as const;
let aroundRenderer: ReactTestRenderer | null = null;

function installWindow(port: number): void {
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: {
      location: { port: String(port) },
      setTimeout: globalThis.setTimeout.bind(globalThis),
      clearTimeout: globalThis.clearTimeout.bind(globalThis),
    },
  });
}

function state(timeline?: ReturnType<typeof createSessionTurnTimeline>): AppState {
  return {
    turnTimelinesBySession: new Map([[SCOPE_KEY, {
      ...(timeline ?? {
        ...createSessionTurnTimeline(SCOPE_KEY, 1),
        threadId: THREAD_ID,
        phase: "ready",
        projectionEpoch: 2,
        beforeCursor: "cursor-epoch-2",
        hasBefore: true,
      }),
    }]]),
    sessionHistoryReloadNonce: 0,
    status: "",
  } as AppState;
}

afterEach(() => {
  if (aroundRenderer) act(() => aroundRenderer?.unmount());
  aroundRenderer = null;
  globalThis.fetch = originalFetch;
  restoreGlobalDescriptor("window", originalWindowDescriptor);
});

interface AroundRequest {
  path: string;
  body: Record<string, unknown>;
  signal: AbortSignal | null | undefined;
  headers: Headers;
}

function aroundPage(turnId: string, overrides: Partial<TurnHistoryPage> = {}): TurnHistoryPage {
  return {
    thread_id: THREAD_ID,
    ...HISTORY_SOURCE_BINDING,
    items: [{ ...turnDetail(2), ...HISTORY_SOURCE_BINDING, turn_id: turnId, job_id: turnId, session_id: SESSION_ID }],
    summaries: [], internal_executions: [], projection_epoch: 2,
    has_before: false, has_after: true, after_cursor: "after-anchor",
    ...overrides,
  };
}

function aroundResponse(page: TurnHistoryPage): Response {
  return Response.json({ code: 0, message: "ok", request_id: "request-around", data: page });
}

function deferredHistory() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((yes) => { resolve = yes; });
  return { promise, resolve };
}

function aroundHarness(port: number, respond: (request: AroundRequest) => Response | Promise<Response>) {
  installWindow(port);
  let currentState = state();
  let load!: ReturnType<typeof useAroundTurnLoader>;
  const generationRef = { current: 1 };
  const controller = new AbortController();
  const requests: AroundRequest[] = [];
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const [input, init] = args;
    const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
    if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: `token-${port}` } });
    if (path === "/api/gateway/users/current") {
      return Response.json({ code: 0, message: "ok", request_id: "request-around-user", data: { kind: "guest", user_id: null } });
    }
    if (path !== `/api/v1/sessions/${SESSION_ID}/threads/${THREAD_ID}/history`) {
      throw new Error(`测试收到未预期请求: ${path}`);
    }
    const request = { path, body: JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>,
      signal: init?.signal, headers: new Headers(init?.headers) };
    requests.push(request);
    return respond(request);
  }, { preconnect: originalFetch.preconnect });
  function Harness() {
    load = useAroundTurnLoader({ apiPort: port, sessionId: SESSION_ID, workspaceId: WORKSPACE_ID, sessionCacheKey: SCOPE_KEY,
      getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
      generationRef, requestSignal: controller.signal,
      setState: (update) => { currentState = typeof update === "function" ? update(currentState) : update; },
    });
    return null;
  }
  act(() => { aroundRenderer = create(<Harness />); });
  return {
    load: (turnId: string, signal?: AbortSignal) => load(turnId, signal),
    requests, generationRef, controller,
    state: () => currentState,
    timeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY)!,
    replaceTimeline(timeline: ReturnType<typeof createSessionTurnTimeline>) {
      currentState = { ...currentState, turnTimelinesBySession: new Map([[SCOPE_KEY, timeline]]) };
    },
  };
}

describe("指定 Turn 的真实 history reader", () => {
  test("精确 thread around 请求和重复同 ID 都返回完整目标窗口", async () => {
    const harness = aroundHarness(9140, (request) => aroundResponse(aroundPage(String(request.body.anchor_turn_id))));
    await act(async () => { await harness.load("turn-anchor"); await harness.load("turn-anchor"); });
    expect(harness.requests).toHaveLength(2);
    for (const request of harness.requests) {
      expect(request.path).toBe(`/api/v1/sessions/${SESSION_ID}/threads/${THREAD_ID}/history`);
      expect(request.body).toEqual({ direction: "around", anchor_turn_id: "turn-anchor", before_turns: 3, after_turns: 3 });
      expect(request.headers.get("X-BoxTeam-Workspace-Id")).toBe(WORKSPACE_ID);
    }
    expect(harness.timeline().orderedTurnIds).toEqual(["turn-anchor"]);
    expect(harness.timeline().loadingBefore).toBe(false);
    expect(harness.timeline().loadingAfter).toBe(false);
    expect(harness.timeline().hasAfter).toBe(true);
    expect(harness.timeline().afterCursor).toBe("after-anchor");
  });

  test("后一次跳转取消前一次，迟到成功不能回写旧目标", async () => {
    const first = deferredHistory();
    let markFirstReached!: () => void;
    const firstReached = new Promise<void>((resolve) => { markFirstReached = resolve; });
    const harness = aroundHarness(9141, (request) => {
      if (request.body.anchor_turn_id === "turn-first") { markFirstReached(); return first.promise; }
      return aroundResponse(aroundPage("turn-second"));
    });
    let firstOutcome!: Promise<unknown>;
    await act(async () => {
      firstOutcome = harness.load("turn-first").then(() => null, (error: unknown) => error);
      await firstReached;
      await harness.load("turn-second");
    });
    expect(harness.requests[0].signal?.aborted).toBe(true);
    const afterSecond = harness.state();
    await act(async () => { first.resolve(aroundResponse(aroundPage("turn-first"))); await firstOutcome; });
    expect(await firstOutcome).toBeInstanceOf(Error);
    expect(harness.state()).toBe(afterSecond);
    expect(harness.timeline().orderedTurnIds).toEqual(["turn-second"]);
  });

  test("同generation后继around未完成时，旧请求取消清理不能释放新请求loading", async () => {
    const first = deferredHistory();
    const second = deferredHistory();
    const reached = [deferredHistory(), deferredHistory()];
    const harness = aroundHarness(9145, (request) => {
      const index = request.body.anchor_turn_id === "turn-first" ? 0 : 1;
      reached[index].resolve(new Response());
      return index === 0 ? first.promise : second.promise;
    });
    let firstOutcome!: Promise<unknown>;
    let secondOutcome!: Promise<void>;
    await act(async () => {
      firstOutcome = harness.load("turn-first").then(() => null, (error: unknown) => error);
      await reached[0].promise;
      secondOutcome = harness.load("turn-second");
      await reached[1].promise;
    });
    expect(harness.requests[0].signal?.aborted).toBe(true);
    await act(async () => { first.resolve(aroundResponse(aroundPage("turn-first"))); await firstOutcome; });
    expect(await firstOutcome).toBeInstanceOf(Error);
    expect(harness.timeline().loadingBefore).toBe(true);
    expect(harness.timeline().loadingAfter).toBe(true);
    expect(harness.timeline().orderedTurnIds).toEqual([]);
    await act(async () => { second.resolve(aroundResponse(aroundPage("turn-second"))); await secondOutcome; });
    expect(harness.timeline().orderedTurnIds).toEqual(["turn-second"]);
    expect(harness.timeline().loadingBefore).toBe(false);
    expect(harness.timeline().loadingAfter).toBe(false);
  });

  test("owner切换已推进generation后，取消仍清理原缓存请求的loading", async () => {
    const pending = deferredHistory();
    const reached = deferredHistory();
    const harness = aroundHarness(9146, () => { reached.resolve(new Response()); return pending.promise; });
    let outcome!: Promise<unknown>;
    await act(async () => {
      outcome = harness.load("turn-old").then(() => null, (error: unknown) => error);
      await reached.promise;
    });
    harness.generationRef.current = 2;
    harness.controller.abort();
    await act(async () => { pending.resolve(aroundResponse(aroundPage("turn-old"))); await outcome; });
    expect(await outcome).toBeInstanceOf(Error);
    expect(harness.timeline().generation).toBe(1);
    expect(harness.timeline().phase).toBe("ready");
    expect(harness.timeline().loadingBefore).toBe(false);
    expect(harness.timeline().loadingAfter).toBe(false);
    expect(harness.timeline().orderedTurnIds).toEqual([]);
  });

  test("切换 generation/thread 后迟到响应不改变新 owner", async () => {
    const pending = deferredHistory();
    let reached!: () => void;
    const requestReached = new Promise<void>((resolve) => { reached = resolve; });
    const harness = aroundHarness(9142, () => { reached(); return pending.promise; });
    let outcome!: Promise<unknown>;
    await act(async () => { outcome = harness.load("turn-old").then(() => null, (error: unknown) => error); await requestReached; });
    harness.generationRef.current = 2;
    harness.replaceTimeline({ ...createSessionTurnTimeline(SCOPE_KEY, 2), threadId: "thread-next", phase: "ready" });
    harness.controller.abort();
    const nextOwner = harness.state();
    await act(async () => { pending.resolve(aroundResponse(aroundPage("turn-old"))); await outcome; });
    expect(await outcome).toBeInstanceOf(Error);
    expect(harness.state()).toBe(nextOwner);
    expect(harness.timeline().threadId).toBe("thread-next");
    expect(harness.timeline().orderedTurnIds).toEqual([]);
  });

  test.each([
    ["目标缺失", { items: [] }, "未找到指定轮次"],
    ["线程不匹配", { thread_id: "thread-foreign" }, "thread owner"],
    ["较新投影", { projection_epoch: 3 }, "历史版本已变化"],
    ["过期投影", { projection_epoch: 1 }, "历史版本已变化"],
  ] satisfies Array<[string, Partial<TurnHistoryPage>, string]>)("%s明确拒绝，不能以tail回退伪装跳转成功", async (_name, overrides, message) => {
    const harness = aroundHarness(9143, () => aroundResponse(aroundPage("turn-anchor", overrides)));
    await expect(harness.load("turn-anchor")).rejects.toThrow(message);
    expect(harness.requests).toHaveLength(1);
    expect(harness.timeline().orderedTurnIds).toEqual([]);
    expect(harness.timeline().error).toContain(message);
    expect(harness.timeline().loadingBefore).toBe(false);
    expect(harness.timeline().loadingAfter).toBe(false);
  });

  test("网络失败保留已有正文，并向调用方拒绝以显示跳转错误", async () => {
    const harness = aroundHarness(9144, () => { throw new TypeError("Failed to fetch"); });
    harness.replaceTimeline(applyTurnHistoryPage(harness.timeline(), aroundPage("turn-existing"), "around"));
    await expect(harness.load("turn-new")).rejects.toThrow();
    expect(harness.timeline().orderedTurnIds).toEqual(["turn-existing"]);
    expect(harness.timeline().error).toBeNull();
    expect(harness.timeline().loadingBefore).toBe(false);
    expect(harness.timeline().loadingAfter).toBe(false);
    expect(harness.state().status).toContain("可继续重试");
  });

  test("指定目标404只拒绝导航，保留ready历史、详情与游标供原事件流继续使用", async () => {
    const harness = aroundHarness(9147, () => Response.json({ detail: "rollout Turn 不存在: turn-missing", request_id: "request-missing" }, { status: 404 }));
    const before = applyTurnHistoryPage(harness.timeline(), aroundPage("turn-existing", {
      has_before: true, before_cursor: "before-existing",
    }), "around");
    harness.replaceTimeline(before);
    await expect(harness.load("turn-missing")).rejects.toMatchObject({ status: 404 });
    expect(harness.requests).toHaveLength(1);
    expect(harness.timeline()).toEqual(before);
    expect(harness.timeline().turnsById).toBe(before.turnsById);
    expect(harness.timeline().phase).toBe("ready");
    expect(harness.timeline().error).toBeNull();
    expect(harness.state().sessionHistoryReloadNonce).toBe(0);
  });
});

async function runPageCase({
  port,
  response,
  abortResponse = false,
  reactState = false,
  timeline,
  historyRequests,
}: {
  port: number;
  response: () => Response;
  abortResponse?: boolean;
  reactState?: boolean;
  timeline?: ReturnType<typeof createSessionTurnTimeline>;
  historyRequests?: { count: number };
}): Promise<AppState> {
  installWindow(port);
  let currentState = state(timeline);
  let loadOlder: (() => Promise<void>) | null = null;
  let abortRequest: (() => void) | null = null;
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), `http://127.0.0.1:${port}`).pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return Response.json({ data: { token: `token-${port}` } });
      }
      if (path === "/api/gateway/users/current") {
        return Response.json({
          code: 0,
          message: "ok",
          request_id: "request-page-current-user",
          data: { kind: "guest", user_id: null },
        });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
        if (abortResponse) abortRequest?.();
        if (historyRequests) historyRequests.count += 1;
        return response();
      }
      throw new Error(`测试收到未预期请求: ${path}`);
    },
    { preconnect: originalFetch.preconnect },
  );

  function Harness(): React.ReactNode {
    const [, setReactState] = React.useState(currentState);
    const generationRef = React.useRef(1);
    const requestController = React.useMemo(() => new AbortController(), []);
    abortRequest = () => requestController.abort();
    loadOlder = useOlderTurnLoader({
      apiPort: port,
      sessionId: SESSION_ID,
      workspaceId: WORKSPACE_ID,
      sessionCacheKey: SCOPE_KEY,
      getCurrentTimeline: () => currentState.turnTimelinesBySession.get(SCOPE_KEY) ?? null,
      generationRef,
      requestSignal: requestController.signal,
      setState: reactState
        ? (update) => {
            setReactState((previous) => {
              const next = typeof update === "function" ? update(previous) : update;
              currentState = next;
              return next;
            });
          }
        : (update) => {
            currentState = typeof update === "function" ? update(currentState) : update;
          },
    });
    return null;
  }

  let renderer: ReactTestRenderer;
  act(() => {
    renderer = create(<Harness />);
  });
  await act(async () => {
    await loadOlder!();
  });
  renderer!.unmount();
  return currentState;
}

describe("Turn 历史分页 epoch 协调", () => {
  test("Gateway 短暂网络切换后有限重试并清除瞬态错误", async () => {
    const port = 9117;
    installWindow(port);
    let currentState = state();
    let historyRequests = 0;
    let loadInitial: ((latestTurnId?: string) => Promise<void>) | null = null;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: `token-${port}` } });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-page-current-user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          historyRequests += 1;
          if (historyRequests === 1) {
            throw new TypeError("Failed to fetch");
          }
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-after-network-change",
            data: {
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [],
              before_cursor: null,
              has_before: false,
              projection_epoch: 2,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const generationRef = React.useRef(1);
      const requestController = React.useMemo(() => new AbortController(), []);
      loadInitial = useInitialTurnLoader({
        apiPort: port,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        generationRef,
        requestSignal: requestController.signal,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
        onMissingTurn: () => undefined,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await loadInitial!("job_latest");
    });

    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY);
    expect(historyRequests).toBe(2);
    expect(timeline?.loadingBefore).toBe(false);
    expect(timeline?.error).toBeNull();
    expect(currentState.status).toBe("");
    renderer!.unmount();
  });

  test("首次加载从尾部请求五个 Turn 并保存 before 游标", async () => {
    const port = 9115;
    installWindow(port);
    let currentState = state();
    let requestBody: unknown = null;
    let loadInitial: ((latestTurnId?: string) => Promise<void>) | null = null;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input, init] = args;
        const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: `token-${port}` } });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-page-current-user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          requestBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-initial-history",
            data: {
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [],
              before_cursor: "cursor-before-five",
              has_before: true,
              projection_epoch: 2,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const generationRef = React.useRef(1);
      const requestController = React.useMemo(() => new AbortController(), []);
      loadInitial = useInitialTurnLoader({
        apiPort: port,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        generationRef,
        requestSignal: requestController.signal,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
        onMissingTurn: () => undefined,
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await loadInitial!("job_latest");
    });

    expect(requestBody).toEqual({ direction: "tail", turns: 5 });
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.beforeCursor)
      .toBe("cursor-before-five");
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.hasBefore).toBe(true);
    renderer!.unmount();
  });

  test("旧历史位置的 409 不重复重试且不会移除活动 Turn", async () => {
    const port = 9116;
    installWindow(port);
    let currentState = state();
    const activeTurn = {
      turn_id: "job_latest",
      job_id: "job_latest",
      session_id: SESSION_ID,
      ordinal: 9,
      root_logical_item_ordinal: 90,
      ...HISTORY_SOURCE_BINDING,
      revision: 1,
      status: "running",
      created_at: "2026-08-30T18:00:00Z",
      updated_at: "2026-08-30T18:00:00Z",
      items_view: "summary",
    } as const;
    const timeline = currentState.turnTimelinesBySession.get(SCOPE_KEY)!;
    currentState.turnTimelinesBySession.set(SCOPE_KEY, {
      ...timeline,
      orderedTurnIds: [activeTurn.turn_id],
      turnsById: { [activeTurn.turn_id]: activeTurn },
    });
    let requestCount = 0;
    let missingTurnIds: string[] = [];
    let loadInitial: ((latestTurnId?: string) => Promise<void>) | null = null;
    globalThis.fetch = Object.assign(
      async (...args: Parameters<typeof fetch>) => {
        const [input] = args;
        const path = new URL(String(input), `http://127.0.0.1:${port}`).pathname;
        if (path === "/api/gateway/auth/local-credential") {
          return Response.json({ data: { token: `token-${port}` } });
        }
        if (path === "/api/gateway/users/current") {
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-page-current-user",
            data: { kind: "guest", user_id: null },
          });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          requestCount += 1;
          if (requestCount === 1) {
            return Response.json({
              detail: {
                code: "stale_turn_reference",
                session_id: SESSION_ID,
                turn_ids: [activeTurn.turn_id],
                message: "Turn 正在提交",
              },
            }, { status: 409, statusText: "Conflict" });
          }
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-after-conflict",
            data: {
              thread_id: THREAD_ID,
              ...HISTORY_SOURCE_BINDING,
              items: [],
              before_cursor: "cursor-after-conflict",
              has_before: false,
              projection_epoch: 2,
            },
          });
        }
        throw new Error(`测试收到未预期请求: ${path}`);
      },
      { preconnect: originalFetch.preconnect },
    );

    function Harness(): React.ReactNode {
      const generationRef = React.useRef(1);
      const requestController = React.useMemo(() => new AbortController(), []);
      loadInitial = useInitialTurnLoader({
        apiPort: port,
        sessionId: SESSION_ID,
        workspaceId: WORKSPACE_ID,
        sessionCacheKey: SCOPE_KEY,
        generationRef,
        requestSignal: requestController.signal,
        setState: (update) => {
          currentState = typeof update === "function" ? update(currentState) : update;
        },
        onMissingTurn: (turnIds) => {
          missingTurnIds = turnIds;
        },
      });
      return null;
    }

    let renderer: ReactTestRenderer;
    act(() => {
      renderer = create(<Harness />);
    });
    await act(async () => {
      await loadInitial!(activeTurn.turn_id);
    });

    expect(requestCount).toBe(1);
    expect(missingTurnIds).toEqual([]);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.orderedTurnIds)
      .toEqual([activeTurn.turn_id]);
    expect(currentState.turnTimelinesBySession.get(SCOPE_KEY)?.error).toBeNull();
    renderer!.unmount();
  });

  test("新 bootstrap 后迟到的旧 page 被丢弃且不抛错", async () => {
    const result = await runPageCase({
      port: 9110,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-older",
        data: {
          thread_id: THREAD_ID,
          ...HISTORY_SOURCE_BINDING,
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 1,
        },
      }),
    });

    const timeline = result.turnTimelinesBySession.get(SCOPE_KEY);
    expect(timeline?.projectionEpoch).toBe(2);
    expect(timeline?.hasBefore).toBe(true);
    expect(timeline?.loadingBefore).toBe(false);
    expect(result.sessionHistoryReloadNonce).toBe(0);
  });

  test("未来 page epoch 触发 bootstrap 校准而不合并", async () => {
    const result = await runPageCase({
      port: 9111,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-newer",
        data: {
          thread_id: THREAD_ID,
          ...HISTORY_SOURCE_BINDING,
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 3,
        },
      }),
    });

    expect(result.sessionHistoryReloadNonce).toBe(1);
    expect(result.status).toBe("Turn 投影已更新，正在重新加载");
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.projectionEpoch).toBe(2);
  });

  test("409 stale cursor 显式触发 bootstrap 校准", async () => {
    const result = await runPageCase({
      port: 9112,
      response: () => Response.json({
        detail: {
          code: "stale_turn_cursor",
          session_id: SESSION_ID,
          cursor_epoch: 1,
          current_epoch: 2,
          message: "cursor 已失效",
        },
      }, { status: 409, statusText: "Conflict" }),
    });

    expect(result.sessionHistoryReloadNonce).toBe(1);
    expect(result.status).toBe("Turn 历史游标已失效，正在重新校准");
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(false);
  });

  test("请求被取消时也必须清除 loadingBefore", async () => {
    const result = await runPageCase({
      port: 9113,
      abortResponse: true,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-aborted",
        data: {
          thread_id: THREAD_ID,
          ...HISTORY_SOURCE_BINDING,
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 2,
        },
      }),
    });

    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(false);
  });

  test("React 异步批处理 setState 时仍使用当前游标发起请求", async () => {
    const result = await runPageCase({
      port: 9114,
      reactState: true,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-react-state",
        data: {
          thread_id: THREAD_ID,
          ...HISTORY_SOURCE_BINDING,
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 2,
        },
      }),
    });

    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.hasBefore).toBe(false);
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingBefore).toBe(false);
  });

  test("around 触顶后 before 方向不再发起陈旧游标请求", async () => {
    // 用真实 reducer 构造 around 触顶后的状态：后端报 before_cursor=null、
    // has_before=false，表示窗口已包含最旧 Turn。
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY, 1), 1);
    timeline = applyTurnBootstrap(timeline, 1, {
      thread_id: THREAD_ID,
      session: {
        session_id: SESSION_ID,
        workspace_id: WORKSPACE_ID,
        title: "page 测试",
        current_agent_id: "default",
        created_at: "2026-07-28T00:00:00Z",
        updated_at: "2026-07-28T00:00:00Z",
      },
      latest_turn: null,
      active_jobs: [],
      older_cursor: "cursor-stale",
      event_cursor: "event-1",
      projection_epoch: 1,
    } as never);
    timeline = applyTurnHistoryPage(timeline, {
      thread_id: THREAD_ID,
      ...HISTORY_SOURCE_BINDING,
      items: [],
      summaries: [],
      next_cursor: null,
      has_more: false,
      before_cursor: null,
      after_cursor: "newer-1",
      has_before: false,
      has_after: true,
      projection_epoch: 1,
    } as never, "around");

    const historyRequests = { count: 0 };
    const result = await runPageCase({
      port: 9118,
      timeline,
      historyRequests,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-around-floor",
        data: { thread_id: THREAD_ID, ...HISTORY_SOURCE_BINDING,
          items: [], next_cursor: null, has_more: false, projection_epoch: 1 },
      }),
    });

    // around 触顶后 hasBefore/beforeCursor 都为空，loadOlder 必须直接短路，
    // 不得用陈旧值再发一次请求。
    expect(historyRequests.count).toBe(0);
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.beforeCursor).toBeNull();
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.hasBefore).toBe(false);
  });
});
