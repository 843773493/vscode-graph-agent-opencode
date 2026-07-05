const TERMINAL_ID = "uuid:12";
const terminalHost = document.querySelector("#terminal-host");
const attachToggle = document.querySelector("#attach-toggle");
const agentSend = document.querySelector("#agent-send");
const agentCommand = document.querySelector("#agent-command");
const statusDot = document.querySelector("#status-dot");
const statusText = document.querySelector("#status-text");

const terminal = new Terminal({
  cursorBlink: true,
  convertEol: true,
  fontFamily: '"Cascadia Mono", "JetBrains Mono", Consolas, monospace',
  fontSize: 13,
  theme: {
    background: "#101214",
    foreground: "#e7edf1",
    cursor: "#f5c542",
    selectionBackground: "#315a7a",
  },
});
const fitAddon = new FitAddon.FitAddon();

let socket = null;
let attached = false;
let pendingDetach = false;

terminal.loadAddon(fitAddon);
terminal.open(terminalHost);
resizeTerminal();
terminal.write("点击 Attach 连接到 uuid:12\r\n");

function setStatus(nextStatus, message) {
  statusDot.className = `status-dot ${nextStatus}`;
  statusText.textContent = message;
}

function setAttachedState(nextAttached) {
  attached = nextAttached;
  attachToggle.textContent = nextAttached ? "Detach" : "Attach";
  agentSend.disabled = !nextAttached;
}

function socketUrl() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/terminal`;
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 未连接，无法发送终端消息");
  }
  socket.send(JSON.stringify(message));
}

function attach() {
  if (socket && socket.readyState === WebSocket.OPEN) {
    throw new Error("WebSocket 已连接，不能重复 attach");
  }
  pendingDetach = false;
  socket = new WebSocket(socketUrl());
  setStatus("connecting", "正在连接...");

  socket.addEventListener("open", () => {
    const { cols, rows } = currentTerminalSize();
    send({ type: "attach", terminalId: TERMINAL_ID, cols, rows });
  });

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "attached") {
      setAttachedState(true);
      setStatus("attached", `已 attach 到 ${message.terminalId}`);
      return;
    }
    if (message.type === "detached") {
      setAttachedState(false);
      setStatus("detached", `已 detach ${message.terminalId}`);
      socket.close(1000, "detached");
      return;
    }
    if (message.type === "output") {
      terminal.write(message.data);
      return;
    }
    if (message.type === "exit") {
      setAttachedState(false);
      setStatus("detached", `终端已退出 exitCode=${message.exitCode ?? "null"}`);
      return;
    }
    if (message.type === "error") {
      setStatus("error", message.message);
      terminal.write(`\r\n[server error] ${message.message}\r\n`);
      return;
    }
    throw new Error(`未知服务端消息类型: ${message.type}`);
  });

  socket.addEventListener("close", () => {
    setAttachedState(false);
    if (!pendingDetach) {
      setStatus("detached", "连接已关闭");
    }
    socket = null;
    pendingDetach = false;
  });

  socket.addEventListener("error", () => {
    setStatus("error", "WebSocket 连接错误");
  });
}

function detach() {
  pendingDetach = true;
  send({ type: "detach", terminalId: TERMINAL_ID });
}

function currentTerminalSize() {
  const proposed = fitAddon.proposeDimensions();
  if (!proposed) {
    throw new Error("无法计算 xterm 终端尺寸");
  }
  return {
    cols: proposed.cols,
    rows: proposed.rows,
  };
}

function resizeTerminal() {
  fitAddon.fit();
  const { cols, rows } = currentTerminalSize();
  if (attached && socket?.readyState === WebSocket.OPEN) {
    send({ type: "resize", terminalId: TERMINAL_ID, cols, rows });
  }
}

terminal.onData((data) => {
  if (!attached) {
    return;
  }
  send({ type: "input", terminalId: TERMINAL_ID, data });
});

attachToggle.addEventListener("click", () => {
  if (attached) {
    detach();
    return;
  }
  attach();
});

agentSend.addEventListener("click", () => {
  const rawCommand = agentCommand.value.trim();
  if (!rawCommand) {
    throw new Error("Agent 输入命令不能为空");
  }
  const data = rawCommand.endsWith("\n") ? rawCommand : `${rawCommand}\n`;
  send({ type: "agentInput", terminalId: TERMINAL_ID, data });
});

window.addEventListener("resize", resizeTerminal);
