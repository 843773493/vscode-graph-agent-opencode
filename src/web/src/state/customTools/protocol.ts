import { isRecord } from "../../utils/jsonDisplay";

export const CUSTOM_TOOL_INVOKER_NAME = "invoke_custom_tool";
export const UNKNOWN_CUSTOM_TOOL_NAME = "unknown_custom_tool";
export const INVALID_CUSTOM_TOOL_CALL_NAME = "invalid_custom_tool_call";

export function safeParseJsonRecord(value: string): Record<string, unknown> | null {
  const trimmed = value.trim();
  if (!trimmed.startsWith("{")) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  return isRecord(parsed) ? parsed : null;
}

export function customToolCallName(value: unknown): string {
  if (!isRecord(value)) {
    return "";
  }
  if (typeof value.name === "string") {
    return value.name;
  }
  if (isRecord(value.function) && typeof value.function.name === "string") {
    return value.function.name;
  }
  return "";
}

export function customToolCallId(value: unknown): string {
  if (!isRecord(value)) {
    return "";
  }
  if (typeof value.tool_call_id === "string") {
    return value.tool_call_id;
  }
  if (typeof value.id === "string") {
    return value.id;
  }
  return "";
}

export function customToolCallArgs(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) {
    return {};
  }
  if (isRecord(value.args)) {
    return value.args;
  }
  if (isRecord(value.arguments)) {
    return value.arguments;
  }
  if (typeof value.arguments === "string") {
    return safeParseJsonRecord(value.arguments) ?? {};
  }
  if (isRecord(value.function)) {
    const functionDef = value.function;
    if (isRecord(functionDef.arguments)) {
      return functionDef.arguments;
    }
    if (typeof functionDef.arguments === "string") {
      return safeParseJsonRecord(functionDef.arguments) ?? {};
    }
  }
  return {};
}

export function customToolTargetNameFromArgs(args: Record<string, unknown>): string {
  const value = args.tool_name;
  return typeof value === "string" ? value.trim() : "";
}

export function customToolTargetNameFromCall(call: unknown): string {
  if (customToolCallName(call) !== CUSTOM_TOOL_INVOKER_NAME) {
    return "";
  }
  return customToolTargetNameFromArgs(customToolCallArgs(call));
}

export function customToolDisplayCallName(call: unknown): string {
  const name = customToolCallName(call);
  if (name !== CUSTOM_TOOL_INVOKER_NAME) {
    return name;
  }
  const targetName = customToolTargetNameFromCall(call);
  return targetName ? `${CUSTOM_TOOL_INVOKER_NAME} -> ${targetName}` : name;
}

export function customToolInvocationLabel(targetToolName: string): string {
  return targetToolName
    ? `${CUSTOM_TOOL_INVOKER_NAME} -> ${targetToolName}`
    : CUSTOM_TOOL_INVOKER_NAME;
}
