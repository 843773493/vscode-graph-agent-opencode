import { describe, expect, test } from "bun:test";
import type {
  SessionTurnBootstrap,
  InternalExecutionHistory,
  TurnDetail,
  TurnPage,
  TurnSummary,
} from "../../types/backend";
import {
  applyTurnBootstrap,
  applyTurnDetails,
  applyTurnHistoryPage,
  applyTurnPage,
  beginTurnBootstrap,
  createSessionTurnTimeline,
  decideTurnProjectionEpoch,
  dropTurn,
  markTurnsLoading,
  upsertTurn,
  upsertTurns,
  writeTurnTimelineCache,
} from "./turnTimeline";

const SESSION_ID = "ses_turns";
const THREAD_ID = "thr_018f0000000070008000000000000001";
const SCOPE_KEY = `workspace::${SESSION_ID}`;

function summary(
  turnId: string,
  ordinal: number,
  revision = 1,
  sourceViewRevision = 1,
): TurnSummary {
  return {
    turn_id: turnId,
    job_id: turnId,
    session_id: SESSION_ID,
    ordinal,
    root_logical_item_ordinal: ordinal * 10,
    source_view_id: "view_history_fixture",
    source_view_revision: sourceViewRevision,
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

function detail(
  turnId: string,
  ordinal: number,
  revision = 1,
  sourceViewRevision = 1,
): TurnDetail {
  return {
    turn_id: turnId,
    job_id: turnId,
    session_id: SESSION_ID,
    ordinal,
    root_logical_item_ordinal: ordinal * 10,
    source_view_id: "view_history_fixture",
    source_view_revision: sourceViewRevision,
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
      thread_id: THREAD_ID,
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
    thread_id: "thr_018f0000000070008000000000000001",
    session: {
      session_id: SESSION_ID,
      workspace_id: "workspace",
      resource_identity: `fixture-${SESSION_ID}`,
      uri: `boxteam://workspace/workspace/resources/session/${SESSION_ID}`,
      scope: "workspace",
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
  test("同 revision 的不同读取快照整体替换，旧部件不借新 binding 展示", () => {
    const old = { ...detail("job_2", 2), final_response: "旧快照完整正文" };
    let timeline = upsertTurn(createSessionTurnTimeline(SCOPE_KEY), old);
    const next = { ...summary("job_2", 2), source_view_id: "view_next", source_view_revision: 2 };
    timeline = upsertTurn(timeline, next);
    expect(timeline.turnsById.job_2).toEqual(next);
    expect("final_response" in timeline.turnsById.job_2!).toBe(false);
  });

  test("同一快照保留 Turn revision 对低版本详情的保护", () => {
    const current = { ...detail("job_2", 2), revision: 3, final_response: "新详情" };
    const stale = { ...detail("job_2", 2), revision: 2, final_response: "旧详情" };
    const timeline = upsertTurn(
      upsertTurn(createSessionTurnTimeline(SCOPE_KEY), current),
      stale,
    );

    expect(timeline.turnsById.job_2).toEqual(current);
  });
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
        thread_id: THREAD_ID,
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
        thread_id: THREAD_ID,
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

  test("bootstrap 绑定 thread 后重复摘要仍推进历史视图版本", () => {
    const latestTurn = summary("job_latest", 1, 1, 2);
    let timeline = upsertTurn(
      createSessionTurnTimeline(SCOPE_KEY),
      latestTurn,
    );
    timeline = beginTurnBootstrap(timeline, 1);

    timeline = applyTurnBootstrap(
      timeline,
      1,
      bootstrap(latestTurn),
    );

    expect(timeline.threadId).toBe(THREAD_ID);
    expect(timeline.historyViewRevision).toBe(2);
    expect(timeline.turnsById.job_latest).toBe(latestTurn);
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
    expect(timeline.hasBefore).toBe(false);
    expect(timeline.beforeCursor).toBeNull();

    timeline = applyTurnBootstrap(timeline, 1, {
      ...bootstrap(summary("latest", 3)),
      projection_state: "ready",
    });

    expect(timeline.projectionState).toBe("ready");
    expect(timeline.hasBefore).toBe(true);
    expect(timeline.beforeCursor).toBe("older-1");
  });

  test("Turn detail epoch 纯决策区分同代、旧响应和未来响应", () => {
    expect(decideTurnProjectionEpoch(null, 3)).toBe("apply");
    expect(decideTurnProjectionEpoch(3, 3)).toBe("apply");
    expect(decideTurnProjectionEpoch(3, 2)).toBe("discard_older");
    expect(decideTurnProjectionEpoch(3, 4)).toBe("refresh_bootstrap");
  });

  test("around 分页后 before 方向事实完全由后端 has_before/before_cursor 决定", () => {
    let timeline = beginTurnBootstrap(createSessionTurnTimeline(SCOPE_KEY), 1);
    timeline = applyTurnBootstrap(timeline, 1, bootstrap(summary("latest", 5)));
    // 前置：bootstrap 建立了一个可继续向 before 翻页的游标。
    expect(timeline.beforeCursor).toBe("older-1");
    expect(timeline.hasBefore).toBe(true);

    // around 命中窗口已经包含最旧 Turn：后端给出 before_cursor=null、has_before=false，
    // 这是权威结论，必须清空 before 方向的游标与 has-before。
    const around = applyTurnHistoryPage(timeline, {
      items: [detail("job_3", 3)],
      summaries: [],
      next_cursor: null,
      has_more: false,
      before_cursor: null,
      after_cursor: "newer-1",
      has_before: false,
      has_after: true,
      thread_id: "thr_018f0000000070008000000000000001",
      source_view_id: "view_history_fixture",
      source_view_revision: 1,
      projection_epoch: 1,
    }, "around");

    expect(around.beforeCursor).toBeNull();
    expect(around.hasBefore).toBe(false);
    expect(around.afterCursor).toBe("newer-1");
    expect(around.hasAfter).toBe(true);

    // 反向验证：around 命中窗口未触顶时，后端给出的 before 事实必须原样生效。
    const partial = applyTurnHistoryPage(timeline, {
      items: [detail("job_3", 3)],
      summaries: [],
      next_cursor: null,
      has_more: false,
      before_cursor: "older-around",
      after_cursor: "newer-around",
      has_before: true,
      has_after: true,
      thread_id: "thr_018f0000000070008000000000000001",
      source_view_id: "view_history_fixture",
      source_view_revision: 1,
      projection_epoch: 1,
    }, "around");

    expect(partial.beforeCursor).toBe("older-around");
    expect(partial.hasBefore).toBe(true);
  });

  test("只有 internal history 的时间线会保留 around 窗口游标", () => {
    const initial = createSessionTurnTimeline(SCOPE_KEY);
    const populated = applyTurnHistoryPage(initial, {
      items: [],
      internal_executions: [{
        execution_id: "execution-1",
        job_id: "job-1",
        session_id: SESSION_ID,
        thread_id: "thr_main",
        status: "completed",
        created_at: "2026-07-28T00:00:01Z",
        updated_at: "2026-07-28T00:00:01Z",
        display_parts: [],
        response_parts: [],
      }],
      before_cursor: "older-internal",
      after_cursor: "newer-internal",
      has_before: true,
      has_after: true,
      thread_id: "thr_018f0000000070008000000000000001",
      source_view_id: "view_history_fixture",
      source_view_revision: 1,
      projection_epoch: 1,
    }, "around");

    const resumed = applyTurnBootstrap(populated, 0, {
      ...bootstrap(summary("latest", 1)),
      latest_turn: null,
      older_cursor: null,
    });

    expect(resumed.orderedTurnIds).toEqual([]);
    expect(resumed.internalExecutions).toHaveLength(1);
    expect(resumed.beforeCursor).toBe("older-internal");
    expect(resumed.afterCursor).toBe("newer-internal");
    expect(resumed.hasBefore).toBe(true);
    expect(resumed.hasAfter).toBe(true);
  });

  test("跨 COW view 的旧 history page 不回退 Turn、internal execution 或游标", () => {
    const execution = (
      status: InternalExecutionHistory["status"],
    ): InternalExecutionHistory => ({
      execution_id: "execution-cow",
      job_id: "job-cow",
      session_id: SESSION_ID,
      thread_id: "thr_main",
      status,
      created_at: "2026-07-28T00:00:01Z",
      updated_at: "2026-07-28T00:00:02Z",
      display_parts: [],
      response_parts: [],
    });
    const page = (
      sourceViewId: string,
      sourceViewRevision: number,
      response: string,
      cursorPrefix: string,
      executionStatus: InternalExecutionHistory["status"],
      extraTurn = false,
    ) => ({
      thread_id: "thr_018f0000000070008000000000000001",
      source_view_id: sourceViewId,
      source_view_revision: sourceViewRevision,
      items: [
        {
          ...detail("job_cow", 1, 1, sourceViewRevision),
          source_view_id: sourceViewId,
          final_response: response,
        },
        ...(extraTurn
          ? [{
              ...detail("job_old_page", 2, 1, sourceViewRevision),
              source_view_id: sourceViewId,
            }]
          : []),
      ],
      internal_executions: [execution(executionStatus)],
      before_cursor: `${cursorPrefix}-before`,
      after_cursor: `${cursorPrefix}-after`,
      has_before: true,
      has_after: true,
      projection_epoch: 1,
    });

    let timeline = applyTurnHistoryPage(
      createSessionTurnTimeline(SCOPE_KEY),
      page("view_a", 5, "A snapshot", "a", "completed"),
      "around",
    );
    timeline = applyTurnHistoryPage(
      timeline,
      page("view_a", 6, "A latest", "a-latest", "completed"),
      "around",
    );
    timeline = { ...timeline, loadingBefore: true, loadingAfter: true };
    const afterLateSameViewPage = applyTurnHistoryPage(
      timeline,
      page("view_a", 5, "late A snapshot", "late-a", "unknown", true),
      "before",
    );

    expect(afterLateSameViewPage.historyViewRevision).toBe(6);
    expect(afterLateSameViewPage.turnsById.job_cow).toMatchObject({
      source_view_revision: 6,
      final_response: "A latest",
    });
    expect(afterLateSameViewPage.internalExecutions[0]?.status).toBe("completed");
    expect(afterLateSameViewPage.beforeCursor).toBe("a-latest-before");
    expect(afterLateSameViewPage.loadingBefore).toBe(false);
    expect(afterLateSameViewPage.loadingAfter).toBe(true);

    timeline = applyTurnHistoryPage(
      afterLateSameViewPage,
      page("view_b", 7, "B snapshot", "b", "failed"),
      "around",
    );
    timeline = { ...timeline, loadingBefore: true, loadingAfter: true };
    const afterLatePage = applyTurnHistoryPage(
      timeline,
      page("view_a", 6, "late A snapshot", "late-a", "unknown", true),
      "before",
    );

    expect(afterLatePage.historyViewRevision).toBe(7);
    expect(afterLatePage.orderedTurnIds).toEqual(["job_cow"]);
    expect(afterLatePage.turnsById.job_cow).toMatchObject({
      source_view_id: "view_b",
      source_view_revision: 7,
      final_response: "B snapshot",
    });
    expect(afterLatePage.internalExecutions[0]?.status).toBe("failed");
    expect(afterLatePage.beforeCursor).toBe("b-before");
    expect(afterLatePage.afterCursor).toBe("b-after");
    expect(afterLatePage.loadingBefore).toBe(false);
    expect(afterLatePage.loadingAfter).toBe(true);

    const oldDetail = {
      ...detail("job_cow", 1, 1, 6),
      source_view_id: "view_a",
      final_response: "late A detail",
    };
    const afterLateDetail = applyTurnDetails(
      markTurnsLoading(afterLatePage, ["job_cow"]),
      { items: [oldDetail], projection_epoch: 1 },
    );
    expect(afterLateDetail.turnsById.job_cow).toMatchObject({
      source_view_id: "view_b",
      source_view_revision: 7,
      final_response: "B snapshot",
    });
    expect(afterLateDetail.loadingDetailIds).not.toContain("job_cow");

    const invalidated = markTurnsLoading(dropTurn(afterLatePage, "job_cow"), ["job_cow"]);
    const afterInvalidatedLateDetail = applyTurnDetails(
      invalidated,
      { items: [oldDetail], projection_epoch: 1 },
    );
    expect(afterInvalidatedLateDetail.turnsById.job_cow).toBeUndefined();
    expect(afterInvalidatedLateDetail.invalidatedTurnIds).toContain("job_cow");
    expect(afterInvalidatedLateDetail.loadingDetailIds).not.toContain("job_cow");
  });

  test("无 binding 的未物化空 history page 不伪造 owner revision", () => {
    const timeline = applyTurnHistoryPage(createSessionTurnTimeline(SCOPE_KEY), {
      thread_id: "thr_018f0000000070008000000000000001",
      items: [],
      before_cursor: "empty-page-cursor",
      has_before: true,
      projection_epoch: 1,
    });

    expect(timeline.historyViewRevision).toBeNull();
    expect(timeline.orderedTurnIds).toEqual([]);
    expect(timeline.beforeCursor).toBe("empty-page-cursor");
    expect(timeline.hasBefore).toBe(true);
    expect(() => applyTurnHistoryPage(createSessionTurnTimeline(SCOPE_KEY), {
      thread_id: "thr_018f0000000070008000000000000001",
      items: [detail("job_without_binding", 1)],
      projection_epoch: 1,
    })).toThrow("已物化 Turn history 缺少 source view/revision");
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

  test("detail 的后端顺序和 identity 原样替换 summary，不按正文去重", () => {
    const summaryTurn = {
      ...summary("job_activity", 1),
      response_parts: [
        {
          part_id: "tool-call:5:0",
          kind: "tool_call" as const,
          projection: "summary" as const,
          status: "completed" as const,
          source: {
            message_sequence: 5,
            assistant_message_sequence: 5,
            call_index: 0,
          },
          tool_call_id: "call-1",
          tool_name: "read_file",
        },
      ],
    };
    const detailTurn = {
      ...detail("job_activity", 1),
      response_parts: [
        {
          part_id: "message:5:reasoning:0:0",
          kind: "reasoning" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 5, content_block_index: 0, item_index: 0 },
          text: "执行工具",
        },
        {
          part_id: "tool-call:5:0",
          kind: "tool_call" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: {
            message_sequence: 5,
            assistant_message_sequence: 5,
            call_index: 0,
          },
          tool_call_id: "call-1",
          tool_name: "read_file",
          arguments: '{"path":"README.md"}',
        },
        {
          part_id: "message:7:reasoning:0:0",
          kind: "reasoning" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 7, content_block_index: 0, item_index: 0 },
          text: "确认结果",
        },
        {
          part_id: "message:8:reasoning:0:0",
          kind: "reasoning" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 8, content_block_index: 0, item_index: 0 },
          text: "确认结果",
        },
      ],
    };

    let timeline = upsertTurn(createSessionTurnTimeline(SCOPE_KEY), summaryTurn);
    timeline = upsertTurn(timeline, detailTurn);
    const merged = timeline.turnsById.job_activity.response_parts ?? [];

    expect(merged.map((part) => part.kind)).toEqual([
      "reasoning",
      "tool_call",
      "reasoning",
      "reasoning",
    ]);
    expect(merged.filter((part) => part.text === "确认结果")).toHaveLength(2);
  });

  test("定点工具详情补丁不丢失同一 Turn 的其他部件", () => {
    const fullTurn = {
      ...detail("job_tool_patch", 1),
      response_parts: [
        {
          part_id: "reasoning:1",
          kind: "reasoning" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 1, content_block_index: 0 },
          text: "分析命令",
        },
        {
          part_id: "tool-call:1",
          kind: "tool_call" as const,
          projection: "summary" as const,
          status: "completed" as const,
          source: { message_sequence: 2, assistant_message_sequence: 2, call_index: 0 },
          tool_call_id: "call-1",
          tool_name: "exec_command",
          arguments: null,
        },
        {
          part_id: "final:1",
          kind: "final_text" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 3 },
          text: "完成",
          final: true,
        },
      ],
    };
    // 定点工具请求只返回被请求的工具部件（携带完整参数）。
    const pointPatchTurn = {
      ...detail("job_tool_patch", 1),
      response_parts: [
        {
          part_id: "tool-call:1",
          kind: "tool_call" as const,
          projection: "detail" as const,
          status: "completed" as const,
          source: { message_sequence: 2, assistant_message_sequence: 2, call_index: 0 },
          tool_call_id: "call-1",
          tool_name: "exec_command",
          arguments: '{"cmd":"pwd"}',
        },
      ],
    };

    let timeline = upsertTurn(createSessionTurnTimeline(SCOPE_KEY), fullTurn);
    timeline = upsertTurn(timeline, pointPatchTurn);
    const merged = timeline.turnsById.job_tool_patch.response_parts ?? [];

    expect(merged.map((part) => part.part_id)).toEqual([
      "reasoning:1",
      "tool-call:1",
      "final:1",
    ]);
    const toolPart = merged.find((part) => part.part_id === "tool-call:1");
    expect(toolPart?.arguments).toBe('{"cmd":"pwd"}');
    expect(toolPart?.projection).toBe("detail");
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

  test("Turn 内容修订复用现有顺序，排序键变化时重新排序", () => {
    const job1 = summary("job_1", 1);
    const job2 = summary("job_2", 2);
    let timeline = upsertTurns(
      createSessionTurnTimeline(SCOPE_KEY),
      [job1, job2],
    );
    const orderedTurnIds = timeline.orderedTurnIds;

    timeline = upsertTurn(timeline, {
      ...job2,
      revision: 2,
      response_preview: "已更新的回复摘要",
    });

    expect(timeline.orderedTurnIds).toBe(orderedTurnIds);
    expect(timeline.orderedTurnIds).toEqual(["job_1", "job_2"]);

    timeline = upsertTurn(timeline, {
      ...job2,
      ordinal: 0,
      revision: 3,
    });

    expect(timeline.orderedTurnIds).not.toBe(orderedTurnIds);
    expect(timeline.orderedTurnIds).toEqual(["job_2", "job_1"]);
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

  test("详情缓存超限时恢复后端原始 summary，不由前端重新摘要", () => {
    const summaries = Array.from({ length: 10 }, (_, index) =>
      summary(`job_detail_${index}`, index + 1)
    );
    let timeline = upsertTurns(
      createSessionTurnTimeline(SCOPE_KEY),
      summaries,
    );
    timeline = applyTurnDetails(timeline, {
      items: summaries.map((item) => ({
        ...detail(item.turn_id, item.ordinal),
        final_response: "x".repeat(10_000),
      })),
      projection_epoch: 1,
    });

    expect(timeline.turnsById.job_detail_0).toBe(summaries[0]);
    expect(timeline.turnsById.job_detail_1).toBe(summaries[1]);
    expect(timeline.turnsById.job_detail_9.items_view).toBe("full");
    expect(timeline.detailAccessOrder).toEqual(
      Array.from({ length: 8 }, (_, index) => `job_detail_${index + 2}`),
    );
  });

  test("history 页携带的权威 summaries 支持分页详情窗口淘汰", () => {
    const summaries = Array.from({ length: 10 }, (_, index) =>
      summary(`job_page_${index}`, index + 1)
    );
    const timeline = applyTurnHistoryPage(
      createSessionTurnTimeline(SCOPE_KEY),
      {
        items: summaries.map((item) => detail(item.turn_id, item.ordinal)),
        summaries,
        thread_id: "thr_018f0000000070008000000000000001",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        projection_epoch: 1,
      },
      "before",
    );

    expect(timeline.orderedTurnIds).toHaveLength(10);
    expect(timeline.turnsById.job_page_0).toBe(summaries[0]);
    expect(timeline.turnsById.job_page_9.items_view).toBe("full");
  });

  test("分页合并独立 internal execution 时按 membership ordinal 排序", () => {
    const responsePart = (
      itemId: string,
      itemSequence: number,
      logicalItemOrdinal: number,
      text: string,
      projection: "summary" | "detail",
    ): InternalExecutionHistory["response_parts"][number] => ({
      part_id: `${itemId}:part:0`,
      kind: "text",
      projection,
      status: "completed",
      source: {
        message_sequence: 1,
        item_id: itemId,
        item_sequence: itemSequence,
        logical_item_ordinal: logicalItemOrdinal,
        part_ordinal: 0,
        created_at: "2026-07-28T00:00:01Z",
      },
      text,
    });
    const execution = (
      executionId: string,
      createdAt: string,
      status: InternalExecutionHistory["status"],
      itemSequence: number,
      logicalItemOrdinal: number,
      responseParts: InternalExecutionHistory["response_parts"] = [],
    ): InternalExecutionHistory => ({
      execution_id: executionId,
      job_id: `job-${executionId}`,
      session_id: SESSION_ID,
      thread_id: "thr_main",
      status,
      created_at: createdAt,
      updated_at: createdAt,
      response_parts: responseParts,
      display_parts: [
        {
          kind: "generated_session_result",
          text: "子任务已完成。",
          item_id: `item-${executionId}-${itemSequence}`,
          item_sequence: itemSequence,
          logical_item_ordinal: logicalItemOrdinal,
          created_at: createdAt,
          provenance: {},
        },
      ],
    });
    const timeline = applyTurnHistoryPage(
      applyTurnHistoryPage(
        createSessionTurnTimeline(SCOPE_KEY),
        {
          items: [],
          internal_executions: [
            execution(
              "execution-2",
              "2026-07-28T00:00:02Z",
              "unknown",
              2,
              80,
              [responsePart("output-2", 20, 90, "摘要文本", "summary")],
            ),
            execution("execution-3", "2026-07-28T00:00:03Z", "completed", 3, 100),
          ],
          thread_id: "thr_main",
          source_view_id: "view_history_fixture",
          source_view_revision: 1,
          projection_epoch: 1,
        },
        "before",
      ),
      {
          items: [],
          internal_executions: [
            execution("execution-1", "2026-07-28T00:00:01Z", "completed", 1, 60),
            execution(
              "execution-2",
              "2026-07-28T00:00:02Z",
              "completed",
              4,
              40,
              [
                responsePart("output-2", 20, 90, "补全后的文本", "detail"),
                responsePart("tool-2", 21, 70, "工具结果", "detail"),
              ],
            ),
        ],
        thread_id: "thr_main",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        projection_epoch: 1,
      },
      "before",
    );

    const execution2 = timeline.internalExecutions.find(
      ({ execution_id }) => execution_id === "execution-2",
    );
    expect(execution2?.status).toBe("completed");
    expect(execution2?.display_parts.map(({ logical_item_ordinal }) => logical_item_ordinal))
      .toEqual([40, 80]);
    expect(execution2?.response_parts.map(({ part_id }) => part_id))
      .toEqual(["tool-2:part:0", "output-2:part:0"]);
    expect(execution2?.response_parts[1]?.text).toBe("补全后的文本");
  });

  test("重页 display identity 的物理坐标变化明确报 projection integrity", () => {
    const execution: InternalExecutionHistory = {
      execution_id: "execution-1",
      job_id: "job-1",
      session_id: SESSION_ID,
      thread_id: "thr_main",
      status: "completed",
      created_at: "2026-07-28T00:00:01Z",
      updated_at: "2026-07-28T00:00:01Z",
      display_parts: [{
        kind: "generated_session_result",
        text: "子任务已完成。",
        item_id: "item-display-1",
        item_sequence: 10,
        logical_item_ordinal: 20,
        created_at: "2026-07-28T00:00:01Z",
        provenance: {},
      }],
      response_parts: [],
    };
    const timeline = applyTurnHistoryPage(
      createSessionTurnTimeline(SCOPE_KEY),
      {
        thread_id: "thr_main",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        items: [],
        internal_executions: [execution],
        projection_epoch: 1,
      },
      "before",
    );

    expect(() => applyTurnHistoryPage(
      timeline,
      {
        items: [],
        internal_executions: [{
          ...execution,
          display_parts: [{ ...execution.display_parts[0], item_sequence: 11 }],
        }],
        thread_id: "thr_main",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        projection_epoch: 1,
      },
      "before",
    )).toThrow("history projection integrity");
  });

  test("重页 response part 的 membership 坐标变化明确报 projection integrity", () => {
    const responsePart: InternalExecutionHistory["response_parts"][number] = {
      part_id: "part-1",
      kind: "text",
      projection: "summary",
      status: "completed",
      source: {
        message_sequence: 1,
        item_id: "item-response-1",
        item_sequence: 10,
        logical_item_ordinal: 20,
        part_ordinal: 0,
        created_at: "2026-07-28T00:00:01Z",
      },
      text: "输出",
    };
    const execution: InternalExecutionHistory = {
      execution_id: "execution-1",
      job_id: "job-1",
      session_id: SESSION_ID,
      thread_id: "thr_main",
      status: "completed",
      created_at: "2026-07-28T00:00:01Z",
      updated_at: "2026-07-28T00:00:01Z",
      display_parts: [],
      response_parts: [responsePart],
    };
    const timeline = applyTurnHistoryPage(
      createSessionTurnTimeline(SCOPE_KEY),
      {
        thread_id: "thr_main",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        items: [],
        internal_executions: [execution],
        projection_epoch: 1,
      },
      "before",
    );

    expect(() => applyTurnHistoryPage(
      timeline,
      {
        items: [],
        internal_executions: [{
          ...execution,
          response_parts: [{
            ...responsePart,
            source: { ...responsePart.source, item_sequence: 11 },
          }],
        }],
        thread_id: "thr_main",
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        projection_epoch: 1,
      },
      "before",
    )).toThrow("history projection integrity");
  });

  test("切换 session scope 时卸载旧会话详情正文但保留摘要窗口", () => {
    const firstSummary = summary("job_first", 1);
    let firstTimeline = upsertTurn(
      createSessionTurnTimeline("scope-first"),
      firstSummary,
    );
    firstTimeline = applyTurnDetails(firstTimeline, {
      items: [detail("job_first", 1)],
      projection_epoch: 1,
    });
    let cache = writeTurnTimelineCache(
      new Map(),
      "scope-first",
      firstTimeline,
    );
    cache = writeTurnTimelineCache(
      cache,
      "scope-second",
      createSessionTurnTimeline("scope-second"),
    );

    expect(cache.get("scope-first")?.turnsById.job_first).toBe(firstSummary);
    expect(cache.get("scope-first")?.detailAccessOrder).toEqual([]);
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
