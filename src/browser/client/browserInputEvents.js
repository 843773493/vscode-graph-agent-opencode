import {
  modifiersFromEvent,
  pointerButtonName,
  remotePointFromEvent,
} from "./browserClientUtils.js";

export function bindBrowserInputEvents({
  canvas,
  isAttached,
  sendIfAttached,
}) {
  canvas.addEventListener("pointerdown", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    canvas.focus();
    canvas.setPointerCapture(event.pointerId);
    const point = remotePointFromEvent(canvas, event);
    sendIfAttached({
      type: "pointer",
      action: "down",
      button: pointerButtonName(event.button),
      x: point.x,
      y: point.y,
      modifiers: modifiersFromEvent(event),
    });
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    const point = remotePointFromEvent(canvas, event);
    sendIfAttached({
      type: "pointer",
      action: "move",
      button: "none",
      x: point.x,
      y: point.y,
      modifiers: modifiersFromEvent(event),
    });
  });

  canvas.addEventListener("pointerup", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
    const point = remotePointFromEvent(canvas, event);
    sendIfAttached({
      type: "pointer",
      action: "up",
      button: pointerButtonName(event.button),
      x: point.x,
      y: point.y,
      modifiers: modifiersFromEvent(event),
    });
  });

  canvas.addEventListener("wheel", (event) => {
    if (!isAttached()) {
      return;
    }
    event.preventDefault();
    const point = remotePointFromEvent(canvas, event);
    sendIfAttached({
      type: "pointer",
      action: "wheel",
      button: "none",
      x: point.x,
      y: point.y,
      deltaX: event.deltaX,
      deltaY: event.deltaY,
      modifiers: modifiersFromEvent(event),
    });
  }, { passive: false });

  canvas.addEventListener("keydown", (event) => {
    if (!isAttached()) {
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

  canvas.addEventListener("keyup", (event) => {
    if (!isAttached()) {
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

  canvas.addEventListener("paste", (event) => {
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
  });
}
