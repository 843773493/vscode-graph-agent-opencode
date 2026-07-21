const params = new URLSearchParams(window.location.search);
const terminalId = params.get("terminalId");
const workspaceId = params.get("workspaceId");
const gatewayMode = Boolean(workspaceId);
const backendBaseUrl = workspaceId
  ? `${window.location.origin}/api/gateway/workspaces/${encodeURIComponent(workspaceId)}/terminal-manager`
  : window.BOXTEAM_TERMINAL_BACKEND_URL || "http://127.0.0.1:8012";
const backendRequestHeaders = gatewayMode
  ? { "X-Local-Token": "local-dev-token" }
  : {};
document.documentElement.classList.toggle("embedded-terminal", params.get("embedded") === "1");

const terminalIdElement = document.querySelector("#terminal-id");
const statusLine = document.querySelector("#status-line");
const attachToggle = document.querySelector("#attach-toggle");
const refreshSnapshotButton = document.querySelector("#refresh-snapshot");
const terminateButton = document.querySelector("#terminate-terminal");
const deleteButton = document.querySelector("#delete-terminal");
const attachToggleLabel = attachToggle.querySelector(".sr-only");
const terminalContainer = document.querySelector("#terminal");
const agentForm = document.querySelector("#agent-form");
const agentInput = document.querySelector("#agent-input");
const agentSubmitButton = agentForm.querySelector("button");

const terminal = new window.Terminal({
  cursorBlink: true,
  convertEol: true,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  fontSize: 14,
  theme: {
    background: "#050607",
    foreground: "#f8fafc",
    cursor: "#93c5fd",
    selectionBackground: "#334155",
  },
});
const fitAddon = new window.FitAddon.FitAddon();
terminal.loadAddon(fitAddon);
terminal.open(terminalContainer);
fitAddon.fit();

let socket = null;
let attached = false;
let deleted = false;
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
  url.search = gatewayMode ? "?token=local-dev-token" : "";
  return url.toString();
}

function setStatus(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", error);
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
  const hasInput = Boolean(agentInput.value.trim());
  attachToggle.disabled = deleted || !terminalRunning;
  refreshSnapshotButton.disabled = deleted;
  terminateButton.disabled = deleted || !terminalRunning;
  deleteButton.disabled = deleted;
  agentInput.disabled = !attached || deleted || !terminalRunning;
  agentSubmitButton.disabled = !attached || deleted || !terminalRunning || !hasInput;
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
    terminal.clear();
    terminal.write(snapshotDisplayBuffer(snapshot));
  }
  setStatus(message);
  updateControls();
}

function describeSnapshot(snapshot) {
  const commandStatus = `最近命令: ${commandStatusLabel(
    snapshot.last_command_status,
    snapshot.last_command_exit_code,
  )}`;
  return `终端: ${statusLabel(snapshot.status)} · ${commandStatus} · cwd: ${snapshot.cwd}`;
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
  socket.send(JSON.stringify(message));
}

function resizeRemote() {
  if (resizeFrame !== null) {
    return;
  }
  resizeFrame = window.requestAnimationFrame(() => {
    resizeFrame = null;
    fitAddon.fit();
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

async function loadSnapshot() {
  if (!terminalId) {
    setStatus("URL 缺少 terminalId 参数", true);
    return;
  }
  const response = await fetch(
    `${backendBaseUrl}/api/terminals/${encodeURIComponent(terminalId)}?missing_as_deleted=1`,
    { headers: backendRequestHeaders },
  );
  if (response.status === 404) {
    markDeleted("终端已删除或不存在");
    return;
  }
  if (!response.ok) {
    throw new Error(`读取终端状态失败: ${response.status}`);
  }
  const payload = await response.json();
  const snapshot = payload.data;
  if (snapshot.status === "deleted") {
    markDeleted("终端已删除", snapshot);
    return;
  }
  currentTerminalStatus = snapshot.status;
  rememberSequence(snapshot.sequence || 0);
  updateTerminalTitle(snapshot);
  setStatus(describeSnapshot(snapshot));
  terminal.clear();
  terminal.write(snapshotDisplayBuffer(snapshot));
  updateControls();
  return snapshot;
}

async function syncTerminalState() {
  if (!terminalId || deleted) {
    return;
  }
  const response = await fetch(
    `${backendBaseUrl}/api/terminals/${encodeURIComponent(terminalId)}?missing_as_deleted=1`,
    { cache: "no-store", headers: backendRequestHeaders },
  );
  if (response.status === 404) {
    markDeleted("终端已删除或不存在");
    return;
  }
  if (!response.ok) {
    throw new Error(`同步终端状态失败: ${response.status}`);
  }
  const payload = await response.json();
  const snapshot = payload.data;
  if (snapshot.status === "deleted") {
    markDeleted("终端已删除", snapshot);
    return;
  }

  currentTerminalStatus = snapshot.status;
  updateTerminalTitle(snapshot);
  if (snapshot.status !== "running") {
    desiredAttached = false;
    attached = false;
    socket?.close();
    socket = null;
    setAttachButtonMode("detached");
    terminal.clear();
    terminal.write(snapshotDisplayBuffer(snapshot));
    setStatus(describeSnapshot(snapshot));
  } else if (!attached) {
    setStatus(describeSnapshot(snapshot));
  }
  updateControls();
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

function attach() {
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
  desiredAttached = true;
  const currentSocket = new WebSocket(backendWsUrl());
  socket = currentSocket;
  setAttachButtonMode("attaching");
  setStatus("正在连接终端...");

  socket.addEventListener("open", () => {
    fitAddon.fit();
    send({
      type: "attach",
      terminalId,
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

  currentSocket.addEventListener("close", () => {
    if (socket !== currentSocket) {
      return;
    }
    attached = false;
    socket = null;
    lastSentCols = null;
    lastSentRows = null;
    setAttachButtonMode("detached");
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

agentForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = agentInput.value;
  if (!value.trim()) {
    setStatus("请输入要发送到终端的内容", true);
    updateControls();
    return;
  }
  if (!attached) {
    setStatus("请先 attach 后再发送输入", true);
    return;
  }
  send({ type: "input", data: `${value}\r` });
  agentInput.value = "";
  updateControls();
  setStatus("输入已写入终端；如果当前命令仍在运行，shell 会在其结束后继续处理。");
});

agentInput.addEventListener("input", updateControls);

terminateButton.addEventListener("click", async () => {
  if (!terminalId) {
    setStatus("URL 缺少 terminalId 参数", true);
    return;
  }
  const response = await fetch(`${backendBaseUrl}/api/terminals/${encodeURIComponent(terminalId)}/kill`, {
    method: "POST",
    headers: backendRequestHeaders,
  });
  if (!response.ok) {
    setStatus(`终止失败: ${response.status}`, true);
    return;
  }
  const payload = await response.json();
  const snapshot = payload.data?.terminal;
  currentTerminalStatus = snapshot?.status || "terminated";
  setStatus(snapshot ? `已终止 · ${describeSnapshot(snapshot)}` : "已终止");
  await loadSnapshot();
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
  const response = await fetch(`${backendBaseUrl}/api/terminals/${encodeURIComponent(terminalId)}`, {
    method: "DELETE",
    headers: backendRequestHeaders,
  });
  if (!response.ok) {
    setStatus(`删除失败: ${response.status}`, true);
    return;
  }
  deleted = true;
  const payload = await response.json();
  markDeleted("终端已删除", payload.data?.terminal ?? null);
});

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
