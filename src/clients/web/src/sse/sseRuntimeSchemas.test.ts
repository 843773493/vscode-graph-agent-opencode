import { describe, expect, test } from "bun:test";

import {
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
      thread_id: "thr_00000000700070008000000000000001",
      job_id: "job_1",
      type: "job_started",
      phase: "job",
      title: "开始",
      content: "开始",
      timestamp: "not-a-date",
    })).toThrow("不是有效日期时间");
  });

  test("Trace 正文允许空串，但仍必须显式提供", () => {
    const event = {
      event_id: "evt_empty_content", session_id: "ses_empty_content",
      thread_id: "thr_00000000700070008000000000000001", job_id: "job_empty_content",
      type: "job_started", phase: "job", title: "任务开始", content: "",
      timestamp: "2026-10-21T00:00:00Z",
    };
    expect(validateTraceEvent(event).content).toBe("");
    for (const content of [undefined, null, 0]) {
      expect(() => validateTraceEvent({ ...event, content })).toThrow("content 必须是字符串");
    }
  });

  test("SSE error 使用生成 schema 校验", () => {
    expect(validateSseError({ message: "failed" })).toEqual({ message: "failed" });
    expect(() => validateSseError({ message: "" })).toThrow("SseErrorDTO 校验失败");
  });

});
