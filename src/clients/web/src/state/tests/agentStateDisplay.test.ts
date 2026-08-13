import { expect, test } from "bun:test";
import { findAgentStateMessageRawContent } from "../agentStateDisplay";

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
