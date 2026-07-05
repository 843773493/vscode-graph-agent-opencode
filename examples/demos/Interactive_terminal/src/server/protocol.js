export const CLIENT_MESSAGE_TYPES = new Set([
  "attach",
  "detach",
  "input",
  "resize",
  "agentInput",
]);

export const SERVER_MESSAGE_TYPES = new Set([
  "attached",
  "detached",
  "output",
  "exit",
  "error",
]);

function assertPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("WebSocket 消息必须是 JSON object");
  }
}

function assertTerminalId(message) {
  if (typeof message.terminalId !== "string" || message.terminalId.trim() === "") {
    throw new Error("terminalId 不能为空");
  }
}

function assertData(message) {
  if (typeof message.data !== "string") {
    throw new Error("data 必须是字符串");
  }
}

function assertSize(message) {
  if (!Number.isInteger(message.cols) || message.cols <= 0) {
    throw new Error("cols 必须是正整数");
  }
  if (!Number.isInteger(message.rows) || message.rows <= 0) {
    throw new Error("rows 必须是正整数");
  }
}

export function validateClientMessage(message) {
  assertPlainObject(message);
  if (typeof message.type !== "string" || message.type.trim() === "") {
    throw new Error("type 不能为空");
  }
  if (!CLIENT_MESSAGE_TYPES.has(message.type)) {
    throw new Error(`未知消息类型: ${message.type}`);
  }

  assertTerminalId(message);
  if (message.type === "input" || message.type === "agentInput") {
    assertData(message);
  }
  if (message.type === "resize") {
    assertSize(message);
  }

  return message;
}

export function parseClientMessage(raw) {
  const text = Buffer.isBuffer(raw) ? raw.toString("utf8") : String(raw);
  return validateClientMessage(JSON.parse(text));
}

export function encodeServerMessage(message) {
  assertPlainObject(message);
  if (typeof message.type !== "string" || !SERVER_MESSAGE_TYPES.has(message.type)) {
    throw new Error(`非法服务端消息类型: ${message.type}`);
  }
  return JSON.stringify(message);
}
