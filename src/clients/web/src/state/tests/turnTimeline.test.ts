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
  dropTurn,
  markTurnsLoading,
  turnIdsInvalidatedByEvents,
  upsertTurn,
  upsertTurns,
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

function projectedToolDetail(
  turnId: string,
  ordinal: number,
  expanded: boolean,
): TurnDetail {
  return {
    ...detail(turnId, ordinal),
    items: [{
      event_id: `${turnId}:tool_call`,
      part_id: "call_1",
      session_id: SESSION_ID,
      job_id: turnId,
      type: "tool_call_start",
      phase: "tool",
      title: "调用工具 read_fixture",
      content: expanded ? "{" : "",
      status: "completed",
      tool_name: "read_fixture",
      skill_names: [],
      step_id: null,
      timestamp: "2026-07-28T00:00:01Z",
      raw: expanded ? { payload: { args: { path: "fixture.json" } } } : {},
    }],
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

    expect(timeline.turnsById.job_2).toMatchObject({
      ...detail("job_2", 2, 3),
      item_count: 1,
      source_message_count: 1,
      merged_job_count: 0,
    });
  });

  test("同 revision 同投影内容一致复用对象，投影差异合并而不崩溃", () => {
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      summary("job_2", 2, 3),
    );
    const previousTimeline = timeline;
    const previousRecord = timeline.turnsById.job_2;

    timeline = upsertTurn(timeline, summary("job_2", 2, 3));
    expect(timeline).toBe(previousTimeline);
    expect(timeline.turnsById.job_2).toBe(previousRecord);
    timeline = upsertTurn(timeline, {
      ...summary("job_2", 2, 3),
      response_preview: "同 revision 的冲突正文",
    });
    expect(timeline.turnsById.job_2.response_preview).toBe("同 revision 的冲突正文");
  });

  test("同 revision 的摘要和完整详情会合并，详情空字段不会抹掉摘要", () => {
    const summaryRecord: TurnSummary = {
      ...summary("job_2", 2, 3),
      tool_summary: [{
        tool_name: "read_fixture",
        status: "completed",
        tool_call_id: "call_1",
      }],
    };
    const fullRecord: TurnDetail = {
      ...projectedToolDetail("job_2", 2, true),
      revision: 3,
      tool_summary: [],
    };

    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      summaryRecord,
    );
    timeline = upsertTurn(timeline, fullRecord);

    const merged = timeline.turnsById.job_2;
    expect(merged.items_view).toBe("full");
    expect("items" in merged && merged.items).toHaveLength(1);
    expect("final_response" in merged && merged.final_response).toBe("full job_2");
    expect(merged.tool_summary).toEqual(summaryRecord.tool_summary);
  });

  test("同 revision 的工具摘要详情可以升级为完整详情", () => {
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      projectedToolDetail("job_2", 2, false),
    );

    timeline = upsertTurn(timeline, projectedToolDetail("job_2", 2, true));
    expect(timeline.turnsById.job_2).toEqual(projectedToolDetail("job_2", 2, true));

    const expandedTimeline = timeline;
    timeline = upsertTurn(timeline, projectedToolDetail("job_2", 2, false));
    expect(timeline).toBe(expandedTimeline);
  });

  test("工具详情替换摘要占位事件，不产生重复工具块", () => {
    const summaryItems = [
      {
        event_id: "job_2:tool_summary:0",
        part_id: "call_1",
        session_id: SESSION_ID,
        job_id: "job_2",
        type: "tool_call_start" as const,
        phase: "tool" as const,
        title: "调用工具 read_fixture",
        content: "",
        status: "completed" as const,
        tool_name: "read_fixture",
        skill_names: [],
        step_id: null,
        timestamp: "2026-07-28T00:00:01Z",
        raw: {},
      },
      {
        event_id: "job_2:tool_summary:1",
        part_id: "call_1",
        session_id: SESSION_ID,
        job_id: "job_2",
        type: "tool_call_end" as const,
        phase: "tool" as const,
        title: "工具结果 read_fixture",
        content: "",
        status: "completed" as const,
        tool_name: "read_fixture",
        skill_names: [],
        step_id: null,
        timestamp: "2026-07-28T00:00:01Z",
        raw: {},
      },
    ];
    const detailItems = [
      {
        ...summaryItems[0],
        event_id: "job_2:tool_call:1:0",
        content: "",
        raw: { payload: { args: { path: "fixture.json" } } },
      },
      {
        ...summaryItems[1],
        event_id: "job_2:tool_result:2",
        content: "mock-tool-ok",
        raw: { payload: { result: "mock-tool-ok" } },
      },
    ];
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      {
        ...detail("job_2", 2),
        items: summaryItems,
      },
    );
    timeline = upsertTurn(timeline, {
      ...detail("job_2", 2),
      items: detailItems,
    });

    const merged = timeline.turnsById.job_2;
    expect("items" in merged && merged.items).toEqual(detailItems);
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

  test("64 Turn 历史页一次性合并并保持顺序", () => {
    const timeline = upsertTurns(
      createSessionTurnTimeline(SCOPE_KEY),
      Array.from({ length: 64 }, (_, index) => summary(
        `job_${index + 1}`,
        index + 1,
      )),
    );

    expect(timeline.orderedTurnIds).toHaveLength(64);
    expect(timeline.orderedTurnIds[0]).toBe("job_1");
    expect(timeline.orderedTurnIds[timeline.orderedTurnIds.length - 1]).toBe("job_64");
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

  test("job_completed 会让执行 Turn 详情失效并触发刷新", () => {
    expect(turnIdsInvalidatedByEvents([
      { type: "text_delta", job_id: "job_ignored" },
      { type: "job_completed", job_id: "job_execution" },
      { type: "text_end", job_id: "job_execution" },
    ])).toEqual(["job_execution"]);
  });

  test("丢弃当前 view 已失效的 Turn，并阻止旧异步响应重新加入", () => {
    let timeline = upsertTurns(
      createSessionTurnTimeline(SCOPE_KEY),
      [summary("job_old", 1), summary("job_new", 2)],
    );
    timeline = dropTurn(timeline, "job_old");
    timeline = upsertTurn(timeline, summary("job_old", 1, 2));

    expect(timeline.orderedTurnIds).toEqual(["job_new"]);
    expect(timeline.turnsById.job_old).toBeUndefined();
    expect(timeline.invalidatedTurnIds).toEqual(["job_old"]);
  });

  test("成功的详情响应可以复活被旧失效标记拦截的 Turn", () => {
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      summary("job_tool", 1),
    );
    timeline = dropTurn(timeline, "job_tool");
    timeline = applyTurnDetails(timeline, {
      items: [projectedToolDetail("job_tool", 1, true)],
      projection_epoch: 1,
    });

    expect(timeline.invalidatedTurnIds).toEqual([]);
    expect(timeline.orderedTurnIds).toEqual(["job_tool"]);
    expect(timeline.turnsById.job_tool.items_view).toBe("full");
    expect("items" in timeline.turnsById.job_tool
      && timeline.turnsById.job_tool.items).toHaveLength(1);
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

  test("LRU 只保留最近 64 个 session scope", () => {
    let cache = new Map<string, ReturnType<typeof createSessionTurnTimeline>>();
    for (let index = 0; index < 70; index += 1) {
      const key = `scope-${index}`;
      cache = writeTurnTimelineCache(
        cache,
        key,
        createSessionTurnTimeline(key),
      );
    }
    expect([...cache.keys()]).toEqual(
      Array.from({ length: 64 }, (_, index) => `scope-${index + 6}`),
    );
  });
});
