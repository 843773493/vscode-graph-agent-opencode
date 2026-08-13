import { describe, expect, test } from "bun:test";
import type { NodeDebugState } from "../../types/backend";
import { resolveNodeDebugSourceSelection } from "./nodeDebugViewState";

function state(overrides: Partial<NodeDebugState> = {}): NodeDebugState {
  return {
    session_id: "ses_debug_view",
    status: "idle",
    configurations: [],
    args: [],
    call_stack: [],
    breakpoints: [],
    output: [],
    evaluations: [],
    actions: [],
    configuration_revision: 0,
    requires_restart: false,
    source_changed_paths: [],
    ...overrides,
  };
}

describe("调试源码预览选择", () => {
  test("没有方案、断点或草稿时保持空白，不跟随中间编辑器文件", () => {
    expect(resolveNodeDebugSourceSelection({
      state: state(),
      selectedPath: null,
      selectedLine: null,
      draftScriptPath: "",
    })).toEqual({ path: null, focusLine: null });
  });

  test("连续跨文件暂停始终以最新顶层 frame 为准", () => {
    const first = resolveNodeDebugSourceSelection({
      state: state({
        status: "paused",
        call_stack: [{
          call_frame_id: "frame-entry",
          function_name: "main",
          url: "file:///workspace/entry.mjs",
          path: "entry.mjs",
          line: 4,
          column: 1,
          scope_names: [],
          variables: [],
        }],
      }),
      selectedPath: "worker.mjs",
      selectedLine: 2,
      draftScriptPath: "entry.mjs",
    });
    const second = resolveNodeDebugSourceSelection({
      state: state({
        status: "paused",
        call_stack: [{
          call_frame_id: "frame-worker",
          function_name: "increment",
          url: "file:///workspace/worker.mjs",
          path: "worker.mjs",
          line: 3,
          column: 1,
          scope_names: [],
          variables: [],
        }],
      }),
      selectedPath: "entry.mjs",
      selectedLine: 4,
      draftScriptPath: "entry.mjs",
    });

    expect(first).toEqual({ path: "entry.mjs", focusLine: 4 });
    expect(second).toEqual({ path: "worker.mjs", focusLine: 3 });
  });

  test("退出后保留最后真实停止位置供复查", () => {
    expect(resolveNodeDebugSourceSelection({
      state: state({
        status: "exited",
        last_stopped_frame: {
          call_frame_id: "frame-last",
          function_name: "increment",
          url: "file:///workspace/worker.mjs",
          path: "worker.mjs",
          line: 3,
          column: 1,
          scope_names: [],
          variables: [],
        },
      }),
      selectedPath: "entry.mjs",
      selectedLine: 4,
      draftScriptPath: "entry.mjs",
    })).toEqual({ path: "worker.mjs", focusLine: 3 });
  });
});
