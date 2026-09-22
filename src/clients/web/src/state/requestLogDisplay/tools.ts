// 请求日志的工具投影：把 request.tools 与消息里的 tool_calls 归一成工具名与定义。

import type { LLMRequestLogRecord } from "../../types/backend";
import { isRecord } from "../../utils/jsonDisplay";
import { customToolDisplayCallName } from "../customTools/protocol";
import type { RequestToolDefinitionDisplay } from "./types";

export function requestToolNames(log: LLMRequestLogRecord): string[] {
  const tools = Array.isArray(log.request.tools) ? log.request.tools : [];
  const names: string[] = [];
  const seen = new Set<string>();

  for (const tool of tools) {
    let name = "";
    if (isRecord(tool)) {
      if (typeof tool.name === "string") {
        name = tool.name;
      } else if (isRecord(tool.function) && typeof tool.function.name === "string") {
        name = tool.function.name;
      }
    }
    if (!name || seen.has(name)) {
      continue;
    }
    seen.add(name);
    names.push(name);
  }

  return names;
}

function toolCallsFromMessageLike(value: unknown): Record<string, unknown>[] {
  if (!isRecord(value)) {
    return [];
  }
  const directToolCalls = Array.isArray(value.tool_calls) ? value.tool_calls : [];
  const additionalToolCalls =
    isRecord(value.additional_kwargs) && Array.isArray(value.additional_kwargs.tool_calls)
      ? value.additional_kwargs.tool_calls
      : [];
  return [...directToolCalls, ...additionalToolCalls].filter(isRecord);
}

export function collectToolCallsFromLog(
  log: LLMRequestLogRecord,
): Record<string, unknown>[] {
  const requestMessages = Array.isArray(log.request.messages) ? log.request.messages : [];
  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  return [...requestMessages, ...responseItems].flatMap(toolCallsFromMessageLike);
}

export function responseCalledToolNames(log: LLMRequestLogRecord): string[] {
  const responseItems = Array.isArray(log.response.result) ? log.response.result : [];
  const names: string[] = [];
  const seen = new Set<string>();

  for (const item of responseItems) {
    if (!isRecord(item)) {
      continue;
    }
    const directToolCalls = Array.isArray(item.tool_calls) ? item.tool_calls : [];
    const additionalToolCalls =
      isRecord(item.additional_kwargs) && Array.isArray(item.additional_kwargs.tool_calls)
        ? item.additional_kwargs.tool_calls
        : [];

    for (const call of [...directToolCalls, ...additionalToolCalls]) {
      const name = customToolDisplayCallName(call);
      if (!name || seen.has(name)) {
        continue;
      }
      seen.add(name);
      names.push(name);
    }
  }

  return names;
}

export function requestToolDefinitions(
  log: LLMRequestLogRecord,
): RequestToolDefinitionDisplay[] {
  const tools = Array.isArray(log.request.tools) ? log.request.tools : [];
  return tools.map((tool, index) => {
    const definition: Record<string, unknown> = isRecord(tool)
      ? tool
      : { value: tool };
    const functionDefinition = isRecord(definition.function)
      ? definition.function
      : null;
    const name =
      (typeof definition.name === "string" ? definition.name : "") ||
      (functionDefinition && typeof functionDefinition.name === "string"
        ? functionDefinition.name
        : "") ||
      `tool_${index + 1}`;
    const description =
      (typeof definition.description === "string" ? definition.description : "") ||
      (functionDefinition && typeof functionDefinition.description === "string"
        ? functionDefinition.description
        : "");
    const schema = isRecord(definition.args)
      ? definition.args
      : functionDefinition && isRecord(functionDefinition.parameters)
        ? functionDefinition.parameters
        : null;
    const schemaFieldCount = schema ? Object.keys(schema).length : 0;
    return {
      name,
      description,
      schemaFieldCount,
      definition: tool,
    };
  });
}
