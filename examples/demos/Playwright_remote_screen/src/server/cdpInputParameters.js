const POINTER_EVENT_TYPES = Object.freeze({
  move: "mouseMoved",
  down: "mousePressed",
  up: "mouseReleased",
  wheel: "mouseWheel",
});

const SPECIAL_KEY_CODES = new Map([
  ["Backspace", 8],
  ["Tab", 9],
  ["Enter", 13],
  ["Shift", 16],
  ["Control", 17],
  ["Alt", 18],
  ["Pause", 19],
  ["CapsLock", 20],
  ["Escape", 27],
  [" ", 32],
  ["PageUp", 33],
  ["PageDown", 34],
  ["End", 35],
  ["Home", 36],
  ["ArrowLeft", 37],
  ["ArrowUp", 38],
  ["ArrowRight", 39],
  ["ArrowDown", 40],
  ["Insert", 45],
  ["Delete", 46],
  ["Meta", 91],
  ["F1", 112],
  ["F2", 113],
  ["F3", 114],
  ["F4", 115],
  ["F5", 116],
  ["F6", 117],
  ["F7", 118],
  ["F8", 119],
  ["F9", 120],
  ["F10", 121],
  ["F11", 122],
  ["F12", 123],
]);

function modifierBitmask(modifiers = {}) {
  return (modifiers.alt ? 1 : 0)
    | (modifiers.ctrl ? 2 : 0)
    | (modifiers.meta ? 4 : 0)
    | (modifiers.shift ? 8 : 0);
}

function cdpVirtualKeyCodeFor(key, code) {
  if (SPECIAL_KEY_CODES.has(key)) {
    return SPECIAL_KEY_CODES.get(key);
  }
  if (/^Key[A-Z]$/.test(code)) {
    return code.charCodeAt(3);
  }
  if (/^Digit\d$/.test(code)) {
    return code.charCodeAt(5);
  }
  if (key.length === 1) {
    return key.toUpperCase().charCodeAt(0);
  }
  return 0;
}

export function toCdpMouseEventParameters(message) {
  const parameters = {
    type: POINTER_EVENT_TYPES[message.action],
    x: message.x,
    y: message.y,
    button: message.action === "move" || message.action === "wheel" ? "none" : message.button,
    modifiers: modifierBitmask(message.modifiers),
  };
  if (message.action === "down" || message.action === "up") {
    parameters.clickCount = 1;
  }
  if (message.action === "wheel") {
    parameters.deltaX = message.deltaX;
    parameters.deltaY = message.deltaY;
  }
  return parameters;
}

export function toCdpKeyEventParameters(message) {
  const isKeyDown = message.action === "down";
  const text = isKeyDown && typeof message.text === "string" ? message.text : "";
  const virtualKeyCode = cdpVirtualKeyCodeFor(message.key, message.code);
  const parameters = {
    type: isKeyDown && text ? "keyDown" : isKeyDown ? "rawKeyDown" : "keyUp",
    key: message.key,
    code: message.code,
    windowsVirtualKeyCode: virtualKeyCode,
    nativeVirtualKeyCode: virtualKeyCode,
    modifiers: modifierBitmask(message.modifiers),
    autoRepeat: message.repeat,
  };
  if (text) {
    parameters.text = text;
    parameters.unmodifiedText = text;
  }
  return parameters;
}
