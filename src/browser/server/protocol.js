export const CLIENT_MESSAGE_TYPES = new Set([
  "attach",
  "detach",
  "pointer",
  "key",
  "paste",
  "command",
  "viewport",
  "inspectElement",
  "selectElement",
  "handleDialog",
  "selectFiles",
  "readClipboard",
  "frameAck",
]);

export const SERVER_MESSAGE_TYPES = new Set([
  "attached",
  "detached",
  "frame",
  "state",
  "commandResult",
  "elementHovered",
  "elementSelected",
  "operationAck",
  "clipboardText",
  "participantPointer",
  "error",
]);

const POINTER_ACTIONS = new Set(["move", "down", "up", "wheel"]);
const POINTER_BUTTONS = new Set(["none", "left", "middle", "right"]);
const KEY_ACTIONS = new Set(["down", "up"]);
const COMMAND_NAMES = new Set([
  "goto",
  "back",
  "forward",
  "reload",
  "stop",
  "newPage",
  "activatePage",
  "closePage",
  "find",
]);

function assertPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("WebSocket 消息必须是 JSON object");
  }
}

function assertBrowserId(message) {
  if (typeof message.browserId !== "string" || !message.browserId.trim()) {
    throw new Error("browserId 不能为空");
  }
}

function assertFiniteNumber(value, fieldName) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${fieldName} 必须是有限数字`);
  }
}

function assertPositiveInteger(value, fieldName) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${fieldName} 必须是正整数`);
  }
}

function assertOptionalNonNegativeNumber(value, fieldName) {
  if (value === undefined) {
    return;
  }
  assertFiniteNumber(value, fieldName);
  if (value < 0) {
    throw new Error(`${fieldName} 不能为负数`);
  }
}

function validateModifiers(modifiers) {
  if (modifiers === undefined) {
    return { alt: false, ctrl: false, meta: false, shift: false };
  }
  assertPlainObject(modifiers);
  return {
    alt: modifiers.alt === true,
    ctrl: modifiers.ctrl === true,
    meta: modifiers.meta === true,
    shift: modifiers.shift === true,
  };
}

function validatePointerMessage(message) {
  if (!POINTER_ACTIONS.has(message.action)) {
    throw new Error(`未知 pointer action: ${message.action}`);
  }
  assertFiniteNumber(message.x, "x");
  assertFiniteNumber(message.y, "y");
  if (message.x < 0 || message.y < 0) {
    throw new Error("x/y 不能为负数");
  }
  const button = message.button ?? "none";
  if (!POINTER_BUTTONS.has(button)) {
    throw new Error(`未知 pointer button: ${button}`);
  }
  if (message.action === "wheel") {
    assertFiniteNumber(message.deltaX, "deltaX");
    assertFiniteNumber(message.deltaY, "deltaY");
  }
  if (message.buttons !== undefined && (!Number.isInteger(message.buttons) || message.buttons < 0 || message.buttons > 31)) {
    throw new Error("buttons 必须是 0 到 31 的整数");
  }
  if (message.clickCount !== undefined
    && (!Number.isInteger(message.clickCount) || message.clickCount < 1 || message.clickCount > 3)) {
    throw new Error("clickCount 必须是 1 到 3 的整数");
  }
  message.buttons = message.buttons ?? 0;
  message.button = button;
  message.modifiers = validateModifiers(message.modifiers);
}

function validateKeyMessage(message) {
  if (!KEY_ACTIONS.has(message.action)) {
    throw new Error(`未知 key action: ${message.action}`);
  }
  if (typeof message.key !== "string" || message.key.length === 0) {
    throw new Error("key 不能为空");
  }
  if (typeof message.code !== "string" || !message.code.trim()) {
    throw new Error("code 不能为空");
  }
  if (message.text !== undefined && typeof message.text !== "string") {
    throw new Error("text 必须是字符串");
  }
  message.repeat = message.repeat === true;
  message.modifiers = validateModifiers(message.modifiers);
}

function validateFileSelectionMessage(message) {
  if (!Array.isArray(message.files) || message.files.length > 20) {
    throw new Error("files 必须是最多包含 20 个文件的数组");
  }
  let encodedBytes = 0;
  for (const file of message.files) {
    assertPlainObject(file);
    if (typeof file.name !== "string" || !file.name || /[\\/]/.test(file.name)) {
      throw new Error("文件名不能为空且不能包含路径分隔符");
    }
    if (typeof file.mimeType !== "string") {
      throw new Error("mimeType 必须是字符串");
    }
    if (typeof file.data !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(file.data)) {
      throw new Error("文件 data 必须是 base64 字符串");
    }
    encodedBytes += file.data.length;
  }
  if (encodedBytes > 35_000_000) {
    throw new Error("单次文件选择总大小不能超过 25 MiB");
  }
}

export function parseClientMessage(raw) {
  const text = Buffer.isBuffer(raw) ? raw.toString("utf8") : String(raw);
  const message = JSON.parse(text);
  assertPlainObject(message);
  if (typeof message.type !== "string" || !CLIENT_MESSAGE_TYPES.has(message.type)) {
    throw new Error(`未知消息类型: ${message.type}`);
  }
  if (message.type !== "detach") {
    assertBrowserId(message);
  }
  if (message.type === "attach" && message.participantId !== undefined) {
    if (typeof message.participantId !== "string" || !/^user_[a-zA-Z0-9_-]{8,80}$/.test(message.participantId)) {
      throw new Error("participantId 格式非法");
    }
  }
  if (message.clientOperationId !== undefined
    && (typeof message.clientOperationId !== "string" || message.clientOperationId.length > 120)) {
    throw new Error("clientOperationId 格式非法");
  }
  if (message.type === "pointer") {
    validatePointerMessage(message);
  }
  if (message.type === "key") {
    validateKeyMessage(message);
  }
  if (message.type === "paste" && typeof message.text !== "string") {
    throw new Error("paste.text 必须是字符串");
  }
  if (message.type === "handleDialog") {
    if (typeof message.accept !== "boolean") {
      throw new Error("handleDialog.accept 必须是 boolean");
    }
    if (message.promptText !== undefined && typeof message.promptText !== "string") {
      throw new Error("handleDialog.promptText 必须是字符串");
    }
  }
  if (message.type === "selectFiles") {
    validateFileSelectionMessage(message);
  }
  if (message.type === "viewport") {
    assertPositiveInteger(message.width, "width");
    assertPositiveInteger(message.height, "height");
    if (message.width > 4096 || message.height > 4096) {
      throw new Error(`viewport 过大: ${message.width}x${message.height}`);
    }
  }
  if (message.type === "frameAck") {
    assertPositiveInteger(message.frameId, "frameId");
    assertOptionalNonNegativeNumber(message.decodeMs, "decodeMs");
    assertOptionalNonNegativeNumber(message.drawMs, "drawMs");
  }
  if (message.type === "inspectElement" || message.type === "selectElement") {
    assertFiniteNumber(message.x, "x");
    assertFiniteNumber(message.y, "y");
    if (message.x < 0 || message.y < 0) {
      throw new Error("x/y 不能为负数");
    }
  }
  if (message.type === "command") {
    if (!COMMAND_NAMES.has(message.name)) {
      throw new Error(`未知 command name: ${message.name}`);
    }
    if (message.name === "goto" && (typeof message.url !== "string" || !message.url.trim())) {
      throw new Error("goto command 必须提供 url");
    }
    if (["activatePage", "closePage"].includes(message.name)
      && (typeof message.pageId !== "string" || !message.pageId)) {
      throw new Error(`${message.name} command 必须提供 pageId`);
    }
    if (message.name === "find" && (typeof message.query !== "string" || !message.query)) {
      throw new Error("find command 必须提供 query");
    }
  }
  return message;
}

export function encodeServerMessage(message) {
  assertPlainObject(message);
  if (typeof message.type !== "string" || !SERVER_MESSAGE_TYPES.has(message.type)) {
    throw new Error(`非法服务端消息类型: ${message.type}`);
  }
  return JSON.stringify(message);
}
