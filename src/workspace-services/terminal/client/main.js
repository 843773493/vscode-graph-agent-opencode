import { installTerminalUserActions } from "./terminalUserActions.js";
import { encodeTerminalClientMessage } from "./protocol/messages.js";

const params = new URLSearchParams(window.location.search);
const terminalId = params.get("terminalId");
const workspaceId = params.get("workspaceId");
const gatewayMode = Boolean(workspaceId);
const nativeWorkspaceId = window.BOXTEAM_WORKSPACE_ID;
const backendBaseUrl = workspaceId
  ? `${window.location.origin}/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/terminal-manager`
  : window.BOXTEAM_TERMINAL_BACKEND_URL || "http://127.0.0.1:8012";
const ownerApiBase = gatewayMode ? window.location.origin : window.BOXTEAM_WORKSPACE_BACKEND_URL;
let ownerResource = null;
let grantLoading = false;
let lastActionError = null;
let gatewayToken = null;
let backendRequestHeaders = {};
document.documentElement.classList.toggle("embedded-terminal", params.get("embedded") === "1");

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
  if (window.parent === window) {
    return fallback;
  }
  // TODO：跨域独立终端没有权限读取主窗口主题，保留终端自身的深色默认值。
  try {
    const value = window.parent.getComputedStyle(
      window.parent.document.documentElement,
    ).getPropertyValue(token).trim();
    return value || fallback;
  } catch {
    return fallback;
  }
}

const terminal = new window.Terminal({
  cursorBlink: true,
  convertEol: true,
  reflowRows: true,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  fontSize: 14,
  theme: {
    background: readHostThemeToken("--bt-terminal-background", "#050607"),
    foreground: readHostThemeToken("--bt-terminal-foreground", "#f8fafc"),
    cursor: readHostThemeToken("--bt-accent", "#93c5fd"),
    selectionBackground: readHostThemeToken(
      "--bt-selection-background",
      "#334155",
    ),
  },
});
const fitAddon = new window.FitAddon.FitAddon();
const searchAddon = new window.SearchAddon.SearchAddon();
terminal.loadAddon(fitAddon);
terminal.loadAddon(searchAddon);
terminal.open(terminalContainer);
fitAddon.fit();

let socket = null;
let attached = false;
let deleted = false;
let snapshotDisplay = "";
let currentTerminalStatus = null;
let statusPollTimer = null;
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

function backendWsUrl() {
  const url = new URL(backendBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/$/, "")}/terminal`;
  url.search = gatewayMode
    ? `?token=${encodeURIComponent(gatewayToken)}`
    : `?workspace_id=${encodeURIComponent(nativeWorkspaceId)}`;
  return url.toString();
}

async function initializeGatewayAuth() {
  if (!gatewayMode) {
    if (typeof nativeWorkspaceId !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(nativeWorkspaceId)) {
      throw new Error("终端前端缺少受控工作区 UUID");
    }
    if (typeof ownerApiBase !== "string" || !ownerApiBase || !window.BOXTEAM_WORKSPACE_API_TOKEN) {
      throw new Error("独立终端必须显式配置工作区 owner API 地址与凭据，或从 Gateway 打开终端");
    }
    backendRequestHeaders = {
      "X-BoxTeam-Workspace-Id": nativeWorkspaceId,
      "X-Local-Token": window.BOXTEAM_WORKSPACE_API_TOKEN,
    };
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
  backendRequestHeaders = { "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId };
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

function sanitizeTerminalDisplay(value) {
  const parts = String(value || "").split(/(\r\n|\n)/);
  let display = "";
  for (let index = 0; index < parts.length; index += 2) {
    const line = parts[index] || "";
    const separator = parts[index + 1] || "";
    if (
      line.includes("__BOXTEAM_CMD_START_") ||
      line.includes("__BOXTEAM_CMD_DONE_")
    ) {
      continue;
    }
    display += line + separator;
  }
  return display;
}

function snapshotDisplayBuffer(snapshot) {
  return snapshot.display_buffer ?? sanitizeTerminalDisplay(snapshot.buffer || "");
}

function renderSnapshot(display) {
  snapshotDisplay = display;
  terminal.clear();
  terminal.write(display);
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
  if (resizeFrame !== null) {
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
  if (!terminalId || deleted) return;
  const snapshot = acceptOwnerResource(await ownerRequest());
  if (snapshot.status !== "running") {
    desiredAttached = false;
    attached = false;
    socket?.close();
    socket = null;
    setAttachButtonMode("detached");
    renderSnapshot(snapshotDisplayBuffer(snapshot));
  }
  if (!lastActionError && !attached) setStatus(describeSnapshot(snapshot));
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

function startStatusPolling() {
  if (statusPollTimer !== null) {
    return;
  }
  statusPollTimer = window.setInterval(() => {
    void syncTerminalState().catch((error) => {
      setStatus(error instanceof Error ? error.message : String(error), true);
    });
  }, 2000);
}

function detach() {
  desiredAttached = false;
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
    await loadSnapshot();
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
  if (!desiredAttached) return;
  const currentSocket = new WebSocket(backendWsUrl());
  socket = currentSocket;
  setAttachButtonMode("attaching");
  setStatus("正在连接终端...");

  socket.addEventListener("open", () => {
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
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      attached = true;
      reconnectAttempts = 0;
      currentTerminalStatus = message.snapshot?.status || currentTerminalStatus;
      setAttachButtonMode("attached");
      if (message.replayMode !== "incremental") {
        terminal.clear();
        terminal.write(
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
      terminal.write(sanitizeTerminalDisplay(message.data), () => {
        acknowledgeOutput(message.sequence);
      });
      return;
    }
    if (message.type === "resync") {
      terminal.clear();
      terminal.write(snapshotDisplayBuffer(message.snapshot), () => {
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

installTerminalUserActions({
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

// 扩展窗口通过父页面调整 iframe 尺寸时，终端窗口自身不会收到 window.resize。
// 监听终端容器尺寸，避免终端仍沿用打开瞬间的窄列数，导致路径逐段折行。
const terminalResizeObserver = new ResizeObserver(() => resizeRemote());
terminalResizeObserver.observe(terminalContainer);
window.addEventListener("resize", resizeRemote);
window.addEventListener("beforeunload", () => {
  if (resizeFrame !== null) {
    window.cancelAnimationFrame(resizeFrame);
  }
  if (statusPollTimer !== null) {
    window.clearInterval(statusPollTimer);
  }
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer);
  }
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
