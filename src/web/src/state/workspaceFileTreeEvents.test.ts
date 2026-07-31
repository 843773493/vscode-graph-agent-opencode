import { describe, expect, test } from "bun:test";
import type { TraceEvent } from "../types/backend";
import { fileChangesFromTraceEvents } from "./workspaceFileTreeEvents";

function event(payload: Record<string, unknown>): TraceEvent {
  return {
    event_id: "evt-file-change",
    session_id: "ses-file-change",
    job_id: "job-file-change",
    type: "tool_call_end",
    timestamp: "2026-07-27T00:00:00Z",
    payload,
  };
}

describe("文件树 Agent 变更通知", () => {
  test("合并单文件和多文件变更并去重", () => {
    const changes = fileChangesFromTraceEvents([
      event({
        file_edit: { file_path: "src/a.ts", kind: "edit" },
        file_edits: [
          { file_path: "src/a.ts", kind: "edit" },
          { file_path: "src/new.ts", kind: "create" },
        ],
      }),
    ]);

    expect(changes).toEqual([
      { path: "src/a.ts", kind: "edit" },
      { path: "src/new.ts", kind: "create" },
    ]);
  });

  test("忽略与文件编辑无关的工具事件", () => {
    expect(fileChangesFromTraceEvents([event({ tool_name: "read_file" })])).toEqual([]);
  });
});
