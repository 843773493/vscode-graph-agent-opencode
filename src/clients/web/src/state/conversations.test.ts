import { expect, test } from "bun:test";

import {
  appendTraceEventsToPendingConversations,
  getConversationsForSession,
  messageStreamTurnIdForSession,
  pendingSnapshotToConversations,
  preservePendingTerminalConversation,
  removePendingForTraceEvent,
  statusForConversationEvents,
  syncActiveJobConversation,
  writePendingSnapshot,
} from "./conversations";
import { createSessionTurnTimeline, upsertTurn } from "./session/turnTimeline";
import {
  buildPendingStatusItem,
  isLiveConversationView,
} from "./trace/traceAggregation";
import {
  createMessageStreamState,
  type MessageStreamState,
} from "./messageStream/index";
import type { AppState, ConversationView } from "../types/frontend";
import type { AttachmentRef, PendingRequestList, TraceEvent } from "../types/backend";
import { updateSessionAttachmentSummary, updateAttachmentSummariesFromTraces } from "./attachments";

test("附件摘要保留同 ID 跨工作区归属并拒绝其它会话的 Trace", () => {
  const summaries: AppState["sessionAttachmentSummaries"] = new Map();
  const attachment = (fileId: string, name: string): AttachmentRef => ({ file_id: fileId, name });
  updateSessionAttachmentSummary(summaries, "gateway_a::ses_same", [attachment("file_a", "A.txt")], "2026-10-14T00:00:00Z");
  updateSessionAttachmentSummary(summaries, "gateway_b::ses_same", [attachment("file_b", "B.txt")], "2026-10-14T00:00:01Z");
  expect(summaries.get("gateway_a::ses_same")?.names).toEqual(["A.txt"]);
  expect(summaries.get("gateway_b::ses_same")?.names).toEqual(["B.txt"]);
  const event = (sessionId: string, name: string): TraceEvent => ({
    event_id: `evt_${sessionId}`, session_id: sessionId, thread_id: "thr_018f0000000070008000000000000001", job_id: "job_attachments",
    type: "message_created", phase: "message", title: "创建消息", content: "",
    timestamp: "2026-10-14T00:00:02Z",
    payload: { session_id: sessionId, attachments: [attachment(sessionId, name)] },
  });
  updateAttachmentSummariesFromTraces(summaries, "ses_same", "gateway_a::ses_same", [
    event("ses_foreign", "foreign.txt"), event("ses_same", "own.txt"),
  ]);
  expect(summaries.get("gateway_a::ses_same")?.names).toEqual(["A.txt", "own.txt"]);
  expect(summaries.get("gateway_b::ses_same")?.names).toEqual(["B.txt"]);
  expect(summaries.has("ses_same")).toBe(false);
});

test("同 session/turn ID 的消息流投影和尾帧订阅按 Gateway 会话隔离", () => {
  const sessionId = "ses_same";
  const turnId = "turn_same";
  const leftKey = "gateway_a::ses_same";
  const rightKey = "gateway_b::ses_same";
  const pending: ConversationView = { threadId: "thr_conversation",
    conversationId: turnId, displayMode: "live", sessionId, turnId,
    userMessage: null, events: [], status: "running", jobId: null, pending: true, source: "pending",
  };
  const foreignStream = {
    ...createMessageStreamState(sessionId, turnId, "stream_same", rightKey, "thr_conversation"),
    lastEventSeq: 100,
    failure: { code: "foreign", message: "其它工作区的失败", afterInterruptRequested: false, resumable: false },
    streamStatus: "failed" as const,
  };
  const state = {
    activeJobIdsBySession: new Map(), turnTimelinesBySession: new Map(),
    pendingConversations: new Map([[leftKey, [pending]], [rightKey, [pending]]]),
    messageStreamsByTurnStream: new Map([["right", foreignStream]]),
  } as unknown as AppState;
  expect(getConversationsForSession(sessionId, state, leftKey)[0]?.messageStream).toBeUndefined();
  expect(getConversationsForSession(sessionId, state, rightKey)[0]?.messageStream?.failure?.message).toBe("其它工作区的失败");
  state.messageStreamsByTurnStream!.set("right", { ...foreignStream, streamStatus: "open" });
  expect(messageStreamTurnIdForSession(sessionId, state, leftKey)).toBeNull();
  expect(messageStreamTurnIdForSession(sessionId, state, rightKey)).toBe(turnId);
});

test("历史 Turn 不显示实时事件流等待状态", () => {
  const historyConversation: ConversationView = { threadId: "thr_conversation",
    conversationId: "turn_history_running",
    displayMode: "history",
    sessionId: "ses_history_projection",
    userMessage: null,
    events: [],
    status: "running",
    jobId: "job_history_projection",
    pending: false,
    source: "turn",
  };

  expect(buildPendingStatusItem(historyConversation)).toBeNull();
});

test("Job/Trace 先结束时仍保留未完成的消息流订阅目标", () => {
  const sessionId = "ses_message_stream_tail";
  const sessionCacheKey = "workspace_message_stream_tail::ses_message_stream_tail";
  const state = {
    activeJobIdsBySession: new Map(),
    pendingConversations: new Map([[sessionCacheKey, [{ threadId: "thr_conversation",
      conversationId: "msg_message_stream_tail",
      displayMode: "live" as const,
      sessionId,
      userMessage: null,
      events: [],
      status: "done" as const,
      jobId: "job_message_stream_tail",
      turnId: "turn_message_stream_tail",
      pending: false,
      source: "pending" as const,
    }]]]),
    messageStreamsByTurnStream: new Map([[
      "stream_message_stream_tail",
      {
        ...createMessageStreamState(sessionId, "turn_message_stream_tail", "stream_message_stream_tail", sessionCacheKey, "thr_conversation"),
        lastEventSeq: 9,
        streamStatus: "open" as const,
      },
    ]]),
  } as unknown as AppState;

  expect(messageStreamTurnIdForSession(sessionId, state, sessionCacheKey)).toBe(
    "turn_message_stream_tail",
  );
});

test("非 pending 来源即使带有 live 标记也不显示实时等待状态", () => {
  const staleHistoryConversation: ConversationView = { threadId: "thr_conversation",
    conversationId: "turn_stale_history",
    displayMode: "live",
    sessionId: "ses_stale_history",
    userMessage: null,
    events: [],
    status: "running",
    jobId: "job_stale_history",
    pending: false,
    source: "turn",
  };

  expect(buildPendingStatusItem(staleHistoryConversation)).toBeNull();
});

test("消息流 active_state 直接映射为对应的阶段状态", () => {
  const base: ConversationView = { threadId: "thr_conversation",
    conversationId: "turn_active_state",
    displayMode: "live",
    sessionId: "ses_active_state",
    userMessage: null,
    events: [],
    status: "running",
    jobId: "job_active_state",
    pending: true,
    source: "pending",
    messageStream: {
      connectionStatus: "connected",
      streamStatus: "open",
      lastEventSeq: 1,
      failure: null,
      resumable: true,
    },
  };
  const cases = [
    ["model_output", "reasoning", "正在思考"],
    ["model_output", "text", "正在生成回复"],
    ["tool_call", "accumulating", "正在准备工具调用"],
    ["tool_execution", "running", "正在运行工具"],
  ] as const;
  for (const [kind, phase, title] of cases) {
    const item = buildPendingStatusItem({
      ...base,
      messageStream: {
        ...base.messageStream!,
        activeState: {
          kind,
          phase,
          entity_id: `${kind}-${phase}`,
          status: "running",
        },
      },
    });
    expect(item?.title).toBe(title);
  }
});

test("Job 终态先移除 live Turn，历史视图不会与实时视图并存", () => {
  const sessionId = "ses_terminal_projection";
  const jobId = "job_terminal_projection";
  const liveConversation: ConversationView = { threadId: "thr_018f0000000070008000000000000001",
    conversationId: "msg_terminal_projection",
    displayMode: "live",
    sessionId,
    userMessage: null,
    events: [],
    status: "running",
    jobId,
    pending: true,
    source: "pending",
  };
  const state = {
    pendingConversations: new Map([[sessionId, [liveConversation]]]),
    turnTimelinesBySession: new Map(),
  } as unknown as AppState;
  const terminalEvent: TraceEvent = {
    event_id: "evt_terminal_projection",
    session_id: sessionId,
    thread_id: "thr_018f0000000070008000000000000001",
    job_id: jobId,
    type: "job_completed",
    phase: "job",
    title: "任务完成",
    content: "",
    timestamp: "2026-08-21T00:00:00Z",
  };

  removePendingForTraceEvent(
    state.pendingConversations,
    sessionId,
    terminalEvent,
  );

  expect(getConversationsForSession(sessionId, state)).toEqual([]);
});

test("历史提交期间的终态 live Turn 不会被空 pending 快照删除", () => {
  const sessionId = "ses_pending_commit";
  const jobId = "job_pending_commit";
  const terminalConversation: ConversationView = { threadId: "thr_018f0000000070008000000000000001",
    conversationId: "msg_pending_commit",
    displayMode: "live",
    sessionId,
    userMessage: null,
    events: [{
      event_id: "evt_pending_commit_done",
      session_id: sessionId,
      thread_id: "thr_018f0000000070008000000000000001",
      job_id: jobId,
      type: "job_completed",
      phase: "job",
      title: "任务完成",
      content: "",
      timestamp: "2026-08-21T00:00:00Z",
    }],
    status: "done",
    jobId,
    pending: false,
    source: "pending",
  };
  const pendingMap = new Map([[sessionId, [terminalConversation]]]);
  writePendingSnapshot(
    pendingMap,
    new Map(),
    { session_id: sessionId, thread_id: "thr_018f0000000070008000000000000001", active_job_id: null, requests: [], snapshot_version: 1 },
  );

  expect(pendingMap.get(sessionId)).toEqual([terminalConversation]);
});

test("历史 stale_turn_reference 期间保留超时回合及可见错误", () => {
  const sessionId = "ses_timeout_fallback";
  const jobId = "job_timeout_fallback";
  const pendingConversation: ConversationView = { threadId: "thr_conversation",
    conversationId: "msg_timeout_fallback",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_timeout_fallback",
      session_id: sessionId,
      thread_id: "thr_conversation",
      role: "user",
      content: "长任务",
      attachments: [],
      metadata: {},
      created_at: "2026-08-31T14:33:00Z",
      updated_at: "2026-08-31T14:33:00Z",
    },
    events: [],
    status: "running",
    jobId,
    pending: true,
    source: "pending",
  };
  const pendingMap = new Map([[sessionId, [pendingConversation]]]);
  const timeoutEvent: TraceEvent = {
    event_id: "evt_timeout_fallback",
    session_id: sessionId,
    thread_id: "thr_conversation",
    job_id: jobId,
    type: "job_failed",
    phase: "job",
    title: "任务超时",
    content: "Job 执行超过总超时上限",
    status: "failed",
    timestamp: "2026-08-31T14:43:00Z",
    skill_names: [],
    payload: { code: "job_timeout", error: "Job 执行超过总超时上限" },
  };

  preservePendingTerminalConversation(
    pendingMap,
    sessionId,
    timeoutEvent,
    "timed_out",
  );

  const fallback = pendingMap.get(sessionId)?.[0];
  expect(fallback?.pending).toBe(false);
  expect(fallback?.turnStatus).toBe("timed_out");
  expect(fallback?.status).toBe("error");
  expect(fallback?.userMessage?.content).toBe("长任务");
  expect(fallback?.events).toEqual([timeoutEvent]);
});

test("内部 Goal continuation 不构造用户可见会话", () => {
  const state = {
    messages: [],
    pendingConversations: new Map(),
    traceEvents: [
      {
        event_id: "evt_internal_goal",
        session_id: "ses_goal",
        job_id: "job_goal",
        type: "message_created",
        phase: "message",
        title: "用户消息已创建",
        content: "<system_reminder>继续 Goal</system_reminder>",
        status: "completed",
        tool_name: null,
        skill_names: [],
        step_id: null,
        timestamp: "2026-07-27T00:00:00Z",
        raw: {
          payload: {
            message_id: "msg_internal_goal",
            session_id: "ses_goal",
            role: "user",
            content: "<system_reminder>继续 Goal</system_reminder>",
            metadata: { internal: true, goal_continuation: true },
          },
        },
      },
    ],
  } as unknown as AppState;

  expect(getConversationsForSession("ses_goal", state)).toEqual([]);
});

test("排队中的内部准入不会伪造用户聊天回合", () => {
  const conversations = pendingSnapshotToConversations({
    session_id: "ses_report",
    thread_id: "thr_018f0000000070008000000000000001",
    active_job_id: "job_active",
    requests: [{
      job_id: "job_report",
      message_id: "msg_report",
      session_id: "ses_report",
      thread_id: "thr_018f0000000070008000000000000001",
      turn_id: null,
      execution_binding: { message_id: "msg_report", execution_id: "execution_report", admission_idempotency_key: "admission_report" },
      content: "生成分支已结束，主会话正在处理返回结果。",
      attachments: [],
      delivery_policy: "after_turn",
      enqueue_sequence: 1,
      status: "queued",
      position: 0,
      agent_id: "default",
      message_created_at: "2026-07-28T00:00:00Z",
      message_metadata: {
        internal_display_kind: "generated_session_result",
      },
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
      snapshot_version: 1,
    }],
    snapshot_version: 1,
  });

  expect(conversations).toEqual([]);
});

test("Turn detail 恢复内部展示消息时保留展示类型", () => {
  const timeline = {
    ...createSessionTurnTimeline("ses_child"),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: ["turn_child"],
    turnsById: {
      turn_child: {
        turn_id: "turn_child",
        job_id: "job_child",
        session_id: "ses_child",
        ordinal: 1,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 1,
        status: "completed" as const,
        created_at: "2026-07-28T00:00:00Z",
        updated_at: "2026-07-28T00:00:01Z",
        completed_at: "2026-07-28T00:00:01Z",
        items_view: "full" as const,
        source_message_ids: ["msg_delegated"],
        merged_job_ids: [],
        user_messages: [{
          message_id: "msg_delegated",
          content: "检查认证模块",
          attachments: [],
          metadata: { internal_display_kind: "delegated_task" },
          created_at: "2026-07-28T00:00:00Z",
        }],
        response_preview: "已完成",
        final_response: "已完成",
        items: [],
      },
    },
  };
  const state = {
    messages: [],
    pendingConversations: new Map(),
    turnTimelinesBySession: new Map([["ses_child", timeline]]),
    traceEvents: [{
      event_id: "evt_stale",
      session_id: "ses_child",
      job_id: "job_stale",
      type: "message_created",
      phase: "message",
      title: "用户消息已创建",
      content: "不应从 Trace 恢复的旧消息",
      status: "completed",
      tool_name: null,
      skill_names: [],
      step_id: null,
      timestamp: "2026-07-28T00:00:00Z",
    }],
  } as unknown as AppState;

  const conversations = getConversationsForSession("ses_child", state);
  expect(conversations).toHaveLength(1);
  expect([conversations[0]?.sessionId, conversations[0]?.threadId,
    conversations[0]?.sourceViewId, conversations[0]?.sourceViewRevision]).toEqual([
    "ses_child", "thr_conversation", "view_history_fixture", 1,
  ]);
  expect(conversations[0]?.userMessage?.content).toBe("检查认证模块");
  expect(
    conversations[0]?.userMessage?.metadata?.internal_display_kind,
  ).toBe("delegated_task");
});

test("Turn 摘要中的空内部消息保留可展开的用户气泡", () => {
  const sessionId = "ses_empty_internal";
  const turnId = "job_empty_internal";
  const state = {
    messages: [],
    pendingConversations: new Map(),
    turnTimelinesBySession: new Map([[
      sessionId,
      {
        ...createSessionTurnTimeline(sessionId),
        threadId: "thr_conversation",
        phase: "ready" as const,
        orderedTurnIds: [turnId],
        turnsById: {
          [turnId]: {
            turn_id: turnId,
            job_id: turnId,
            session_id: sessionId,
            ordinal: 1,
            source_view_id: "view_history_fixture",
            source_view_revision: 1,
            revision: 1,
            status: "completed" as const,
            created_at: "2026-07-30T07:56:41Z",
            updated_at: "2026-07-30T07:56:42Z",
            completed_at: "2026-07-30T07:56:42Z",
            items_view: "summary" as const,
            user_messages: [{
              message_id: "msg_empty_internal",
              preview: "",
              content_truncated: false,
              attachment_count: 0,
              created_at: "2026-07-30T07:56:41Z",
            }],
            user_message_count: 1,
            user_messages_truncated: false,
            response_preview: "Goal 已完成",
            preview_truncated: false,
            item_count: 1,
          },
        },
      },
    ]]),
  } as unknown as AppState;

  const conversations = getConversationsForSession(sessionId, state);

  expect(conversations).toHaveLength(1);
  expect(conversations[0]?.userMessage?.content).toBe("");
});

test("切入已有 active Job 后立即把流式文本和工具事件合入 Turn", () => {
  const sessionId = "ses_active_history";
  const jobId = "job_active_history";
  const turnId = "turn_active_history";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: [turnId],
    turnsById: {
      [turnId]: {
        turn_id: turnId,
        job_id: jobId,
        session_id: sessionId,
        ordinal: 1,
        root_logical_item_ordinal: 0,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 1,
        status: "running" as const,
        created_at: "2026-07-29T00:00:00Z",
        updated_at: "2026-07-29T00:00:01Z",
        items_view: "summary" as const,
        source_message_ids: [],
        merged_job_ids: [],
        user_messages: [],
        response_preview: "",
      },
    },
  };
  const state = {
    pendingConversations: new Map(),
    turnTimelinesBySession: new Map([[sessionId, timeline]]),
  } as unknown as AppState;
  syncActiveJobConversation(
    state.pendingConversations,
    sessionId,
    jobId,
    sessionId,
    timeline.turnsById[turnId],
    timeline.threadId,
  );
  appendTraceEventsToPendingConversations(
    state.pendingConversations,
    sessionId,
    [
      {
        event_id: "evt_live_text",
        part_id: "part_live",
        session_id: sessionId,
        thread_id: timeline.threadId,
        job_id: jobId,
        type: "text_delta",
        phase: "text",
        title: "文本增量",
        content: "正在生成",
        timestamp: "2026-07-29T00:00:02Z",
        raw: { payload: { text: "正在生成" } },
      },
      {
        event_id: "evt_live_tool",
        session_id: sessionId,
        thread_id: timeline.threadId,
        job_id: jobId,
        type: "tool_call_start",
        phase: "tool",
        title: "运行工具",
        content: "读取文件",
        timestamp: "2026-07-29T00:00:03Z",
        raw: { payload: { tool_name: "read" } },
      },
    ],
    sessionId,
    true,
  );

  const conversations = getConversationsForSession(sessionId, state);
  expect(conversations).toHaveLength(1);
  expect(conversations[0]?.turnId).toBe(turnId);
  expect(conversations[0]?.events.map((event) => event.type)).toEqual([
    "text_delta",
    "tool_call_start",
  ]);
  expect(conversations[0]?.status).toBe("running");
  expect(conversations[0]?.activeJobOverlay).toBe(true);
  expect(isLiveConversationView(conversations[0]!)).toBe(true);

  syncActiveJobConversation(state.pendingConversations, sessionId, null, sessionId, null, "thr_conversation");
  expect(state.pendingConversations.has(sessionId)).toBe(false);
});

test("活动内部 Job 没有权威 Turn 时不创建聊天 overlay", () => {
  const pending = new Map<string, ConversationView[]>();
  syncActiveJobConversation(pending, "ses_internal", "job_internal", "workspace::ses_internal", null, "thr_conversation");
  expect(pending.size).toBe(0);
});

test("活动 Turn 的正反和重复快照顺序只消费当前视图坐标并保留 live 内容", () => {
  const sessionId = "ses_active_identity";
  const jobId = "job_active_identity";
  const turnId = "turn_active_identity";
  const mapKey = "workspace::ses_active_identity";
  const activeTurn = {
    turn_id: turnId,
    job_id: jobId,
    session_id: sessionId,
    ordinal: 2,
    root_logical_item_ordinal: 16,
    source_view_id: "view_history_fixture",
    source_view_revision: 1,
    revision: 3,
    status: "running" as const,
    created_at: "2026-10-09T00:00:00Z",
    updated_at: "2026-10-09T00:00:01Z",
    items_view: "summary" as const,
    source_message_ids: ["msg_active_identity"],
    merged_job_ids: [],
    user_messages: [],
    response_preview: "历史摘要不覆盖正在生成的正文",
  };
  const live: ConversationView = { threadId: "thr_018f0000000070008000000000000001",
    conversationId: "msg_active_identity",
    displayMode: "live",
    sessionId,
    turnId,
    userMessage: null,
    assistantMessages: [{
      message_id: "msg_live_assistant",
      session_id: sessionId,
      thread_id: "thr_018f0000000070008000000000000001",
      role: "assistant",
      content: "已收到的实时正文",
      attachments: [],
      metadata: {},
      created_at: "2026-10-09T00:00:01Z",
      updated_at: "2026-10-09T00:00:01Z",
    }],
    thinkingBlocks: [{ kind: "reasoning", text: "已收到的实时思考" }],
    responseParts: [],
    events: [{
      event_id: "evt_active_identity", session_id: sessionId, thread_id: "thr_018f0000000070008000000000000001", job_id: jobId,
      type: "text_delta", phase: "text", title: "文本增量", content: "实时事件正文",
      timestamp: "2026-10-09T00:00:02Z",
    }],
    status: "running",
    jobId,
    pending: true,
    source: "pending",
  };
  const threadId = "thr_018f0000000070008000000000000001";
  const snapshot: PendingRequestList = {
    session_id: sessionId, thread_id: threadId, active_job_id: jobId, snapshot_version: 2,
    requests: [{
      session_id: sessionId, thread_id: threadId, job_id: jobId, turn_id: turnId,
      message_id: "msg_active_identity", content: "权威用户消息", attachments: [],
      execution_binding: { job_id: jobId, turn_id: turnId, admission_idempotency_key: "admission_active_identity" },
      delivery_policy: "after_turn", enqueue_sequence: 2, position: 0, status: "queued",
      agent_id: "default", message_created_at: "2026-10-09T00:00:00Z",
      message_metadata: { updated_at: "2026-10-09T00:00:01Z" },
      created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:00:01Z", snapshot_version: 2,
    }],
  };
  const expected = {
    turnId,
    turnRevision: 3,
    turnOrdinal: 2,
    rootLogicalItemOrdinal: 16,
    turnItemsView: "summary",
    turnStatus: "running",
    jobId,
    sessionId,
    displayMode: "live",
    activeJobOverlay: true,
  };
  for (const order of ["turn_first", "snapshot_first"] as const) {
    const state = {
      pendingConversations: new Map([[mapKey, [live]]]),
      activeJobIdsBySession: new Map<string, string>(),
      turnTimelinesBySession: new Map([[mapKey, createSessionTurnTimeline(mapKey)]]),
    } as unknown as AppState;
    const projectTurn = () => {
      state.turnTimelinesBySession.set(mapKey, upsertTurn({ ...createSessionTurnTimeline(mapKey), threadId: "thr_018f0000000070008000000000000001" }, activeTurn));
      syncActiveJobConversation(state.pendingConversations, sessionId, jobId, mapKey, activeTurn, threadId);
    };
    if (order === "turn_first") projectTurn();
    writePendingSnapshot(state.pendingConversations, state.activeJobIdsBySession, snapshot, mapKey);
    if (order === "snapshot_first") projectTurn();
    for (const version of [2, 3, 1]) {
      writePendingSnapshot(state.pendingConversations, state.activeJobIdsBySession,
        { ...snapshot, snapshot_version: version }, mapKey);
      const projected = getConversationsForSession(sessionId, state, mapKey)[0]!;
      expect(projected).toMatchObject(expected);
      expect(projected.assistantMessages).toBe(live.assistantMessages);
      expect(projected.events).toBe(live.events);
      expect(projected.thinkingBlocks).toBe(live.thinkingBlocks);
      expect(projected.responseParts).toBe(live.responseParts);
      expect(state.pendingConversations.get(mapKey)?.[0]?.rootLogicalItemOrdinal).toBeUndefined();
    }
    state.turnTimelinesBySession.set(mapKey, upsertTurn({ ...createSessionTurnTimeline(mapKey), threadId: "thr_018f0000000070008000000000000001" },
      { ...activeTurn, root_logical_item_ordinal: 4, revision: 4 }));
    expect(getConversationsForSession(sessionId, state, mapKey)[0]?.rootLogicalItemOrdinal).toBe(4);
    state.turnTimelinesBySession.set(mapKey, createSessionTurnTimeline(mapKey));
    expect(getConversationsForSession(sessionId, state, mapKey)[0]?.rootLogicalItemOrdinal).toBeUndefined();
    const before = state.pendingConversations.get(mapKey);
    expect(() => writePendingSnapshot(state.pendingConversations, state.activeJobIdsBySession,
      { ...snapshot, thread_id: "thr_other" }, mapKey)).toThrow("范围之外的消息");
    expect(state.pendingConversations.get(mapKey)).toBe(before);
  }
  const freshState = {
    pendingConversations: new Map<string, ConversationView[]>(),
    turnTimelinesBySession: new Map([[mapKey, upsertTurn({ ...createSessionTurnTimeline(mapKey), threadId: "thr_018f0000000070008000000000000001" }, activeTurn)]]),
  } as unknown as AppState;
  syncActiveJobConversation(freshState.pendingConversations, sessionId, jobId, mapKey, activeTurn, threadId);
  expect(getConversationsForSession(sessionId, freshState, mapKey)[0]).toMatchObject(expected);
  expect(getConversationsForSession(sessionId, freshState, mapKey)[0]?.assistantMessages).toEqual([]);
  const existing = new Map([[mapKey, [live]]]);
  const fresh = freshState.pendingConversations;
  for (const owner of [{ session_id: "ses_other", job_id: jobId },
    { session_id: sessionId, job_id: "job_other" }]) {
    for (const map of [existing, fresh]) {
      const before = map.get(mapKey);
      expect(() => syncActiveJobConversation(map, sessionId, jobId, mapKey, {
        ...activeTurn, ...owner,
      }, threadId)).toThrow("活动 Job 与权威 Turn owner 不一致");
      expect(map.get(mapKey)).toBe(before);
    }
  }
});

test("历史摘要与 replay live Turn 合并时保留回退元数据", () => {
  const sessionId = "ses_replay_metadata_merge";
  const jobId = "job_replay_metadata_merge";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: [jobId],
    turnsById: {
      [jobId]: {
        turn_id: jobId,
        job_id: jobId,
        session_id: sessionId,
        ordinal: 1,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 1,
        status: "running" as const,
        created_at: "2026-07-29T00:00:00Z",
        updated_at: "2026-07-29T00:00:01Z",
        items_view: "summary" as const,
        source_message_ids: ["msg_replay_metadata_merge"],
        user_messages: [{
          message_id: "msg_replay_metadata_merge",
          preview: "重新生成",
          content_truncated: false,
          attachment_count: 0,
          created_at: "2026-07-29T00:00:00Z",
        }],
        response_preview: "",
      },
    },
  };
  const pending: ConversationView = { threadId: "thr_conversation",
    conversationId: "msg_replay_metadata_merge",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_replay_metadata_merge",
      session_id: sessionId,
      thread_id: "thr_conversation",
      role: "user",
      content: "重新生成",
      attachments: [],
      metadata: {
        source: "optimistic_replay",
        replay_action: "regenerate",
        replaced_message_id: "user-original",
      },
      created_at: "2026-07-29T00:00:02Z",
      updated_at: "2026-07-29T00:00:02Z",
    },
    assistantMessages: [],
    events: [],
    status: "running",
    jobId,
    pending: true,
    source: "pending",
  };
  const state = {
    pendingConversations: new Map([[sessionId, [pending]]]),
    turnTimelinesBySession: new Map([[sessionId, timeline]]),
  } as unknown as AppState;

  const [conversation] = getConversationsForSession(sessionId, state);

  expect(conversation?.source).toBe("pending");
  expect(conversation?.userMessage?.metadata?.replay_action).toBe("regenerate");
  expect(conversation?.userMessage?.metadata?.replaced_message_id).toBe(
    "user-original",
  );
});

test("terminal 历史投影替换 live 镜像并保留权威 Item 统计", () => {
  const sessionId = "ses_terminal_history_merge";
  const jobId = "job_terminal_history_merge";
  const messageId = "msg_terminal_history_merge";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: [jobId],
    turnsById: {
      [jobId]: {
        turn_id: jobId,
        job_id: jobId,
        session_id: sessionId,
        ordinal: 1,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 2,
        status: "completed" as const,
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:07.100Z",
        completed_at: "2026-09-08T00:00:07.100Z",
        items_view: "summary" as const,
        source_message_ids: [messageId],
        user_messages: [{
          message_id: messageId,
          preview: "执行工具",
          content_truncated: false,
          attachment_count: 0,
          created_at: "2026-09-08T00:00:00Z",
        }],
        response_preview: "OK",
        response_parts: [{
          part_id: "final-history",
          kind: "final_text" as const,
          projection: "summary" as const,
          status: "completed" as const,
          source: { item_id: "item-final" },
          text: "OK",
          final: true,
        }],
        activity_stats: { duration_ms: 7100, item_count: 5 },
      },
    },
  };
  const pending: ConversationView = { threadId: "thr_conversation",
    conversationId: messageId,
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: messageId,
      session_id: sessionId,
      thread_id: "thr_conversation",
      role: "user",
      content: "执行工具",
      attachments: [],
      metadata: {},
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
    },
    responseParts: [{
      part_id: "reasoning-live",
      kind: "reasoning",
      projection: "streaming",
      status: "completed",
      source: { message_sequence: 1, item_id: "item-reasoning" },
      text: "Confirming test tool success with results",
      final: false,
    }],
    events: [],
    status: "done",
    jobId,
    pending: false,
    source: "pending",
  };
  const state = {
    pendingConversations: new Map([[sessionId, [pending]]]),
    turnTimelinesBySession: new Map([[sessionId, timeline]]),
    messageStreamsByTurnStream: new Map(),
  } as unknown as AppState;

  const [conversation] = getConversationsForSession(sessionId, state);

  expect(conversation?.displayMode).toBe("history");
  expect(conversation?.source).toBe("turn");
  expect(conversation?.activityStats).toEqual({ duration_ms: 7100, item_count: 5 });
  expect(conversation?.responseParts?.map((part) => part.text)).toEqual(["OK"]);
});

test("terminal live 与 history Item 数不一致时显式报告协议错误", () => {
  const sessionId = "ses_item_count_mismatch";
  const turnId = "job_item_count_mismatch";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: [turnId],
    turnsById: {
      [turnId]: {
        turn_id: turnId,
        job_id: turnId,
        session_id: sessionId,
        ordinal: 1,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 1,
        status: "completed" as const,
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:01Z",
        items_view: "summary" as const,
        activity_stats: { duration_ms: 1000, item_count: 2 },
      },
    },
  };
  const stream: MessageStreamState = {
    ...createMessageStreamState(sessionId, turnId, "stream_item_count_mismatch", sessionId, "thr_conversation"),
    streamStatus: "completed",
    connectionStatus: "terminal",
    blocks: [{
      block_id: "reasoning-1",
      model_call_id: "model-1",
      block_index: 0,
      carrier_type: "reasoning_content",
      status: "completed",
      text: "一个 live Item",
      items: [],
      redacted: false,
      projection: "final",
    }],
  };
  const state = {
    pendingConversations: new Map(),
    turnTimelinesBySession: new Map([[sessionId, timeline]]),
    messageStreamsByTurnStream: new Map([[stream.turnStreamId, stream]]),
  } as unknown as AppState;

  const [conversation] = getConversationsForSession(sessionId, state);

  expect(conversation?.activityStats?.item_count).toBe(2);
  expect(conversation?.messageStream?.protocolError).toBe(
    "Turn Item 统计不一致: live=1 history=2",
  );
});

test("同一 terminal Turn 保留历史正文并选择最高序号消息流作为诊断", () => {
  const sessionId = "ses_duplicate_message_stream";
  const turnId = "job_duplicate_message_stream";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    threadId: "thr_conversation",
    phase: "ready" as const,
    orderedTurnIds: [turnId],
    turnsById: {
      [turnId]: {
        turn_id: turnId,
        job_id: turnId,
        session_id: sessionId,
        ordinal: 1,
        source_view_id: "view_history_fixture",
        source_view_revision: 1,
        revision: 1,
        status: "completed" as const,
        created_at: "2026-08-24T00:00:00Z",
        updated_at: "2026-08-24T00:00:01Z",
        completed_at: "2026-08-24T00:00:01Z",
        items_view: "full" as const,
        user_messages: [{
          message_id: "msg_duplicate_message_stream",
          content: "测试重复消息流镜像",
          attachments: [],
          metadata: {},
          created_at: "2026-08-24T00:00:00Z",
        }],
        response_preview: "历史正文",
        final_response: "历史正文",
        items: [],
      },
    },
  };
  const stale: MessageStreamState = {
    ...createMessageStreamState(sessionId, turnId, "strm_stale", sessionId, "thr_conversation"),
    lastEventSeq: 12,
    connectionStatus: "gap" as const,
  };
  const terminal: MessageStreamState = {
    ...createMessageStreamState(sessionId, turnId, "strm_terminal", sessionId, "thr_conversation"),
    lastEventSeq: 12,
    streamStatus: "completed" as const,
    connectionStatus: "terminal" as const,
    blocks: [{
      block_id: "block_terminal",
      model_call_id: null,
      block_index: 0,
      carrier_type: "text",
      status: "completed" as const,
      text: "snapshot 已恢复",
      items: [],
      redacted: false,
      projection: "streaming",
    }],
  };
  const state = {
    pendingConversations: new Map(),
    turnTimelinesBySession: new Map([[sessionId, timeline]]),
    messageStreamsByTurnStream: new Map([
      [stale.turnStreamId, stale],
      [terminal.turnStreamId, terminal],
    ]),
  } as unknown as AppState;

  const [conversation] = getConversationsForSession(sessionId, state);

  expect(conversation?.messageStream?.streamStatus).toBe("completed");
  expect(conversation?.assistantMessages?.[0]?.content).toBe("历史正文");
  expect(conversation?.responseParts).toEqual([]);
});

test("Job failed 镜像优先于迟到的 open message stream", () => {
  const sessionId = "ses_failed_job_mirror";
  const jobId = "job_failed_job_mirror";
  const failure: TraceEvent = {
    event_id: "evt_failed_job_mirror",
    session_id: sessionId,
    thread_id: "thr_conversation",
    job_id: jobId,
    type: "job_failed",
    phase: "job",
    title: "任务失败",
    content: "No tool call found for function call output with call_id call_old",
    status: "failed",
    timestamp: "2026-08-31T18:52:00Z",
    payload: {
      error: "No tool call found for function call output with call_id call_old",
    },
  };
  const pending: ConversationView = { threadId: "thr_conversation",
    conversationId: "msg_failed_job_mirror",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_failed_job_mirror",
      session_id: sessionId,
      thread_id: "thr_conversation",
      role: "user",
      content: "请执行只读检查",
      attachments: [],
      metadata: {},
      created_at: "2026-08-31T18:47:34Z",
      updated_at: "2026-08-31T18:47:34Z",
    },
    events: [failure],
    status: "error",
    turnStatus: "failed",
    jobId,
    turnId: "turn_failed_job_mirror",
    pending: false,
    activeJobOverlay: false,
    source: "pending",
  };
  const staleStream = {
    ...createMessageStreamState(sessionId, "turn_failed_job_mirror", "strm_stale_open", sessionId, "thr_conversation"),
    lastEventSeq: 99,
    streamStatus: "open" as const,
    connectionStatus: "connected" as const,
  };
  const state = {
    pendingConversations: new Map([[sessionId, [pending]]]),
    turnTimelinesBySession: new Map(),
    messageStreamsByTurnStream: new Map([[staleStream.turnStreamId, staleStream]]),
  } as unknown as AppState;

  const [conversation] = getConversationsForSession(sessionId, state);

  expect(conversation?.status).toBe("error");
  expect(conversation?.pending).toBe(false);
  expect(conversation?.activeJobOverlay).toBe(false);
  expect(conversation?.events[0]?.content).toContain("No tool call found");
  expect(conversation?.messageStream?.streamStatus).toBe("open");
});

test("终态事件之后的迟到 running 事件不能恢复转圈", () => {
  const events: TraceEvent[] = [
    {
      event_id: "evt_terminal_first",
      session_id: "ses_terminal_priority",
      thread_id: "thr_018f0000000070008000000000000001",
      job_id: "job_terminal_priority",
      type: "job_failed",
      phase: "job",
      title: "任务失败",
      content: "provider failed",
      timestamp: "2026-08-31T18:52:00Z",
    },
    {
      event_id: "evt_late_running",
      session_id: "ses_terminal_priority",
      thread_id: "thr_018f0000000070008000000000000001",
      job_id: "job_terminal_priority",
      type: "status_change",
      phase: "job",
      title: "任务运行中",
      content: "旧 SSE",
      timestamp: "2026-08-31T18:52:01Z",
      payload: { status: "running" },
    },
  ];

  expect(statusForConversationEvents(events, "running")).toBe("error");
});
