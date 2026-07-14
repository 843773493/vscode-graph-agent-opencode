import type { TraceEvent } from "../../types/backend";
import type { ConversationView } from "../../types/frontend";
import { conversationTokenUsage } from "../tokenUsage";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

const tokenUsage = {
  input_tokens: 240,
  output_tokens: 30,
  total_tokens: 270,
  cache_read_input_tokens: 180,
  model_calls: 2,
  reported_model_calls: 2,
};

const agentEnd: TraceEvent = {
  event_id: "evt_agent_end_usage",
  part_id: null,
  session_id: "ses_usage",
  job_id: "job_usage",
  type: "agent_end",
  timestamp: "2026-07-13T00:00:00.000Z",
  payload: { token_usage: tokenUsage },
  raw: { payload: { token_usage: tokenUsage } },
};

const traceConversation = {
  conversationId: "msg_usage",
  sessionId: "ses_usage",
  userMessage: null,
  assistantMessages: [],
  events: [agentEnd],
  status: "done",
  jobId: "job_usage",
  pending: false,
  source: "messages",
} satisfies ConversationView;

const traceResult = conversationTokenUsage(traceConversation);
assert(traceResult?.totalTokens === 270, "应从持久化 agent_end 恢复总 token");
assert(
  traceResult.cacheReadInputTokens === 180,
  "应从持久化 agent_end 恢复缓存命中 token",
);

const messageConversation = {
  ...traceConversation,
  events: [],
  assistantMessages: [
    {
      message_id: "msg_assistant_usage",
      session_id: "ses_usage",
      role: "assistant",
      content: "完成",
      attachments: [],
      metadata: { token_usage: tokenUsage },
      created_at: "2026-07-13T00:00:01.000Z",
      updated_at: "2026-07-13T00:00:01.000Z",
    },
  ],
} satisfies ConversationView;

assert(
  conversationTokenUsage(messageConversation)?.modelCalls === 2,
  "trace 缺失时应从 Assistant checkpoint metadata 恢复统计",
);

assert(
  conversationTokenUsage({
    ...traceConversation,
    events: [],
  }) === null,
  "旧回复没有 token_usage 时不应伪造零值",
);
