import { expect, test } from "bun:test";
import type { LLMRequestLogRecord } from "../../types/backend";
import {
  buildUpstreamAttemptDisplay,
  buildRequestLogDisplay,
  buildRequestLogKeyFlow,
  buildRequestReplayDisplay,
  normalizeRequestLogJsonForDisplay,
} from "../requestLogDisplay/index";

function requestLog(
  overrides: Partial<LLMRequestLogRecord>,
): LLMRequestLogRecord {
  return {
    session_id: "ses_request_log_fixture",
    timestamp: 0,
    file_name: "request-log.json",
    file_path: "/tmp/request-log.json",
    request: {},
    response: {},
    upstream: {},
    ...overrides,
  };
}

test("buildUpstreamAttemptDisplay exposes merged upstream payloads", () => {
  const log = requestLog({
    upstream: {
      attempts: [
        {
          call_type: "aresponses",
          provider: "openai",
          model: "gpt-5.6-luna",
          api_base: "https://example.com/v1",
          request: { input: "hello" },
          response: { status: "completed" },
          error: null,
        },
      ],
    },
  });

  expect(buildUpstreamAttemptDisplay(log)).toEqual([
    {
      callType: "aresponses",
      provider: "openai",
      model: "gpt-5.6-luna",
      apiBase: "https://example.com/v1",
      request: { input: "hello" },
      response: { status: "completed" },
      error: null,
    },
  ]);
});

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

const multiCustomToolLog = requestLog({
  timestamp: 1,
  session_id: "ses_multi_custom_tool",
  file_path: "/tmp/request-log.json",
  file_name: "request-log.json",
  request: {
    tools: [{ name: "invoke_extension_tool" }],
    messages: [
      {
        type: "ai",
        tool_calls: [
          {
            id: "call_alpha",
            name: "invoke_extension_tool",
            args: { tool_name: "alpha_tool", arguments: {} },
          },
          {
            id: "call_beta",
            name: "invoke_extension_tool",
            args: { tool_name: "beta_tool", arguments: {} },
          },
        ],
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_alpha_result",
        name: "invoke_extension_tool",
        tool_call_id: "call_alpha",
        content: "alpha-result",
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_beta_result",
        name: "invoke_extension_tool",
        tool_call_id: "call_beta",
        content: "beta-result",
      },
    ],
  },
  response: { result: [] },
});

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

const splitResultLog = requestLog({
  timestamp: 1,
  session_id: "ses_split_custom_tool",
  file_path: "/tmp/result-log.json",
  file_name: "result-log.json",
  request: {
    tools: [{ name: "invoke_extension_tool" }],
    messages: [
      {
        type: "tool",
        role: "tool",
        id: "msg_split_result",
        name: "invoke_extension_tool",
        tool_call_id: "call_split",
        content: "4568",
      },
    ],
  },
  response: { result: [] },
});
const splitCallLog = requestLog({
  timestamp: 2,
  session_id: "ses_split_custom_tool",
  file_path: "/tmp/call-log.json",
  file_name: "call-log.json",
  request: {
    tools: [{ name: "invoke_extension_tool" }],
    messages: [],
  },
  response: {
    result: [
      {
        type: "ai",
        tool_calls: [
          {
            id: "call_split",
            name: "invoke_extension_tool",
            args: { tool_name: "test_tool_2", arguments: {} },
          },
        ],
      },
    ],
  },
});
const splitRequestKeyFlow = buildRequestLogKeyFlow([splitResultLog, splitCallLog]);
assert(
  splitRequestKeyFlow.customToolResults.some(
    (item) => item.toolName === "test_tool_2" && item.resultText === "4568",
  ),
  "请求日志应先收集全部 tool_call_id 映射，再归因跨请求文件的扩展工具结果",
);

const retryAfterInvalidInvocationLog = requestLog({
  timestamp: 3,
  session_id: "ses_retry_after_invalid_invocation",
  file_path: "/tmp/retry-after-invalid-invocation.json",
  file_name: "retry-after-invalid-invocation.json",
  request: {
    tools: [{ name: "invoke_extension_tool" }],
    messages: [
      {
        type: "ai",
        tool_calls: [
          {
            id: "call_invalid",
            name: "invoke_extension_tool",
            args: {},
          },
          {
            id: "call_valid",
            name: "invoke_extension_tool",
            args: { tool_name: "test_tool_2", arguments: {} },
          },
        ],
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_invalid_invocation",
        name: "invoke_extension_tool",
        tool_call_id: "call_invalid",
        content:
          "Error invoking tool 'invoke_extension_tool' with kwargs {} with error:\n tool_name: Field required\n Please fix the error and try again.",
      },
      {
        type: "tool",
        role: "tool",
        id: "msg_valid_invocation",
        name: "invoke_extension_tool",
        tool_call_id: "call_valid",
        content: "4568",
      },
    ],
  },
  response: { result: [] },
});
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

const malformedArgumentLog = requestLog({
  timestamp: 2,
  session_id: "ses_malformed_argument",
  file_path: "/tmp/malformed-request-log.json",
  file_name: "malformed-request-log.json",
  request: {
    tools: [{ name: "invoke_extension_tool" }],
    messages: [],
  },
  response: {
    result: [
      {
        role: "assistant",
        tool_calls: [
          {
            id: "call_bad_json",
            name: "invoke_extension_tool",
            arguments: "{\"tool_name\":\"test_tool_2\"",
          },
        ],
      },
    ],
  },
});
const malformedDisplay = buildRequestLogDisplay(malformedArgumentLog);
assert(
  malformedDisplay.calledToolNames.includes("invoke_extension_tool"),
  "请求日志遇到不完整 JSON 参数时不应崩溃，应退回显示固定入口",
);

// 条目概览的相位标签是请求日志面板的可见文案，三条分支都必须被覆盖。
const toolCallPhase = buildRequestLogDisplay(
  requestLog({
    request: { model_name: "gpt-5.6-luna", messages: [{ type: "human", content: "hi" }] },
    response: {
      result: [
        {
          type: "ai",
          tool_calls: [{ id: "call_phase", name: "read_file", args: {} }],
        },
      ],
    },
  }),
);
assert(
  toolCallPhase.phaseLabel === "工具调用请求",
  "响应含 tool_calls 时相位必须显示工具调用请求",
);
assert(
  toolCallPhase.model === "gpt-5.6-luna",
  "概览模型名应优先取 request.model_name",
);
assert(
  toolCallPhase.messageCount === 1 && toolCallPhase.responseMessageCount === 1,
  "概览应分别统计请求与响应消息数",
);
assert(
  toolCallPhase.calledToolNames.join(",") === "read_file",
  "概览应列出响应实际调用的工具名",
);

const naturalLanguagePhase = buildRequestLogDisplay(
  requestLog({
    request: { messages: [] },
    response: { result: [{ type: "ai", content: "最终回复" }] },
  }),
);
assert(
  naturalLanguagePhase.phaseLabel === "自然语言回复",
  "响应有自然语言正文时相位必须显示自然语言回复",
);
assert(
  naturalLanguagePhase.responseText === "最终回复",
  "概览应投影响应的自然语言正文",
);

const intermediatePhase = buildRequestLogDisplay(
  requestLog({ request: { messages: [] }, response: { result: [] } }),
);
assert(
  intermediatePhase.phaseLabel === "中间请求",
  "既无工具调用也无正文时相位必须显示中间请求",
);
assert(
  intermediatePhase.model === "unknown_model",
  "缺少模型信息时概览应回退为 unknown_model",
);

const replayLog = requestLog({
  timestamp: 4,
  session_id: "ses_request_replay",
  file_path: "/tmp/request-replay.json",
  file_name: "request-replay.json",
  request: {
    messages: [{ type: "human", content: "测试" }],
    system_message: { type: "system", content: [{ type: "text", text: "最终提示词" }] },
    tools: [
      {
        name: "read_file",
        description: "读取文件",
        args: { type: "object", properties: {} },
      },
    ],
    replay: {
      schema_version: 1,
      message_count: 1,
      system_prompt_char_count: 123,
      prompt_components: [
        {
          source: "agent_factory",
          label: "默认指令",
          operation: "append",
          content_blocks: [{ type: "text", text: "基础提示词" }],
          block_count: 1,
          char_count: 5,
        },
        {
          source: "WorkspaceAgentsMiddleware",
          label: "工作区 AGENTS.md",
          operation: "append",
          content_blocks: [{ type: "text", text: "工作区规则" }],
          block_count: 1,
          char_count: 5,
        },
      ],
      tools: { count: 1, names: ["read_file"], schema_char_count: 88 },
    },
  },
  response: { result: [] },
});

const replayDisplay = buildRequestReplayDisplay(replayLog);
assert(!replayDisplay.legacy, "新日志应读取后端记录的 Prompt 来源，而不是退回旧日志模式");
assert(
  replayDisplay.promptComponents.map((item) => item.label).join(",") ===
    "默认指令,工作区 AGENTS.md",
  "请求组成应按 middleware 实际执行顺序展示",
);
assert(
  replayDisplay.tools.length === 1 && replayDisplay.tools[0]?.name === "read_file",
  "请求组成应包含当次真正发送给模型的工具定义",
);
assert(
  replayDisplay.systemPromptCharCount === 123 && replayDisplay.toolSchemaCharCount === 88,
  "折叠摘要应优先使用后端记录的统计值",
);

// 旧日志（无 replay 元数据）只允许标记来源未记录；即使日志里残留
// system_message，也不得再从它合成第二套 Prompt 组成。
const legacyReplay = buildRequestReplayDisplay({
  ...replayLog,
  request: {
    messages: [],
    tools: [],
    system_message: { content: "旧日志最终提示词" },
  },
});
assert(legacyReplay.legacy, "缺少 replay 元数据的日志必须标记来源未记录");
assert(
  legacyReplay.promptComponents.length === 0
    && !legacyReplay.promptComponents.some((item) => item.source === "legacy_log"),
  "旧日志不得从 system_message 合成 Prompt 组成",
);

const normalized = normalizeRequestLogJsonForDisplay({
  response: {
    content: [
      { type: "text", text: "你" },
      { type: "text", text: "好" },
      { type: "reasoning", reasoning: "先" },
      { type: "reasoning", reasoning: "想" },
      { type: "text_delta", payload: { text: "A" } },
      { type: "text_delta", payload: { text: "B" } },
      { type: "text_delta", payload: { text: "C" }, source: "another" },
    ],
  },
}) as { response: { content: Array<Record<string, unknown>> } };
const normalizedContent = normalized.response.content;
assert(normalizedContent.length === 4, "连续的文本、reasoning 和 text_delta 应分别合并");
assert(normalizedContent[0]?.text === "你好", "连续 text 块应合并为一个展示块");
assert(normalizedContent[1]?.reasoning === "先想", "连续 reasoning 块应合并为一个展示块");
assert(
  (normalizedContent[2]?.payload as { text: string }).text === "AB",
  "同元数据的连续 text_delta 应合并",
);
assert(
  (normalizedContent[3]?.payload as { text: string }).text === "C",
  "来源不同的 text_delta 不应错误合并",
);
