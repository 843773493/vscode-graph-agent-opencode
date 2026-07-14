const backendBaseUrl = window.BOXTEAM_TERMINAL_BACKEND_URL || "http://127.0.0.1:8012";
const params = new URLSearchParams(window.location.search);
const terminalId = params.get("terminalId");
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
  url.pathname = "/terminal";
  url.search = "";
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
    { cache: "no-store" },
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
  socket = new WebSocket(backendWsUrl());
  setAttachButtonMode("attaching");
  setStatus("正在连接终端...");

  socket.addEventListener("open", () => {
    fitAddon.fit();
    send({
      type: "attach",
      terminalId,
      cols: terminal.cols,
      rows: terminal.rows,
    });
    lastSentCols = terminal.cols;
    lastSentRows = terminal.rows;
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      attached = true;
      currentTerminalStatus = message.snapshot?.status || currentTerminalStatus;
      setAttachButtonMode("attached");
      terminal.clear();
      terminal.write(message.snapshot ? snapshotDisplayBuffer(message.snapshot) : "");
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
      terminal.write(sanitizeTerminalDisplay(message.data));
      return;
    }
    if (message.type === "exit") {
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

  socket.addEventListener("close", () => {
    attached = false;
    lastSentCols = null;
    lastSentRows = null;
    setAttachButtonMode("detached");
    updateControls();
  });

  socket.addEventListener("error", () => {
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
