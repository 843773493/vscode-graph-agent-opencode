import { stripCommandMarkerLines } from "./shared/commandText.js";
import { installTerminalUserActions } from "./terminalUserActions.js";
import { encodeTerminalClientMessage } from "./protocol/messages.js";
import { consumeSseResponse, decodeJsonSseData, defineSseEvent } from "./shared/sse.js";
import { openRealtimeResponse } from "./shared/realtime.js";
import { validateResourceChanges } from "./shared/sseRuntime.js";

const params = new URLSearchParams(window.location.search);
const terminalId = params.get("terminalId");
const workspaceId = params.get("workspaceId");
const targetGatewayId = params.get("target_gateway_id");
const ownerApiBase = window.location.origin;
let ownerResource = null;
let grantLoading = false;
let resumeLoading = false;
let lastActionError = null;
let gatewayToken = null;
let backendRequestHeaders = {};
const embedded = params.get("embedded") === "1";

const terminalIdElement = document.querySelector("#terminal-id");
const statusLine = document.querySelector("#status-line");
const attachToggle = document.querySelector("#attach-toggle");
const searchTerminalButton = document.querySelector("#search-terminal");
const refreshSnapshotButton = document.querySelector("#refresh-snapshot");
const terminateButton = document.querySelector("#terminate-terminal");
const deleteButton = document.querySelector("#delete-terminal");
const attachToggleLabel = attachToggle.querySelector(".sr-only");
const terminalContainer = document.querySelector("#terminal");
const terminalSearchBar = document.querySelector("#terminal-search-bar");
const terminalSearchInput = document.querySelector("#terminal-search-input");
const terminalSearchResult = document.querySelector("#terminal-search-result");
const terminalSearchPrevious = document.querySelector("#terminal-search-previous");
const terminalSearchNext = document.querySelector("#terminal-search-next");
const terminalSearchClose = document.querySelector("#terminal-search-close");

function readHostThemeToken(token, fallback) {
  if (window.parent === window) return fallback;
  // 独立跨域嵌入不能读取宿主样式，使用终端自身的初始配色。
  try {
    return window.parent.getComputedStyle(
      window.parent.document.querySelector(".monaco-workbench") ?? window.parent.document.documentElement,
    ).getPropertyValue(token).trim() || fallback;
  } catch {
    return fallback;
  }
}

// 首帧继承宿主样式；后续配色由 Workbench 的主题变更通知完整替换。
const terminalTheme = {
  background: readHostThemeToken("--vscode-terminal-background", readHostThemeToken("--vscode-panel-background", "#050607")),
  foreground: readHostThemeToken("--vscode-terminal-foreground", readHostThemeToken("--vscode-foreground", "#f8fafc")),
  cursor: readHostThemeToken("--vscode-terminalCursor-foreground", readHostThemeToken("--vscode-foreground", "#93c5fd")),
  selectionBackground: readHostThemeToken("--vscode-terminal-selectionBackground", "#334155"),
};
document.documentElement.style.setProperty("--terminal-background", terminalTheme.background);

const terminal = new window.Terminal({
  // SearchAddon 的匹配装饰需要 xterm 的 marker／decoration API。
  allowProposedApi: true,
  cursorBlink: true,
  convertEol: true,
  reflowRows: true,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  fontSize: 14,
  theme: terminalTheme,
});
const fitAddon = new window.FitAddon.FitAddon();
const searchAddon = new window.SearchAddon.SearchAddon();
terminal.loadAddon(fitAddon);
terminal.loadAddon(searchAddon);
terminal.open(terminalContainer);
fitAddon.fit();

let socket = null;
let attached = false;
let intersecting = false;
let visible = false;
let deleted = false;
let snapshotDisplay = "";
let currentTerminalStatus = null;
let statusObservation = null;
let statusRetryTimer = null;
let statusSyncRetryTimer = null;
let statusSyncPending = null;
let statusDirty = false;
let resizeFrame = null;
let lastSentCols = null;
let lastSentRows = null;
let desiredAttached = false;
let reconnectTimer = null;
let reconnectAttempts = 0;
const sequenceStorageKey = `boxteam-terminal-sequence:${terminalId || "missing"}`;
let lastSequence = Number(window.sessionStorage.getItem(sequenceStorageKey) || 0);

function setAttachButtonMode(mode) {
  const labels = {
    detached: "连接终端",
    attaching: "正在连接终端",
    attached: "断开终端",
  };
  const label = labels[mode] || labels.detached;
  attachToggle.classList.toggle("is-attached", mode === "attached");
  attachToggle.title = label;
  attachToggle.setAttribute("aria-label", label);
  if (attachToggleLabel) {
    attachToggleLabel.textContent = label;
  }
}

function backendWsUrl(service) {
  if (!service || service.workspace_id !== workspaceId || service.gateway_id !== targetGatewayId
    || !service.package_id || !service.plugin_id || !service.package_version || !service.instance_id
    || service.service_id !== "socket") {
    throw new Error("Terminal 传输授权缺少原目标的精确插件服务身份");
  }
  const parts = [service.package_id, service.plugin_id].map(encodeURIComponent);
  const url = new URL(`/api/gateway/service-plugins/${parts.join("/")}/instances/${encodeURIComponent(service.instance_id)}/services/socket/terminal`, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", gatewayToken);
  url.searchParams.set("workspace_id", workspaceId);
  url.searchParams.set("target_gateway_id", service.gateway_id);
  url.searchParams.set("package_version", service.package_version);
  return url.toString();
}

async function initializeGatewayAuth() {
  if (!workspaceId || !targetGatewayId) throw new Error("请从明确 Gateway 和工作区的终端资源入口打开");
  const response = await fetch(`${window.location.origin}/api/gateway/auth/local-credential`);
  if (!response.ok) throw new Error(`获取 Gateway 本地凭据失败: ${response.status}`);
  const payload = await response.json();
  const token = payload.data?.token;
  if (typeof token !== "string" || !token) throw new Error("Gateway 本地凭据响应缺少 token");
  gatewayToken = token;
  backendRequestHeaders = {
    "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId,
    "X-BoxTeam-Target-Gateway-Id": targetGatewayId,
  };
}

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", error);
  statusLine.hidden = !error;
}

function statusLabel(status) {
  const labels = {
    running: "运行中",
    terminated: "已终止",
    deleted: "已删除",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    lost: "已断开",
    exited: "已退出",
    created: "已创建",
  };
  return labels[status] || status || "未知";
}

function commandStatusLabel(status, exitCode) {
  if (!status) {
    return "无";
  }
  const suffix = exitCode === null || exitCode === undefined ? "" : `，退出码 ${exitCode}`;
  return `${statusLabel(status)}${suffix}`;
}

function updateControls() {
  const terminalRunning = currentTerminalStatus === "running";
  attachToggle.disabled = deleted || !terminalRunning;
  refreshSnapshotButton.disabled = deleted;
  terminateButton.disabled = deleted || !terminalRunning;
  deleteButton.disabled = deleted;
  terminalContainer.classList.toggle("is-disabled", deleted || !terminalRunning);
  terminal.options.disableStdin = !attached || deleted || !terminalRunning;
}

function updateTerminalTitle(snapshot) {
  terminalIdElement.textContent = `${snapshot.terminal_id} · ${statusLabel(snapshot.status)}`;
}

function markDeleted(message = "终端已删除或不存在", snapshot = null) {
  deleted = true;
  stopStatusObservation();
  desiredAttached = false;
  attached = false;
  currentTerminalStatus = "deleted";
  socket?.close();
  socket = null;
  setAttachButtonMode("detached");
  if (terminalId) {
    terminalIdElement.textContent = `${terminalId} · ${statusLabel("deleted")}`;
  }
  if (snapshot) {
    renderSnapshot(snapshotDisplayBuffer(snapshot));
  }
  setStatus(message);
  updateControls();
}

function describeSnapshot(snapshot) {
  const commandStatus = `最近命令: ${commandStatusLabel(
    snapshot.last_command_status,
    snapshot.last_command_exit_code,
  )}`;
  // 快照只承载工作区内相对目录；绝对真实路径不再下发到客户端（裁定 D-A2/D-A3）。
  return `终端: ${statusLabel(snapshot.status)} · ${commandStatus} · cwd: ${snapshot.cwd_relative}`;
}

function snapshotDisplayBuffer(snapshot) {
  return snapshot.display_buffer ?? stripCommandMarkerLines(String(snapshot.buffer || ""));
}

function renderSnapshot(display, onRendered) {
  snapshotDisplay = display;
  // clear() 会保留当前行和终端模式；RIS 与快照一起排队，避免旧输出污染重放。
  terminal.write(`\x1bc${display}`, onRendered);
}

function rememberSequence(sequence) {
  if (!Number.isInteger(sequence) || sequence < lastSequence) {
    return;
  }
  lastSequence = sequence;
  window.sessionStorage.setItem(sequenceStorageKey, String(sequence));
}

function acknowledgeOutput(sequence) {
  rememberSequence(sequence);
  if (socket?.readyState === WebSocket.OPEN) {
    send({ type: "ack", sequence });
  }
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 尚未连接");
  }
  socket.send(encodeTerminalClientMessage(message));
}

function resizeRemote() {
  if (!visible || resizeFrame !== null) {
    return;
  }
  resizeFrame = window.requestAnimationFrame(() => {
    resizeFrame = null;
    fitAddon.fit();
    const terminalElement = terminal.element;
    const terminalParent = terminalElement?.parentElement;
    const measureElement = terminalElement?.querySelector(
      ".xterm-char-measure-element",
    );
    const measureTextLength = measureElement?.textContent?.length || 0;
    const cellWidth = measureTextLength > 0
      ? measureElement.getBoundingClientRect().width / measureTextLength
      : 0;
    const cellHeight = measureElement?.getBoundingClientRect().height || 0;
    if (terminalParent && cellWidth > 0 && cellHeight > 0) {
      const parentStyle = window.getComputedStyle(terminalParent);
      const paddingWidth =
        parseFloat(parentStyle.paddingLeft) + parseFloat(parentStyle.paddingRight);
      const paddingHeight =
        parseFloat(parentStyle.paddingTop) + parseFloat(parentStyle.paddingBottom);
      const availableWidth = terminalParent.clientWidth - paddingWidth;
      const availableHeight = terminalParent.clientHeight - paddingHeight;
      const cols = Math.max(2, Math.floor(availableWidth / cellWidth));
      const rows = Math.max(1, Math.floor(availableHeight / cellHeight));
      if (terminal.cols !== cols || terminal.rows !== rows) {
        terminal.resize(cols, rows);
        if (deleted && snapshotDisplay) {
          renderSnapshot(snapshotDisplay);
        }
      }
    }
    if (!attached || socket?.readyState !== WebSocket.OPEN) {
      return;
    }
    if (terminal.cols === lastSentCols && terminal.rows === lastSentRows) {
      return;
    }
    send({
      type: "resize",
      cols: terminal.cols,
      rows: terminal.rows,
    });
    lastSentCols = terminal.cols;
    lastSentRows = terminal.rows;
  });
}

async function ownerRequest(suffix = "", body = undefined) {
  const response = await fetch(`${ownerApiBase}/api/v1/resources/${encodeURIComponent(terminalId)}${suffix}`, {
    method: body === undefined ? "GET" : "POST",
    cache: "no-store",
    headers: { ...backendRequestHeaders, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(`终端 owner 请求失败 ${response.status}: ${JSON.stringify(payload.detail ?? payload.error ?? payload)}`);
  if (!payload.data) throw new Error("终端 owner 响应缺少完整 data");
  return payload.data;
}

function acceptOwnerResource(resource) {
  if (resource.resource_id !== terminalId || !resource.source_thread || !Number.isInteger(resource.owner_revision)) {
    throw new Error("Terminal owner 完整投影格式错误");
  }
  ownerResource = resource;
  const snapshot = { ...resource.metadata, ...resource, terminal_id: resource.resource_id };
  currentTerminalStatus = snapshot.status;
  updateTerminalTitle(snapshot);
  updateControls();
  if (resource.status === "deleted") markDeleted("终端已删除", snapshot);
  return snapshot;
}

async function loadSnapshot() {
  if (!terminalId) throw new Error("URL 缺少 terminalId 参数");
  const snapshot = acceptOwnerResource(await ownerRequest());
  rememberSequence(snapshot.sequence || 0);
  renderSnapshot(snapshotDisplayBuffer(snapshot));
  if (!lastActionError) setStatus(describeSnapshot(snapshot));
  return snapshot;
}

async function syncTerminalState() {
  if (!terminalId || deleted || !visible) return;
  const resource = await ownerRequest("?include_output=false");
  if (!visible) return;
  // 终态只在转换时补一份最终正文，后续状态检查不重复搬运输出。
  const snapshot = resource.status !== "running" && resource.status !== currentTerminalStatus
    ? await loadSnapshot() : acceptOwnerResource(resource);
  if (snapshot.status !== "running") {
    desiredAttached = false;
    attached = false;
    socket?.close();
    socket = null;
    setAttachButtonMode("detached");
  }
  if (!lastActionError) setStatus(describeSnapshot(snapshot));
}

async function ownerCommand(action) {
  try {
    const resource = await ownerRequest(`/${action}`, { expected_revision: ownerResource.owner_revision });
    lastActionError = null;
    const snapshot = acceptOwnerResource(resource);
    renderSnapshot(snapshotDisplayBuffer(snapshot));
    setStatus(describeSnapshot(snapshot));
  } catch (error) {
    let message = error instanceof Error ? error.message : String(error);
    try { acceptOwnerResource(await ownerRequest()); }
    catch (refreshError) { message += `；刷新失败: ${refreshError instanceof Error ? refreshError.message : String(refreshError)}`; }
    lastActionError = message;
    setStatus(message, true);
  }
}

function requestStatusSync(attempt = 0) {
  window.clearTimeout(statusSyncRetryTimer);
  statusDirty = true;
  return statusSyncPending ??= (async () => {
    while (statusDirty && visible && !deleted) {
      statusDirty = false;
      await syncTerminalState();
    }
  })().catch(error => {
    if (visible && !deleted && attempt < 2) {
      statusSyncRetryTimer = window.setTimeout(() => {
        void requestStatusSync(attempt + 1).catch(cause => setStatus(cause.message, true));
      }, 1000 * 2 ** attempt);
    }
    throw error;
  }).finally(() => { statusSyncPending = null; });
}

function stopStatusObservation() {
  statusObservation?.abort();
  statusObservation = null;
  if (statusRetryTimer !== null) window.clearTimeout(statusRetryTimer);
  statusRetryTimer = null;
  window.clearTimeout(statusSyncRetryTimer);
}

function observeHostResources(controller) {
  return new Promise(resolve => {
    const subscriptionId = crypto.randomUUID();
    const send = type => window.parent.postMessage({ type, terminalId, subscriptionId }, window.location.origin);
    const receive = event => {
      if (event.origin !== window.location.origin || event.source !== window.parent
        || event.data?.type !== "boxteam.terminal.resources" || event.data.terminalId !== terminalId
        || event.data.subscriptionId !== subscriptionId) return;
      if (typeof event.data.error === "string") setStatus(event.data.error, true);
      else void requestStatusSync().catch(error => setStatus(error.message, true));
    };
    window.addEventListener("message", receive);
    controller.signal.addEventListener("abort", () => {
      window.removeEventListener("message", receive);
      send("boxteam.terminal.unobserve");
      resolve();
    }, { once: true });
    send("boxteam.terminal.observe");
  });
}

async function startStatusObservation() {
  if (statusObservation || !visible || deleted) return;
  const controller = new AbortController();
  statusObservation = controller;
  try {
    if (embedded && window.parent !== window) {
      await observeHostResources(controller);
      return;
    }
    const url = new URL('/api/gateway/realtime', ownerApiBase);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    const response = await openRealtimeResponse(url.href, gatewayToken, {
      path: '/api/v1/resources/events', headers: {
        'X-BoxTeam-Workspace-ID': workspaceId, 'X-BoxTeam-Target-Gateway-ID': targetGatewayId,
      },
    }, controller.signal);
    if (!response.ok) throw new Error(`终端状态订阅失败: HTTP ${response.status}`);
    await consumeSseResponse(response, {
      signal: controller.signal, idleTimeoutMs: 45_000,
      events: { resources_changed: defineSseEvent(decodeJsonSseData, value => {
        const resourceIds = validateResourceChanges(value);
        if (resourceIds === null || resourceIds.includes(terminalId)) {
          void requestStatusSync().catch(error => setStatus(error.message, true));
        }
      }) },
    });
    if (!controller.signal.aborted) throw new Error("终端状态订阅已断开");
  } catch (error) {
    if (!controller.signal.aborted) {
      setStatus(error instanceof Error ? error.message : String(error), true);
      statusRetryTimer = window.setTimeout(() => {
        statusRetryTimer = null;
        void startStatusObservation();
      }, 5000);
    }
  } finally {
    if (statusObservation === controller) statusObservation = null;
  }
}

function detach({ preserveIntent = false } = {}) {
  if (!preserveIntent) desiredAttached = false;
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    send({ type: "detach", terminalId });
  }
  socket?.close();
  socket = null;
  attached = false;
  lastSentCols = null;
  lastSentRows = null;
  setAttachButtonMode("detached");
  setStatus("已 detach，终端仍在后台运行");
  updateControls();
}

async function attach() {
  if (!visible) return;
  if (!terminalId) {
    setStatus("URL 缺少 terminalId 参数", true);
    return;
  }
  if (attached) {
    return;
  }
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    setStatus("正在连接终端...");
    return;
  }
  if (grantLoading) return;
  desiredAttached = true;
  grantLoading = true;
  let grant;
  try {
    acceptOwnerResource(await ownerRequest("?include_output=false"));
    const querySession = params.get("sessionId");
    const queryThread = params.get("threadId");
    if (Boolean(querySession) !== Boolean(queryThread)) throw new Error("Terminal 传输 Thread 参数不完整");
    if (ownerResource.retention_scope === "thread" && !queryThread) {
      throw new Error("临时终端连接缺少明确的 Thread；请从会话资源或工作区终端面板打开");
    }
    const thread = querySession ? { session_id: querySession, thread_id: queryThread } : undefined;
    grant = await ownerRequest("/transport-grant", { expected_revision: ownerResource.owner_revision, thread });
    acceptOwnerResource(grant.resource);
    lastActionError = null;
  } catch (error) {
    lastActionError = error instanceof Error ? error.message : String(error);
    try { acceptOwnerResource(await ownerRequest()); } catch (refreshError) { lastActionError += `；刷新失败: ${refreshError.message}`; }
    setStatus(lastActionError, true);
    setAttachButtonMode("detached");
    desiredAttached = false;
    return;
  } finally { grantLoading = false; }
  if (!desiredAttached || !visible) return;
  const currentSocket = new WebSocket(backendWsUrl(grant.service));
  socket = currentSocket;
  setAttachButtonMode("attaching");
  setStatus("正在连接终端...");

  socket.addEventListener("open", () => {
    if (socket !== currentSocket || !visible) { currentSocket.close(); return; }
    fitAddon.fit();
    send({
      type: "attach",
      terminalId,
      grantToken: grant.grant_token,
      cols: terminal.cols,
      rows: terminal.rows,
      afterSequence: lastSequence,
    });
    lastSentCols = terminal.cols;
    lastSentRows = terminal.rows;
  });

  socket.addEventListener("message", (event) => {
    if (socket !== currentSocket || !visible) return;
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      attached = true;
      reconnectAttempts = 0;
      currentTerminalStatus = message.snapshot?.status || currentTerminalStatus;
      setAttachButtonMode("attached");
      if (message.replayMode !== "incremental") {
        renderSnapshot(
          message.snapshot ? snapshotDisplayBuffer(message.snapshot) : "",
          () => acknowledgeOutput(message.snapshot?.sequence || 0),
        );
      }
      setStatus(message.snapshot ? `已 attach · ${describeSnapshot(message.snapshot)}` : "已 attach");
      if (currentTerminalStatus !== "running") {
        attached = false;
        socket?.close();
        setAttachButtonMode("detached");
      }
      updateControls();
      return;
    }
    if (message.type === "detached") {
      attached = false;
      setAttachButtonMode("detached");
      setStatus("已 detach，终端仍在后台运行");
      updateControls();
      return;
    }
    if (message.type === "output") {
      if (message.sequence <= lastSequence) {
        acknowledgeOutput(message.sequence);
        return;
      }
      terminal.write(stripCommandMarkerLines(String(message.data || "")), () => {
        acknowledgeOutput(message.sequence);
      });
      return;
    }
    if (message.type === "resync") {
      renderSnapshot(snapshotDisplayBuffer(message.snapshot), () => {
        acknowledgeOutput(message.snapshot.sequence || 0);
      });
      return;
    }
    if (message.type === "exit") {
      desiredAttached = false;
      currentTerminalStatus = "terminated";
      setStatus(`终端已退出: ${message.exitCode ?? ""} ${message.signal ?? ""}`);
      updateControls();
      return;
    }
    if (message.type === "deleted") {
      markDeleted("终端已删除", message.snapshot ?? null);
      return;
    }
    if (message.type === "error") {
      setStatus(message.message, true);
    }
  });

  currentSocket.addEventListener("close", (event) => {
    if (socket !== currentSocket) {
      return;
    }
    attached = false;
    socket = null;
    lastSentCols = null;
    lastSentRows = null;
    setAttachButtonMode("detached");
    if (event.code === 4009) {
      desiredAttached = false;
      lastActionError = "终端资源关联或版本已变化，请刷新后重新连接";
      setStatus(lastActionError, true);
      void syncTerminalState().catch((error) => setStatus(error.message, true));
    }
    if (desiredAttached && !deleted && currentTerminalStatus === "running") {
      reconnectAttempts += 1;
      const delay = Math.min(500 * 2 ** (reconnectAttempts - 1), 5000);
      setStatus(`连接已断开，${delay}ms 后自动重连...`);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        attach();
      }, delay);
    }
    updateControls();
  });

  currentSocket.addEventListener("error", () => {
    setStatus("WebSocket 连接失败", true);
  });
}

terminal.onData((data) => {
  if (!attached) {
    return;
  }
  send({ type: "input", data });
});

const userActions = installTerminalUserActions({
  terminal,
  searchAddon,
  elements: {
    searchButton: searchTerminalButton,
    searchBar: terminalSearchBar,
    searchInput: terminalSearchInput,
    searchResult: terminalSearchResult,
    searchPrevious: terminalSearchPrevious,
    searchNext: terminalSearchNext,
    searchClose: terminalSearchClose,
  },
  getAttached: () => attached,
  setStatus,
  resizeTerminal: resizeRemote,
});

// 嵌入时由工作台的终端行菜单发起；沿用本页唯一的连接、搜索与快照逻辑。
window.addEventListener("message", event => {
  if (!embedded || event.source !== window.parent || event.origin !== window.location.origin
    || event.data?.terminalId !== terminalId) return;
  if (event.data.type === "boxteam.terminal.theme") {
    terminal.options.theme = event.data.theme;
    document.documentElement.style.setProperty("--terminal-background", event.data.theme.background);
    return;
  }
  if (event.data.type !== "boxteam.terminal.action") return;
  switch (event.data.action) {
    case "connect": void attach(); break;
    case "disconnect": detach(); break;
    case "search": userActions.openSearch(); break;
    case "refresh": void loadSnapshot().catch(error => setStatus(error.message, true)); break;
  }
});

async function resumeVisibleTerminal() {
  if (!visible || !gatewayToken || resumeLoading) return;
  // 首次正文尚未返回时切换工作区，仍由原恢复取得快照并检查最新可见性。
  resumeLoading = true;
  try {
    if (ownerResource === null) {
      const snapshot = await loadSnapshot();
      desiredAttached = snapshot.status === "running";
    } else {
      await syncTerminalState();
    }
    if (!visible) return;
    resizeRemote();
    if (desiredAttached) void attach();
    void startStatusObservation();
  } finally {
    resumeLoading = false;
  }
}

function updateVisibility() {
  const next = intersecting && !document.hidden;
  if (visible === next) return;
  visible = next;
  if (visible) {
    void resumeVisibleTerminal().catch(error => setStatus(error.message, true));
  } else {
    stopStatusObservation();
    detach({ preserveIntent: true });
  }
}

// 原生观察会穿过iframe祖先的hidden/display:none，覆盖工作区和面板切换。
const visibilityObserver = new IntersectionObserver(([entry]) => {
  intersecting = entry.isIntersecting;
  updateVisibility();
});
visibilityObserver.observe(terminalContainer);
document.addEventListener("visibilitychange", updateVisibility);

// 扩展窗口通过父页面调整 iframe 尺寸时，终端窗口自身不会收到 window.resize。
// 监听终端容器尺寸，避免终端仍沿用打开瞬间的窄列数，导致路径逐段折行。
const terminalResizeObserver = new ResizeObserver(() => resizeRemote());
terminalResizeObserver.observe(terminalContainer);
window.addEventListener("resize", resizeRemote);
window.addEventListener("beforeunload", () => {
  if (resizeFrame !== null) {
    window.cancelAnimationFrame(resizeFrame);
  }
  stopStatusObservation();
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
  }
  visibilityObserver.disconnect();
  document.removeEventListener("visibilitychange", updateVisibility);
  terminalResizeObserver.disconnect();
});

setAttachButtonMode("detached");

attachToggle.addEventListener("click", () => {
  if (attached) {
    detach();
  } else {
    attach();
  }
});

refreshSnapshotButton.addEventListener("click", () => {
  void loadSnapshot().catch((error) => {
    setStatus(error instanceof Error ? error.message : String(error), true);
  });
});

terminateButton.addEventListener("click", async () => {
  if (!terminalId) {
    setStatus("URL 缺少 terminalId 参数", true);
    return;
  }
  await ownerCommand("stop");
});

deleteButton.addEventListener("click", async () => {
  if (!terminalId) {
    setStatus("URL 缺少 terminalId 参数", true);
    return;
  }
  if (!window.confirm(`确认删除终端 ${terminalId}？删除后不可再 attach。`)) {
    return;
  }
  detach();
  await ownerCommand("delete");
});

void initializeGatewayAuth()
  .then(() => {
    // HTML 加载不代表模块及 xterm 初始化成功，只有本页真正可用后才通知宿主。
    if (embedded) window.parent.postMessage({ type: "boxteam.terminal.ready", terminalId }, window.location.origin);
    return resumeVisibleTerminal();
  })
  .catch((error) => {
    setStatus(error instanceof Error ? error.message : String(error), true);
    updateControls();
  });
