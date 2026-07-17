import { bindBrowserInputEvents } from "./browserInputEvents.js";
import { bindBrowserToolbarEvents } from "./browserToolbarEvents.js";
import {
  backendWsUrl,
  clamp,
  sameViewport,
  shortUrlLabel,
  statusLabel,
} from "./browserClientUtils.js";

const params = new URLSearchParams(window.location.search);
const browserId = params.get("browserId");
const workspaceId = params.get("workspaceId");
const gatewayMode = Boolean(workspaceId);
const backendBaseUrl = workspaceId
  ? `${window.location.origin}/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/browser-manager`
  : window.BOXTEAM_BROWSER_BACKEND_URL || "http://127.0.0.1:8015";
const backendRequestHeaders = gatewayMode
  ? { "X-Local-Token": "local-dev-token" }
  : {};
document.documentElement.classList.toggle("embedded-browser", params.get("embedded") === "1");

const browserIdElement = document.querySelector("#browser-id");
const attachStateBadge = document.querySelector("#attach-state-badge");
const statusLine = document.querySelector("#status-line");
const attachToggle = document.querySelector("#attach-toggle");
const attachToggleLabel = attachToggle.querySelector(".sr-only");
const refreshStateButton = document.querySelector("#refresh-state");
const closeBrowserButton = document.querySelector("#close-browser");
const deleteBrowserButton = document.querySelector("#delete-browser");
const backButton = document.querySelector("#back-button");
const forwardButton = document.querySelector("#forward-button");
const reloadButton = document.querySelector("#reload-button");
const urlForm = document.querySelector("#url-form");
const addressInput = document.querySelector("#address-input");
const goButton = document.querySelector("#go-button");
const screenStage = document.querySelector("#screen-stage");
const screenScroll = document.querySelector("#screen-scroll");
const canvas = document.querySelector("#screen-canvas");
const overlay = document.querySelector("#screen-overlay");
const context = canvas.getContext("2d", { alpha: false });

const MIN_VIEWPORT = { width: 360, height: 240 };
const MAX_VIEWPORT = { width: 2560, height: 1440 };

let socket = null;
let attached = false;
let deleted = false;
let currentStatus = null;
let statusPollTimer = null;
let lastViewport = null;
let viewportTimer = null;
let frameSerial = 0;

canvas.width = 1280;
canvas.height = 800;
clearCanvas();

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", error);
}

function setAttachButtonMode(mode) {
  const labels = {
    detached: "连接浏览器",
    attaching: "正在连接浏览器",
    attached: "断开浏览器",
  };
  const label = labels[mode] || labels.detached;
  attachToggle.classList.toggle("is-attached", mode === "attached");
  attachToggle.title = label;
  attachToggle.setAttribute("aria-label", label);
  attachToggleLabel.textContent = label;
  attachStateBadge.className = `attach-state-badge ${mode}`;
  attachStateBadge.textContent = mode === "attached" ? "已连接" : "未连接";
}

function updateControls() {
  const hasBrowserId = Boolean(browserId);
  const running = currentStatus === "running";
  attachToggle.disabled = !hasBrowserId || deleted || !running;
  refreshStateButton.disabled = !hasBrowserId || deleted;
  closeBrowserButton.disabled = !hasBrowserId || deleted || !running;
  deleteBrowserButton.disabled = !hasBrowserId || deleted;
  backButton.disabled = !hasBrowserId || !attached || deleted || !running;
  forwardButton.disabled = !hasBrowserId || !attached || deleted || !running;
  reloadButton.disabled = !hasBrowserId || !attached || deleted || !running;
  addressInput.disabled = !hasBrowserId || !attached || deleted || !running;
  goButton.disabled = !hasBrowserId || !attached || deleted || !running || !addressInput.value.trim();
  canvas.classList.toggle("is-disabled", !hasBrowserId || deleted || !running);
}

function clearCanvas() {
  context.fillStyle = "#11161d";
  context.fillRect(0, 0, canvas.width, canvas.height);
}

function describeSnapshot(snapshot) {
  return `${statusLabel(snapshot.status)} · ${snapshot.title || "无标题"} · ${shortUrlLabel(snapshot.url)}`;
}

function overlayLabelForSnapshot(snapshot) {
  if (attached && snapshot.status === "running") {
    return "";
  }
  if (snapshot.status === "running") {
    return "已断开连接，浏览器页面仍在后台运行";
  }
  return statusLabel(snapshot.status);
}

function applyState(snapshot) {
  currentStatus = snapshot.status;
  browserIdElement.textContent = `${snapshot.browser_id} · ${snapshot.title || "无标题"}`;
  if (document.activeElement !== addressInput) {
    addressInput.value = snapshot.url || "";
    addressInput.title = snapshot.url || "";
  }
  overlay.hidden = attached && snapshot.status === "running";
  overlay.textContent = overlayLabelForSnapshot(snapshot);
  const badgeMode = attached
    ? "attached"
    : snapshot.status === "running"
      ? "detached"
      : snapshot.status;
  attachStateBadge.className = `attach-state-badge ${badgeMode}`;
  attachStateBadge.textContent = attached
    ? "已连接"
    : snapshot.status === "running"
      ? "未连接"
      : statusLabel(snapshot.status);
  setStatus(describeSnapshot(snapshot), ["failed", "lost"].includes(snapshot.status));
  updateControls();
}

function markDeleted(message = "浏览器页面已删除或不存在", snapshot = null) {
  deleted = true;
  attached = false;
  currentStatus = "deleted";
  socket?.close();
  socket = null;
  setAttachButtonMode("detached");
  if (browserId) {
    browserIdElement.textContent = `${browserId} · ${statusLabel("deleted")}`;
  }
  overlay.hidden = false;
  overlay.textContent = "已删除";
  attachStateBadge.className = "attach-state-badge deleted";
  attachStateBadge.textContent = "已删除";
  setStatus(message);
  if (snapshot) {
    addressInput.value = snapshot.url || "";
  }
  updateControls();
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 尚未连接");
  }
  socket.send(JSON.stringify(message));
}

function sendIfAttached(message) {
  if (!attached || !browserId) {
    return;
  }
  send({ browserId, ...message });
}

async function loadSnapshot() {
  if (!browserId) {
    currentStatus = "invalid";
    browserIdElement.textContent = "URL 缺少 browserId";
    overlay.hidden = false;
    overlay.textContent = "链接缺少 browserId 参数";
    setStatus("URL 缺少 browserId 参数", true);
    updateControls();
    return null;
  }
  const response = await fetch(
    `${backendBaseUrl}/api/browsers/${encodeURIComponent(browserId)}?missing_as_deleted=1`,
    { cache: "no-store", headers: backendRequestHeaders },
  );
  if (response.status === 404) {
    markDeleted("浏览器页面已删除或不存在");
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取浏览器状态失败: ${response.status}`);
  }
  const payload = await response.json();
  const snapshot = payload.data;
  if (snapshot.status === "deleted") {
    markDeleted("浏览器页面已删除", snapshot);
    return snapshot;
  }
  applyState(snapshot);
  return snapshot;
}

async function syncBrowserState() {
  if (!browserId || deleted) {
    return;
  }
  const snapshot = await loadSnapshot();
  if (!snapshot) {
    return;
  }
  if (snapshot.status !== "running" && attached) {
    attached = false;
    socket?.close();
    socket = null;
    setAttachButtonMode("detached");
  }
}

function startStatusPolling() {
  if (statusPollTimer !== null) {
    return;
  }
  statusPollTimer = window.setInterval(() => {
    void syncBrowserState().catch((error) => {
      setStatus(error instanceof Error ? error.message : String(error), true);
    });
  }, 2000);
}

function detach() {
  if (socket && socket.readyState === WebSocket.OPEN && browserId) {
    send({ type: "detach", browserId });
  }
  socket?.close();
  socket = null;
  attached = false;
  setAttachButtonMode("detached");
  overlay.hidden = false;
  overlay.textContent = "已断开";
  attachStateBadge.className = "attach-state-badge detached";
  attachStateBadge.textContent = "未连接";
  setStatus("已断开连接，浏览器页面仍在后台运行");
  updateControls();
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

function attach() {
  if (!browserId) {
    setStatus("URL 缺少 browserId 参数", true);
    return;
  }
  if (attached) {
    return;
  }
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    setStatus("正在连接浏览器...");
    return;
  }
  socket = new WebSocket(
    backendWsUrl(backendBaseUrl, gatewayMode ? "local-dev-token" : null),
  );
  setAttachButtonMode("attaching");
  setStatus("正在连接浏览器...");

  socket.addEventListener("open", () => {
    send({ type: "attach", browserId });
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      attached = true;
      setAttachButtonMode("attached");
      applyState(message.state);
      scheduleViewport();
      canvas.focus();
      return;
    }
    if (message.type === "detached") {
      attached = false;
      setAttachButtonMode("detached");
      applyState(message.state);
      socket?.close();
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
      setStatus("浏览器命令已完成");
      return;
    }
    if (message.type === "error") {
      setStatus(message.message, true);
      return;
    }
    throw new Error(`未知服务端消息类型: ${message.type}`);
  });

  socket.addEventListener("close", () => {
    attached = false;
    setAttachButtonMode("detached");
    if (!deleted && currentStatus === "running") {
      overlay.hidden = false;
      overlay.textContent = "已断开连接，浏览器页面仍在后台运行";
      setStatus("已断开连接，浏览器页面仍在后台运行");
    }
    updateControls();
  });

  socket.addEventListener("error", () => {
    setStatus("WebSocket 连接失败", true);
  });
}

function command(name, extra = {}) {
  sendIfAttached({ type: "command", name, ...extra });
}

function computeViewport() {
  const rect = screenScroll.getBoundingClientRect();
  const scale = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
  return {
    width: clamp(Math.round(rect.width * scale), MIN_VIEWPORT.width, MAX_VIEWPORT.width),
    height: clamp(Math.round(rect.height * scale), MIN_VIEWPORT.height, MAX_VIEWPORT.height),
  };
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
  send({ type: "viewport", browserId, width: viewport.width, height: viewport.height });
}

function scheduleViewport() {
  window.clearTimeout(viewportTimer);
  viewportTimer = window.setTimeout(sendViewportNow, 80);
}

bindBrowserToolbarEvents({
  browserId,
  backendBaseUrl,
  requestHeaders: backendRequestHeaders,
  attachToggle,
  refreshStateButton,
  backButton,
  forwardButton,
  reloadButton,
  addressInput,
  urlForm,
  closeBrowserButton,
  deleteBrowserButton,
  isAttached: () => attached,
  attach,
  detach,
  loadSnapshot,
  command,
  updateControls,
  applyState,
  markDeleted,
  setStatus,
});

bindBrowserInputEvents({
  canvas,
  isAttached: () => attached,
  sendIfAttached,
});

new ResizeObserver(scheduleViewport).observe(screenScroll);
window.addEventListener("beforeunload", () => {
  if (statusPollTimer !== null) {
    window.clearInterval(statusPollTimer);
  }
  if (attached && socket?.readyState === WebSocket.OPEN && browserId) {
    socket.send(JSON.stringify({ type: "detach", browserId }));
  }
});

setAttachButtonMode("detached");
void loadSnapshot()
  .then((snapshot) => {
    if (snapshot?.status === "running") {
      attach();
    }
    startStatusPolling();
  })
  .catch((error) => {
    setStatus(error instanceof Error ? error.message : String(error), true);
    updateControls();
  });
