const BROWSER_ID = "uuid:12";
const MIN_VIEWPORT = { width: 360, height: 240 };
const DEFAULT_REMOTE_VIEWPORT_WIDTH = 1920;
const MAX_VIEWPORT = { width: 2560, height: 1200 };

const browserIdLabel = document.querySelector("#browser-id");
const browserIdInput = document.querySelector("#browser-id-input");
const attachToggle = document.querySelector("#attach-toggle");
const backButton = document.querySelector("#back-button");
const forwardButton = document.querySelector("#forward-button");
const reloadButton = document.querySelector("#reload-button");
const stopButton = document.querySelector("#stop-button");
const devtoolsButton = document.querySelector("#devtools-button");
const urlForm = document.querySelector("#url-form");
const addressInput = document.querySelector("#address-input");
const goButton = document.querySelector("#go-button");
const screenStage = document.querySelector("#screen-stage");
const screenScroll = document.querySelector("#screen-scroll");
const canvas = document.querySelector("#screen-canvas");
const overlay = document.querySelector("#screen-overlay");
const statusDot = document.querySelector("#status-dot");
const statusText = document.querySelector("#status-text");
const pageTitle = document.querySelector("#page-title");
const context = canvas.getContext("2d", { alpha: false });

let socket = null;
let attached = false;
let pendingDetach = false;
let lastViewport = null;
let viewportTimer = null;
let frameSerial = 0;

canvas.width = 1280;
canvas.height = 800;
clearCanvas();

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function setStatus(nextStatus, message) {
  statusDot.className = `status-dot ${nextStatus}`;
  statusText.textContent = message;
}

function setAttachedState(nextAttached) {
  attached = nextAttached;
  attachToggle.textContent = nextAttached ? "Detach" : "Attach";
  browserIdInput.disabled = nextAttached;
  backButton.disabled = !nextAttached;
  forwardButton.disabled = !nextAttached;
  reloadButton.disabled = !nextAttached;
  stopButton.disabled = !nextAttached;
  addressInput.disabled = !nextAttached;
  goButton.disabled = !nextAttached;
  overlay.hidden = nextAttached;
}

function clearCanvas() {
  context.fillStyle = "#11161d";
  context.fillRect(0, 0, canvas.width, canvas.height);
}

function socketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/remote-screen`;
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 未连接，无法发送远程屏幕消息");
  }
  socket.send(JSON.stringify(message));
}

function sendIfAttached(message) {
  if (!attached) {
    return;
  }
  send({ browserId: BROWSER_ID, ...message });
}

function modifiersFromEvent(event) {
  return {
    alt: event.altKey,
    ctrl: event.ctrlKey,
    meta: event.metaKey,
    shift: event.shiftKey,
  };
}

function pointerButtonName(button) {
  if (button === 0) return "left";
  if (button === 1) return "middle";
  if (button === 2) return "right";
  return "none";
}

function remotePointFromEvent(event) {
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    throw new Error("canvas 尺寸无效，无法映射指针坐标");
  }
  return {
    x: clamp(((event.clientX - rect.left) / rect.width) * canvas.width, 0, canvas.width),
    y: clamp(((event.clientY - rect.top) / rect.height) * canvas.height, 0, canvas.height),
  };
}

function command(name, extra = {}) {
  sendIfAttached({
    type: "command",
    name,
    ...extra,
  });
}

function applyState(state) {
  browserIdLabel.textContent = state.browserId;
  if (document.activeElement !== addressInput) {
    addressInput.value = state.url;
  }
  pageTitle.textContent = state.title || "";
  const streamingText = state.streaming ? "streaming" : "idle";
  setStatus(attached ? "attached" : "detached", `${state.browserId} ${streamingText} ${state.viewport.width}x${state.viewport.height}`);
}

function drawFrame(message) {
  const serial = ++frameSerial;
  const image = new Image();
  image.onload = () => {
    if (serial !== frameSerial) {
      return;
    }
    if (canvas.width !== message.width || canvas.height !== message.height) {
      canvas.width = message.width;
      canvas.height = message.height;
    }
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
  };
  image.src = message.dataUrl;
}

function computeViewport() {
  const rect = screenScroll.getBoundingClientRect();
  return {
    width: clamp(Math.max(Math.round(rect.width), DEFAULT_REMOTE_VIEWPORT_WIDTH), MIN_VIEWPORT.width, MAX_VIEWPORT.width),
    height: clamp(Math.round(rect.height), MIN_VIEWPORT.height, MAX_VIEWPORT.height),
  };
}

function sameViewport(left, right) {
  return left && right && left.width === right.width && left.height === right.height;
}

function sendViewportNow() {
  if (!attached) {
    return;
  }
  const viewport = computeViewport();
  if (sameViewport(viewport, lastViewport)) {
    return;
  }
  lastViewport = viewport;
  send({
    type: "viewport",
    browserId: BROWSER_ID,
    width: viewport.width,
    height: viewport.height,
  });
}

function scheduleViewport() {
  window.clearTimeout(viewportTimer);
  viewportTimer = window.setTimeout(sendViewportNow, 80);
}

function attach() {
  if (browserIdInput.value.trim() !== BROWSER_ID) {
    throw new Error(`只能 attach 到 ${BROWSER_ID}`);
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    throw new Error("WebSocket 已连接，不能重复 attach");
  }

  pendingDetach = false;
  socket = new WebSocket(socketUrl());
  setStatus("connecting", "正在连接...");

  socket.addEventListener("open", () => {
    send({ type: "attach", browserId: BROWSER_ID });
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      setAttachedState(true);
      applyState(message.state);
      scheduleViewport();
      canvas.focus();
      return;
    }
    if (message.type === "detached") {
      setAttachedState(false);
      applyState(message.state);
      pendingDetach = true;
      socket.close(1000, "detached");
      return;
    }
    if (message.type === "frame") {
      drawFrame(message);
      return;
    }
    if (message.type === "state") {
      applyState(message.state);
      return;
    }
    if (message.type === "commandResult") {
      applyState(message.state);
      setStatus("attached", message.output || "命令已完成");
      return;
    }
    if (message.type === "error") {
      setStatus("error", message.message);
      return;
    }
    throw new Error(`未知服务端消息类型: ${message.type}`);
  });

  socket.addEventListener("close", () => {
    setAttachedState(false);
    socket = null;
    lastViewport = null;
    if (!pendingDetach) {
      setStatus("detached", "连接已关闭");
    }
    pendingDetach = false;
  });

  socket.addEventListener("error", () => {
    setStatus("error", "WebSocket 连接错误");
  });
}

function detach() {
  pendingDetach = true;
  send({ type: "detach", browserId: BROWSER_ID });
}

attachToggle.addEventListener("click", () => {
  if (attached) {
    detach();
    return;
  }
  attach();
});

urlForm.addEventListener("submit", (event) => {
  event.preventDefault();
  command("goto", { url: addressInput.value.trim() });
});

backButton.addEventListener("click", () => command("back"));
forwardButton.addEventListener("click", () => command("forward"));
reloadButton.addEventListener("click", () => command("reload"));
stopButton.addEventListener("click", () => command("stop"));
devtoolsButton.addEventListener("click", () => {
  window.open(`${window.location.origin}/devtools/page`, "_blank", "noopener,noreferrer");
});

canvas.addEventListener("pointerdown", (event) => {
  if (!attached) {
    return;
  }
  event.preventDefault();
  canvas.focus();
  canvas.setPointerCapture(event.pointerId);
  const point = remotePointFromEvent(event);
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
  if (!attached) {
    return;
  }
  event.preventDefault();
  const point = remotePointFromEvent(event);
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
  if (!attached) {
    return;
  }
  event.preventDefault();
  if (canvas.hasPointerCapture(event.pointerId)) {
    canvas.releasePointerCapture(event.pointerId);
  }
  const point = remotePointFromEvent(event);
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
  if (!attached) {
    return;
  }
  event.preventDefault();
  const point = remotePointFromEvent(event);
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
  if (!attached) {
    return;
  }
  event.preventDefault();
  const text = event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey ? event.key : "";
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
  if (!attached) {
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
  if (!attached) {
    return;
  }
  event.preventDefault();
  const text = event.clipboardData.getData("text");
  sendIfAttached({
    type: "paste",
    text,
  });
});

canvas.addEventListener("contextmenu", (event) => {
  event.preventDefault();
});

new ResizeObserver(scheduleViewport).observe(screenScroll);
window.addEventListener("beforeunload", () => {
  if (attached && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "detach", browserId: BROWSER_ID }));
  }
});

setAttachedState(false);
