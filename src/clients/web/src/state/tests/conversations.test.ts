import { expect, test } from "bun:test";

import {
  appendTraceEventsToPendingConversations,
  getConversationsForSession,
  pendingSnapshotToConversations,
  preservePendingTerminalConversation,
  removePendingForTraceEvent,
  statusForConversationEvents,
  syncActiveJobConversation,
  writePendingSnapshot,
} from "../conversations";
import { createSessionTurnTimeline } from "../session/turnTimeline";
import {
  buildPendingStatusItem,
  isLiveConversationView,
} from "../trace/traceAggregation";
import {
  createMessageStreamState,
  type MessageStreamState,
} from "../messageStream";
import type { AppState, ConversationView } from "../../types/frontend";
import type { TraceEvent } from "../../types/backend";

test("历史 Turn 不显示实时事件流等待状态", () => {
  const historyConversation: ConversationView = {
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

test("非 pending 来源即使带有 live 标记也不显示实时等待状态", () => {
  const staleHistoryConversation: ConversationView = {
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
  const base: ConversationView = {
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
  const liveConversation: ConversationView = {
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
  const terminalConversation: ConversationView = {
    conversationId: "msg_pending_commit",
    displayMode: "live",
    sessionId,
    userMessage: null,
    events: [{
      event_id: "evt_pending_commit_done",
      session_id: sessionId,
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
    { session_id: sessionId, active_job_id: null, requests: [], snapshot_version: 1 },
  );

  expect(pendingMap.get(sessionId)).toEqual([terminalConversation]);
});

test("历史 stale_turn_reference 期间保留超时回合及可见错误", () => {
  const sessionId = "ses_timeout_fallback";
  const jobId = "job_timeout_fallback";
  const pendingConversation: ConversationView = {
    conversationId: "msg_timeout_fallback",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_timeout_fallback",
      session_id: sessionId,
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

test("排队中的内部展示消息保留安全正文和展示类型", () => {
  const conversations = pendingSnapshotToConversations({
    session_id: "ses_report",
    active_job_id: "job_active",
    requests: [{
      job_id: "job_report",
      message_id: "msg_report",
      session_id: "ses_report",
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

  expect(conversations[0]?.userMessage?.content).not.toContain(
    "generated_session_result",
  );
  expect(
    conversations[0]?.userMessage?.metadata?.internal_display_kind,
  ).toBe("generated_session_result");
});

test("Turn detail 恢复内部展示消息时保留展示类型", () => {
  const timeline = {
    ...createSessionTurnTimeline("ses_child"),
    phase: "ready" as const,
    orderedTurnIds: ["turn_child"],
    turnsById: {
      turn_child: {
        turn_id: "turn_child",
        job_id: "job_child",
        session_id: "ses_child",
        ordinal: 1,
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
        phase: "ready" as const,
        orderedTurnIds: [turnId],
        turnsById: {
          [turnId]: {
            turn_id: turnId,
            job_id: turnId,
            session_id: sessionId,
            ordinal: 1,
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
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    phase: "ready" as const,
    orderedTurnIds: [jobId],
    turnsById: {
      [jobId]: {
        turn_id: jobId,
        job_id: jobId,
        session_id: sessionId,
        ordinal: 1,
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
  );
  appendTraceEventsToPendingConversations(
    state.pendingConversations,
    sessionId,
    [
      {
        event_id: "evt_live_text",
        part_id: "part_live",
        session_id: sessionId,
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
  expect(conversations[0]?.events.map((event) => event.type)).toEqual([
    "text_delta",
    "tool_call_start",
  ]);
  expect(conversations[0]?.status).toBe("running");
  expect(conversations[0]?.activeJobOverlay).toBe(true);
  expect(isLiveConversationView(conversations[0]!)).toBe(true);

  syncActiveJobConversation(state.pendingConversations, sessionId, null);
  expect(state.pendingConversations.has(sessionId)).toBe(false);
});

test("历史摘要与 replay live Turn 合并时保留回退元数据", () => {
  const sessionId = "ses_replay_metadata_merge";
  const jobId = "job_replay_metadata_merge";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    phase: "ready" as const,
    orderedTurnIds: [jobId],
    turnsById: {
      [jobId]: {
        turn_id: jobId,
        job_id: jobId,
        session_id: sessionId,
        ordinal: 1,
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
  const pending: ConversationView = {
    conversationId: "msg_replay_metadata_merge",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_replay_metadata_merge",
      session_id: sessionId,
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

test("同一 Turn 的重复消息流镜像优先使用最高序号的终态", () => {
  const sessionId = "ses_duplicate_message_stream";
  const turnId = "job_duplicate_message_stream";
  const timeline = {
    ...createSessionTurnTimeline(sessionId),
    phase: "ready" as const,
    orderedTurnIds: [turnId],
    turnsById: {
      [turnId]: {
        turn_id: turnId,
        job_id: turnId,
        session_id: sessionId,
        ordinal: 1,
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
    ...createMessageStreamState(sessionId, turnId, "strm_stale"),
    lastEventSeq: 12,
    connectionStatus: "gap" as const,
  };
  const terminal: MessageStreamState = {
    ...createMessageStreamState(sessionId, turnId, "strm_terminal"),
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
  expect(conversation?.responseParts?.[0]?.text).toBe("snapshot 已恢复");
});

test("Job failed 镜像优先于迟到的 open message stream", () => {
  const sessionId = "ses_failed_job_mirror";
  const jobId = "job_failed_job_mirror";
  const failure: TraceEvent = {
    event_id: "evt_failed_job_mirror",
    session_id: sessionId,
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
  const pending: ConversationView = {
    conversationId: "msg_failed_job_mirror",
    displayMode: "live",
    sessionId,
    userMessage: {
      message_id: "msg_failed_job_mirror",
      session_id: sessionId,
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
    pending: false,
    activeJobOverlay: false,
    source: "pending",
  };
  const staleStream = {
    ...createMessageStreamState(sessionId, jobId, "strm_stale_open"),
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
