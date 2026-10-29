import { parseJsonInput } from "../../../shared/serverJsonRequest.mjs";
import { normalizeTerminalClientMessage } from "../protocol/messages.js";

function protocolFailure(message, stage, cause, logger) {
  const error = new Error(message, { cause });
  try {
    logger.error("Terminal WebSocket 输入拒绝", { stage }, cause);
  } catch (diagnosticError) {
    error.cause = new AggregateError([cause, diagnosticError], "协议输入与诊断输出均失败", { cause });
  }
  return error;
}

function normalizeParsedJsonNumber(_fieldName, value) {
  if (typeof value !== "number") return value;
  if (!Number.isFinite(value)) return null;
  return Object.is(value, -0) ? 0 : value;
}

export function parseTerminalClientMessage(raw, logger) {
  const text = typeof raw === "string" ? raw
    : raw instanceof Uint8Array ? new TextDecoder().decode(raw) : String(raw);
  const message = parseJsonInput(text, {
    logger, transport: "WebSocket", stage: "websocket_json", reviver: normalizeParsedJsonNumber,
  });
  try {
    return normalizeTerminalClientMessage(message);
  } catch (cause) {
    // 仅已知schema名称可公开；扩展消息的任意type值仍只留原诊断。
    const type = ["attach", "detach", "input", "agentInput", "resize", "ack"].includes(message?.type)
      ? message.type : "json";
    const errorType = cause instanceof Error ? cause.constructor.name : typeof cause;
    throw protocolFailure(
      `Terminal WebSocket 消息不符合协议: type=${type}, field=payload, stage=websocket_schema, error=${errorType}`,
      "websocket_schema", cause, logger,
    );
  }
}

export function rejectTerminalMessageType(type, logger) {
  throw protocolFailure("未知 WebSocket 消息类型: field=type", "websocket_type",
    new Error(`未知 WebSocket 消息类型: ${type}`), logger);
}
