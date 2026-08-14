import { describe, expect, test } from "bun:test";

import {
  validateSessionExecutionSse,
  validateSseError,
  validateTraceEvent,
  validateWorkspaceFileChangeBatch,
} from "./sseRuntimeSchemas";

describe("生成的 SSE runtime validator", () => {
  test("接受权威 DTO 并拒绝非法文件变更 kind", () => {
    expect(validateWorkspaceFileChangeBatch({
      overflow: false,
      changes: [{ kind: "edit", path: "/tmp/a.ts" }],
    })).toEqual({
      overflow: false,
      changes: [{ kind: "edit", path: "/tmp/a.ts" }],
    });
    expect(() => validateWorkspaceFileChangeBatch({
      overflow: false,
      changes: [{ kind: "rename", path: "/tmp/a.ts" }],
    })).toThrow("WorkspaceFileChangeBatchDTO 校验失败");
  });

  test("拒绝缺少字段或时间无效的 Trace", () => {
    expect(() => validateTraceEvent({ event_id: "evt_missing" }))
      .toThrow("TraceEventDTO 校验失败");
    expect(() => validateTraceEvent({
      event_id: "evt_bad_time",
      session_id: "ses_1",
      job_id: "job_1",
      type: "job_started",
      phase: "job",
      title: "开始",
      content: "开始",
      timestamp: "not-a-date",
    })).toThrow("不是有效日期时间");
  });

  test("SSE error 使用生成 schema 校验", () => {
    expect(validateSseError({ message: "failed" })).toEqual({ message: "failed" });
    expect(() => validateSseError({ message: "" })).toThrow("SseErrorDTO 校验失败");
  });

  test("Job SSE 组合事件使用生成 schema 校验", () => {
    expect(validateSessionExecutionSse({
      event: {
        event_id: "event-1",
        session_id: "session-1",
        job_id: "job-1",
        type: "session.completed",
        time: "2026-07-28T06:00:00Z",
        payload: { job_id: "job-1", status: "completed" },
      },
      raw_type: "session.completed",
      raw_payload: {},
    }).event.event_id).toBe("event-1");

    expect(() => validateSessionExecutionSse({
      event: {
        event_id: "event-1",
        session_id: "session-1",
        type: "unknown.event",
        time: "2026-07-28T06:00:00Z",
        payload: {},
      },
      raw_type: "unknown.event",
    })).toThrow("SessionExecutionSseDTO 校验失败");

    expect(() => validateSessionExecutionSse({
      event: {
        event_id: "event-2",
        session_id: "session-1",
        type: "message.updated",
        time: "2026-07-28T06:00:00Z",
        payload: { garbage: true },
      },
      raw_type: "message_created",
    })).toThrow("SessionExecutionSseDTO 校验失败");
  });
});
