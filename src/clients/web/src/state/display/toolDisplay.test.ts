import { expect, spyOn, test } from "bun:test";

import type { TimelineItem } from "../timeline/timelineTypes";
import {
  deriveToolRowDisplay,
  isRoutineInternalToolItem,
  isSkillInternalToolItem,
} from "./toolDisplay";

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

function newProtocolToolItem(
  toolName: string,
  input: Record<string, unknown>,
  result: Record<string, unknown>,
): AggregatedToolItem {
  return {
    ...toolItem(toolName),
    inputText: JSON.stringify(input),
    resultText: JSON.stringify(result),
    rawStart: {},
    rawEnd: {},
  };
}

test("exec_command 使用持久终端专用展示", () => {
  const item = toolItem("exec_command");
  const display = deriveToolRowDisplay(item, true);

  expect(display.previewText).toBe("命令已完成，终端仍可打开");
  expect(display.detailsContent).toContain("term_exec_command");
  expect(display.detailsContent).toContain("终端关闭");
  expect(display.detailsContent).toContain("/workspace");
  expect(display.detailsContent).toContain("10000");
});

test("exec_command 优先使用新协议的参数和结果字段，并识别 exit_code 为 0", () => {
  const item = newProtocolToolItem(
    "exec_command",
    { cmd: "ls -la", workdir: ".", yield_time_ms: 10000 },
    {
      chunk_id: "term_live",
      output: "README.md",
      exit_code: 0,
      status: "success",
    },
  );
  const display = deriveToolRowDisplay(item, true);

  expect(display.previewText).toBe("命令已完成，终端仍可打开");
  expect(display.detailsContent).toContain("ls -la");
  expect(display.detailsContent).toContain("README.md");
  expect(display.detailsContent).toContain("命令已完成");
});

test("展开终端工具行时预览和详情共用一次参数与结果解析", () => {
  const item = newProtocolToolItem(
    "exec_command",
    { cmd: "bun run dev", workdir: "/workspace" },
    { output: "x".repeat(100_000), exit_code: 0 },
  );
  const observation = (() => {
    const parse = spyOn(JSON, "parse");
    try {
      return {
        display: deriveToolRowDisplay(item, true),
        parsedPayloads: parse.mock.calls.map(([payload]) => payload),
      };
    } finally {
      parse.mockRestore();
    }
  })();

  expect(observation.parsedPayloads).toHaveLength(2);
  expect(observation.parsedPayloads.filter((payload) => payload === item.inputText)).toHaveLength(1);
  expect(observation.parsedPayloads.filter((payload) => payload === item.resultText)).toHaveLength(1);
  expect(observation.display.previewText).toBe("命令已完成，终端仍可打开");
  expect(observation.display.detailsContent).toContain("bun run dev");
  expect(observation.display.detailsContent).toContain("x".repeat(100_000));
});

test("后台 exec_command 只返回 session_id 时显示仍在运行", () => {
  const item = newProtocolToolItem(
    "exec_command",
    { cmd: "bun run dev", yield_time_ms: 1000 },
    {
      session_id: "term_background",
      output: "服务已启动",
    },
  );
  const display = deriveToolRowDisplay(item, true);

  expect(display.previewText).toBe("命令仍在运行，终端可 attach");
  expect(display.detailsContent).toContain("命令运行中");
  expect(display.detailsContent).toContain("term_background");
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
  const display = deriveToolRowDisplay(item, true);

  expect(display.previewText).toBe("已写入，命令仍在运行");
  expect(display.detailsContent).toContain("term_running");
  expect(display.detailsContent).toContain("yes");
});

test("未知工具结果不使用会误导为成功的折叠文案", () => {
  const item = toolItem("read_file");
  item.outcomeUnknown = true;

  expect(deriveToolRowDisplay(item, false).previewText).toBe("未确认返回结果");
});

test("普通文件读取不按路径推断 Skill 来源或宣称已加载", () => {
  const item = toolItem("read_file");
  item.rawStart.args = {
    path: ".boxteam/bundled-skills/browser-control/SKILL.md",
  };

  expect(isSkillInternalToolItem(item)).toBe(false);
  expect(deriveToolRowDisplay(item, false).previewText).toBe("read_file 处理：.boxteam/bundled-skills/browser-control/SKILL.md");
});

test("Skill 加载工具由明确工具名分类，错误回执保留原结果", () => {
  const item = toolItem("skill_load");
  item.resultText = '{"status":"error","name":"browser-control"}';
  item.rawEnd.result = item.resultText;
  expect(isSkillInternalToolItem(item)).toBe(true);
  expect(deriveToolRowDisplay(item, false).previewText).toContain('"status":"error"');
});
