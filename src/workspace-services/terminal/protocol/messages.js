import { create, toJson } from "@bufbuild/protobuf";
import {
  TerminalClientMessageSchema,
} from "../../protocol/generated/boxteam/terminal/v1/terminal_input_pb.js";
import {
  TerminalServerMessageSchema,
} from "../../protocol/generated/boxteam/terminal/v1/terminal_output_pb.js";

function assertPlainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 必须是 JSON object`);
  }
}

function jsonSafe(value) {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("Terminal 协议消息无法转换为 JSON");
  }
  return JSON.parse(serialized);
}

function rawText(raw) {
  if (typeof raw === "string") return raw;
  if (raw instanceof Uint8Array) return new TextDecoder().decode(raw);
  return String(raw);
}

function typedClientMessage(message) {
  if (message.type === "attach") {
    const value = { terminalId: message.terminalId };
    if (message.cols !== undefined) value.cols = message.cols;
    if (message.rows !== undefined) value.rows = message.rows;
    if (message.afterSequence !== undefined) value.afterSequence = message.afterSequence;
    return create(TerminalClientMessageSchema, {
      payload: { case: "attach", value },
    });
  }
  if (message.type === "detach") {
    const value = {};
    if (message.terminalId !== undefined) value.terminalId = message.terminalId;
    return create(TerminalClientMessageSchema, {
      payload: { case: "detach", value },
    });
  }
  if (message.type === "input" || message.type === "agentInput") {
    return create(TerminalClientMessageSchema, {
      payload: {
        case: message.type === "agentInput" ? "agentInput" : "input",
        value: {
          data: message.data,
          source: message.source || (message.type === "agentInput" ? "agent" : "user"),
        },
      },
    });
  }
  if (message.type === "resize") {
    return create(TerminalClientMessageSchema, {
      payload: {
        case: "resize",
        value: { cols: message.cols, rows: message.rows },
      },
    });
  }
  if (message.type === "ack") {
    return create(TerminalClientMessageSchema, {
      payload: { case: "acknowledge", value: { sequence: message.sequence } },
    });
  }
  return create(TerminalClientMessageSchema, {
    payload: {
      case: "json",
      value: {
        type: message.type,
        payload: Object.fromEntries(Object.entries(message).filter(([key]) => key !== "type")),
      },
    },
  });
}

function typedClientJson(message) {
  const json = toJson(TerminalClientMessageSchema, message);
  const payloadCase = Object.keys(json)[0];
  if (!payloadCase) {
    throw new Error("Terminal WebSocket 消息缺少 payload");
  }
  if (payloadCase === "json") {
    return { type: json.json.type, ...json.json.payload };
  }
  const type = payloadCase === "acknowledge"
    ? "ack"
    : payloadCase === "agentInput"
      ? "agentInput"
      : payloadCase;
  return { type, ...json[payloadCase] };
}

export function parseTerminalClientMessage(raw) {
  const message = JSON.parse(rawText(raw));
  assertPlainObject(message, "Terminal WebSocket 消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Terminal WebSocket 消息缺少 type: ${message.type}`);
  }
  return typedClientJson(typedClientMessage(jsonSafe(message)));
}

export function encodeTerminalClientMessage(message) {
  assertPlainObject(message, "Terminal 客户端消息");
  const safeMessage = jsonSafe(message);
  return JSON.stringify(typedClientJson(typedClientMessage(safeMessage)));
}

export function encodeTerminalServerMessage(message) {
  assertPlainObject(message, "Terminal 服务端消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Terminal 服务端消息缺少 type: ${message.type}`);
  }
  const safeMessage = jsonSafe(message);
  const proto = create(TerminalServerMessageSchema, {
    payload: {
      case: "json",
      value: {
        type: safeMessage.type,
        payload: Object.fromEntries(Object.entries(safeMessage).filter(([key]) => key !== "type")),
      },
    },
  });
  const json = toJson(TerminalServerMessageSchema, proto);
  return JSON.stringify({ type: json.json.type, ...json.json.payload });
}
