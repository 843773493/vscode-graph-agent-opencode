import { describe, expect, test } from "bun:test";

import type { NodeDebugBreakpoint } from "../../types/backend";
import {
  nodeDebugBreakpointKind,
  nodeDebugBreakpointLabel,
} from "./NodeDebugBreakpointGutter";

function breakpoint(
  overrides: Partial<NodeDebugBreakpoint> = {},
): NodeDebugBreakpoint {
  return {
    breakpoint_id: "node-bp-test",
    path: "fixture.mjs",
    line: 3,
    created_at: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

describe("NodeDebugBreakpointGutter", () => {
  test("区分四类断点并生成用户可读标签", () => {
    expect(nodeDebugBreakpointKind(breakpoint())).toBe("ordinary");
    expect(nodeDebugBreakpointKind(breakpoint({ condition: "count > 2" }))).toBe("condition");
    expect(nodeDebugBreakpointKind(breakpoint({ hit_condition: 3 }))).toBe("hit");
    expect(nodeDebugBreakpointKind(breakpoint({ log_message: "count={count}" }))).toBe("log");
    expect(nodeDebugBreakpointLabel(breakpoint({ hit_condition: 3 }))).toBe("命中次数断点：第 3 次");
    expect(nodeDebugBreakpointLabel(breakpoint({ log_message: "count={count}" }))).toBe("日志点：count={count}");
  });

  test("组合定义优先展示日志点类型", () => {
    const combined = breakpoint({
      condition: "count > 0",
      hit_condition: 2,
      log_message: "count={count}",
    });

    expect(nodeDebugBreakpointKind(combined)).toBe("log");
    expect(nodeDebugBreakpointLabel(combined)).toBe("日志点：count={count}");
  });
});
