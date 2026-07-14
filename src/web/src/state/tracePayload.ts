import type { TraceEvent } from "../types/backend";

export type TraceEventInfo = {
  eventType: string;
  payload: Record<string, unknown>;
  timestamp: string | null;
  eventId: string | null;
  partId: string | null;
};

function getRequiredString(
  payload: Record<string, unknown>,
  key: string,
  eventType: string,
): string {
  const value = payload[key];
  if (typeof value !== "string" || value.trim() === "") {
    const detail = JSON.stringify(payload, null, 2);
    console.error(`事件结构异常 ${eventType}`, detail);
    throw new Error(
      `事件 ${eventType} 缺少必需字段 ${key}\n完整结构:\n${detail}`,
    );
  }
  return value;
}

export function getOptionalString(
  payload: Record<string, unknown>,
  key: string,
): string {
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

function stringifyPayload(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value, null, 2);
}

export function formatToolDetail(value: unknown): string {
  const text = stringifyPayload(value);
  const trimmed = text.trim();
  if (!trimmed) {
    return "";
  }
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) {
    return text;
  }
  try {
    return stringifyPayload(JSON.parse(trimmed));
  } catch {
    return text;
  }
}

export function extractEventInfo(event: TraceEvent): TraceEventInfo {
  // 后端 DTO 格式可能将真实 payload 嵌套在 raw.payload 中。
  const directPayload = event.payload ?? {};
  const hasDirectPayload = Object.keys(directPayload).length > 0;
  const innerPayload = event.raw?.payload ?? {};
  const hasInnerPayload = Object.keys(innerPayload).length > 0;
  const dtoPayload = {
    title: event.title,
    message: event.content,
    status: event.status,
    tool_name: event.tool_name,
  };
  const effectivePayload = hasDirectPayload
    ? directPayload
    : hasInnerPayload
      ? innerPayload
      : dtoPayload;
  return {
    eventType: event.type,
    payload: effectivePayload,
    timestamp: event.timestamp ?? null,
    eventId: event.event_id ?? null,
    partId: event.part_id ?? null,
  };
}

export function normalizeTraceData(
  eventType: string,
  payload: Record<string, unknown>,
): {
  kind:
    | "thought"
    | "tool_call"
    | "tool_result"
    | "response"
    | "system"
    | "error";
  title: string;
  summary: string;
  content: string;
} {
  const type = String(eventType ?? "").toLowerCase();
  const message = getOptionalString(payload, "message");
  const resultText = getOptionalString(payload, "result");
  const toolName = getOptionalString(payload, "tool_name");
  const modelName = getOptionalString(payload, "model");
  const errorText = String(
    payload.error ?? payload.message ?? payload.detail ?? "",
  ).trim();

  if (type === "tool_call_start") {
    const requiredToolName = getRequiredString(payload, "tool_name", type);
    return {
      kind: "tool_call",
      title: `调用工具 ${requiredToolName}`,
      summary: String(payload.phase ?? ""),
      content: message || resultText || `正在调用 ${requiredToolName}`,
    };
  }

  if (type === "tool_call_end" || type === "file_write") {
    const requiredToolName =
      type === "tool_call_end"
        ? getRequiredString(payload, "tool_name", type)
        : "";
    const requiredPath =
      type === "file_write" ? getRequiredString(payload, "path", type) : "";
    return {
      kind: "tool_result",
      title:
        type === "tool_call_end"
          ? `工具结果 ${requiredToolName}`
          : `文件写入 ${requiredPath}`,
      summary: type === "tool_call_end" ? "工具执行完成" : "文件已写入",
      content: resultText || message || requiredPath,
    };
  }

  if (type === "session_interrupted") {
    return {
      kind: "system",
      title: "会话已打断",
      summary: errorText,
      content: message || errorText || "会话已打断",
    };
  }

  if (type === "job_cancelled") {
    return {
      kind: "system",
      title: "任务已取消",
      summary: errorText,
      content: message || errorText || "任务已取消",
    };
  }

  if (type === "error" || type === "job_failed") {
    return {
      kind: "error",
      title: "执行异常",
      summary: errorText,
      content: String(
        payload.stack ??
          payload.detail ??
          payload.message ??
          payload.error ??
          errorText ??
          "",
      ),
    };
  }

  if (type === "llm_empty_response") {
    const modelName = getOptionalString(payload, "model");
    const msg = getOptionalString(payload, "message");
    return {
      kind: "error",
      title: "LLM 空响应",
      summary: modelName
        ? `模型 ${modelName} 未返回内容`
        : "模型未返回任何内容",
      content:
        msg || "Agent 已执行但 LLM 返回空响应。请检查模型配置或 API 连接。",
    };
  }

  // 防御：model_call 是已废弃的事件类型，不应再出现。fallthrough 到通用 system 卡片。
  if (type === "model_call") {
    console.warn(
      "normalizeTraceData: 已废弃的事件类型 model_call 已收到, payload=",
      payload,
    );
  }

  return {
    kind: "system",
    title: `事件 ${eventType}`,
    summary: [
      toolName ? `工具: ${toolName}` : "",
      modelName ? `模型: ${modelName}` : "",
    ]
      .filter(Boolean)
      .join(" · "),
    content: message || resultText || errorText,
  };
}
