import { bindBrowserInputEvents } from "./browserInputEvents.js";
import { bindBrowserToolbarEvents } from "./browserToolbarEvents.js";
import { createBrowserModalUi } from "./browserModalUi.js";
import { createBrowserCollaborationUi } from "./browserCollaborationUi.js";
import { createBrowserShortcuts } from "./browserShortcuts.js";
import {
  backendWsUrl,
  createOpaqueId,
  shortUrlLabel,
  statusLabel,
} from "./browserClientUtils.js";

const params = new URLSearchParams(window.location.search);
const browserId = params.get("browserId");
const workspaceId = params.get("workspaceId");
const gatewayMode = Boolean(workspaceId);
const participantStorageKey = `boxteam-browser-participant:${workspaceId || "local"}:${browserId || "unknown"}`;
let participantId = window.sessionStorage.getItem(participantStorageKey);
if (!participantId) {
  participantId = createOpaqueId("user");
  window.sessionStorage.setItem(participantStorageKey, participantId);
}
const backendBaseUrl = workspaceId
  ? `${window.location.origin}/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/browser-manager`
  : window.BOXTEAM_BROWSER_BACKEND_URL || "http://127.0.0.1:8015";
let gatewayToken = null;
let backendRequestHeaders = {};
document.documentElement.classList.toggle("embedded-browser", params.get("embedded") === "1");

const browserIdElement = document.querySelector("#browser-id");
const attachStateBadge = document.querySelector("#attach-state-badge");
const aiControlBadge = document.querySelector("#ai-control-badge");
const statusLine = document.querySelector("#status-line");
const attachToggle = document.querySelector("#attach-toggle");
const attachToggleLabel = attachToggle.querySelector(".sr-only");
const refreshStateButton = document.querySelector("#refresh-state");
const closeBrowserButton = document.querySelector("#close-browser");
const deleteBrowserButton = document.querySelector("#delete-browser");
const agentLockToggle = document.querySelector("#agent-lock-toggle");
const agentLockToggleLabel = agentLockToggle.querySelector(".sr-only");
const elementPickerToggle = document.querySelector("#element-picker-toggle");
const backButton = document.querySelector("#back-button");
const forwardButton = document.querySelector("#forward-button");
const reloadButton = document.querySelector("#reload-button");
const urlForm = document.querySelector("#url-form");
const addressInput = document.querySelector("#address-input");
const goButton = document.querySelector("#go-button");
const browserTabList = document.querySelector("#browser-tab-list");
const newTabButton = document.querySelector("#new-tab-button");
const downloadShelf = document.querySelector("#download-shelf");
const downloadSummary = document.querySelector("#download-summary");
const downloadList = document.querySelector("#download-list");
const screenStage = document.querySelector("#screen-stage");
const canvas = document.querySelector("#screen-canvas");
const keyboardTarget = document.querySelector("#browser-input-proxy");
const overlay = document.querySelector("#screen-overlay");
const elementHighlight = document.querySelector("#element-highlight");
const elementHighlightLabel = document.querySelector("#element-highlight-label");
const participantCursors = document.querySelector("#participant-cursors");
const browserModal = document.querySelector("#browser-modal");
const browserModalForm = document.querySelector("#browser-modal-form");
const browserModalTitle = document.querySelector("#browser-modal-title");
const browserModalMessage = document.querySelector("#browser-modal-message");
const browserModalPrompt = document.querySelector("#browser-modal-prompt");
const browserModalFiles = document.querySelector("#browser-modal-files");
const browserModalCancel = document.querySelector("#browser-modal-cancel");
const browserModalAccept = document.querySelector("#browser-modal-accept");
const browserContextMenu = document.querySelector("#browser-context-menu");
const findBar = document.querySelector("#find-bar");
const findInput = document.querySelector("#find-input");
const findPrevious = document.querySelector("#find-previous");
const findClose = document.querySelector("#find-close");
const context = canvas.getContext("2d", { alpha: false });

let socket = null;
let attached = false;
let deleted = false;
let manualDetach = false;
let reconnectTimer = null;
let reconnectAttempt = 0;
let currentStatus = null;
let statusPollTimer = null;
let frameSerial = 0;
let pendingFrame = null;
let renderingFrame = false;
let pickingElement = false;
let inspectTimer = null;
let pendingInspectPoint = null;
let agentAccessLocked = false;
let agentLockOwnerId = null;
let agentLockHeartbeatTimer = null;
let clientOperationSequence = 0;
let pendingNavigationUrl = null;
let browserModalUi = null;
let collaborationUi = null;
let handleBrowserShortcut = null;
let currentActivePageId = null;
let waitingForFramePageId = null;
let currentNavigationError = null;

canvas.width = 1280;
canvas.height = 800;
clearCanvas();

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", error);
}

async function initializeGatewayAuth() {
  if (!gatewayMode) {
    return;
  }
  const response = await fetch(`${window.location.origin}/api/gateway/auth/local-credential`);
  if (!response.ok) {
    throw new Error(`获取 Gateway 本地凭据失败: ${response.status}`);
  }
  const payload = await response.json();
  const token = payload.data?.token;
  if (typeof token !== "string" || !token) {
    throw new Error("Gateway 本地凭据响应缺少 token");
  }
  gatewayToken = token;
  backendRequestHeaders["X-Local-Token"] = token;
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
  attachStateBadge.textContent = mode === "attached"
    ? "已连接"
    : mode === "attaching"
      ? "连接中"
      : "未连接";
}

function updateControls() {
  const hasBrowserId = Boolean(browserId);
  const running = currentStatus === "running";
  const addressAvailable = hasBrowserId
    && !deleted
    && !["closed", "failed", "lost", "deleted", "invalid"].includes(currentStatus);
  attachToggle.disabled = !hasBrowserId || deleted || !running;
  refreshStateButton.disabled = !hasBrowserId || deleted;
  closeBrowserButton.disabled = !hasBrowserId || deleted || !running;
  deleteBrowserButton.disabled = !hasBrowserId || deleted;
  backButton.disabled = !hasBrowserId || !attached || deleted || !running;
  forwardButton.disabled = !hasBrowserId || !attached || deleted || !running;
  reloadButton.disabled = !hasBrowserId || !attached || deleted || !running;
  addressInput.disabled = !addressAvailable;
  goButton.disabled = !addressAvailable || !addressInput.value.trim();
  elementPickerToggle.disabled = !hasBrowserId || !attached || deleted || !running;
  agentLockToggle.disabled = !hasBrowserId
    || deleted
    || !running
    || (agentAccessLocked && agentLockOwnerId !== participantId);
  newTabButton.disabled = !hasBrowserId || !attached || deleted || !running;
  canvas.classList.toggle("is-disabled", !hasBrowserId || deleted || !running);
}

function setAgentAccessLocked(locked, ownerId = null) {
  const ownedByCurrentUser = locked === true && ownerId === participantId;
  const ownershipChanged = agentAccessLocked !== (locked === true) || agentLockOwnerId !== ownerId;
  agentAccessLocked = locked === true;
  agentLockOwnerId = agentAccessLocked ? ownerId : null;
  const label = agentAccessLocked
    ? ownedByCurrentUser ? "允许 AI 操作" : "另一位用户已锁定 AI"
    : "锁定 AI 操作";
  agentLockToggle.classList.toggle("is-locked", agentAccessLocked);
  agentLockToggle.setAttribute("aria-pressed", String(agentAccessLocked));
  agentLockToggle.setAttribute("aria-label", label);
  agentLockToggle.title = agentAccessLocked
    ? ownedByCurrentUser
      ? "你已锁定 AI 操作；点击允许 AI 操作"
      : "另一位用户已锁定 AI 操作"
    : "当前用户和 AI 均可操作；点击锁定 AI";
  agentLockToggleLabel.textContent = label;
  aiControlBadge.hidden = !agentAccessLocked;
  aiControlBadge.textContent = ownedByCurrentUser ? "AI 已由你锁定" : "AI 已由另一位用户锁定";
  if (ownershipChanged) {
    if (agentLockHeartbeatTimer !== null) {
      window.clearInterval(agentLockHeartbeatTimer);
      agentLockHeartbeatTimer = null;
    }
    if (ownedByCurrentUser) {
      agentLockHeartbeatTimer = window.setInterval(() => {
        void requestAgentLock(true, { silent: true });
      }, 20_000);
    }
  }
}

function clearElementHighlight() {
  elementHighlight.hidden = true;
  elementHighlightLabel.textContent = "";
}

function setPickingElement(active) {
  pickingElement = active;
  elementPickerToggle.classList.toggle("is-active", active);
  elementPickerToggle.setAttribute("aria-pressed", String(active));
  elementPickerToggle.title = active ? "退出元素选择 (Esc)" : "选择页面元素";
  canvas.classList.toggle("is-picking", active);
  if (!active) {
    clearElementHighlight();
  }
  setStatus(active ? "选择元素：悬停预览，点击添加到消息，Esc 退出" : "已退出元素选择");
}

function drawElementHighlight(element) {
  if (!pickingElement || !element?.bounds || canvas.width <= 0 || canvas.height <= 0) {
    clearElementHighlight();
    return;
  }
  const { x, y, width, height } = element.bounds;
  elementHighlight.style.left = `${x / canvas.width * 100}%`;
  elementHighlight.style.top = `${y / canvas.height * 100}%`;
  elementHighlight.style.width = `${width / canvas.width * 100}%`;
  elementHighlight.style.height = `${height / canvas.height * 100}%`;
  elementHighlightLabel.textContent = `${element.tag}${element.text ? ` · ${element.text}` : ""}`;
  elementHighlight.hidden = false;
}

function announceSelectedElement(element) {
  if (!element) {
    setStatus("该位置没有可选择的页面元素", true);
    return;
  }
  const selectionMessage = {
    type: "boxteam:browser-element-selected",
    workspaceId,
    browserId,
    element,
  };
  if (window.parent !== window) {
    window.parent.postMessage(selectionMessage, window.location.origin);
  } else {
    const channel = new BroadcastChannel("boxteam-browser-elements");
    channel.postMessage(selectionMessage);
    channel.close();
  }
  setPickingElement(false);
  setStatus(`已将 <${element.tag}> 添加到消息草稿`);
}

function clearCanvas() {
  context.fillStyle = "#11161d";
  context.fillRect(0, 0, canvas.width, canvas.height);
}

function describeSnapshot(snapshot) {
  if (snapshot.navigation_error?.message) {
    return snapshot.navigation_error.message;
  }
  const viewers = Number(snapshot.client_count || 0);
  const viewerLabel = viewers > 0 ? ` · ${viewers} 位用户已连接` : "";
  const operation = snapshot.active_operation;
  const operationLabel = operation?.actor?.startsWith("agent")
    ? ` · AI 正在执行 ${operation.action}`
    : operation?.actor?.startsWith("user:") && operation.actor !== `user:${participantId}`
      ? ` · 另一位用户正在执行 ${operation.action}`
      : "";
  return `${statusLabel(snapshot.status)}${viewerLabel}${operationLabel} · ${snapshot.title || "无标题"} · ${shortUrlLabel(snapshot.url)}`;
}

function overlayLabelForSnapshot(snapshot) {
  if (snapshot.navigation_error?.message) {
    return snapshot.navigation_error.message;
  }
  if (attached && snapshot.status === "running" && waitingForFramePageId) {
    return "正在加载标签页画面…";
  }
  if (attached && snapshot.status === "running") {
    return "";
  }
  if (snapshot.status === "running") {
    if (socket?.readyState === WebSocket.CONNECTING) {
      return "正在连接浏览器…";
    }
    return "已断开连接，浏览器页面仍在后台运行";
  }
  return statusLabel(snapshot.status);
}

function applyState(snapshot) {
  currentStatus = snapshot.status;
  currentNavigationError = snapshot.navigation_error || null;
  const nextActivePageId = snapshot.active_page_id || snapshot.page_id || null;
  if (nextActivePageId !== currentActivePageId) {
    currentActivePageId = nextActivePageId;
    waitingForFramePageId = nextActivePageId;
    pendingFrame = null;
    frameSerial += 1;
    clearCanvas();
  }
  setAgentAccessLocked(snapshot.agent_access_locked, snapshot.agent_lock_owner_id || null);
  renderBrowserTabs(snapshot);
  renderDownloads(snapshot);
  browserIdElement.textContent = `${snapshot.browser_id} · ${snapshot.title || "无标题"}`;
  if (document.activeElement !== addressInput) {
    addressInput.value = snapshot.url || "";
    addressInput.title = snapshot.url || "";
  }
  overlay.hidden = attached
    && snapshot.status === "running"
    && !waitingForFramePageId
    && !snapshot.navigation_error;
  overlay.textContent = overlayLabelForSnapshot(snapshot);
  const badgeMode = attached
    ? "attached"
    : socket?.readyState === WebSocket.CONNECTING
      ? "attaching"
    : snapshot.status === "running"
      ? "detached"
      : snapshot.status;
  attachStateBadge.className = `attach-state-badge ${badgeMode}`;
  attachStateBadge.textContent = attached
    ? `已连接 · ${Number(snapshot.client_count || 1)}`
    : socket?.readyState === WebSocket.CONNECTING
      ? "连接中"
    : snapshot.status === "running"
      ? "未连接"
      : statusLabel(snapshot.status);
  setStatus(
    describeSnapshot(snapshot),
    ["failed", "lost"].includes(snapshot.status) || Boolean(snapshot.navigation_error),
  );
  browserModalUi.sync(snapshot);
  updateControls();
}

function renderDownloads(snapshot) {
  const downloads = snapshot.downloads || [];
  downloadShelf.hidden = downloads.length === 0 && !snapshot.download_error;
  downloadSummary.textContent = snapshot.download_error
    ? `下载失败 · ${snapshot.download_error}`
    : `下载 · ${downloads.length}`;
  downloadList.replaceChildren();
  for (const download of downloads) {
    const row = document.createElement("div");
    row.className = "download-item";
    const name = document.createElement("span");
    name.className = "download-item-name";
    name.textContent = download.filename;
    name.title = download.filename;
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "保存到设备";
    save.addEventListener("click", () => {
      void saveDownloadToDevice(download).catch((error) => {
        setStatus(error instanceof Error ? error.message : String(error), true);
      });
    });
    row.append(name, save);
    downloadList.append(row);
  }
}

async function saveDownloadToDevice(download) {
  const response = await fetch(
    `${backendBaseUrl}/api/browsers/${encodeURIComponent(browserId)}/downloads/${encodeURIComponent(download.download_id)}`,
    { headers: backendRequestHeaders },
  );
  if (!response.ok) {
    throw new Error(`读取下载文件失败: ${response.status}`);
  }
  const blobUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = download.filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 30_000);
}

function renderBrowserTabs(snapshot) {
  browserTabList.replaceChildren();
  for (const page of snapshot.pages || []) {
    const tab = document.createElement("div");
    tab.className = `browser-tab${page.active ? " is-active" : ""}`;
    tab.title = page.url || "about:blank";

    const activate = document.createElement("button");
    activate.type = "button";
    activate.className = "browser-tab-label";
    activate.textContent = page.title || "新标签页";
    activate.disabled = page.active;
    activate.addEventListener("click", () => {
      command("activatePage", { pageId: page.page_id });
    });

    const close = document.createElement("button");
    close.type = "button";
    close.className = "browser-tab-close";
    close.title = "关闭标签页";
    close.setAttribute("aria-label", `关闭 ${page.title || "标签页"}`);
    close.innerHTML = '<span class="codicon codicon-close" aria-hidden="true"></span>';
    close.addEventListener("click", () => {
      command("closePage", { pageId: page.page_id });
    });

    tab.append(activate, close);
    browserTabList.append(tab);
  }
}

function markDeleted(message = "浏览器页面已删除或不存在", snapshot = null) {
  deleted = true;
  attached = false;
  pendingNavigationUrl = null;
  currentStatus = "deleted";
  setAgentAccessLocked(false);
  socket?.close();
  socket = null;
  cancelReconnect();
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
  const needsAck = message.type !== "inspectElement"
    && !(message.type === "pointer" && message.action === "move");
  send({
    browserId,
    ...message,
    ...(needsAck
      ? { clientOperationId: `${participantId}:${++clientOperationSequence}` }
      : {}),
  });
}

function acknowledgeFrame(message, { decodeMs = 0, drawMs = 0 } = {}) {
  if (!socket || socket.readyState !== WebSocket.OPEN || !browserId) {
    return;
  }
  if (!Number.isInteger(message.frameId) || message.frameId <= 0) {
    return;
  }
  send({
    type: "frameAck",
    browserId,
    frameId: message.frameId,
    decodeMs: Number(Math.max(0, decodeMs).toFixed(2)),
    drawMs: Number(Math.max(0, drawMs).toFixed(2)),
  });
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
    if (attached && socket?.readyState === WebSocket.OPEN) {
      return;
    }
    void syncBrowserState().catch((error) => {
      setStatus(error instanceof Error ? error.message : String(error), true);
    });
  }, 2000);
}

function cancelReconnect() {
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

function scheduleReconnect(reason) {
  if (manualDetach || deleted || currentStatus !== "running" || reconnectTimer !== null) {
    return;
  }
  const delay = Math.min(250 * (2 ** reconnectAttempt), 3000);
  reconnectAttempt += 1;
  overlay.hidden = false;
  overlay.textContent = `连接中断，正在重连…`;
  setAttachButtonMode("attaching");
  setStatus(`${reason}；${delay}ms 后自动重连`, true);
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    attach();
  }, delay);
}

function detach() {
  manualDetach = true;
  cancelReconnect();
  setPickingElement(false);
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
  if (message.pageId && currentActivePageId && message.pageId !== currentActivePageId) {
    acknowledgeFrame(message);
    return;
  }
  pendingFrame = message;
  if (renderingFrame) {
    return;
  }
  renderingFrame = true;
  void renderLatestFrame();
}

async function renderLatestFrame() {
  try {
    while (pendingFrame) {
      const message = pendingFrame;
      pendingFrame = null;
      const serial = ++frameSerial;
      const decodeStartedAt = performance.now();
      let bitmap;
      try {
        bitmap = await createImageBitmap(message.jpeg);
      } catch (error) {
        acknowledgeFrame(message, { decodeMs: performance.now() - decodeStartedAt });
        throw error;
      }
      const decodeMs = performance.now() - decodeStartedAt;
      if (pendingFrame
        || serial !== frameSerial
        || (message.pageId && currentActivePageId && message.pageId !== currentActivePageId)) {
        bitmap.close();
        acknowledgeFrame(message, { decodeMs });
        continue;
      }
      if (canvas.width !== message.width || canvas.height !== message.height) {
        canvas.width = message.width;
        canvas.height = message.height;
      }
      const drawStartedAt = performance.now();
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const drawMs = performance.now() - drawStartedAt;
      bitmap.close();
      acknowledgeFrame(message, { decodeMs, drawMs });
      waitingForFramePageId = null;
      if (attached && currentStatus === "running" && !currentNavigationError) {
        overlay.hidden = true;
      }
    }
  } catch (error) {
    setStatus(`浏览器画面解码失败: ${error}`, true);
  } finally {
    renderingFrame = false;
    if (pendingFrame) {
      drawFrame(pendingFrame);
    }
  }
}

function attach() {
  if (!browserId) {
    setStatus("URL 缺少 browserId 参数", true);
    return;
  }
  if (attached) {
    return;
  }
  manualDetach = false;
  cancelReconnect();
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    setStatus("正在连接浏览器...");
    return;
  }
  socket = new WebSocket(
    backendWsUrl(backendBaseUrl, gatewayMode ? gatewayToken : null),
  );
  socket.binaryType = "arraybuffer";
  setAttachButtonMode("attaching");
  setStatus("正在连接浏览器...");

  socket.addEventListener("open", () => {
    reconnectAttempt = 0;
    send({ type: "attach", browserId, participantId });
  });

  socket.addEventListener("message", (event) => {
    if (event.data instanceof ArrayBuffer) {
      const view = new DataView(event.data);
      if (view.byteLength < 4) {
        throw new Error("浏览器二进制画面缺少元数据头");
      }
      const metadataLength = view.getUint32(0);
      if (metadataLength <= 0 || metadataLength > view.byteLength - 4) {
        throw new Error(`浏览器二进制画面元数据长度非法: ${metadataLength}`);
      }
      const metadata = JSON.parse(new TextDecoder().decode(
        new Uint8Array(event.data, 4, metadataLength),
      ));
      if (metadata.type !== "frame") {
        throw new Error(`未知二进制浏览器消息类型: ${metadata.type}`);
      }
      drawFrame({
        ...metadata,
        jpeg: new Blob([event.data.slice(4 + metadataLength)], { type: "image/jpeg" }),
      });
      return;
    }
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      attached = true;
      setAttachButtonMode("attached");
      applyState(message.state);
      if (pendingNavigationUrl) {
        const targetUrl = pendingNavigationUrl;
        pendingNavigationUrl = null;
        addressInput.value = targetUrl;
        command("goto", { url: targetUrl });
        setStatus(`正在打开 ${shortUrlLabel(targetUrl)}`);
        addressInput.focus({ preventScroll: true });
      } else if (message.state.url === "about:blank") {
        addressInput.focus({ preventScroll: true });
        addressInput.select();
      } else {
        keyboardTarget.focus({ preventScroll: true });
      }
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
      setStatus(message.state.find_found === false ? "未找到匹配文字" : "浏览器命令已完成");
      return;
    }
    if (message.type === "elementHovered") {
      drawElementHighlight(message.element);
      return;
    }
    if (message.type === "elementSelected") {
      announceSelectedElement(message.element);
      return;
    }
    if (message.type === "operationAck") {
      document.documentElement.dataset.browserOperationRevision = String(
        message.operation?.operation_revision || "",
      );
      return;
    }
    if (message.type === "clipboardText") {
      void collaborationUi.copyText(message.text).catch((error) => {
        setStatus(error instanceof Error ? error.message : String(error), true);
      });
      return;
    }
    if (message.type === "participantPointer") {
      collaborationUi.showParticipantPointer(message.pointer);
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
      scheduleReconnect("浏览器连接已断开");
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

function navigate(targetUrl) {
  addressInput.value = targetUrl;
  if (attached) {
    command("goto", { url: targetUrl });
    return;
  }
  pendingNavigationUrl = targetUrl;
  setStatus("浏览器正在连接，将在连接后打开该地址");
  if (currentStatus === "running") {
    attach();
  }
  updateControls();
}

browserModalUi = createBrowserModalUi({
  modal: browserModal,
  form: browserModalForm,
  title: browserModalTitle,
  message: browserModalMessage,
  prompt: browserModalPrompt,
  files: browserModalFiles,
  cancelButton: browserModalCancel,
  acceptButton: browserModalAccept,
  isAttached: () => attached,
  sendIfAttached,
  setStatus,
});

collaborationUi = createBrowserCollaborationUi({
  canvas,
  keyboardTarget,
  cursorLayer: participantCursors,
  contextMenu: browserContextMenu,
  command,
  sendIfAttached,
  setStatus,
});

handleBrowserShortcut = createBrowserShortcuts({
  addressInput,
  findBar,
  findInput,
  findPrevious,
  findClose,
  activePageId: () => currentActivePageId,
  command,
});

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
  navigate,
  updateControls,
  applyState,
  markDeleted,
  setStatus,
});
window.BOXTEAM_BROWSER_CLIENT_READY = true;
const earlyNavigationUrl = typeof window.BOXTEAM_BROWSER_EARLY_URL === "string"
  ? window.BOXTEAM_BROWSER_EARLY_URL.trim()
  : "";
window.BOXTEAM_BROWSER_EARLY_URL = null;
if (earlyNavigationUrl) {
  navigate(earlyNavigationUrl);
}

bindBrowserInputEvents({
  canvas,
  keyboardTarget,
  isAttached: () => attached,
  isPicking: () => pickingElement,
  onPickMove: (point) => {
    pendingInspectPoint = point;
    if (inspectTimer !== null) {
      return;
    }
    inspectTimer = window.setTimeout(() => {
      inspectTimer = null;
      if (pendingInspectPoint) {
        sendIfAttached({ type: "inspectElement", ...pendingInspectPoint });
        pendingInspectPoint = null;
      }
    }, 45);
  },
  onPickSelect: (point) => sendIfAttached({ type: "selectElement", ...point }),
  onCancelPick: () => setPickingElement(false),
  onContextMenu: collaborationUi.showContextMenu,
  onBrowserShortcut: handleBrowserShortcut,
  sendIfAttached,
});

elementPickerToggle.addEventListener("click", () => {
  if (attached) {
    setPickingElement(!pickingElement);
    canvas.focus();
  }
});

async function requestAgentLock(locked, { silent = false } = {}) {
  if (!browserId || deleted || currentStatus !== "running") {
    return;
  }
  agentLockToggle.disabled = true;
  try {
    const response = await fetch(
      `${backendBaseUrl}/api/browsers/${encodeURIComponent(browserId)}/agent-lock`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...backendRequestHeaders },
        body: JSON.stringify({ locked, owner_id: participantId }),
      },
    );
    if (!response.ok) {
      throw new Error(`更新 AI 操作锁失败: ${response.status}`);
    }
    const payload = await response.json();
    applyState(payload.data);
    if (!silent) {
      setStatus(locked
        ? "已锁定 AI 操作；用户仍可正常操作浏览器"
        : "已允许 AI 操作；用户和 AI 均可操作浏览器");
    }
  } catch (error) {
    await syncBrowserState();
    setStatus(error instanceof Error ? error.message : String(error), true);
  } finally {
    updateControls();
  }
}

agentLockToggle.addEventListener("click", async () => {
  await requestAgentLock(!agentAccessLocked);
});

newTabButton.addEventListener("click", () => {
  command("newPage");
});

window.addEventListener("beforeunload", () => {
  manualDetach = true;
  cancelReconnect();
  if (statusPollTimer !== null) {
    window.clearInterval(statusPollTimer);
  }
  if (agentLockHeartbeatTimer !== null) {
    window.clearInterval(agentLockHeartbeatTimer);
  }
  if (attached && socket?.readyState === WebSocket.OPEN && browserId) {
    socket.send(JSON.stringify({ type: "detach", browserId }));
  }
});

setAttachButtonMode("attaching");
setAgentAccessLocked(false);
void initializeGatewayAuth()
  .then(() => loadSnapshot())
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
