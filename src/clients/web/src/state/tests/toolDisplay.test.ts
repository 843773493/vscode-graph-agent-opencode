import { expect, test } from "bun:test";

import type { TimelineItem } from "../timelineTypes";
import {
  formatToolCardContent,
  isRoutineInternalToolItem,
  toolCollapsedText,
} from "../toolDisplay";

type AggregatedToolItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;

function toolItem(toolName: string): AggregatedToolItem {
  return {
    kind: "aggregated_tool",
    id: `tool_${toolName}`,
    toolName,
    inputText: "",
    resultText: "",
    timestamp: null,
    rawStart: {
      args: {
        cmd: "pwd",
        workdir: "/workspace",
        yield_time_ms: 10000,
      },
    },
    rawEnd: {
      result: {
        chunk_id: "term_exec_command",
        exit_code: 0,
        output: "/workspace",
        original_token_count: 2,
      },
    },
    active: false,
  };
}

test("exec_command 使用持久终端专用展示", () => {
  const item = toolItem("exec_command");

  expect(toolCollapsedText(item)).toBe("命令已完成，终端仍可打开");
  expect(formatToolCardContent(item)).toContain("term_exec_command");
  expect(formatToolCardContent(item)).toContain("终端关闭");
  expect(formatToolCardContent(item)).toContain("/workspace");
  expect(formatToolCardContent(item)).toContain("10000");
});

test("已移除的 filesystem execute 不再属于常规内部工具", () => {
  expect(isRoutineInternalToolItem(toolItem("execute"))).toBe(false);
});

test("write_stdin 使用模型熟悉的 session_id 展示持续终端", () => {
  const item = toolItem("write_stdin");
  item.rawStart.args = {
    session_id: "term_running",
    chars: "yes\r",
  };
  item.rawEnd.result = {
    chunk_id: "term_running",
    session_id: "term_running",
    output: "yes",
    wall_time_seconds: 0.25,
  };

  expect(toolCollapsedText(item)).toBe("已写入，命令仍在运行");
  expect(formatToolCardContent(item)).toContain("term_running");
  expect(formatToolCardContent(item)).toContain("yes");
});

test("未知工具结果不使用会误导为成功的折叠文案", () => {
  const item = toolItem("read_file");
  item.outcomeUnknown = true;

  expect(toolCollapsedText(item)).toBe("未确认返回结果");
});

test("bundled Skill 的读取明确显示为 Skill 而不是项目文件", () => {
  const item = toolItem("read_file");
  item.rawStart.args = {
    path: ".boxteam/bundled-skills/browser-control/SKILL.md",
  };

  expect(toolCollapsedText(item)).toBe("已读取 skill：browser-control");
});
