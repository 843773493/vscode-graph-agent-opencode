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

function normalizeJsonSerializableValue(value) {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new Error("Browser 协议消息无法转换为 JSON");
  }
  return JSON.parse(serialized);
}

function jsonFallbackClientProto(message) {
  const { type, ...payload } = message;
  return create(BrowserClientMessageSchema, {
    payload: {
      case: "json",
      value: {
        type,
        payload,
      },
    },
  });
}

function clientProtoFromJson(message) {
  if (message.type === "attach") {
    const value = { browserId: message.browserId, grantToken: message.grantToken };
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
  return jsonFallbackClientProto(message);
}

function clientMessageFromProto(message) {
  const protobufJson = toJson(BrowserClientMessageSchema, message);
  const messageOneofCase = Object.keys(protobufJson)[0];
  if (!messageOneofCase) {
    throw new Error("Browser WebSocket 消息缺少 payload");
  }
  if (messageOneofCase === "json") {
    return { type: protobufJson.json.type, ...protobufJson.json.payload };
  }
  return { type: messageOneofCase, ...protobufJson[messageOneofCase] };
}

/** 将已解析的消息投影到schema合同；原始文本解析与业务输入校验归传输入口。 */
export function normalizeBrowserClientMessage(message) {
  assertPlainObject(message, "Browser WebSocket 消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Browser WebSocket 消息缺少 type: ${message.type}`);
  }
  return clientMessageFromProto(
    clientProtoFromJson(normalizeJsonSerializableValue(message)),
  );
}

export function encodeBrowserClientMessage(message) {
  assertPlainObject(message, "Browser 客户端消息");
  const normalizedMessage = normalizeJsonSerializableValue(message);
  return JSON.stringify(
    clientMessageFromProto(clientProtoFromJson(normalizedMessage)),
  );
}

export function encodeBrowserServerMessage(message) {
  assertPlainObject(message, "Browser 服务端消息");
  if (typeof message.type !== "string" || !message.type) {
    throw new Error(`Browser 服务端消息缺少 type: ${message.type}`);
  }
  const normalizedMessage = normalizeJsonSerializableValue(message);
  const { type, ...payload } = normalizedMessage;
  const proto = create(BrowserServerMessageSchema, {
    payload: {
      case: "json",
      value: {
        type,
        payload,
      },
    },
  });
  const protobufJson = toJson(BrowserServerMessageSchema, proto);
  return JSON.stringify({
    type: protobufJson.json.type,
    ...protobufJson.json.payload,
  });
}
