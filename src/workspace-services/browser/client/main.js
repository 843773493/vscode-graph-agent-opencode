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
import {
  formatBrowserElementBasicClipboard,
  formatBrowserElementClipboard,
} from "./browserElementContext.js";

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

const browserShell = document.querySelector(".browser-shell");
const browserIdElement = document.querySelector("#browser-id");
const attachStateBadge = document.querySelector("#attach-state-badge");
const aiControlBadge = document.querySelector("#ai-control-badge");
const statusLine = document.querySelector("#status-line");
const attachToggle = document.querySelector("#attach-toggle");
const attachToggleLabel = attachToggle.querySelector(".sr-only");
const refreshStateButton = document.querySelector("#refresh-state");
const debugToggle = document.querySelector("#debug-toggle");
const closeBrowserButton = document.querySelector("#close-browser");
const deleteBrowserButton = document.querySelector("#delete-browser");
const agentLockToggle = document.querySelector("#agent-lock-toggle");
const agentLockToggleLabel = agentLockToggle.querySelector(".sr-only");
const elementPickerToggle = document.querySelector("#element-picker-toggle");
const elementPickerAddToggle = document.querySelector("#element-picker-add-toggle");
const copyBrowserLogsButton = document.querySelector("#copy-browser-logs");
const backButton = document.querySelector("#back-button");
const forwardButton = document.querySelector("#forward-button");
const reloadButton = document.querySelector("#reload-button");
const urlForm = document.querySelector("#url-form");
const addressInput = document.querySelector("#address-input");
const goButton = document.querySelector("#go-button");
const deviceProfileSelect = document.querySelector("#device-profile-select");
const deviceWidthInput = document.querySelector("#device-width-input");
const deviceHeightInput = document.querySelector("#device-height-input");
const deviceDprSelect = document.querySelector("#device-dpr-select");
const deviceFitButton = document.querySelector("#device-fit-button");
const deviceRotateButton = document.querySelector("#device-rotate-button");
const deviceNetworkSelect = document.querySelector("#device-network-select");
const deviceSaveMenu = document.querySelector("#device-save-menu");
const devicePresetName = document.querySelector("#device-preset-name");
const deviceSavePresetButton = document.querySelector("#device-save-preset");
const deviceSettingsMenu = document.querySelector("#device-settings-menu");
const deviceUaInput = document.querySelector("#device-ua-input");
const deviceTouchInput = document.querySelector("#device-touch-input");
const deviceScreenshotButton = document.querySelector("#device-screenshot-button");
const deviceResetButton = document.querySelector("#device-reset-button");
const deviceSummary = document.querySelector("#device-summary");
const browserTabList = document.querySelector("#browser-tab-list");
const newTabButton = document.querySelector("#new-tab-button");
const downloadShelf = document.querySelector("#download-shelf");
const downloadSummary = document.querySelector("#download-summary");
const downloadList = document.querySelector("#download-list");
const screenStage = document.querySelector("#screen-stage");
const screenScroll = document.querySelector("#screen-scroll");
const screenContent = document.querySelector("#screen-content");
const canvas = document.querySelector("#screen-canvas");
const viewportResizeHandles = [...document.querySelectorAll(".viewport-resize-handle")];
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
const debugPanel = document.querySelector("#debug-panel");
const debugBannerClose = document.querySelector("#debug-banner-close");
const debugRefresh = document.querySelector("#debug-refresh");
const debugClose = document.querySelector("#debug-close");
const debugClear = document.querySelector("#debug-clear");
const debugPageLabel = document.querySelector("#debug-page-label");
const debugElementsTree = document.querySelector("#debug-elements-tree");
const debugConsoleList = document.querySelector("#debug-console-list");
const debugSourcesList = document.querySelector("#debug-sources-list");
const debugNetworkList = document.querySelector("#debug-network-list");
const debugDrawerSummary = document.querySelector("#debug-drawer-summary");
const debugDrawerToggle = document.querySelector("#debug-drawer-toggle");
const debugTabs = [...document.querySelectorAll("[data-debug-tab]")];
const debugContents = [...document.querySelectorAll("[data-debug-content]")];
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
let pickingMode = null;
let browserToolLogs = [];
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
let currentToolPageUrl = null;
let waitingForFramePageId = null;
let currentNavigationError = null;
let remoteViewport = { width: 1280, height: 800 };
let remoteViewportInitialized = false;
let viewportDraft = null;
let fitViewportToWindow = false;
let autoFitSuppressed = false;
let viewportZoom = 1;
let viewportDrag = null;
let viewportResize = null;
let viewportPosition = null;
let currentDeviceSnapshot = null;
let debugSnapshotData = null;

function viewportForLayout() {
  return viewportDraft || remoteViewport;
}

function clampViewportPosition(position) {
  const margin = 12;
  const width = screenContent.getBoundingClientRect().width;
  const height = screenContent.getBoundingClientRect().height;
  return {
    x: Math.max(margin - width, Math.min(screenScroll.clientWidth - margin, position.x)),
    y: Math.max(margin - height, Math.min(screenScroll.clientHeight - margin, position.y)),
  };
}

function layoutScreen() {
  const containerWidth = screenScroll.clientWidth;
  const containerHeight = screenScroll.clientHeight;
  const viewport = viewportForLayout();
  if (containerWidth <= 0 || containerHeight <= 0 || viewport.width <= 0 || viewport.height <= 0) {
    return;
  }
  const autoFit = browserShell.classList.contains("debug-open")
    && currentDeviceProfile?.is_mobile === true
    && !autoFitSuppressed;
  const scale = autoFit || fitViewportToWindow
    ? Math.min(
      (containerWidth - 32) / viewport.width,
      (containerHeight - 32) / viewport.height,
    )
    : viewportZoom;
  deviceFitButton.setAttribute("aria-pressed", String(autoFit || fitViewportToWindow));
  deviceFitButton.title = autoFit || fitViewportToWindow
    ? "适应窗口"
    : `实际尺寸 (1:1) · 缩放 ${Math.round(viewportZoom * 100)}%`;
  const contentWidth = Math.max(1, Math.floor(viewport.width * Math.max(0.05, scale)));
  const contentHeight = Math.max(1, Math.floor(viewport.height * Math.max(0.05, scale)));
  screenContent.style.width = `${contentWidth}px`;
  screenContent.style.height = `${contentHeight}px`;
  if (viewportDrag === null && viewportResize === null && viewportPosition === null) {
    const canCenter = contentWidth <= containerWidth - 32 && contentHeight <= containerHeight - 32;
    screenContent.style.left = canCenter ? "50%" : "16px";
    screenContent.style.top = canCenter ? "50%" : "16px";
    screenContent.style.transform = canCenter ? "translate(-50%, -50%)" : "none";
  } else if (viewportPosition) {
    screenContent.style.left = `${viewportPosition.x}px`;
    screenContent.style.top = `${viewportPosition.y}px`;
    screenContent.style.transform = "none";
  }
}

function setDebugVisibility(open) {
  debugPanel.hidden = !open;
  debugToggle.hidden = open;
  browserShell.classList.toggle("debug-open", open);
  if (open) {
    autoFitSuppressed = false;
  }
  layoutScreen();
}

function setStatus(message, error = false) {
  statusLine.title = message;
  statusLine.setAttribute("aria-label", message);
  statusLine.dataset.status = message;
  statusLine.classList.toggle("error", error);
}

function setRemoteViewport(viewport, pixelRatio = 1) {
  remoteViewport = {
    width: viewport.width,
    height: viewport.height,
  };
  viewportDraft = null;
  remoteViewportInitialized = true;
  const pixelWidth = Math.max(1, Math.round(viewport.width * pixelRatio));
  const pixelHeight = Math.max(1, Math.round(viewport.height * pixelRatio));
  const canvasSizeChanged = canvas.width !== pixelWidth || canvas.height !== pixelHeight;
  if (canvasSizeChanged) {
    awaitingDeviceFrame = true;
    matchingDeviceFrameCount = 0;
    firstDeviceFrameProfile = null;
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
    clearCanvas();
    if (attached && currentStatus === "running") {
      overlay.hidden = false;
      overlay.textContent = "正在加载设备画面…";
    }
  }
  layoutScreen();
}

const screenResizeObserver = new ResizeObserver(layoutScreen);
screenResizeObserver.observe(screenScroll);
window.addEventListener("resize", layoutScreen);

canvas.width = 1280;
canvas.height = 800;
clearCanvas();

function addBrowserToolLog(level, message) {
  const entry = { level, message, time: new Date() };
  browserToolLogs = [...browserToolLogs, entry].slice(-100);
  const logger = console[level] || console.log;
  logger.call(console, `[BoxTeam Browser] ${message}`);
}

function formatBrowserToolLogs() {
  return browserToolLogs.map((entry) => {
    const time = entry.time.toLocaleTimeString();
    return `[${time}] ${entry.level}: ${entry.message}`;
  }).join("\n");
}

async function copyBrowserLogs() {
  if (browserToolLogs.length === 0) {
    setStatus("当前没有可复制的日志", true);
    return;
  }
  await collaborationUi.copyText(formatBrowserToolLogs());
  const message = `已复制 ${browserToolLogs.length} 条日志到本地剪贴板`;
  setStatus(message);
  addBrowserToolLog("info", message);
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
  const stateLabel = mode === "attached"
    ? "已连接"
    : mode === "attaching"
      ? "连接中"
      : "未连接";
  attachStateBadge.className = `attach-state-badge ${mode}`;
  attachStateBadge.title = stateLabel;
  attachStateBadge.setAttribute("aria-label", stateLabel);
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
  deviceProfileSelect.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceWidthInput.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceHeightInput.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceDprSelect.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceFitButton.disabled = !hasBrowserId || deleted;
  deviceNetworkSelect.disabled = !hasBrowserId || !attached || deleted || !running;
  devicePresetName.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceSavePresetButton.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceUaInput.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceTouchInput.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceScreenshotButton.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceResetButton.disabled = !hasBrowserId || !attached || deleted || !running;
  deviceRotateButton.disabled = !hasBrowserId || !attached || deleted || !running
    || currentDeviceProfile?.is_mobile !== true;
  elementPickerToggle.disabled = !hasBrowserId || !attached || deleted || !running;
  elementPickerAddToggle.disabled = !hasBrowserId || !attached || deleted || !running;
  copyBrowserLogsButton.disabled = !hasBrowserId || deleted;
  agentLockToggle.disabled = !hasBrowserId
    || deleted
    || !running
    || (agentAccessLocked && agentLockOwnerId !== participantId);
  newTabButton.disabled = !hasBrowserId || !attached || deleted || !running;
  canvas.classList.toggle("is-disabled", !hasBrowserId || deleted || !running);
  for (const handle of viewportResizeHandles) {
    handle.hidden = !hasBrowserId || !attached || deleted || !running;
  }
}

let currentDeviceProfile = null;
let currentDeviceOrientation = "portrait";
let awaitingDeviceFrame = false;
let matchingDeviceFrameCount = 0;
let firstDeviceFrameProfile = null;

function canvasFrameProfile() {
  const data = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const step = Math.max(4, Math.floor(data.length / 512));
  let hash = 2_166_136_261;
  let sampleCount = 0;
  let sampleSum = 0;
  for (let index = 0; index < data.length; index += step) {
    const value = data[index];
    sampleSum += value;
    sampleCount += 1;
    hash ^= value;
    hash = Math.imul(hash, 16_777_619);
  }
  return {
    hash: hash >>> 0,
    average: sampleSum / sampleCount,
  };
}

function renderDeviceState(snapshot) {
  const profiles = Array.isArray(snapshot.device_profiles) ? snapshot.device_profiles : [];
  const presets = Array.isArray(snapshot.device_presets) ? snapshot.device_presets : [];
  const profileOptionsKey = profiles.map((profile) => `${profile.id}:${profile.label}`).join("|")
    + `::${presets.map((preset) => `${preset.id}:${preset.name}`).join("|")}`;
  if (deviceProfileSelect.dataset.optionsKey !== profileOptionsKey && profiles.length > 0) {
    const profileOptions = profiles.map((profile) => {
      const option = document.createElement("option");
      option.value = profile.id;
      option.textContent = profile.label;
      return option;
    });
    if (presets.length > 0) {
      const presetGroup = document.createElement("optgroup");
      presetGroup.label = "已保存预设";
      for (const preset of presets) {
        const option = document.createElement("option");
        option.value = `preset:${preset.id}`;
        option.textContent = preset.name;
        presetGroup.append(option);
      }
      profileOptions.push(presetGroup);
    }
    deviceProfileSelect.replaceChildren(...profileOptions);
    deviceProfileSelect.dataset.optionsKey = profileOptionsKey;
  }
  const profileId = snapshot.device_profile || snapshot.device_emulation?.profile_id || "desktop";
  const profile = profiles.find((candidate) => candidate.id === profileId) || null;
  currentDeviceProfile = profile;
  if (deviceProfileSelect.querySelector(`option[value="${CSS.escape(profileId)}"]`)) {
    deviceProfileSelect.value = profileId;
  }
  const orientation = snapshot.device_orientation
    || snapshot.device_emulation?.orientation
    || "portrait";
  currentDeviceOrientation = orientation;
  const viewport = snapshot.viewport || snapshot.device_emulation?.viewport || { width: 1280, height: 800 };
  const pixelRatio = snapshot.device_scale_factor
    || snapshot.device_emulation?.pixel_ratio
    || 1;
  setRemoteViewport(viewport, pixelRatio);
  currentDeviceSnapshot = snapshot;
  if (document.activeElement !== deviceWidthInput) deviceWidthInput.value = String(viewport.width);
  if (document.activeElement !== deviceHeightInput) deviceHeightInput.value = String(viewport.height);
  if (document.activeElement !== deviceDprSelect) {
    const dprOverride = snapshot.device_scale_factor_override;
    deviceDprSelect.value = dprOverride === null || dprOverride === undefined
      ? "auto"
      : String(dprOverride);
  }
  const networkProfiles = Array.isArray(snapshot.network_profiles) ? snapshot.network_profiles : [];
  const networkOptionsKey = networkProfiles.map((network) => `${network.id}:${network.label}`).join("|");
  if (deviceNetworkSelect.dataset.optionsKey !== networkOptionsKey) {
    deviceNetworkSelect.replaceChildren(...networkProfiles.map((network) => {
      const option = document.createElement("option");
      option.value = network.id;
      option.textContent = network.label;
      return option;
    }));
    deviceNetworkSelect.dataset.optionsKey = networkOptionsKey;
  }
  deviceNetworkSelect.value = snapshot.network_profile_id
    || snapshot.device_emulation?.network_profile_id
    || "none";
  if (document.activeElement !== deviceUaInput) {
    deviceUaInput.value = snapshot.user_agent_override
      || snapshot.device_emulation?.user_agent_override
      || "";
  }
  deviceTouchInput.checked = snapshot.touch_simulation_override
    ?? snapshot.device_emulation?.touch_simulation
    ?? false;
  const touch = snapshot.touch_simulation_enabled
    ?? snapshot.device_emulation?.touch_simulation
    ?? false;
  deviceSummary.textContent = `${profile?.label || profileId} · ${viewport.width}×${viewport.height}`
    + ` · DPR ${pixelRatio}${orientation === "landscape" ? " · 横向" : " · 纵向"}`
    + (touch ? " · 触摸" : "");
  deviceRotateButton.title = orientation === "landscape" ? "切换为纵向" : "切换为横向";
  deviceRotateButton.setAttribute("aria-label", deviceRotateButton.title);
  debugPageLabel.textContent = `${snapshot.title || "无标题"} · ${shortUrlLabel(snapshot.url || "about:blank")}`;
}

function activeDebugPanel() {
  return debugTabs.find((tab) => tab.classList.contains("is-active"))?.dataset.debugTab || "elements";
}

function setDebugPanel(panel) {
  for (const tab of debugTabs) {
    const active = tab.dataset.debugTab === panel;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const content of debugContents) {
    content.classList.toggle("is-active", content.dataset.debugContent === panel);
  }
}

function renderDebugElements(root) {
  debugElementsTree.replaceChildren();
  if (!root) {
    debugElementsTree.innerHTML = '<span class="debug-empty">暂无 DOM 数据</span>';
    return;
  }
  const renderNode = (node, depth) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "debug-element-node";
    row.style.paddingLeft = `${10 + depth * 16}px`;
    row.title = node.node_id || "DOM 节点";
    const tag = document.createElement("span");
    tag.className = "debug-element-tag";
    tag.textContent = `<${node.tag || "element"}`;
    row.append(tag);
    for (const [name, value] of Object.entries(node.attributes || {})) {
      const attribute = document.createElement("span");
      attribute.className = "debug-element-attribute";
      attribute.textContent = ` ${name}="${value}"`;
      row.append(attribute);
    }
    const end = document.createElement("span");
    end.className = "debug-element-tag";
    end.textContent = ">";
    row.append(end);
    if (node.text) {
      const text = document.createElement("span");
      text.className = "debug-element-text";
      text.textContent = ` ${node.text}`;
      row.append(text);
    }
    debugElementsTree.append(row);
    for (const child of node.children || []) {
      renderNode(child, depth + 1);
    }
  };
  renderNode(root, 0);
}

function renderDebugConsole(messages) {
  debugConsoleList.replaceChildren();
  if (!messages?.length) {
    debugConsoleList.innerHTML = '<span class="debug-empty">暂无控制台消息</span>';
    return;
  }
  for (const message of messages) {
    const row = document.createElement("div");
    row.className = `debug-console-row ${message.level === "error" ? "error" : message.level === "warning" ? "warn" : ""}`;
    const level = document.createElement("span");
    level.className = "debug-console-level";
    level.textContent = message.level || "log";
    const text = document.createElement("span");
    text.textContent = message.text || "";
    row.append(level, text);
    debugConsoleList.append(row);
  }
}

function renderDebugNetwork(requests) {
  debugNetworkList.replaceChildren();
  if (!requests?.length) {
    debugNetworkList.innerHTML = '<span class="debug-empty">暂无网络请求</span>';
    return;
  }
  for (const request of requests) {
    const row = document.createElement("div");
    row.className = "debug-network-row";
    const url = document.createElement("span");
    url.textContent = `${request.method || "GET"} ${request.url || ""}`;
    url.title = request.url || "";
    const status = document.createElement("span");
    status.className = request.failed ? "failed" : request.status ? "ok" : "";
    status.textContent = request.failed ? "失败" : String(request.status || "等待");
    const type = document.createElement("span");
    type.textContent = request.resource_type || "other";
    row.append(url, status, type);
    debugNetworkList.append(row);
  }
}

function renderDebugSources(sources) {
  debugSourcesList.replaceChildren();
  if (!sources?.length) {
    debugSourcesList.innerHTML = '<span class="debug-empty">暂无源代码资源</span>';
    return;
  }
  for (const source of sources) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "debug-source-row";
    row.title = source.url || "";
    const type = document.createElement("span");
    type.className = "debug-source-type";
    type.textContent = source.type || "resource";
    const url = document.createElement("span");
    url.textContent = source.url || "about:blank";
    row.append(type, url);
    debugSourcesList.append(row);
  }
}

function renderDebugSnapshot(data) {
  debugSnapshotData = data;
  renderDebugElements(data?.elements);
  renderDebugConsole(data?.console || []);
  renderDebugSources(data?.sources || []);
  renderDebugNetwork(data?.network || []);
  const consoleCount = data?.console?.length || 0;
  const networkCount = data?.network?.length || 0;
  debugDrawerSummary.textContent = `${consoleCount} 条控制台消息 · ${networkCount} 条网络请求`;
  if (data?.title || data?.url) {
    debugPageLabel.textContent = `${data.title || "无标题"} · ${shortUrlLabel(data.url || "about:blank")}`;
  }
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

function setPickingElement(active, mode = pickingMode) {
  pickingElement = active;
  pickingMode = active ? (mode === "rich" ? "rich" : "basic") : null;
  elementPickerToggle.classList.toggle("is-active", active);
  elementPickerToggle.setAttribute("aria-pressed", String(active));
  elementPickerToggle.title = active
    ? "退出元素选择 (Esc)"
    : "选择页面元素";
  elementPickerAddToggle.classList.toggle("is-active", active && pickingMode === "rich");
  elementPickerAddToggle.setAttribute("aria-pressed", String(active && pickingMode === "rich"));
  elementPickerAddToggle.title = active && pickingMode === "rich"
    ? "退出元素选择 (Esc)"
    : "选择元素+";
  canvas.classList.toggle("is-picking", active);
  if (!active) {
    clearElementHighlight();
  }
  setStatus(active
    ? pickingMode === "rich"
      ? "选择元素+已开启：悬停预览，点击复制完整元素上下文，点击按钮或按 Esc 退出"
      : "选择元素已开启：悬停预览，点击选择元素，点击按钮或按 Esc 退出"
    : "已退出元素选择");
}

function drawElementHighlight(element) {
  if (!pickingElement || !element?.bounds || remoteViewport.width <= 0 || remoteViewport.height <= 0) {
    clearElementHighlight();
    return;
  }
  const { x, y, width, height } = element.bounds;
  elementHighlight.style.left = `${x / remoteViewport.width * 100}%`;
  elementHighlight.style.top = `${y / remoteViewport.height * 100}%`;
  elementHighlight.style.width = `${width / remoteViewport.width * 100}%`;
  elementHighlight.style.height = `${height / remoteViewport.height * 100}%`;
  elementHighlightLabel.textContent = `${element.tag}${element.text ? ` · ${element.text}` : ""}`;
  elementHighlight.hidden = false;
}

function postBrowserMessage(message) {
  if (window.parent !== window) {
    window.parent.postMessage(message, window.location.origin);
    return;
  }
  const channel = new BroadcastChannel("boxteam-browser-elements");
  channel.postMessage(message);
  channel.close();
}

function announceSelectedElement(element, mode = pickingMode) {
  if (!element) {
    setStatus("该位置没有可选择的页面元素", true);
    addBrowserToolLog("error", "元素选择失败：没有命中页面元素");
    return;
  }
  const selectionMode = mode === "rich" ? "rich" : "basic";
  const selectionMessage = {
    type: "boxteam:browser-element-selected",
    workspaceId,
    browserId,
    element,
    mode: selectionMode,
  };
  postBrowserMessage(selectionMessage);
  // VS Code 选中一次元素后会结束拾取会话，避免后续普通点击误触发新的选择。
  setPickingElement(false);
  const selectedMessage = selectionMode === "rich"
    ? `已选择 <${element.tag}> 元素并生成完整上下文`
    : `已选择 <${element.tag}> 元素`;
  setStatus(selectedMessage);
  addBrowserToolLog("info", `${selectedMessage}: ${element.selector || element.tag}`);
  const clipboardText = selectionMode === "rich"
    ? formatBrowserElementClipboard([element])
    : formatBrowserElementBasicClipboard(element);
  void collaborationUi.copyText(clipboardText)
    .then(() => {
      const copiedMessage = selectionMode === "rich"
        ? "已复制 VS Code 格式的完整元素上下文到本地剪贴板"
        : "已复制元素内容到本地剪贴板";
      setStatus(copiedMessage);
      addBrowserToolLog("info", copiedMessage);
    })
    .catch((error) => {
      const message = `复制失败：${error instanceof Error ? error.message : String(error)}`;
      setStatus(message, true);
      addBrowserToolLog("error", message);
    });
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
  return `${statusLabel(snapshot.status)}${viewerLabel}${operationLabel}`;
}

function overlayLabelForSnapshot(snapshot) {
  if (snapshot.navigation_error?.message) {
    return snapshot.navigation_error.message;
  }
  if (attached && snapshot.status === "running" && (waitingForFramePageId || awaitingDeviceFrame)) {
    return awaitingDeviceFrame ? "正在加载设备画面…" : "正在加载标签页画面…";
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
  const pageChanged = currentActivePageId !== null
    && (nextActivePageId !== currentActivePageId
      || (currentToolPageUrl !== null && snapshot.url !== currentToolPageUrl));
  if (pageChanged || nextActivePageId !== currentActivePageId) {
    const hadActivePage = currentActivePageId !== null;
    currentActivePageId = nextActivePageId;
    if (hadActivePage && pageChanged) {
      addBrowserToolLog("info", "页面已变化，工具已就绪");
    }
    waitingForFramePageId = nextActivePageId;
    pendingFrame = null;
    frameSerial += 1;
    clearCanvas();
  }
  currentToolPageUrl = snapshot.url || null;
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
    && !awaitingDeviceFrame
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
  const badgeLabel = attached
    ? "已连接"
    : socket?.readyState === WebSocket.CONNECTING
      ? "连接中"
    : snapshot.status === "running"
      ? "未连接"
      : statusLabel(snapshot.status);
  attachStateBadge.title = badgeLabel;
  attachStateBadge.setAttribute("aria-label", badgeLabel);
  setStatus(
    describeSnapshot(snapshot),
    ["failed", "lost"].includes(snapshot.status) || Boolean(snapshot.navigation_error),
  );
  renderDeviceState(snapshot);
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
  attachStateBadge.title = "已删除";
  attachStateBadge.setAttribute("aria-label", "已删除");
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
  attachStateBadge.title = "未连接";
  attachStateBadge.setAttribute("aria-label", "未连接");
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
      if (remoteViewportInitialized
        && (message.width !== remoteViewport.width || message.height !== remoteViewport.height)) {
        bitmap.close();
        acknowledgeFrame(message, { decodeMs });
        continue;
      }
      if (!remoteViewportInitialized) {
        setRemoteViewport(
          { width: message.width, height: message.height },
          (message.pixelWidth || message.width) / message.width,
        );
      }
      layoutScreen();
      const pixelWidth = message.pixelWidth || message.width;
      const pixelHeight = message.pixelHeight || message.height;
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      const drawStartedAt = performance.now();
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const drawMs = performance.now() - drawStartedAt;
      bitmap.close();
      acknowledgeFrame(message, { decodeMs, drawMs });
      waitingForFramePageId = null;
      if (awaitingDeviceFrame) {
        matchingDeviceFrameCount += 1;
        const profile = canvasFrameProfile();
        if (firstDeviceFrameProfile === null) {
          firstDeviceFrameProfile = profile;
        }
        const averageDelta = Math.abs(profile.average - firstDeviceFrameProfile.average);
        if (profile.average >= 200
          || averageDelta >= 24
          || (profile.hash !== firstDeviceFrameProfile.hash && averageDelta >= 8)
          || matchingDeviceFrameCount >= 6) {
          awaitingDeviceFrame = false;
        }
      }
      if (attached && currentStatus === "running" && !currentNavigationError && !awaitingDeviceFrame) {
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
      if (!debugPanel.hidden) {
        window.setTimeout(requestDebugSnapshot, 120);
      }
      setStatus(message.state.find_found === false ? "未找到匹配文字" : "浏览器命令已完成");
      return;
    }
    if (message.type === "debugSnapshot") {
      renderDebugSnapshot(message.data);
      setStatus("调试面板已刷新");
      return;
    }
    if (message.type === "screenshotResult") {
      downloadClientScreenshot(message.data, message.mimeType || "image/png");
      setStatus("截图已下载");
      return;
    }
    if (message.type === "elementHovered") {
      drawElementHighlight(message.element);
      return;
    }
    if (message.type === "elementSelected") {
      announceSelectedElement(message.element, message.mode);
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
      void syncBrowserState().catch((error) => {
        setStatus(error instanceof Error ? error.message : String(error), true);
      });
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

function changeDeviceProfile(profileId, orientation) {
  if (profileId.startsWith("preset:")) {
    const presetId = profileId.slice("preset:".length);
    const preset = (currentDeviceSnapshot?.device_presets || []).find((item) => item.id === presetId);
    if (!preset) {
      setStatus("设备预设不存在，已重新读取状态", true);
      void syncBrowserState();
      return;
    }
    changeDeviceSettings({
      profileId: preset.profile_id,
      orientation: preset.orientation,
      width: preset.viewport.width,
      height: preset.viewport.height,
      deviceScaleFactor: preset.device_scale_factor,
      userAgent: preset.user_agent,
      touchSimulation: preset.touch_simulation,
      networkProfileId: preset.network_profile_id,
    });
    return;
  }
  sendIfAttached({
    type: "deviceProfile",
    profileId,
    orientation,
  });
  setStatus("正在切换浏览器设备模拟…");
}

function changeDeviceSettings(settings) {
  sendIfAttached({ type: "deviceSettings", settings });
  setStatus("正在应用设备模拟设置…");
}

function requestDebugSnapshot() {
  if (!attached) {
    setStatus("请先连接浏览器，再读取调试面板", true);
    return;
  }
  sendIfAttached({ type: "debugSnapshot", panel: "all" });
  setStatus("正在刷新 Elements、Console、Network…");
}

function downloadClientScreenshot(data, mimeType = "image/png") {
  const binary = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([binary], { type: mimeType }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `browser-${browserId}-${Date.now()}.png`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
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
  getRemoteViewport: () => remoteViewport,
});
addBrowserToolLog("info", "工具已就绪");

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

deviceProfileSelect.addEventListener("change", () => {
  const selectedPreset = deviceProfileSelect.value.startsWith("preset:");
  changeDeviceProfile(
    deviceProfileSelect.value,
    selectedPreset ? currentDeviceOrientation : "portrait",
  );
});

function commitViewportInputs() {
  const width = Number(deviceWidthInput.value);
  const height = Number(deviceHeightInput.value);
  if (!Number.isInteger(width) || width <= 0 || width > 4096
    || !Number.isInteger(height) || height <= 0 || height > 4096) {
    setStatus("宽度和高度必须是 1 到 4096 的整数", true);
    return;
  }
  changeDeviceSettings({ width, height });
}

deviceWidthInput.addEventListener("change", commitViewportInputs);
deviceHeightInput.addEventListener("change", commitViewportInputs);
deviceWidthInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    commitViewportInputs();
  }
});
deviceHeightInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    commitViewportInputs();
  }
});

deviceDprSelect.addEventListener("change", () => {
  changeDeviceSettings({
    deviceScaleFactor: deviceDprSelect.value === "auto" ? null : Number(deviceDprSelect.value),
  });
});

deviceNetworkSelect.addEventListener("change", () => {
  changeDeviceSettings({ networkProfileId: deviceNetworkSelect.value });
});

deviceUaInput.addEventListener("change", () => {
  changeDeviceSettings({ userAgent: deviceUaInput.value.trim() || null });
});

deviceTouchInput.addEventListener("change", () => {
  changeDeviceSettings({ touchSimulation: deviceTouchInput.checked });
});

deviceFitButton.addEventListener("click", () => {
  fitViewportToWindow = !fitViewportToWindow;
  viewportPosition = null;
  deviceFitButton.setAttribute("aria-pressed", String(fitViewportToWindow));
  deviceFitButton.title = fitViewportToWindow ? "适应窗口" : "实际尺寸 (1:1)";
  layoutScreen();
});

deviceSavePresetButton.addEventListener("click", () => {
  const name = devicePresetName.value.trim();
  if (!name) {
    setStatus("请输入设备预设名称", true);
    devicePresetName.focus();
    return;
  }
  sendIfAttached({ type: "saveDevicePreset", name });
  devicePresetName.value = "";
  deviceSaveMenu.open = false;
  setStatus("正在保存设备预设…");
});

deviceScreenshotButton.addEventListener("click", () => {
  sendIfAttached({ type: "captureScreenshot" });
  deviceSettingsMenu.open = false;
  setStatus("正在生成截图…");
});

deviceResetButton.addEventListener("click", () => {
  changeDeviceSettings({ reset: true });
  deviceSettingsMenu.open = false;
});

deviceRotateButton.addEventListener("click", () => {
  const settings = {
    orientation: currentDeviceOrientation === "landscape" ? "portrait" : "landscape",
  };
  if (currentDeviceSnapshot?.device_emulation?.viewport_override) {
    settings.width = Number(deviceHeightInput.value);
    settings.height = Number(deviceWidthInput.value);
  }
  changeDeviceSettings(settings);
});

screenScroll.addEventListener("wheel", (event) => {
  if (!event.ctrlKey) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const direction = event.deltaY < 0 ? 1 : -1;
  viewportZoom = Math.max(0.25, Math.min(4, viewportZoom * (direction > 0 ? 1.1 : 0.9)));
  fitViewportToWindow = false;
  autoFitSuppressed = true;
  viewportPosition = null;
  deviceFitButton.setAttribute("aria-pressed", "false");
  deviceFitButton.title = `实际尺寸 · 缩放 ${Math.round(viewportZoom * 100)}%`;
  layoutScreen();
}, { capture: true, passive: false });

screenScroll.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || event.target === canvas || event.target.closest(".viewport-resize-handle")) {
    return;
  }
  const workspaceRect = screenScroll.getBoundingClientRect();
  const contentRect = screenContent.getBoundingClientRect();
  viewportPosition = {
    x: contentRect.left - workspaceRect.left + screenScroll.scrollLeft,
    y: contentRect.top - workspaceRect.top + screenScroll.scrollTop,
  };
  viewportDrag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: viewportPosition.x,
    originY: viewportPosition.y,
  };
  screenScroll.setPointerCapture(event.pointerId);
  screenContent.style.transform = "none";
  event.preventDefault();
});

screenScroll.addEventListener("pointermove", (event) => {
  if (!viewportDrag || viewportDrag.pointerId !== event.pointerId) return;
  viewportPosition = {
    x: viewportDrag.originX + event.clientX - viewportDrag.startX,
    y: viewportDrag.originY + event.clientY - viewportDrag.startY,
  };
  viewportPosition = clampViewportPosition(viewportPosition);
  layoutScreen();
});

function finishViewportDrag(event) {
  if (!viewportDrag || viewportDrag.pointerId !== event.pointerId) return;
  viewportDrag = null;
  if (screenScroll.hasPointerCapture(event.pointerId)) screenScroll.releasePointerCapture(event.pointerId);
  layoutScreen();
}

screenScroll.addEventListener("pointerup", finishViewportDrag);
screenScroll.addEventListener("pointercancel", finishViewportDrag);

for (const handle of viewportResizeHandles) {
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const contentRect = screenContent.getBoundingClientRect();
    const viewport = viewportForLayout();
    viewportResize = {
      pointerId: event.pointerId,
      axis: handle.dataset.resizeAxis || "xy",
      startX: event.clientX,
      startY: event.clientY,
      startWidth: viewport.width,
      startHeight: viewport.height,
      scaleX: contentRect.width / viewport.width,
      scaleY: contentRect.height / viewport.height,
    };
    viewportDraft = { ...viewport };
    handle.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  });
  handle.addEventListener("pointermove", (event) => {
    if (!viewportResize || viewportResize.pointerId !== event.pointerId) return;
    const next = { ...viewportDraft };
    if (viewportResize.axis.includes("x")) {
      next.width = Math.max(120, Math.min(4096,
        Math.round(viewportResize.startWidth + (event.clientX - viewportResize.startX) / viewportResize.scaleX)));
    }
    if (viewportResize.axis.includes("y")) {
      next.height = Math.max(120, Math.min(4096,
        Math.round(viewportResize.startHeight + (event.clientY - viewportResize.startY) / viewportResize.scaleY)));
    }
    viewportDraft = next;
    deviceWidthInput.value = String(next.width);
    deviceHeightInput.value = String(next.height);
    layoutScreen();
  });
  const finishResize = (event) => {
    if (!viewportResize || viewportResize.pointerId !== event.pointerId) return;
    const next = { ...viewportDraft };
    viewportResize = null;
    viewportDraft = null;
    if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
    layoutScreen();
    changeDeviceSettings({ width: next.width, height: next.height });
  };
  handle.addEventListener("pointerup", finishResize);
  handle.addEventListener("pointercancel", finishResize);
}

for (const tab of debugTabs) {
  tab.addEventListener("click", () => setDebugPanel(tab.dataset.debugTab));
}
debugRefresh.addEventListener("click", requestDebugSnapshot);
debugBannerClose.addEventListener("click", () => {
  debugBannerClose.closest(".debug-info-banner").hidden = true;
});
debugClose.addEventListener("click", () => {
  setDebugVisibility(false);
});
debugToggle.addEventListener("click", () => {
  setDebugVisibility(true);
  requestDebugSnapshot();
});
debugClear.addEventListener("click", () => {
  const panel = activeDebugPanel();
  if (panel === "elements") renderDebugElements(null);
  if (panel === "console") {
    renderDebugConsole([]);
    if (debugSnapshotData) {
      debugSnapshotData = { ...debugSnapshotData, console: [] };
    }
  }
  if (panel === "sources") renderDebugSources([]);
  if (panel === "network") {
    renderDebugNetwork([]);
    if (attached) {
      command("clearNetwork");
      setStatus("正在清空网络记录…");
    }
  }
  debugDrawerSummary.textContent = "当前面板已清空";
});
debugDrawerToggle.addEventListener("click", () => {
  setDebugPanel("console");
});
document.querySelector("#debug-add-panel").addEventListener("click", () => {
  setStatus("更多开发者工具面板暂未启用");
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
  isPicking: () => pickingMode !== null,
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
  onPickSelect: (point) => sendIfAttached({
    type: "selectElement",
    mode: pickingMode,
    ...point,
  }),
  onCancelPick: () => {
    if (pickingMode !== null) {
      setPickingElement(false);
      addBrowserToolLog("info", "已退出元素选择");
    }
  },
  onContextMenu: collaborationUi.showContextMenu,
  onBrowserShortcut: handleBrowserShortcut,
  sendIfAttached,
  getRemoteViewport: () => remoteViewport,
});

function toggleElementPicker(mode) {
  if (attached) {
    if (pickingMode === mode) {
      setPickingElement(false);
      addBrowserToolLog("info", "已退出元素选择");
    } else {
      setPickingElement(true, mode);
      addBrowserToolLog("info", mode === "rich" ? "开始选择元素并复制完整上下文" : "开始选择元素");
    }
    canvas.focus();
  }
}

elementPickerToggle.addEventListener("click", () => toggleElementPicker("basic"));
elementPickerAddToggle.addEventListener("click", () => toggleElementPicker("rich"));
copyBrowserLogsButton.addEventListener("click", () => {
  void copyBrowserLogs().catch((error) => {
    const message = `复制失败：${error instanceof Error ? error.message : String(error)}`;
    setStatus(message, true);
    addBrowserToolLog("error", message);
  });
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
