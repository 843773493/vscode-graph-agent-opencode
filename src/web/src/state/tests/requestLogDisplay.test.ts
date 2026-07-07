import type { LLMRequestLogRecord } from "../../types/backend";
import { buildRequestLogDisplay, buildRequestLogKeyFlow } from "../requestLogDisplay";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

const multiCustomToolLog = {
  timestamp: 1,
  session_id: "ses_multi_custom_tool",
  file_path: "/tmp/request-log.json",
  file_name: "request-log.json",
  request: {
    tools: [{ name: "invoke_custom_tool" }],
    messages: [
      {
        type: "ai",
        tool_calls: [
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
        type: "tool",
        role: "tool",
        id: "msg_alpha_result",
        name: "invoke_custom_tool",
        tool_call_id: "call_alpha",
        content: "alpha-result",
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_beta_result",
        name: "invoke_custom_tool",
        tool_call_id: "call_beta",
        content: "beta-result",
      },
    ],
  },
  response: { result: [] },
} as LLMRequestLogRecord;

const requestKeyFlow = buildRequestLogKeyFlow([multiCustomToolLog]);
assert(
  requestKeyFlow.customToolResults.some(
    (item) => item.toolName === "alpha_tool" && item.resultText === "alpha-result",
  ),
  "请求日志应按 tool_call_id 把 alpha 结果归因给 alpha_tool",
);
assert(
  requestKeyFlow.customToolResults.some(
    (item) => item.toolName === "beta_tool" && item.resultText === "beta-result",
  ),
  "请求日志应按 tool_call_id 把 beta 结果归因给 beta_tool",
);

const splitResultLog = {
  timestamp: 1,
  session_id: "ses_split_custom_tool",
  file_path: "/tmp/result-log.json",
  file_name: "result-log.json",
  request: {
    tools: [{ name: "invoke_custom_tool" }],
    messages: [
      {
        type: "tool",
        role: "tool",
        id: "msg_split_result",
        name: "invoke_custom_tool",
        tool_call_id: "call_split",
        content: "4568",
      },
    ],
  },
  response: { result: [] },
} as LLMRequestLogRecord;
const splitCallLog = {
  timestamp: 2,
  session_id: "ses_split_custom_tool",
  file_path: "/tmp/call-log.json",
  file_name: "call-log.json",
  request: {
    tools: [{ name: "invoke_custom_tool" }],
    messages: [],
  },
  response: {
    result: [
      {
        type: "ai",
        tool_calls: [
          {
            id: "call_split",
            name: "invoke_custom_tool",
            args: { tool_name: "test_tool_2", arguments: {} },
          },
        ],
      },
    ],
  },
} as LLMRequestLogRecord;
const splitRequestKeyFlow = buildRequestLogKeyFlow([splitResultLog, splitCallLog]);
assert(
  splitRequestKeyFlow.customToolResults.some(
    (item) => item.toolName === "test_tool_2" && item.resultText === "4568",
  ),
  "请求日志应先收集全部 tool_call_id 映射，再归因跨请求文件的扩展工具结果",
);

const retryAfterInvalidInvocationLog = {
  timestamp: 3,
  session_id: "ses_retry_after_invalid_invocation",
  file_path: "/tmp/retry-after-invalid-invocation.json",
  file_name: "retry-after-invalid-invocation.json",
  request: {
    tools: [{ name: "invoke_custom_tool" }],
    messages: [
      {
        type: "ai",
        tool_calls: [
          {
            id: "call_invalid",
            name: "invoke_custom_tool",
            args: {},
          },
          {
            id: "call_valid",
            name: "invoke_custom_tool",
            args: { tool_name: "test_tool_2", arguments: {} },
          },
        ],
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_invalid_invocation",
        name: "invoke_custom_tool",
        tool_call_id: "call_invalid",
        content:
          "Error invoking tool 'invoke_custom_tool' with kwargs {} with error:\n tool_name: Field required\n Please fix the error and try again.",
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_valid_invocation",
        name: "invoke_custom_tool",
        tool_call_id: "call_valid",
        content: "4568",
      },
    ],
  },
  response: { result: [] },
} as LLMRequestLogRecord;
const retryKeyFlow = buildRequestLogKeyFlow([retryAfterInvalidInvocationLog]);
assert(
  retryKeyFlow.customToolResults.some(
    (item) =>
      item.toolName === "invalid_custom_tool_call" &&
      item.resultText.includes("tool_name: Field required"),
  ),
  "请求日志应把空参数固定入口失败显示为 invalid_custom_tool_call，而不是 unknown_custom_tool",
);
assert(
  retryKeyFlow.customToolResults.some(
    (item) => item.toolName === "test_tool_2" && item.resultText === "4568",
  ),
  "请求日志应继续把重试后的成功结果归因给 test_tool_2",
);
assert(
  retryKeyFlow.customToolResults.every((item) => item.toolName !== "unknown_custom_tool"),
  "已知的入口校验错误和成功结果都不应显示为 unknown_custom_tool",
);

const malformedArgumentLog = {
  timestamp: 2,
  session_id: "ses_malformed_argument",
  file_path: "/tmp/malformed-request-log.json",
  file_name: "malformed-request-log.json",
  request: {
    tools: [{ name: "invoke_custom_tool" }],
    messages: [],
  },
  response: {
    result: [
      {
        role: "assistant",
        tool_calls: [
          {
            id: "call_bad_json",
            name: "invoke_custom_tool",
            arguments: "{\"tool_name\":\"test_tool_2\"",
          },
        ],
      },
    ],
  },
} as LLMRequestLogRecord;
const malformedDisplay = buildRequestLogDisplay(malformedArgumentLog);
assert(
  malformedDisplay.calledToolNames.includes("invoke_custom_tool"),
  "请求日志遇到不完整 JSON 参数时不应崩溃，应退回显示固定入口",
);
