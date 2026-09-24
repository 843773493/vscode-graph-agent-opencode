import { isRecord } from "../../utils/jsonDisplay";

export const EXTENSION_TOOL_INVOKER_NAME = "invoke_extension_tool";
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
  if (customToolCallName(call) !== EXTENSION_TOOL_INVOKER_NAME) {
    return "";
  }
  return customToolTargetNameFromArgs(customToolCallArgs(call));
}

export function customToolDisplayCallName(call: unknown): string {
  const name = customToolCallName(call);
  if (name !== EXTENSION_TOOL_INVOKER_NAME) {
    return name;
  }
  const targetName = customToolTargetNameFromCall(call);
  return targetName ? `${EXTENSION_TOOL_INVOKER_NAME} -> ${targetName}` : name;
}

/**
 * 固定入口 `invoke_extension_tool` 因缺少 tool_name 参数而失败的唯一判定：
 * 依据模型返回的原始错误文本判定，请求日志与 Agent State 必须共用这一实现。
 */
export function isCustomInvokerValidationError(resultText: string): boolean {
  return (
    resultText.includes("Error invoking tool 'invoke_extension_tool'") &&
    resultText.includes("tool_name: Field required")
  );
}
