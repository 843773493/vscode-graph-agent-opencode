import { createRemoteFrameRenderer } from "./remoteFrameRenderer.js";
import { createLatestPointerMoveScheduler } from "./latestPointerMoveScheduler.js";
import { createCanvasRectCache } from "./canvasRectCache.js";

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

canvas.width = 1280;
canvas.height = 800;
clearCanvas();

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function setStatus(nextStatus, statusMessage) {
  statusDot.className = `status-dot ${nextStatus}`;
  statusText.textContent = statusMessage;
}

const renderRemoteFrame = createRemoteFrameRenderer({
  canvas,
  context,
  onFrameError: (error) => {
    console.error(`[viewer] 远程屏幕帧处理失败: ${error.message}`);
    setStatus("error", "远程屏幕帧处理失败");
  },
});

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

function sendViewerMessage(outboundMessage) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 未连接，无法发送远程屏幕消息");
  }
  socket.send(JSON.stringify(outboundMessage));
}

function sendIfAttached(outboundMessage) {
  if (outboundMessage.type !== "pointer" || outboundMessage.action !== "move") {
    pointerMoveScheduler.flush();
  }
  if (!attached || pendingDetach) {
    return;
  }
  sendViewerMessage({ browserId: BROWSER_ID, ...outboundMessage });
}

const pointerMoveScheduler = createLatestPointerMoveScheduler({
  requestFrame: (callback) => window.requestAnimationFrame(callback),
  cancelFrame: (frameId) => window.cancelAnimationFrame(frameId),
  send: (pointerMove) => sendIfAttached({
    type: "pointer",
    action: "move",
    button: "none",
    ...pointerMove,
  }),
});

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

const canvasRectCache = createCanvasRectCache({
  readRect: () => canvas.getBoundingClientRect(),
  requestFrame: (callback) => window.requestAnimationFrame(callback),
  cancelFrame: (frameId) => window.cancelAnimationFrame(frameId),
});

function remotePointFromEvent(event, refreshRect = false) {
  const rect = refreshRect ? canvasRectCache.refresh() : canvasRectCache.getRect();
  if (rect.width <= 0 || rect.height <= 0) {
    throw new Error("canvas 尺寸无效，无法映射指针坐标");
  }
  return {
    x: clamp(((event.clientX - rect.left) / rect.width) * canvas.width, 0, canvas.width),
    y: clamp(((event.clientY - rect.top) / rect.height) * canvas.height, 0, canvas.height),
  };
}

function sendBrowserCommand(commandName, commandOptions = {}) {
  sendIfAttached({
    type: "command",
    name: commandName,
    ...commandOptions,
  });
}

function applyBrowserState(browserState) {
  browserIdLabel.textContent = browserState.browserId;
  if (document.activeElement !== addressInput) {
    addressInput.value = browserState.url;
  }
  pageTitle.textContent = browserState.title || "";
  const streamingText = browserState.streaming ? "streaming" : "idle";
  setStatus(attached ? "attached" : "detached", `${browserState.browserId} ${streamingText} ${browserState.viewport.width}x${browserState.viewport.height}`);
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
  if (!attached || pendingDetach) {
    return;
  }
  pointerMoveScheduler.flush();
  const viewport = computeViewport();
  if (sameViewport(viewport, lastViewport)) {
    return;
  }
  lastViewport = viewport;
  sendViewerMessage({
    type: "viewport",
    browserId: BROWSER_ID,
    width: viewport.width,
    height: viewport.height,
  });
}

function cancelViewportUpdate() {
  window.clearTimeout(viewportTimer);
  viewportTimer = null;
}

function scheduleViewport() {
  if (!attached || pendingDetach) {
    return;
  }
  cancelViewportUpdate();
  viewportTimer = window.setTimeout(() => {
    viewportTimer = null;
    sendViewportNow();
  }, 80);
}

function attach() {
  if (browserIdInput.value.trim() !== BROWSER_ID) {
    throw new Error(`只能 attach 到 ${BROWSER_ID}`);
  }
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    setStatus("connecting", "远程屏幕连接正在处理中...");
    return;
  }

  pendingDetach = false;
  const connectionSocket = new WebSocket(socketUrl());
  socket = connectionSocket;
  setStatus("connecting", "正在连接...");

  connectionSocket.addEventListener("open", () => {
    if (socket !== connectionSocket) {
      return;
    }
    sendViewerMessage({ type: "attach", browserId: BROWSER_ID });
  });

  connectionSocket.addEventListener("message", (event) => {
    if (socket !== connectionSocket) {
      return;
    }
    const serverMessage = JSON.parse(event.data);
    if (serverMessage.type === "attached") {
      setAttachedState(true);
      applyBrowserState(serverMessage.state);
      scheduleViewport();
      canvas.focus();
      return;
    }
    if (serverMessage.type === "detached") {
      cancelViewportUpdate();
      pointerMoveScheduler.clear();
      setAttachedState(false);
      applyBrowserState(serverMessage.state);
      pendingDetach = true;
      connectionSocket.close(1000, "detached");
      return;
    }
    if (serverMessage.type === "frame") {
      renderRemoteFrame(serverMessage);
      return;
    }
    if (serverMessage.type === "state") {
      applyBrowserState(serverMessage.state);
      return;
    }
    if (serverMessage.type === "commandResult") {
      applyBrowserState(serverMessage.state);
      setStatus("attached", serverMessage.output || "命令已完成");
      return;
    }
    if (serverMessage.type === "error") {
      setStatus("error", serverMessage.message);
      if (pendingDetach) {
        connectionSocket.close(1000, "detach failed");
      }
      return;
    }
    throw new Error(`未知服务端消息类型: ${serverMessage.type}`);
  });

  connectionSocket.addEventListener("close", () => {
    if (socket !== connectionSocket) {
      return;
    }
    cancelViewportUpdate();
    pointerMoveScheduler.clear();
    setAttachedState(false);
    socket = null;
    lastViewport = null;
    if (!pendingDetach) {
      setStatus("detached", "连接已关闭");
    }
    pendingDetach = false;
  });

  connectionSocket.addEventListener("error", () => {
    if (socket !== connectionSocket) {
      return;
    }
    cancelViewportUpdate();
    setStatus("error", "WebSocket 连接错误");
  });
}

function detach() {
  if (pendingDetach) {
    return;
  }
  cancelViewportUpdate();
  pointerMoveScheduler.flush();
  pendingDetach = true;
  sendViewerMessage({ type: "detach", browserId: BROWSER_ID });
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
  sendBrowserCommand("goto", { url: addressInput.value.trim() });
});

backButton.addEventListener("click", () => sendBrowserCommand("back"));
forwardButton.addEventListener("click", () => sendBrowserCommand("forward"));
reloadButton.addEventListener("click", () => sendBrowserCommand("reload"));
stopButton.addEventListener("click", () => sendBrowserCommand("stop"));
devtoolsButton.addEventListener("click", () => {
  window.open(`${window.location.origin}/devtools/page`, "_blank", "noopener,noreferrer");
});

canvas.addEventListener("pointerdown", (event) => {
  if (!attached || pendingDetach) {
    return;
  }
  event.preventDefault();
  canvas.focus();
  canvas.setPointerCapture(event.pointerId);
  const point = remotePointFromEvent(event, true);
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
  if (!attached || pendingDetach) {
    return;
  }
  event.preventDefault();
  const point = remotePointFromEvent(event);
  pointerMoveScheduler.schedule({
    x: point.x,
    y: point.y,
    modifiers: modifiersFromEvent(event),
  });
});

canvas.addEventListener("pointerup", (event) => {
  if (!attached || pendingDetach) {
    return;
  }
  event.preventDefault();
  if (canvas.hasPointerCapture(event.pointerId)) {
    canvas.releasePointerCapture(event.pointerId);
  }
  const point = remotePointFromEvent(event, true);
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
  if (!attached || pendingDetach) {
    return;
  }
  event.preventDefault();
  const point = remotePointFromEvent(event, true);
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
  if (!attached || pendingDetach) {
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
  if (!attached || pendingDetach) {
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
  if (!attached || pendingDetach) {
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

const screenGeometryObserver = new ResizeObserver(() => {
  canvasRectCache.scheduleRefresh();
  scheduleViewport();
});
screenGeometryObserver.observe(screenScroll);
screenGeometryObserver.observe(canvas);
screenScroll.addEventListener("scroll", canvasRectCache.scheduleRefresh, { passive: true });
window.addEventListener("scroll", canvasRectCache.scheduleRefresh, { capture: true, passive: true });
window.addEventListener("resize", canvasRectCache.scheduleRefresh);
canvasRectCache.refresh();
window.addEventListener("beforeunload", () => {
  cancelViewportUpdate();
  canvasRectCache.dispose();
  pointerMoveScheduler.flush();
  if (attached && socket?.readyState === WebSocket.OPEN) {
    sendViewerMessage({ type: "detach", browserId: BROWSER_ID });
  }
});

setAttachedState(false);
