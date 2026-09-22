import { expect, test } from "bun:test";
import {
  agentStateInvalidLineNote,
  buildAgentStateSummary,
  findAgentStateMessageRawContent,
  formatAgentStateLinesForDisplay,
  parseAgentStateJsonlLines,
  parseAgentStateRecords,
} from "../display/agentStateDisplay";

test("按 message_id 展开 Agent State 中默认隐藏的标记文本", () => {
  const jsonl = [
    JSON.stringify({
      role: "user",
      content: "<system_reminder>\n隐藏内容\n</system_reminder>",
      response_metadata: { message_id: "msg_internal" },
    }),
  ].join("\n");

  expect(findAgentStateMessageRawContent(jsonl, "msg_internal")).toBe(
    "<system_reminder>\n隐藏内容\n</system_reminder>",
  );
  expect(findAgentStateMessageRawContent(jsonl, "msg_missing")).toBeNull();
});

test("原始消息内容为空时仍区分于找不到消息", () => {
  const jsonl = JSON.stringify({
    role: "user",
    content: "",
    response_metadata: { message_id: "msg_empty" },
  });

  expect(findAgentStateMessageRawContent(jsonl, "msg_empty")).toBe("");
});

test("固定入口缺参数失败归为 invalid_custom_tool_call，而不是 unknown_custom_tool", () => {
  const summary = buildAgentStateSummary([
    {
      role: "tool",
      name: "invoke_extension_tool",
      tool_call_id: "call_missing_tool_name",
      content:
        "Error invoking tool 'invoke_extension_tool' with kwargs {} with error:\n tool_name: Field required\n Please fix the error and try again.",
    },
  ]);

  expect(summary.customToolResults).toHaveLength(1);
  expect(summary.customToolResults[0]?.toolName).toBe("invalid_custom_tool_call");
});

test("固定入口其它失败文本归为 unknown_custom_tool", () => {
  const summary = buildAgentStateSummary([
    {
      role: "tool",
      name: "invoke_extension_tool",
      tool_call_id: "call_other_failure",
      content: "Error invoking tool 'invoke_extension_tool' with error:\n 工具不存在",
    },
  ]);

  expect(summary.customToolResults).toHaveLength(1);
  expect(summary.customToolResults[0]?.toolName).toBe("unknown_custom_tool");
});

test("非法、截断与超长行不抛异常，且逐行标注原文片段", () => {
  const oversizedFragment = "x".repeat(900);
  const jsonl = [
    JSON.stringify({ role: "user", content: "正常行" }),
    '{"role": "assistant", "content": ',
    "这不是 JSON",
    `{"role":"user","content":"${oversizedFragment}"`,
    JSON.stringify({ role: "tool", content: "尾部正常行" }),
  ].join("\n");

  // 解析原语本身不得把 render 阶段的异常抛出去。
  const lines = parseAgentStateJsonlLines(jsonl);
  expect(lines.map((line) => line.ok)).toEqual([true, false, false, false, true]);

  const failures = lines.filter(
    (line): line is Extract<typeof lines[number], { ok: false }> => !line.ok,
  );
  expect(failures.map((line) => line.lineNumber)).toEqual([2, 3, 4]);

  // 超长损坏行被裁剪到上限，原文片段不会淹没面板。
  expect(failures[2]!.raw.length).toBe(403);
  expect(failures[2]!.raw.endsWith("...")).toBe(true);

  const display = formatAgentStateLinesForDisplay(lines);
  expect(display).toContain("正常行");
  expect(display).toContain("尾部正常行");
  expect(display).toContain("[[第 2 行解析失败");
  expect(display).toContain("[[第 3 行解析失败");
  expect(display).toContain("原文片段：这不是 JSON");

  // 非法行不得进入摘要记录，但合法行必须完整保留。
  expect(parseAgentStateRecords(jsonl)).toHaveLength(2);
});

test("解析失败标注同时给出原因与原文", () => {
  const [line] = parseAgentStateJsonlLines("{ 坏行 }");
  if (!line || line.ok) throw new Error("该行应解析失败");
  const note = agentStateInvalidLineNote(line);

  expect(note).toContain("第 1 行解析失败");
  expect(note).toContain("原文片段：{ 坏行 }");
});
