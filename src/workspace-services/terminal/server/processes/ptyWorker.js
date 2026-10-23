import { createRequire } from "node:module";
import {
  readProcessStat,
  terminateTerminalProcessTree,
} from "./terminalProcessLifecycle.js";

const require = createRequire(import.meta.url);
const pty = require("node-pty");

const OUTPUT_HIGH_WATERMARK_BYTES = 1024 * 1024;
const OUTPUT_LOW_WATERMARK_BYTES = 256 * 1024;
const STARTUP_CONFIRM_DELAY_MS = 25;

let ptyProcess = null;
// 已分配的最后一个IPC输出包编号；每个worker从1开始，与终端sequence无关。
let lastOutputPacketId = 0;
let unacknowledgedOutputBytes = 0;
// 只暂停node-pty输出读取，不代表OS进程或聊天Job进入暂停态。
let ptyOutputPaused = false;
let shellProcessMetadata = null;
let shuttingDown = false;
let readySent = false;
let exitCleanupPromise = null;
let spawnContext = null;
/** @type {Map<number, number>} IPC输出包编号→尚未由宿主确认的UTF-8字节数，不是聊天消息ID。 */
const unacknowledgedBytesByOutputPacketId = new Map();

function send(message) {
  if (!process.connected || !process.send) {
    return false;
  }
  try {
    process.send(message, (error) => {
      if (error && !shuttingDown) {
        void shutdown({ orphaned: true }).catch(() => process.exit(1));
      }
    });
    return true;
  } catch {
    if (!shuttingDown) {
      void shutdown({ orphaned: true }).catch(() => process.exit(1));
    }
    return false;
  }
}

async function shutdown({ orphaned = false, terminatePty = true } = {}) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  const activePty = ptyProcess;
  ptyProcess = null;
  if (!activePty) {
    await exitCleanupPromise;
    process.exit(0);
    return;
  }
  if (orphaned || terminatePty) {
    const metadata = await shellProcessMetadata;
    const result = await terminateTerminalProcessTree({
      pid: activePty.pid,
      processSessionId: metadata?.processSessionId ?? null,
      processStartTime: metadata?.processStartTime ?? null,
    });
    if (result === "still_running") {
      process.exit(1);
      return;
    }
  }
  process.exit(0);
}

async function handlePtyExit(activePty, { exitCode, signal }) {
  if (shuttingDown) {
    return;
  }
  ptyProcess = null;
  const metadata = await shellProcessMetadata;
  const cleanupResult = await terminateTerminalProcessTree({
    pid: activePty.pid,
    processSessionId: metadata?.processSessionId ?? null,
    processStartTime: metadata?.processStartTime ?? null,
  });
  if (shuttingDown) {
    return;
  }
  if (!readySent) {
    send({
      type: "startupError",
      message: `PTY 启动后立即退出: command=${spawnContext?.command || "unknown"}, cwd=${spawnContext?.cwd || "unknown"}, exit_code=${exitCode}, signal=${signal}, cleanup=${cleanupResult === "missing" ? "already_exited" : cleanupResult}`,
    });
    setImmediate(() => process.exit(cleanupResult === "still_running" ? 1 : 0));
    return;
  }
  send({ type: "exit", exitCode, signal, cleanupResult });
  setImmediate(() => process.exit(cleanupResult === "still_running" ? 1 : 0));
}

function handleStart(message) {
  if (ptyProcess) {
    throw new Error("PTY Worker 不允许重复启动终端");
  }
  spawnContext = {
    command: message.command,
    cwd: message.options?.cwd,
  };
  ptyProcess = pty.spawn(message.command, message.args, message.options);
  shellProcessMetadata = readProcessStat(ptyProcess.pid);
  ptyProcess.onData((data) => {
    if (shuttingDown) {
      return;
    }
    const outputPacketId = ++lastOutputPacketId;
    const byteCount = Buffer.byteLength(data, "utf8");
    unacknowledgedBytesByOutputPacketId.set(outputPacketId, byteCount);
    unacknowledgedOutputBytes += byteCount;
    send({ type: "output", messageId: outputPacketId, data });
    if (!ptyOutputPaused && unacknowledgedOutputBytes > OUTPUT_HIGH_WATERMARK_BYTES) {
      ptyOutputPaused = true;
      ptyProcess.pause();
    }
  });
  const activePty = ptyProcess;
  ptyProcess.onExit((event) => {
    exitCleanupPromise = handlePtyExit(activePty, event).catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      if (!readySent) {
        send({ type: "startupError", message });
      } else {
        send({
          type: "exit",
          exitCode: event.exitCode,
          signal: event.signal,
          cleanupResult: "cleanup_failed",
          error: message,
        });
      }
      setImmediate(() => process.exit(1));
    });
  });
  setTimeout(() => {
    if (!ptyProcess || shuttingDown) {
      return;
    }
    readySent = true;
    send({ type: "ready", pid: ptyProcess.pid });
  }, STARTUP_CONFIRM_DELAY_MS);
}

// 宿主按单个包回执；重复或未知编号不改变背压计费，不是UI的累计sequence ACK。
function acknowledgeOutputPacket(outputPacketId) {
  const byteCount = unacknowledgedBytesByOutputPacketId.get(outputPacketId);
  if (byteCount === undefined) {
    return;
  }
  unacknowledgedBytesByOutputPacketId.delete(outputPacketId);
  unacknowledgedOutputBytes = Math.max(unacknowledgedOutputBytes - byteCount, 0);
  if (ptyOutputPaused && unacknowledgedOutputBytes < OUTPUT_LOW_WATERMARK_BYTES) {
    ptyOutputPaused = false;
    ptyProcess?.resume();
  }
}

process.on("message", (message) => {
  try {
    if (!message || typeof message !== "object") {
      throw new Error("PTY Worker 消息必须是 object");
    }
    if (message.type === "start") {
      handleStart(message);
      return;
    }
    if (message.type === "ack") {
      acknowledgeOutputPacket(message.messageId);
      return;
    }
    if (message.type === "input") {
      if (!ptyProcess) {
        throw new Error("PTY 尚未启动");
      }
      ptyProcess.write(message.data);
      return;
    }
    if (message.type === "resize") {
      if (!ptyProcess) {
        throw new Error("PTY 尚未启动");
      }
      ptyProcess.resize(message.cols, message.rows);
      return;
    }
    if (message.type === "shutdown") {
      void shutdown({ terminatePty: message.terminatePty !== false });
      return;
    }
    throw new Error(`未知 PTY Worker 消息: ${message.type}`);
  } catch (error) {
    send({
      type: "error",
      message: error instanceof Error ? error.message : String(error),
    });
    if (!ptyProcess) {
      setImmediate(() => process.exit(1));
    }
  }
});

process.once("disconnect", () => {
  void shutdown({ orphaned: true }).catch(() => process.exit(1));
});
process.once("SIGTERM", () => {
  setTimeout(() => {
    if (!shuttingDown) {
      void shutdown({ orphaned: true }).catch(() => process.exit(1));
    }
  }, 1000);
});
process.once("SIGINT", () => {
  setTimeout(() => {
    if (!shuttingDown) {
      void shutdown({ orphaned: true }).catch(() => process.exit(1));
    }
  }, 1000);
});
