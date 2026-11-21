import { expect, mock, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import { createSessionTurnTimeline } from "../../state/session/turnTimeline";
import { createMessageStreamState } from "../../state/messageStream/state";
import type { AppState } from "../../types/frontend";
import { useSessionJobTurnLoader } from "./useSessionTurnTimeline";
import type { useSessionTurnHistory } from "../sessionTurnHistory/useSessionTurnHistory";

test("运行中派发新调用时读取标记元数据，不依赖 Turn revision；sibling 调用不触发本 Thread", async () => {
  const scope = "workspace::session", turnId = "turn-active", threadId = "thread-main";
  const timeline = { ...createSessionTurnTimeline(scope, 1), threadId, phase: "ready" as const,
    turnsById: { [turnId]: { turn_id: turnId, revision: 1 } } };
  const main = { ...createMessageStreamState("session", turnId, "stream-main", scope, threadId),
    currentModelCallId: "call-first" };
  const sibling = { ...createMessageStreamState("session", turnId, "stream-child", scope, "thread-child"),
    currentModelCallId: "call-child" };
  let state = {
    activeJobIdsBySession: new Map([[scope, "job-active"]]),
    pendingConversations: new Map([[scope, [{ jobId: "job-active", turnId }]]]),
    turnTimelinesBySession: new Map([[scope, timeline]]),
    // sibling 故意排在前面，不能把同 Session 的其它 Thread 当成当前调用。
    messageStreamsByTurnStream: new Map([["child", sibling], ["main", main]]),
  } as unknown as AppState;
  const latest = { current: state };
  const load = mock(async (..._args: Parameters<ReturnType<typeof useSessionTurnHistory>["loadTurnDetails"]>) => {});
  const refresh = mock(() => {}), setState = mock(() => {});
  function Harness({ snapshot }: { snapshot: AppState }) {
    useSessionJobTurnLoader({ state: snapshot, latestStateRef: latest, sessionCacheKey: scope,
      loadTurnDetails: load, refreshTurnHistory: refresh, setState });
    return null;
  }
  let renderer!: ReturnType<typeof create>;
  try {
    await act(async () => { renderer = create(<Harness snapshot={state} />); });
    expect(load.mock.calls).toEqual([[[turnId], `active-turn:${turnId}:call-first`, false, ["metadata", "user"]]]);
    state = { ...state, messageStreamsByTurnStream: new Map([
      ["child", { ...sibling, currentModelCallId: "call-child-new" }], ["main", main],
    ]) };
    await act(async () => { renderer.update(<Harness snapshot={state} />); });
    expect(load).toHaveBeenCalledTimes(1);
    state = { ...state, messageStreamsByTurnStream: new Map([
      ["child", sibling], ["main", { ...main, currentModelCallId: "call-second" }],
    ]) };
    await act(async () => { renderer.update(<Harness snapshot={state} />); });
    expect(load.mock.calls[1]).toEqual([[turnId], `active-turn:${turnId}:call-second`, false, ["metadata", "user"]]);
    expect(load).toHaveBeenCalledTimes(2);
    expect(timeline.turnsById[turnId].revision).toBe(1);
    expect(refresh).not.toHaveBeenCalled();
    expect(setState).not.toHaveBeenCalled();
  } finally {
    act(() => renderer.unmount());
  }
});
