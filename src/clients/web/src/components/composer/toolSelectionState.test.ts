import { describe, expect, test } from "bun:test";
import type { ToolCatalogItem } from "../../types/toolTesting";
import {
  applyToolSelectionChanges,
  restoreToolSelectionAfterSaveFailure,
} from "./toolSelectionState";

function makeTool(toolId: string, executionEnabled: boolean): ToolCatalogItem {
  return {
    tool_id: toolId,
    name: toolId,
    origin: "builtin",
    description: "工具描述",
    parameters: {},
    category: "general",
    group_id: "default",
    group_name: "默认工具",
    kind: "default",
    execution_enabled: executionEnabled,
    model_visible: executionEnabled,
    test_supported: false,
  };
}

describe("工具选择状态恢复", () => {
  test("乐观更新只改目标工具的双状态", () => {
    const tools = [makeTool("read_file", true), makeTool("write_file", true)];

    expect(applyToolSelectionChanges(tools, [
      {
        tool_id: "read_file",
        execution_enabled: false,
        model_visible: false,
      },
    ])).toEqual([
      makeTool("read_file", false),
      makeTool("write_file", true),
    ]);
  });

  test("刷新失败时恢复保存前的完整目录，刷新成功时采用后端完整目录", () => {
    const previous = [makeTool("read_file", true)];
    const refreshed = [makeTool("read_file", false)];

    expect(restoreToolSelectionAfterSaveFailure(previous, null)).toBe(previous);
    expect(restoreToolSelectionAfterSaveFailure(previous, refreshed)).toBe(refreshed);
  });
});
