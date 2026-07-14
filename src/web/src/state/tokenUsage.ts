import type {
  ConversationTokenUsage,
  ConversationView,
} from "../types/frontend";
import { rawTracePayload } from "./traceEvents";

function nonNegativeInteger(
  value: unknown,
  fieldName: string,
): number {
  if (!Number.isInteger(value) || (value as number) < 0) {
    throw new TypeError(`token_usage.${fieldName} 必须是非负整数`);
  }
  return value as number;
}

function parseTokenUsage(value: unknown): ConversationTokenUsage | null {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("token_usage 必须是对象");
  }

  const usage = value as Record<string, unknown>;
  const reportedModelCalls = nonNegativeInteger(
    usage.reported_model_calls,
    "reported_model_calls",
  );
  if (reportedModelCalls === 0) {
    return null;
  }
  const rawCacheRead = usage.cache_read_input_tokens;
  return {
    inputTokens: nonNegativeInteger(usage.input_tokens, "input_tokens"),
    outputTokens: nonNegativeInteger(usage.output_tokens, "output_tokens"),
    totalTokens: nonNegativeInteger(usage.total_tokens, "total_tokens"),
    cacheReadInputTokens: rawCacheRead === null || rawCacheRead === undefined
      ? null
      : nonNegativeInteger(rawCacheRead, "cache_read_input_tokens"),
    modelCalls: nonNegativeInteger(usage.model_calls, "model_calls"),
    reportedModelCalls,
  };
}

export function conversationTokenUsage(
  conversation: ConversationView,
): ConversationTokenUsage | null {
  for (let index = conversation.events.length - 1; index >= 0; index -= 1) {
    const event = conversation.events[index];
    if (event.type !== "agent_end") {
      continue;
    }
    const usage = parseTokenUsage(rawTracePayload(event).token_usage);
    if (usage) {
      return usage;
    }
  }

  const assistantMessages = conversation.assistantMessages ?? [];
  for (let index = assistantMessages.length - 1; index >= 0; index -= 1) {
    const usage = parseTokenUsage(assistantMessages[index].metadata?.token_usage);
    if (usage) {
      return usage;
    }
  }
  return null;
}
