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

function windowsVirtualKeyCodeFor(key, code) {
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

export async function dispatchPointer(cdpSession, message) {
  const typeByAction = {
    move: "mouseMoved",
    down: "mousePressed",
    up: "mouseReleased",
    wheel: "mouseWheel",
  };
  const params = {
    type: typeByAction[message.action],
    x: message.x,
    y: message.y,
    button: message.action === "move" || message.action === "wheel" ? "none" : message.button,
    modifiers: modifierBitmask(message.modifiers),
  };
  if (message.action === "down" || message.action === "up") {
    params.clickCount = 1;
  }
  if (message.action === "wheel") {
    params.deltaX = message.deltaX;
    params.deltaY = message.deltaY;
  }
  await cdpSession.send("Input.dispatchMouseEvent", params);
}

export async function dispatchKey(cdpSession, message) {
  const isKeyDown = message.action === "down";
  const text = isKeyDown && typeof message.text === "string" ? message.text : "";
  const keyCode = windowsVirtualKeyCodeFor(message.key, message.code);
  const params = {
    type: isKeyDown && text ? "keyDown" : isKeyDown ? "rawKeyDown" : "keyUp",
    key: message.key,
    code: message.code,
    windowsVirtualKeyCode: keyCode,
    nativeVirtualKeyCode: keyCode,
    modifiers: modifierBitmask(message.modifiers),
    autoRepeat: message.repeat,
  };
  if (text) {
    params.text = text;
    params.unmodifiedText = text;
  }
  await cdpSession.send("Input.dispatchKeyEvent", params);
}

export async function insertText(cdpSession, text) {
  await cdpSession.send("Input.insertText", { text });
}
