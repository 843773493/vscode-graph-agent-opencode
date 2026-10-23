const TERMINAL_ID = "uuid:12";
const TERMINAL_RESIZE_DEBOUNCE_MS = 80;
const TERMINAL_RENDER_FAILURE_CLOSE_CODE = 4001;
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
let terminalExitMessage = null;
let terminalServerErrorMessage = null;
let resizeTimer = null;
let lastSentTerminalSize = null;

terminal.loadAddon(fitAddon);
terminal.open(terminalHost);
resizeTerminal();
terminal.write("点击 Attach 连接到 uuid:12\r\n");

function setStatus(nextStatus, statusMessage) {
  statusDot.className = `status-dot ${nextStatus}`;
  statusText.textContent = statusMessage;
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

function sendClientMessage(outboundMessage) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("WebSocket 未连接，无法发送终端消息");
  }
  socket.send(JSON.stringify(outboundMessage));
}

function attach() {
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    setStatus("connecting", "终端连接正在处理中...");
    return;
  }
  pendingDetach = false;
  terminalExitMessage = null;
  terminalServerErrorMessage = null;
  const connectionSocket = new WebSocket(socketUrl());
  let terminalOutputWriteFailure = null;
  socket = connectionSocket;
  setStatus("connecting", "正在连接...");

  connectionSocket.addEventListener("open", () => {
    if (socket !== connectionSocket) {
      return;
    }
    const { cols, rows } = resizeTerminal();
    sendClientMessage({ type: "attach", terminalId: TERMINAL_ID, cols, rows });
    lastSentTerminalSize = { cols, rows };
  });

  connectionSocket.addEventListener("message", (event) => {
    if (socket !== connectionSocket) {
      return;
    }
    const serverMessage = JSON.parse(event.data);
    if (serverMessage.type === "attached") {
      setAttachedState(true);
      resizeTerminal();
      setStatus("attached", `已 attach 到 ${serverMessage.terminalId}`);
      return;
    }
    if (serverMessage.type === "detached") {
      setAttachedState(false);
      setStatus("detached", `已 detach ${serverMessage.terminalId}`);
      connectionSocket.close(1000, "detached");
      return;
    }
    if (serverMessage.type === "output") {
      if (terminalOutputWriteFailure !== null) {
        return;
      }
      try {
        terminal.write(serverMessage.data);
      } catch (error) {
        terminalOutputWriteFailure = error instanceof Error ? error.message : String(error);
        setAttachedState(false);
        setStatus("error", `终端输出渲染失败：${terminalOutputWriteFailure}`);
        connectionSocket.close(
          TERMINAL_RENDER_FAILURE_CLOSE_CODE,
          "terminal output rendering failed",
        );
      }
      return;
    }
    if (serverMessage.type === "exit") {
      setAttachedState(false);
      terminalExitMessage = `终端已退出 exitCode=${serverMessage.exitCode ?? "null"}`;
      setStatus("detached", terminalExitMessage);
      return;
    }
    if (serverMessage.type === "error") {
      terminalServerErrorMessage = serverMessage.message;
      setStatus("error", serverMessage.message);
      terminal.write(`\r\n[server error] ${serverMessage.message}\r\n`);
      return;
    }
    throw new Error(`未知服务端消息类型: ${serverMessage.type}`);
  });

  connectionSocket.addEventListener("close", (event) => {
    if (socket !== connectionSocket) {
      return;
    }
    window.clearTimeout(resizeTimer);
    resizeTimer = null;
    lastSentTerminalSize = null;
    setAttachedState(false);
    if (!pendingDetach) {
      const outputRenderFailed = terminalOutputWriteFailure !== null;
      const serverReportedFailure = terminalServerErrorMessage !== null;
      const fallbackStatusMessage = event.code === 1013
        ? "终端输出接收过慢，连接已关闭"
        : terminalExitMessage ?? "连接已关闭";
      const statusMessage = outputRenderFailed
        ? `终端输出渲染失败，连接已关闭：${terminalOutputWriteFailure}`
        : terminalServerErrorMessage ?? fallbackStatusMessage;
      setStatus(outputRenderFailed || serverReportedFailure ? "error" : "detached", statusMessage);
    }
    terminalExitMessage = null;
    terminalServerErrorMessage = null;
    socket = null;
    pendingDetach = false;
  });

  connectionSocket.addEventListener("error", () => {
    if (socket !== connectionSocket) {
      return;
    }
    setStatus("error", "WebSocket 连接错误");
  });
}

function detach() {
  sendClientMessage({ type: "detach", terminalId: TERMINAL_ID });
  pendingDetach = true;
}

function currentTerminalSize() {
  const proposed = fitAddon.proposeDimensions();
  if (!proposed) {
    throw new Error("无法计算 xterm 终端尺寸");
  }
  return proposed;
}

function resizeTerminal() {
  let { cols, rows } = currentTerminalSize();
  if (terminal.cols !== cols || terminal.rows !== rows) {
    fitAddon.fit();
    cols = terminal.cols;
    rows = terminal.rows;
  }
  if (
    attached
    && socket?.readyState === WebSocket.OPEN
    && (lastSentTerminalSize?.cols !== cols || lastSentTerminalSize?.rows !== rows)
  ) {
    sendClientMessage({ type: "resize", terminalId: TERMINAL_ID, cols, rows });
    lastSentTerminalSize = { cols, rows };
  }
  return { cols, rows };
}

function scheduleTerminalResize() {
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {
    resizeTimer = null;
    resizeTerminal();
  }, TERMINAL_RESIZE_DEBOUNCE_MS);
}

terminal.onData((data) => {
  if (!attached) {
    return;
  }
  sendClientMessage({ type: "input", terminalId: TERMINAL_ID, data });
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
  sendClientMessage({ type: "agentInput", terminalId: TERMINAL_ID, data });
});

window.addEventListener("resize", scheduleTerminalResize);
