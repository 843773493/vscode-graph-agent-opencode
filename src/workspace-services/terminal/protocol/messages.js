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

function normalizeJsonSerializableValue(value) {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("Terminal 协议消息无法转换为 JSON");
  }
  return JSON.parse(serialized);
}

function normalizeParsedJsonNumber(_fieldName, value) {
  if (typeof value !== "number") return value;
  if (!Number.isFinite(value)) return null;
  return Object.is(value, -0) ? 0 : value;
}

function rawText(raw) {
  if (typeof raw === "string") return raw;
  if (raw instanceof Uint8Array) return new TextDecoder().decode(raw);
  return String(raw);
}

function clientProtoFromJson(message) {
  if (message.type === "attach") {
    const value = { terminalId: message.terminalId };
    value.grantToken = message.grantToken;
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
  const { type, ...payload } = message;
  return create(TerminalClientMessageSchema, {
    payload: {
      case: "json",
      value: {
        type,
        payload,
      },
    },
  });
}

function clientMessageFromProto(message) {
  const protobufJson = toJson(TerminalClientMessageSchema, message);
  const messageOneofCase = Object.keys(protobufJson)[0];
  if (!messageOneofCase) {
    throw new Error("Terminal WebSocket 消息缺少 payload");
  }
  if (messageOneofCase === "json") {
    return { type: protobufJson.json.type, ...protobufJson.json.payload };
  }
  const type = messageOneofCase === "acknowledge"
    ? "ack"
    : messageOneofCase === "agentInput"
      ? "agentInput"
      : messageOneofCase;
  return { type, ...protobufJson[messageOneofCase] };
}

export function parseTerminalClientMessage(raw) {
  const message = JSON.parse(rawText(raw), normalizeParsedJsonNumber);
  assertPlainObject(message, "Terminal WebSocket 消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Terminal WebSocket 消息缺少 type: ${message.type}`);
  }
  return clientMessageFromProto(clientProtoFromJson(message));
}

export function encodeTerminalClientMessage(message) {
  assertPlainObject(message, "Terminal 客户端消息");
  const normalizedMessage = normalizeJsonSerializableValue(message);
  return JSON.stringify(
    clientMessageFromProto(clientProtoFromJson(normalizedMessage)),
  );
}

export function encodeTerminalServerMessage(message) {
  assertPlainObject(message, "Terminal 服务端消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Terminal 服务端消息缺少 type: ${message.type}`);
  }
  const normalizedMessage = normalizeJsonSerializableValue(message);
  const { type, ...payload } = normalizedMessage;
  const proto = create(TerminalServerMessageSchema, {
    payload: {
      case: "json",
      value: {
        type,
        payload,
      },
    },
  });
  const protobufJson = toJson(TerminalServerMessageSchema, proto);
  return JSON.stringify({
    type: protobufJson.json.type,
    ...protobufJson.json.payload,
  });
}
