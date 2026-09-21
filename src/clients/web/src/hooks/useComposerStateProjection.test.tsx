import { describe, expect, test } from "bun:test";
import React, { Suspense, useRef, useState } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AppState, ConversationContentView } from "../types/frontend";
import { sessionScopeKey } from "../state/session/sessionScope";
import {
  createSessionTurnTimeline,
  type TurnRecord,
} from "../state/session/turnTimeline";
import type { ComposerStateSnapshot } from "../state/composerState";
import {
  useComposerStateProjection,
  type ComposerStateProjection,
  type ComposerStateProjectionInput,
} from "./useComposerStateProjection";

const WORKSPACE_ID = "workspace";
const SESSION_ID = "session";
const SCOPE_KEY = sessionScopeKey(WORKSPACE_ID, SESSION_ID);

type ComposerActionsInput = Omit<
  ComposerStateProjectionInput,
  "state" | "currentSessionCacheKey" | "latestStateRef"
>;

const noopActions = {
  setStatus: () => undefined,
  sendMessage: async () => undefined,
  compactSession: async () => ({} as never),
  refreshGoal: async () => undefined,
  updateGoal: async () => undefined,
  clearGoal: async () => undefined,
  interruptSession: async () => undefined,
  switchAgent: async () => undefined,
  switchModel: async () => undefined,
  refreshAgents: async () => undefined,
  setWorkspaceDefaultAgent: async () => undefined,
  setWorkspaceDefaultProvider: async () => undefined,
  switchContentView: () => undefined,
  createSession: async () => undefined,
  renameSession: async () => undefined,
  updateUiSettings: async () => undefined,
} as unknown as ComposerActionsInput;

/** 构造只关心本链路读取字段的 AppState。 */
function appState({
  sessionId = SESSION_ID as string | null,
  contentView = "default",
} = {}): AppState {
  return {
    apiPort: 8014,
    activeGatewayWorkspaceId: WORKSPACE_ID,
    currentSession: sessionId
      ? { session_id: sessionId, workspace_id: WORKSPACE_ID }
      : null,
    currentSessionWorkspaceId: WORKSPACE_ID,
    contentView,
    pendingConversations: new Map(),
    activeJobIdsBySession: new Map(),
    turnTimelinesBySession: new Map(),
  } as unknown as AppState;
}

function turn(turnId: string, fields: Partial<TurnRecord>): TurnRecord {
  return { turn_id: turnId, ...fields } as unknown as TurnRecord;
}

function timelineWith(turns: Array<[string, TurnRecord]>) {
  const timeline = createSessionTurnTimeline(SCOPE_KEY);
  timeline.orderedTurnIds = turns.map(([turnId]) => turnId);
  timeline.turnsById = Object.fromEntries(turns);
  return timeline;
}

/** 只负责把 hook 结果抛给测试，不参与断言。 */
function Harness({
  latestStateRef,
  currentSessionCacheKey,
  onRender,
}: {
  latestStateRef: { current: AppState };
  currentSessionCacheKey: string | null;
  onRender: (projection: ComposerStateProjection) => void;
}) {
  onRender(useComposerStateProjection({
    state: latestStateRef.current,
    currentSessionCacheKey,
    latestStateRef,
    ...noopActions,
  }));
  return null;
}

function mountHarness(
  latestState: AppState,
  currentSessionCacheKey: string | null = SCOPE_KEY,
): { latestStateRef: { current: AppState }; projection: () => ComposerStateProjection } {
  const latestStateRef = { current: latestState };
  let captured: ComposerStateProjection | null = null;
  act(() => {
    create(
      <Harness
        latestStateRef={latestStateRef}
        currentSessionCacheKey={currentSessionCacheKey}
        onRender={(projection) => { captured = projection; }}
      />,
    );
  });
  return {
    latestStateRef,
    projection: () => {
      if (!captured) {
        throw new Error("Harness 尚未渲染");
      }
      return captured;
    },
  };
}

describe("Composer 最新助手正文提取", () => {
  test("最新 turn 的 final_response 非空时返回它", () => {
    const state = appState();
    state.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { final_response: "更早的正文" })],
      ["turn_2", turn("turn_2", { final_response: "最新正文" })],
    ]));

    expect(mountHarness(state).projection().getLatestAssistantContent())
      .toBe("最新正文");
  });

  test("最新 turn 正文为空或纯空白时倒序回退到更早的非空正文", () => {
    const state = appState();
    state.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { final_response: "更早的正文" })],
      ["turn_2", turn("turn_2", { final_response: "   \n  " })],
      ["turn_3", turn("turn_3", {})],
    ]));

    expect(mountHarness(state).projection().getLatestAssistantContent())
      .toBe("更早的正文");
  });

  test("摘要 turn 无 final_response 字段时回退到 response_preview", () => {
    const state = appState();
    state.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { response_preview: "更早的摘要" })],
      ["turn_2", turn("turn_2", { response_preview: "最新摘要" })],
    ]));

    expect(mountHarness(state).projection().getLatestAssistantContent())
      .toBe("最新摘要");
  });

  test("全部为空或缺失时返回 null", () => {
    const state = appState();
    state.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { response_preview: "" })],
      ["turn_2", turn("turn_2", { final_response: "   " })],
    ]));

    expect(mountHarness(state).projection().getLatestAssistantContent()).toBeNull();
    expect(mountHarness(appState()).projection().getLatestAssistantContent()).toBeNull();
  });

  test("无 currentSession 或作用域键不命中时返回 null", () => {
    const noSession = appState({ sessionId: null });
    noSession.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { final_response: "不该被读到" })],
    ]));

    expect(mountHarness(noSession).projection().getLatestAssistantContent()).toBeNull();

    // 存在 currentSession，但 timeline 挂在其它工作区作用域键下：scopeKey 不命中。
    const otherScope = appState();
    otherScope.turnTimelinesBySession.set(
      sessionScopeKey("other-workspace", SESSION_ID),
      timelineWith([
        ["turn_1", turn("turn_1", { final_response: "不该被读到" })],
      ]),
    );
    expect(mountHarness(otherScope).projection().getLatestAssistantContent()).toBeNull();
  });

  test("读取 latestStateRef 最新快照而非渲染期闭包", () => {
    const state = appState();
    state.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_1", turn("turn_1", { final_response: "渲染期正文" })],
    ]));
    const { latestStateRef, projection } = mountHarness(state);
    expect(projection().getLatestAssistantContent()).toBe("渲染期正文");

    // 不触发重渲染，直接替换 ref 指向的新快照。
    const next = appState();
    next.turnTimelinesBySession.set(SCOPE_KEY, timelineWith([
      ["turn_2", turn("turn_2", { final_response: "ref 更新后正文" })],
    ]));
    latestStateRef.current = next;

    expect(projection().getLatestAssistantContent()).toBe("ref 更新后正文");
  });
});

describe("Composer 快照身份的渲染期复用时机", () => {
  test("被丢弃的更新渲染仍写入快照 ref，重渲染复用被丢弃渲染算出的快照身份", async () => {
    const snapshots: ComposerStateSnapshot[] = [];
    let setContentView!: (view: ConversationContentView) => void;
    let releaseDiscardedRender!: () => void;
    let blockNextRender = false;

    function TimingHarness() {
      const [contentView, set] = useState<ConversationContentView>("default");
      setContentView = set;
      // 同一 contentView 复用同一个 AppState 对象，模拟真实 Provider 里
      // useState 持有的稳定 state 身份，否则字段身份差异会掩盖复用行为。
      const statesByView = useRef(new Map<ConversationContentView, AppState>());
      if (!statesByView.current.has(contentView)) {
        statesByView.current.set(contentView, appState({ contentView }));
      }
      const state = statesByView.current.get(contentView)!;
      const latestStateRef = useRef(state);
      latestStateRef.current = state;
      const projection = useComposerStateProjection({
        state,
        currentSessionCacheKey: SCOPE_KEY,
        latestStateRef,
        ...noopActions,
      });
      snapshots.push(projection.composerState);
      // hook 已经跑完再挂起：本次渲染会被 Suspense 整体丢弃。
      if (blockNextRender) {
        blockNextRender = false;
        throw new Promise<void>((resolve) => { releaseDiscardedRender = resolve; });
      }
      return null;
    }

    let renderer: ReactTestRenderer | null = null;
    act(() => {
      renderer = create(
        <Suspense fallback={null}><TimingHarness /></Suspense>,
        { unstable_isConcurrent: true } as never,
      );
    });
    expect(snapshots).toHaveLength(1);

    blockNextRender = true;
    act(() => { setContentView("changes"); });
    // 第二次渲染被丢弃，但快照已算出并压入数组。
    expect(snapshots).toHaveLength(2);
    expect(snapshots[1]).not.toBe(snapshots[0]);

    await act(async () => {
      releaseDiscardedRender();
      await new Promise((resolve) => { setTimeout(resolve, 0); });
    });
    expect(snapshots.length).toBeGreaterThanOrEqual(3);

    // 重渲染的字段值与被丢弃渲染完全相同；只有渲染期写入 ref 才能让
    // reuseComposerStateSnapshot 复用被丢弃渲染的快照对象。
    expect(snapshots[2]).toBe(snapshots[1]);

    act(() => renderer!.unmount());
  });
});
