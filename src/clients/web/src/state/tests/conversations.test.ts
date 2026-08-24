import { expect, test } from "bun:test";

import {
  appendTraceEventsToPendingConversations,
  getConversationsForSession,
  pendingSnapshotToConversations,
  removePendingForTraceEvent,
  syncActiveJobConversation,
} from "../conversations";
import { createSessionTurnTimeline } from "../session/turnTimeline";
import {
  buildPendingStatusItem,
  isLiveConversationView,
} from "../trace/traceAggregation";
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
