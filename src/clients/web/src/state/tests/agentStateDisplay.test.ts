import { expect, test } from "bun:test";
import {
  buildAgentStateSummary,
  findAgentStateMessageRawContent,
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
