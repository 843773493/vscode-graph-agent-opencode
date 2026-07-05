export const FIXED_BROWSER_ID = "uuid:12";

export const CLIENT_MESSAGE_TYPES = new Set([
  "attach",
  "detach",
  "pointer",
  "key",
  "paste",
  "command",
  "viewport",
]);

export const SERVER_MESSAGE_TYPES = new Set([
  "attached",
  "detached",
  "frame",
  "state",
  "commandResult",
  "error",
]);

const POINTER_ACTIONS = new Set(["move", "down", "up", "wheel"]);
const POINTER_BUTTONS = new Set(["none", "left", "middle", "right"]);
const KEY_ACTIONS = new Set(["down", "up"]);
const COMMAND_NAMES = new Set(["goto", "back", "forward", "reload", "stop", "title", "url"]);

function assertPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("WebSocket 消息必须是 JSON object");
  }
}

function assertBrowserId(message) {
  if (message.browserId !== FIXED_BROWSER_ID) {
    throw new Error(`browserId 必须是 ${FIXED_BROWSER_ID}`);
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

function validateModifiers(modifiers) {
  if (modifiers === undefined) {
    return {
      alt: false,
      ctrl: false,
      meta: false,
      shift: false,
    };
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

  message.button = button;
  message.modifiers = validateModifiers(message.modifiers);
}

function validateKeyMessage(message) {
  if (!KEY_ACTIONS.has(message.action)) {
    throw new Error(`未知 key action: ${message.action}`);
  }
  if (typeof message.key !== "string" || message.key.trim() === "") {
    throw new Error("key 不能为空");
  }
  if (typeof message.code !== "string" || message.code.trim() === "") {
    throw new Error("code 不能为空");
  }
  if (message.text !== undefined && typeof message.text !== "string") {
    throw new Error("text 必须是字符串");
  }
  message.repeat = message.repeat === true;
  message.modifiers = validateModifiers(message.modifiers);
}

function validatePasteMessage(message) {
  if (typeof message.text !== "string") {
    throw new Error("paste.text 必须是字符串");
  }
}

function validateCommandMessage(message) {
  if (!COMMAND_NAMES.has(message.name)) {
    throw new Error(`未知 command name: ${message.name}`);
  }
  if (message.name === "goto" && (typeof message.url !== "string" || message.url.trim() === "")) {
    throw new Error("goto command 必须提供 url");
  }
}

function validateViewportMessage(message) {
  assertPositiveInteger(message.width, "width");
  assertPositiveInteger(message.height, "height");
  if (message.width > 4096 || message.height > 4096) {
    throw new Error(`viewport 过大: ${message.width}x${message.height}`);
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

  assertBrowserId(message);
  if (message.type === "pointer") {
    validatePointerMessage(message);
  }
  if (message.type === "key") {
    validateKeyMessage(message);
  }
  if (message.type === "paste") {
    validatePasteMessage(message);
  }
  if (message.type === "command") {
    validateCommandMessage(message);
  }
  if (message.type === "viewport") {
    validateViewportMessage(message);
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
