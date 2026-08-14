import {
  modifiersFromEvent,
  pointerButtonName,
  remotePointFromEvent,
} from "./browserClientUtils.js";

export function bindBrowserInputEvents({
  canvas,
  keyboardTarget = canvas,
  isAttached,
  isPicking,
  onPickMove,
  onPickSelect,
  onCancelPick,
  onContextMenu,
  onBrowserShortcut,
  sendIfAttached,
}) {
  let consumePickerEscapeKeyUp = false;
  let composing = false;
  const composingKeys = new Set();
  const consumedShortcutKeys = new Set();
  const clickState = new Map();
  const activeClickCounts = new Map();
  let activeButtons = 0;
  let activeButton = "none";
  let pendingPointerMove = null;
  let pendingWheel = null;
  let inputFrame = null;

  function flushContinuousInput() {
    inputFrame = null;
    if (pendingPointerMove) {
      sendIfAttached(pendingPointerMove);
      pendingPointerMove = null;
    }
    if (pendingWheel) {
      sendIfAttached(pendingWheel);
      pendingWheel = null;
    }
  }

  function scheduleContinuousInput() {
    if (inputFrame === null) {
      inputFrame = requestAnimationFrame(flushContinuousInput);
    }
  }

  function flushBeforeBarrier() {
    if (inputFrame !== null) {
      cancelAnimationFrame(inputFrame);
    }
    flushContinuousInput();
  }

  function buttonMask(button) {
    if (button === 0) return 1;
    if (button === 2) return 2;
    if (button === 1) return 4;
    return 0;
  }

  function clickCountFor(event, point) {
    const button = pointerButtonName(event.button);
    const previous = clickState.get(button);
    const closeToPrevious = previous
      && performance.now() - previous.timestamp <= 500
      && Math.hypot(previous.x - point.x, previous.y - point.y) <= 5;
    const count = closeToPrevious ? Math.min(previous.count + 1, 3) : 1;
    clickState.set(button, {
      count,
      timestamp: performance.now(),
      x: point.x,
      y: point.y,
    });
    return count;
  }

  function focusKeyboardTarget() {
    keyboardTarget.focus({ preventScroll: true });
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    const point = remotePointFromEvent(canvas, event);
    if (isPicking()) {
      onPickSelect(point);
      return;
    }
    focusKeyboardTarget();
    flushBeforeBarrier();
    canvas.setPointerCapture(event.pointerId);
    const clickCount = clickCountFor(event, point);
    activeClickCounts.set(event.pointerId, clickCount);
    activeButtons |= buttonMask(event.button);
    activeButton = pointerButtonName(event.button);
    sendIfAttached({
      type: "pointer",
      action: "down",
      button: pointerButtonName(event.button),
      x: point.x,
      y: point.y,
      buttons: activeButtons,
      clickCount,
      modifiers: modifiersFromEvent(event),
    });
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    const point = remotePointFromEvent(canvas, event);
    if (isPicking()) {
      onPickMove(point);
      return;
    }
    pendingPointerMove = {
      type: "pointer",
      action: "move",
      button: activeButton,
      x: point.x,
      y: point.y,
      buttons: activeButtons || event.buttons,
      modifiers: modifiersFromEvent(event),
    };
    scheduleContinuousInput();
  });

  canvas.addEventListener("pointerup", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    if (isPicking()) {
      return;
    }
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    flushBeforeBarrier();
    const point = remotePointFromEvent(canvas, event);
    const clickCount = activeClickCounts.get(event.pointerId) || 1;
    activeClickCounts.delete(event.pointerId);
    activeButtons &= ~buttonMask(event.button);
    if (activeButtons === 0) {
      activeButton = "none";
    }
    sendIfAttached({
      type: "pointer",
      action: "up",
      button: pointerButtonName(event.button),
      x: point.x,
      y: point.y,
      buttons: activeButtons,
      clickCount,
      modifiers: modifiersFromEvent(event),
    });
  });

  canvas.addEventListener("wheel", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    const point = remotePointFromEvent(canvas, event);
    if (pendingWheel) {
      pendingWheel.x = point.x;
      pendingWheel.y = point.y;
      pendingWheel.buttons = event.buttons;
      pendingWheel.deltaX += event.deltaX;
      pendingWheel.deltaY += event.deltaY;
      pendingWheel.modifiers = modifiersFromEvent(event);
    } else {
      pendingWheel = {
        type: "pointer",
        action: "wheel",
        button: "none",
        x: point.x,
        y: point.y,
        buttons: event.buttons,
        deltaX: event.deltaX,
        deltaY: event.deltaY,
        modifiers: modifiersFromEvent(event),
      };
    }
    scheduleContinuousInput();
  }, { passive: false });

  canvas.addEventListener("dblclick", (event) => {
    event.preventDefault();
  });

  keyboardTarget.addEventListener("compositionstart", () => {
    composing = true;
  });

  keyboardTarget.addEventListener("compositionend", (event) => {
    composing = false;
    if (isAttached() && event.data) {
      sendIfAttached({ type: "paste", text: event.data });
    }
    keyboardTarget.value = "";
  });

  keyboardTarget.addEventListener("keydown", (event) => {
    if (!isAttached()) {
      return;
    }
    if (isPicking() && event.key === "Escape") {
      event.preventDefault();
      consumePickerEscapeKeyUp = true;
      onCancelPick();
      return;
    }
    if (composing || event.isComposing || event.key === "Process") {
      composingKeys.add(event.code);
      return;
    }
    if (onBrowserShortcut?.(event)) {
      event.preventDefault();
      consumedShortcutKeys.add(event.code);
      return;
    }
    const shortcut = event.ctrlKey || event.metaKey;
    if (shortcut && event.key.toLowerCase() === "c") {
      event.preventDefault();
      consumedShortcutKeys.add(event.code);
      sendIfAttached({ type: "readClipboard" });
      return;
    }
    if (shortcut && event.key.toLowerCase() === "v") {
      consumedShortcutKeys.add(event.code);
      return;
    }
    event.preventDefault();
    const text = event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey
      ? event.key
      : "";
    sendIfAttached({
      type: "key",
      action: "down",
      key: event.key,
      code: event.code,
      text,
      repeat: event.repeat,
      modifiers: modifiersFromEvent(event),
    });
  });

  keyboardTarget.addEventListener("keyup", (event) => {
    if (!isAttached()) {
      return;
    }
    if (consumePickerEscapeKeyUp && event.key === "Escape") {
      event.preventDefault();
      consumePickerEscapeKeyUp = false;
      return;
    }
    if (consumedShortcutKeys.delete(event.code)
      || composingKeys.delete(event.code)
      || composing
      || event.isComposing
      || event.key === "Process") {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    sendIfAttached({
      type: "key",
      action: "up",
      key: event.key,
      code: event.code,
      text: "",
      repeat: false,
      modifiers: modifiersFromEvent(event),
    });
  });

  keyboardTarget.addEventListener("paste", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    sendIfAttached({
      type: "paste",
      text: event.clipboardData.getData("text"),
    });
  });

  canvas.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    onContextMenu?.(event);
  });

  canvas.addEventListener("pointercancel", (event) => {
    flushBeforeBarrier();
    activeClickCounts.delete(event.pointerId);
    activeButtons = 0;
    activeButton = "none";
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
  });
}
