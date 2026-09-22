// 请求重放投影：把后端记录的 Prompt 组成、工具定义与统计值投影成展示模型。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import { requestToolDefinitions } from "./tools";
import type {
  RequestPromptComponentDisplay,
  RequestReplayDisplay,
} from "./types";

function numberField(value: unknown, key: string): number | null {
  if (!isRecord(value)) {
    return null;
  }
  const item = value[key];
  return typeof item === "number" && Number.isFinite(item) ? item : null;
}

function replayContentBlocks(value: unknown): unknown[] {
  if (Array.isArray(value)) {
    return value;
  }
  if (typeof value === "string") {
    return [{ type: "text", text: value }];
  }
  return value === undefined || value === null ? [] : [value];
}

function contentCharCount(value: unknown): number {
  if (typeof value === "string") {
    return value.length;
  }
  if (Array.isArray(value)) {
    return value.reduce((total, item) => total + contentCharCount(item), 0);
  }
  if (isRecord(value)) {
    return Object.values(value).reduce<number>(
      (total, item) => total + contentCharCount(item),
      0,
    );
  }
  return 0;
}

export function buildRequestReplayDisplay(
  log: LLMRequestLogRecord,
): RequestReplayDisplay {
  const replay = isRecord(log.request.replay) ? log.request.replay : null;
  const rawComponents = replay && Array.isArray(replay.prompt_components)
    ? replay.prompt_components
    : [];
  const promptComponents = rawComponents.flatMap((item, index) => {
    if (!isRecord(item)) {
      return [];
    }
    const contentBlocks = replayContentBlocks(item.content_blocks);
    const operation = item.operation === "replace" ? "replace" : "append";
    return [{
      source: typeof item.source === "string" ? item.source : "unknown_middleware",
      label: typeof item.label === "string" ? item.label : `Prompt 组成 ${index + 1}`,
      operation,
      contentBlocks,
      blockCount: numberField(item, "block_count") ?? contentBlocks.length,
      charCount: numberField(item, "char_count") ?? contentCharCount(contentBlocks),
    } satisfies RequestPromptComponentDisplay];
  });

  // 缺少 replay 元数据的日志只标记来源未记录，不再从 system_message 伪造
  // 一份 Prompt 组成：真实落盘日志的 request.system_message 恒为 null，
  // 该合成分支对任何可达输入都产不出内容。
  const legacy = promptComponents.length === 0;

  const tools = requestToolDefinitions(log);
  const replayTools = replay && isRecord(replay.tools) ? replay.tools : null;
  const requestMessages = Array.isArray(log.request.messages)
    ? log.request.messages
    : [];
  return {
    schemaVersion: replay ? numberField(replay, "schema_version") : null,
    legacy,
    promptComponents,
    tools,
    messageCount: replay
      ? numberField(replay, "message_count") ?? requestMessages.length
      : requestMessages.length,
    systemPromptCharCount: replay
      ? numberField(replay, "system_prompt_char_count") ??
        contentCharCount(promptComponents.map((item) => item.contentBlocks))
      : contentCharCount(promptComponents.map((item) => item.contentBlocks)),
    toolSchemaCharCount: replayTools
      ? numberField(replayTools, "schema_char_count") ?? 0
      : contentCharCount(log.request.tools),
  };
}

export function requestPromptComponentText(
  component: RequestPromptComponentDisplay,
): string {
  return component.contentBlocks
    .map((block) => {
      if (typeof block === "string") {
        return block;
      }
      if (isRecord(block) && typeof block.text === "string") {
        return block.text;
      }
      return JSON.stringify(block, null, 2);
    })
    .filter((text) => text.length > 0)
    .join("\n\n");
}
