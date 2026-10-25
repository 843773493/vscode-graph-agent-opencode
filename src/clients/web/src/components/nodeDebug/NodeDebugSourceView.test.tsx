import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import type { NodeDebugState } from "../../types/backend";
import NodeDebugSourceView from "./NodeDebugSourceView";

function debugState(status: NodeDebugState["status"]): NodeDebugState {
  return {
    session_id: "session-test",
    status,
    configurations: [],
    args: [],
    call_stack: status === "paused"
      ? [{
        call_frame_id: "frame-test",
        function_name: "main",
        url: "file:///workspace/src/main.js",
        path: "src/main.js",
        line: 42,
        column: 1,
        scope_names: ["local"],
        variables: [{ name: "count", value: "2", scope: "local" }],
      }]
      : [],
    breakpoints: [],
    output: status === "exited" ? ["completed"] : [],
    evaluations: [],
    actions: [],
    source_changed_paths: [],
    thread_id: "thread-main",
  };
}

function renderSourceView(
  state: NodeDebugState | null,
  status: NodeDebugState["status"],
): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      <NodeDebugSourceView
        apiPort={8010}
        workspaceId="workspace-test"
        sessionId="session-test"
        state={state}
        status={status}
        sourcePath={null}
        sourceFocusLine={null}
        breakpoints={[]}
        actionBusy={false}
        extensionWindow={false}
        breakpointLine=""
        setBreakpointLine={() => undefined}
        breakpointCondition=""
        setBreakpointCondition={() => undefined}
        onChangeBreakpoint={() => undefined}
        onAddBreakpoint={() => undefined}
        onClearBreakpoint={() => undefined}
        onSelectSource={() => undefined}
        onShowConfiguration={() => undefined}
        onShowContext={() => undefined}
        onShowConsole={() => undefined}
        onOpenWorkspacePath={async () => undefined}
      />,
    );
  });
  return renderer;
}

describe("NodeDebugSourceView 的状态布局", () => {
  test("暂停、退出和空闲共用状态槽，未选入口时只显示一条说明", () => {
    const idle = renderSourceView(null, "idle");
    const paused = renderSourceView(debugState("paused"), "paused");
    const exited = renderSourceView(debugState("exited"), "exited");

    expect(idle.root.findAllByProps({ className: "node-debug-source-status" })).toHaveLength(1);
    expect(idle.root.findByProps({ className: "node-debug-source-status" }).children).toHaveLength(0);
    expect(idle.root.findAllByProps({ className: "debug-empty-state compact" })).toHaveLength(1);

    const pausedSlot = paused.root.findByProps({ className: "node-debug-source-status" });
    expect(pausedSlot.findByProps({ role: "status" }).findByType("span").children.join(""))
      .toBe("已暂停在 src/main.js:42，1 个局部变量。");
    expect(pausedSlot.findByType("button").children.join("")).toBe("查看调用栈与变量");

    const exitedSlot = exited.root.findByProps({ className: "node-debug-source-status" });
    expect(exitedSlot.findByProps({ role: "status" }).findByType("span").children.join(""))
      .toBe("调试已结束，保留 1 行程序输出。");
    expect(exitedSlot.findByType("button").children.join("")).toBe("查看控制台");

    idle.unmount();
    paused.unmount();
    exited.unmount();
  });
});
