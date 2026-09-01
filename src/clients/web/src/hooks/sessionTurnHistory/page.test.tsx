import { afterEach, describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createSessionTurnTimeline } from "../../state/session/turnTimeline";
import type { AppState } from "../../types/frontend";
import { useInitialTurnLoader, useOlderTurnLoader } from "./page";

const originalFetch = globalThis.fetch;
const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
const SESSION_ID = "session-page-epoch";
const WORKSPACE_ID = "workspace-page-epoch";
const SCOPE_KEY = `${WORKSPACE_ID}::${SESSION_ID}`;

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

function state(): AppState {
  return {
    turnTimelinesBySession: new Map([[SCOPE_KEY, {
      ...createSessionTurnTimeline(SCOPE_KEY, 1),
      phase: "ready",
      projectionEpoch: 2,
      olderCursor: "cursor-epoch-2",
      hasMore: true,
    }]]),
    sessionHistoryReloadNonce: 0,
    status: "",
  } as AppState;
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

async function runPageCase({
  port,
  response,
  abortResponse = false,
  reactState = false,
}: {
  port: number;
  response: () => Response;
  abortResponse?: boolean;
  reactState?: boolean;
}): Promise<AppState> {
  installWindow(port);
  let currentState = state();
  let loadOlder: (() => Promise<void>) | null = null;
  let abortRequest: (() => void) | null = null;
  globalThis.fetch = Object.assign(
    async (...args: Parameters<typeof fetch>) => {
      const path = new URL(String(args[0]), `http://127.0.0.1:${port}`).pathname;
      if (path === "/api/gateway/auth/local-credential") {
        return Response.json({ data: { token: `token-${port}` } });
      }
      if (path === "/api/gateway/users/current") {
        return Response.json({ data: { kind: "guest", user_id: null } });
      }
      if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
        if (abortResponse) abortRequest?.();
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
          return Response.json({ data: { kind: "guest", user_id: null } });
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
          return Response.json({ data: { kind: "guest", user_id: null } });
        }
        if (path === `/api/v1/sessions/${SESSION_ID}/history`) {
          requestBody = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
          return Response.json({
            code: 0,
            message: "ok",
            request_id: "request-initial-history",
            data: {
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
          return Response.json({ data: { kind: "guest", user_id: null } });
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
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 1,
        },
      }),
    });

    const timeline = result.turnTimelinesBySession.get(SCOPE_KEY);
    expect(timeline?.projectionEpoch).toBe(2);
    expect(timeline?.hasMore).toBe(true);
    expect(timeline?.loadingOlder).toBe(false);
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
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingOlder).toBe(false);
  });

  test("请求被取消时也必须清除 loadingOlder", async () => {
    const result = await runPageCase({
      port: 9113,
      abortResponse: true,
      response: () => Response.json({
        code: 0,
        message: "ok",
        request_id: "request-page-aborted",
        data: {
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 2,
        },
      }),
    });

    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingOlder).toBe(false);
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
          items: [],
          next_cursor: null,
          has_more: false,
          projection_epoch: 2,
        },
      }),
    });

    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.hasMore).toBe(false);
    expect(result.turnTimelinesBySession.get(SCOPE_KEY)?.loadingOlder).toBe(false);
  });
});
