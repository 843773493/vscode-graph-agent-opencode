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
      directories: [], event_id: 0, invalidated_roots: [], snapshots: [], overflow: false,
      changes: [{ kind: "edit", path: "/tmp/a.ts" }],
    })).toEqual({
      directories: [], event_id: 0, invalidated_roots: [], snapshots: [], overflow: false,
      changes: [{ kind: "edit", path: "/tmp/a.ts" }],
    });
    expect(() => validateWorkspaceFileChangeBatch({
      directories: [], event_id: 0, invalidated_roots: [], snapshots: [], overflow: false,
      changes: [{ kind: "rename", path: "/tmp/a.ts" }],
    })).toThrow("WorkspaceFileChangeBatchDTO 校验失败");
  });

  test("保留配对的有界目录页并拒绝旧版本、错误owner或缺字段快照", () => {
    const key = { workspace_id: "workspace-sse", scope: "workspace", root: "/workspace", path: "src" };
    const snapshot = { root_path: "/workspace", path: "src", directory_key: key, directory_revision: 12,
      items: [{ name: "a.ts", path: "src/a.ts", kind: "file", has_children: false, size: 7 }],
      truncated: true, limit: 1, next_cursor: "revision-bound-cursor" };
    const batch = { changes: [], overflow: false, event_id: 24, invalidated_roots: [],
      directories: [{ directory_key: key, directory_revision: 12, exists: true }], snapshots: [snapshot] };
    expect(validateWorkspaceFileChangeBatch(batch)).toEqual(batch);
    for (const invalid of [
      { ...snapshot, directory_revision: 11 },
      { ...snapshot, directory_key: { ...key, workspace_id: "other-owner" } },
      { ...snapshot, path: "other" },
      { ...snapshot, directory_key: undefined },
      { ...snapshot, items: undefined },
    ]) {
      expect(() => validateWorkspaceFileChangeBatch({ ...batch, snapshots: [invalid] }))
        .toThrow("校验失败");
    }
    expect(() => validateWorkspaceFileChangeBatch({ ...batch,
      directories: [{ ...batch.directories[0], exists: false }] })).toThrow("同次目录键/revision 配对");
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
