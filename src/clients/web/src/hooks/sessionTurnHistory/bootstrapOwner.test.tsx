import { afterEach, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import {
  SESSION_ID, THREAD_ID, SCOPE_KEY, WORKSPACE_KEY, appState, emptyPendingResponse, bootstrap,
  emptyHistoryPageResponse, installWindow, originalFetch, restoreTurnHistoryTestGlobals,
  turnDetail,
} from "./testFixtures";
import { useSessionTurnHistory } from "./useSessionTurnHistory";
import type { SessionTurnBootstrap } from "../../types/backend";
import type { SetAppState } from "../contentViewLoaderTypes";

afterEach(restoreTurnHistoryTestGlobals);

test.each(["ready", "partial"] as const)("宿主 %s bootstrap 只用于首次装载，后续校准重新读取权威状态", async (projectionState) => {
  const apiPort = 9153;
  installWindow(apiPort);
  let current = appState();
  let history!: ReturnType<typeof useSessionTurnHistory>;
  let bootstrapReads = 0;
  let historyReads = 0;
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
    if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "bootstrap-seed-token" } });
    if (path.endsWith("/pending-requests")) return emptyPendingResponse();
    if (path.endsWith("/bootstrap")) {
      bootstrapReads += 1;
      return Response.json({ code: 0, message: "ok", request_id: "req-bootstrap-refreshed",
        data: { ...bootstrap("ready", 2), latest_turn: null } });
    }
    if (path.endsWith("/history")) {
      historyReads += 1;
      return emptyHistoryPageResponse(`req-seeded-history-${historyReads}`, bootstrapReads === 0 ? 1 : 2);
    }
    throw new Error(`未声明的宿主 bootstrap 请求: ${path}`);
  }, { preconnect: originalFetch.preconnect });
  let resolveInitial!: (value: SessionTurnBootstrap) => void;
  const initialBootstrap = new Promise<SessionTurnBootstrap>(resolve => { resolveInitial = resolve; });
  const getCurrentTimeline = () => current.turnTimelinesBySession.get(SCOPE_KEY) ?? null;
  const setState: SetAppState = (update) => {
    current = typeof update === "function" ? update(current) : update;
  };
  function Harness(): React.ReactNode {
    history = useSessionTurnHistory({ initialBootstrap, apiPort, sessionId: SESSION_ID, threadId: THREAD_ID,
      workspaceId: WORKSPACE_KEY, sessionCacheKey: SCOPE_KEY, getCurrentTimeline, reloadNonce: 0, setState });
    return null;
  }
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<Harness />); });
    expect(bootstrapReads).toBe(0);
    expect(historyReads).toBe(0);
    expect(getCurrentTimeline()?.phase).toBe("bootstrapping");
    await act(async () => { resolveInitial({ ...bootstrap(projectionState, 1), latest_turn: null }); });
    expect(historyReads).toBe(1);
    expect(getCurrentTimeline()?.threadId).toBe(THREAD_ID);
    expect(getCurrentTimeline()?.projectionEpoch).toBe(1);
    await act(async () => {
      if (projectionState === "partial") await new Promise(resolve => globalThis.setTimeout(resolve, 275));
      else history.refreshTurnHistory();
    });
    expect(bootstrapReads).toBe(1);
    expect(historyReads).toBe(2);
    expect(getCurrentTimeline()?.projectionEpoch).toBe(2);
  } finally {
    if (renderer) act(() => renderer.unmount());
  }
});

test.each(["manual", "event"] as const)("%s 历史校准清空旧窗口但始终保留已确认的 thread owner", async (trigger) => {
  const apiPort = 9152;
  installWindow(apiPort);
  let current = appState();
  let history!: ReturnType<typeof useSessionTurnHistory>;
  let resolveBootstrap!: (response: Response) => void;
  let bootstrapReached!: () => void;
  let reached = new Promise<void>((resolve) => { bootstrapReached = resolve; });
  const owners: Array<string | null | undefined> = [];
  let recordOwners = false;
  let historyReads = 0;
  globalThis.fetch = Object.assign(async (...args: Parameters<typeof fetch>) => {
    const path = new URL(String(args[0]), `http://127.0.0.1:${apiPort}`).pathname;
    if (path === "/api/gateway/auth/local-credential") return Response.json({ data: { token: "bootstrap-owner-token" } });
    if (path.endsWith("/pending-requests")) return emptyPendingResponse();
    if (path.endsWith("/bootstrap")) return new Promise<Response>((resolve) => {
      resolveBootstrap = resolve;
      bootstrapReached();
    });
    if (path.endsWith("/history")) {
      historyReads += 1;
      return emptyHistoryPageResponse("req-owner-history");
    }
    throw new Error(`未声明的 owner 校准请求: ${path}`);
  }, { preconnect: originalFetch.preconnect });
  const respond = (threadId: string) => resolveBootstrap(Response.json({
    code: 0, message: "ok", request_id: "req-owner-bootstrap",
    data: { ...bootstrap("ready", 1), thread_id: threadId, latest_turn: null },
  }));
  const getCurrentTimeline = () => current.turnTimelinesBySession.get(SCOPE_KEY) ?? null;
  function Harness({ reloadNonce = 0 }: { reloadNonce?: number }): React.ReactNode {
    history = useSessionTurnHistory({ apiPort, sessionId: SESSION_ID, workspaceId: WORKSPACE_KEY,
      sessionCacheKey: SCOPE_KEY, getCurrentTimeline, reloadNonce,
      setState: (update) => {
        current = typeof update === "function" ? update(current) : update;
        if (recordOwners) owners.push(getCurrentTimeline()?.threadId);
      } });
    return null;
  }
  let renderer!: ReactTestRenderer;
  try {
    await act(async () => { renderer = create(<Harness />); await reached; });
    await act(async () => { respond(THREAD_ID); });
    expect(getCurrentTimeline()?.threadId).toBe(THREAD_ID);
    expect(historyReads).toBe(1);
    const prior = getCurrentTimeline()!;
    current.turnTimelinesBySession.set(SCOPE_KEY, {
      ...prior, orderedTurnIds: ["job_latest"], turnsById: { job_latest: turnDetail(1) },
      beforeCursor: "stale-cursor", hasBefore: true,
    });
    recordOwners = true;
    reached = new Promise<void>((resolve) => { bootstrapReached = resolve; });
    await act(async () => {
      if (trigger === "manual") renderer.update(<Harness reloadNonce={1} />);
      else history.refreshTurnHistory();
      await reached;
    });
    expect(getCurrentTimeline()?.phase).toBe("bootstrapping");
    expect(getCurrentTimeline()?.orderedTurnIds).toEqual([]);
    expect(getCurrentTimeline()?.beforeCursor).toBeNull();
    expect(getCurrentTimeline()?.threadId).toBe(THREAD_ID);
    // 同一 Session 的 main thread 不可变；后续 bootstrap 不能借刷新替换 owner。
    await act(async () => { respond("thr_foreign_bootstrap"); });
    expect(getCurrentTimeline()?.error).toContain("thread owner");
    expect(getCurrentTimeline()?.threadId).toBe(THREAD_ID);
    expect(historyReads).toBe(1);
    expect(owners.length).toBeGreaterThan(0);
    expect(owners.every((owner) => owner === THREAD_ID)).toBe(true);
  } finally {
    if (renderer) act(() => renderer.unmount());
  }
});
