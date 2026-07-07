import { buildAgentStateSummary } from "../agentStateDisplay";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

const agentStateSummary = buildAgentStateSummary([
  {
    role: "assistant",
    tool_calls: [
      {
        id: "call_invalid",
        name: "invoke_custom_tool",
        args: {},
      },
      {
        id: "call_alpha",
        name: "invoke_custom_tool",
        args: { tool_name: "alpha_tool", arguments: {} },
      },
      {
        id: "call_beta",
        name: "invoke_custom_tool",
        args: { tool_name: "beta_tool", arguments: {} },
      },
    ],
  },
  {
    role: "tool",
    id: "msg_invalid_agent_state",
    name: "invoke_custom_tool",
    tool_call_id: "call_invalid",
    content:
      "Error invoking tool 'invoke_custom_tool' with kwargs {} with error:\n tool_name: Field required\n Please fix the error and try again.",
  },
  {
    role: "tool",
    id: "msg_alpha_agent_state",
    name: "invoke_custom_tool",
    tool_call_id: "call_alpha",
    content: "alpha-result",
  },
  {
    role: "tool",
    id: "msg_beta_agent_state",
    name: "invoke_custom_tool",
    tool_call_id: "call_beta",
    content: "beta-result",
  },
]);

assert(
  agentStateSummary.customToolResults.some(
    (item) =>
      item.toolName === "invalid_custom_tool_call" &&
      item.resultText.includes("tool_name: Field required"),
  ),
  "Agent State 应把空参数固定入口失败显示为 invalid_custom_tool_call",
);
assert(
  agentStateSummary.customToolResults.some(
    (item) => item.toolName === "alpha_tool" && item.resultText === "alpha-result",
  ),
  "Agent State 应按 tool_call_id 把 alpha 结果归因给 alpha_tool",
);
assert(
  agentStateSummary.customToolResults.some(
    (item) => item.toolName === "beta_tool" && item.resultText === "beta-result",
  ),
  "Agent State 应按 tool_call_id 把 beta 结果归因给 beta_tool",
);
assert(
  agentStateSummary.customToolResults.every((item) => item.toolName !== "unknown_custom_tool"),
  "Agent State 中已知的入口校验错误和成功结果都不应显示为 unknown_custom_tool",
);
