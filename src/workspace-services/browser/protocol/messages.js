import { create, toJson } from "@bufbuild/protobuf";
import {
  BrowserClientMessageSchema,
} from "../../protocol/generated/boxteam/browser/v1/browser_input_pb.js";
import {
  BrowserServerMessageSchema,
} from "../../protocol/generated/boxteam/browser/v1/browser_page_pb.js";

function assertPlainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 必须是 JSON object`);
  }
}

function jsonSafe(value) {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("Browser 协议消息无法转换为 JSON");
  }
  return JSON.parse(serialized);
}

function rawText(raw) {
  if (typeof raw === "string") return raw;
  if (raw instanceof Uint8Array) return new TextDecoder().decode(raw);
  return String(raw);
}

function genericClientMessage(message) {
  return create(BrowserClientMessageSchema, {
    payload: {
      case: "json",
      value: {
        type: message.type,
        payload: Object.fromEntries(Object.entries(message).filter(([key]) => key !== "type")),
      },
    },
  });
}

function typedClientMessage(message) {
  if (message.type === "attach") {
    const value = { browserId: message.browserId };
    if (message.participantId !== undefined) value.participantId = message.participantId;
    return create(BrowserClientMessageSchema, {
      payload: { case: "attach", value },
    });
  }
  if (message.type === "detach") {
    const value = {};
    if (message.browserId !== undefined) value.browserId = message.browserId;
    return create(BrowserClientMessageSchema, {
      payload: { case: "detach", value },
    });
  }
  if (message.type === "viewport") {
    return create(BrowserClientMessageSchema, {
      payload: {
        case: "viewport",
        value: { width: message.width, height: message.height },
      },
    });
  }
  return genericClientMessage(message);
}

function clientJson(message) {
  const json = toJson(BrowserClientMessageSchema, message);
  const payloadCase = Object.keys(json)[0];
  if (!payloadCase) {
    throw new Error("Browser WebSocket 消息缺少 payload");
  }
  if (payloadCase === "json") {
    return { type: json.json.type, ...json.json.payload };
  }
  return { type: payloadCase, ...json[payloadCase] };
}

export function parseBrowserClientMessage(raw) {
  const message = JSON.parse(rawText(raw));
  assertPlainObject(message, "Browser WebSocket 消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Browser WebSocket 消息缺少 type: ${message.type}`);
  }
  return clientJson(typedClientMessage(jsonSafe(message)));
}

export function encodeBrowserClientMessage(message) {
  assertPlainObject(message, "Browser 客户端消息");
  const safeMessage = jsonSafe(message);
  return JSON.stringify(clientJson(typedClientMessage(safeMessage)));
}

export function encodeBrowserServerMessage(message) {
  assertPlainObject(message, "Browser 服务端消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Browser 服务端消息缺少 type: ${message.type}`);
  }
  const safeMessage = jsonSafe(message);
  const proto = create(BrowserServerMessageSchema, {
    payload: {
      case: "json",
      value: {
        type: safeMessage.type,
        payload: Object.fromEntries(Object.entries(safeMessage).filter(([key]) => key !== "type")),
      },
    },
  });
  const json = toJson(BrowserServerMessageSchema, proto);
  return JSON.stringify({ type: json.json.type, ...json.json.payload });
}
