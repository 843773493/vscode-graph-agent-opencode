import { expect, test } from "bun:test";

import {
  appendTraceEventsToPendingConversations,
  getConversationsForSession,
  pendingSnapshotToConversations,
  syncActiveJobConversation,
} from "../conversations";
import { createSessionTurnTimeline } from "../session/turnTimeline";
import type { AppState } from "../../types/frontend";

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
      kind: "queued",
      position: 0,
      agent_id: "default",
      message_created_at: "2026-07-28T00:00:00Z",
      message_metadata: {
        internal_display_kind: "generated_session_result",
      },
      created_at: "2026-07-28T00:00:00Z",
      updated_at: "2026-07-28T00:00:00Z",
    }],
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

  syncActiveJobConversation(state.pendingConversations, sessionId, null);
  expect(state.pendingConversations.has(sessionId)).toBe(false);
});
