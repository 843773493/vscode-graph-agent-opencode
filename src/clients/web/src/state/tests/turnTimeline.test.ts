import { describe, expect, test } from "bun:test";
import type {
  SessionTurnBootstrap,
  TurnDetail,
  TurnPage,
  TurnSummary,
} from "../../types/backend";
import {
  applyTurnBootstrap,
  applyTurnDetails,
  applyTurnPage,
  beginTurnBootstrap,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  markTurnsLoading,
  turnIdsInvalidatedByEvents,
  upsertTurn,
  writeTurnTimelineCache,
} from "../session/turnTimeline";

const SESSION_ID = "ses_turns";
const SCOPE_KEY = `workspace::${SESSION_ID}`;

function summary(turnId: string, ordinal: number, revision = 1): TurnSummary {
  return {
    turn_id: turnId,
    job_id: turnId,
    session_id: SESSION_ID,
    ordinal,
    revision,
    status: "completed",
    created_at: `2026-07-28T00:00:0${ordinal}Z`,
    updated_at: `2026-07-28T00:00:0${ordinal}Z`,
    completed_at: `2026-07-28T00:00:0${ordinal}Z`,
    items_view: "summary",
    source_message_ids: [`msg_${turnId}`],
    source_message_count: 1,
    merged_job_ids: [],
    merged_job_count: 0,
    sources_truncated: false,
    user_messages: [],
    user_message_count: 0,
    user_messages_truncated: false,
    response_preview: `preview ${turnId}`,
    preview_truncated: false,
    item_count: 1,
  };
}

function detail(turnId: string, ordinal: number, revision = 1): TurnDetail {
  return {
    turn_id: turnId,
    job_id: turnId,
    session_id: SESSION_ID,
    ordinal,
    revision,
    status: "completed",
    created_at: `2026-07-28T00:00:0${ordinal}Z`,
    updated_at: `2026-07-28T00:00:0${ordinal}Z`,
    completed_at: `2026-07-28T00:00:0${ordinal}Z`,
    items_view: "full",
    source_message_ids: [`msg_${turnId}`],
    merged_job_ids: [],
    user_messages: [],
    response_preview: `preview ${turnId}`,
    preview_truncated: false,
    final_response: `full ${turnId}`,
    items: [],
  };
}

function bootstrap(latestTurn: TurnSummary, epoch = 1): SessionTurnBootstrap {
  return {
    session: {
      session_id: SESSION_ID,
      workspace_id: "workspace",
      title: "Turn 测试",
      current_agent_id: "default",
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
    },
    latest_turn: latestTurn,
    active_jobs: [],
    older_cursor: "older-1",
    event_cursor: "event-1",
    projection_epoch: epoch,
  };
}

describe("Turn timeline revision 合并", () => {
  test("低 revision 和同 revision summary 不覆盖较新的 full detail", () => {
    let timeline = createSessionTurnTimeline(SCOPE_KEY);
    timeline = upsertTurn(timeline, detail("job_2", 2, 3));
    timeline = upsertTurn(timeline, summary("job_2", 2, 2));
    timeline = upsertTurn(timeline, summary("job_2", 2, 3));

    expect(timeline.turnsById.job_2).toEqual(detail("job_2", 2, 3));
  });

  test("同 revision 同 view 内容一致复用对象，不一致明确报错", () => {
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      summary("job_2", 2, 3),
    );
    const previousTimeline = timeline;
    const previousRecord = timeline.turnsById.job_2;

    timeline = upsertTurn(timeline, summary("job_2", 2, 3));
    expect(timeline).toBe(previousTimeline);
    expect(timeline.turnsById.job_2).toBe(previousRecord);
    expect(() => upsertTurn(timeline, {
      ...summary("job_2", 2, 3),
      response_preview: "同 revision 的冲突正文",
    })).toThrow("Turn 同 revision 内容不一致");
  });

  test("较低 revision 的 merge 元数据不会隐藏较新 Turn", () => {
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      summary("job_execution", 2, 4),
    );
    timeline = upsertTurn(timeline, summary("job_visible", 1, 1));
    const beforeStale = timeline;

    timeline = upsertTurn(timeline, {
      ...summary("job_execution", 2, 3),
      merged_job_ids: ["job_visible"],
      merged_job_count: 1,
    });

    expect(timeline).toBe(beforeStale);
    expect(timeline.orderedTurnIds).toEqual(["job_visible", "job_execution"]);
    expect(timeline.mergedTurnIds).toEqual([]);
  });

  test("bootstrap 乱序响应受 generation 保护", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 2);
    timeline = applyTurnBootstrap(timeline, 1, bootstrap(summary("stale", 1)));
    expect(timeline.orderedTurnIds).toEqual([]);

    timeline = applyTurnBootstrap(timeline, 2, bootstrap(summary("current", 2)));
    expect(timeline.orderedTurnIds).toEqual(["current"]);
  });

  test("partial bootstrap 保留最新 Turn 并在 ready 后开放历史游标", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 1);
    timeline = applyTurnBootstrap(timeline, 1, {
      ...bootstrap(summary("latest", 3)),
      projection_state: "partial",
      older_cursor: null,
    });

    expect(timeline.projectionState).toBe("partial");
    expect(timeline.orderedTurnIds).toEqual(["latest"]);
    expect(timeline.hasMore).toBe(false);

    timeline = applyTurnBootstrap(timeline, 1, {
      ...bootstrap(summary("latest", 3)),
      projection_state: "ready",
    });

    expect(timeline.projectionState).toBe("ready");
    expect(timeline.hasMore).toBe(true);
    expect(timeline.olderCursor).toBe("older-1");
  });

  test("Turn detail epoch 纯决策区分同代、旧响应和未来响应", () => {
    expect(decideTurnProjectionEpoch(null, 3)).toBe("apply");
    expect(decideTurnProjectionEpoch(3, 3)).toBe("apply");
    expect(decideTurnProjectionEpoch(3, 2)).toBe("discard_older");
    expect(decideTurnProjectionEpoch(3, 4)).toBe("refresh_bootstrap");
  });

  test("历史分页和详情水合保留已加载 Turn", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 1);
    timeline = applyTurnBootstrap(timeline, 1, bootstrap(summary("job_3", 3)));
    const page: TurnPage = {
      items: [summary("job_2", 2), summary("job_1", 1)],
      next_cursor: null,
      has_more: false,
      projection_epoch: 1,
    };
    timeline = applyTurnPage(timeline, page);
    timeline = applyTurnDetails(timeline, {
      items: [detail("job_2", 2, 2)],
      projection_epoch: 1,
    });

    expect(timeline.orderedTurnIds).toEqual(["job_1", "job_2", "job_3"]);
    expect(timeline.turnsById.job_2.revision).toBe(2);
    expect(timeline.turnsById.job_3.turn_id).toBe("job_3");
  });

  test("乱序旧详情不覆盖更新后的 summary", () => {
    let timeline = createSessionTurnTimeline(SCOPE_KEY);
    timeline = upsertTurn(timeline, detail("job_2", 2, 2));
    timeline = upsertTurn(timeline, summary("job_2", 2, 3));
    timeline = upsertTurn(timeline, detail("job_2", 2, 2));

    expect(timeline.turnsById.job_2).toEqual(summary("job_2", 2, 3));
  });

  test("历史前插保持现有 Turn 身份与顺序", () => {
    let timeline = createSessionTurnTimeline(SCOPE_KEY);
    timeline = upsertTurn(timeline, summary("job_4", 4));
    timeline = upsertTurn(timeline, summary("job_5", 5));
    const existingJob4 = timeline.turnsById.job_4;

    timeline = applyTurnPage(timeline, {
      items: [summary("job_3", 3), summary("job_2", 2)],
      next_cursor: "older-2",
      has_more: true,
      projection_epoch: 1,
    });

    expect(timeline.orderedTurnIds).toEqual(["job_2", "job_3", "job_4", "job_5"]);
    expect(timeline.turnsById.job_4).toBe(existingJob4);
  });

  test("执行 Turn 水合后隐藏已合并 Job，旧分页也不能重新加入", () => {
    let timeline = createSessionTurnTimeline(SCOPE_KEY);
    timeline = upsertTurn(timeline, summary("job_1", 1));
    timeline = upsertTurn(timeline, summary("job_2", 2));
    timeline = upsertTurn(timeline, summary("job_3", 3));
    timeline = markTurnsLoading(timeline, ["job_2"]);
    timeline = upsertTurn(timeline, {
      ...detail("job_1", 1, 2),
      merged_job_ids: ["job_2", "job_3"],
    });

    expect(timeline.orderedTurnIds).toEqual(["job_1"]);
    expect(timeline.mergedTurnIds).toEqual(["job_2", "job_3"]);
    expect(timeline.loadingDetailIds).toEqual([]);

    timeline = upsertTurn(timeline, summary("job_2", 2, 3));
    expect(timeline.orderedTurnIds).toEqual(["job_1"]);
  });

  test("job_merged 会让执行 Turn 详情失效并触发刷新", () => {
    expect(turnIdsInvalidatedByEvents([
      { type: "text_delta", job_id: "job_ignored" },
      { type: "job_merged", job_id: "job_execution" },
      { type: "text_end", job_id: "job_execution" },
    ])).toEqual(["job_execution"]);
  });

  test("破坏性 epoch 变化在 bootstrap 时清空旧投影", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 1);
    timeline = applyTurnBootstrap(timeline, 1, bootstrap(summary("old", 1), 1));
    timeline = beginTurnBootstrap(timeline, 2);
    timeline = applyTurnBootstrap(timeline, 2, bootstrap(summary("new", 1), 2));

    expect(timeline.orderedTurnIds).toEqual(["new"]);
    expect(timeline.projectionEpoch).toBe(2);
  });

  test("partial bootstrap 保持 bootstrapping，禁止被误判为可开启主 SSE", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 1);
    timeline = applyTurnBootstrap(timeline, 1, {
      ...bootstrap(summary("latest", 1), 1),
      projection_state: "partial",
      event_cursor: null,
      older_cursor: null,
    });

    expect(timeline.phase).toBe("bootstrapping");
    expect(timeline.projectionState).toBe("partial");
    expect(timeline.eventCursor).toBe(null);
  });

  test("LRU 只保留最近八个 session scope", () => {
    let cache = new Map<string, ReturnType<typeof createSessionTurnTimeline>>();
    for (let index = 0; index < 10; index += 1) {
      const key = `scope-${index}`;
      cache = writeTurnTimelineCache(
        cache,
        key,
        createSessionTurnTimeline(key),
      );
    }
    expect([...cache.keys()]).toEqual([
      "scope-2",
      "scope-3",
      "scope-4",
      "scope-5",
      "scope-6",
      "scope-7",
      "scope-8",
      "scope-9",
    ]);
  });
});
