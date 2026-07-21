import {
  readProcessStat,
  terminateTerminalProcessTree,
} from "./terminalProcessUtils.js";
import { IsolatedPtyProcess } from "./isolatedPtyProcess.js";
import { TerminalOutputMultiplexer } from "./terminalOutputMultiplexer.js";

const MAX_BUFFER_BYTES = 256 * 1024;
const MAX_OUTPUT_EVENT_BYTES = 64 * 1024;

export function nowIso() {
  return new Date().toISOString();
}

export function resolveShell() {
  if (process.platform === "win32") {
    return process.env.COMSPEC || "cmd.exe";
  }
  return process.env.SHELL || "/bin/bash";
}

export function shellArgs() {
  if (process.platform === "win32") {
    return [];
  }
  return ["-i"];
}

function trimBuffer(value) {
  const buffer = Buffer.from(value, "utf8");
  if (buffer.length <= MAX_BUFFER_BYTES) {
    return value;
  }
  return buffer.subarray(buffer.length - MAX_BUFFER_BYTES).toString("utf8");
}

function splitUtf8Chunks(value, maxBytes = MAX_OUTPUT_EVENT_BYTES) {
  const chunks = [];
  let chunk = "";
  let chunkBytes = 0;
  for (const character of value) {
    const characterBytes = Buffer.byteLength(character, "utf8");
    if (chunk && chunkBytes + characterBytes > maxBytes) {
      chunks.push(chunk);
      chunk = "";
      chunkBytes = 0;
    }
    chunk += character;
    chunkBytes += characterBytes;
  }
  if (chunk) {
    chunks.push(chunk);
  }
  return chunks;
}

function normalizeTerminalLines(value) {
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function commandMarkersFromInput(data) {
  const startMatch = data.match(/__BOXTEAM_CMD_START_([a-f0-9]+)__/);
  const doneMatch = data.match(/__BOXTEAM_CMD_DONE_([a-f0-9]+)__/);
  if (!startMatch || !doneMatch || startMatch[1] !== doneMatch[1]) {
    return { startMarker: null, doneMarker: null };
  }
  return {
    startMarker: startMatch[0],
    doneMarker: doneMatch[0],
  };
}

function inputLabel(data) {
  return normalizeTerminalLines(data)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .at(-1) || null;
}

function stripAnsi(value) {
  return value
    .replace(/\x1B\][^\x07]*(?:\x07|\x1B\\)/g, "")
    .replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "");
}

function latestInteractiveInputFromBuffer(buffer) {
  const lines = stripAnsi(displayBuffer(buffer)).split(/\r?\n/).reverse();
  for (const line of lines) {
    const match = line.match(/\$\s+(.+)$/);
    if (!match) {
      continue;
    }
    const command = match[1].trim();
    if (!command || command.includes("__BOXTEAM_CMD_START_") || command.includes("__BOXTEAM_CMD_DONE_")) {
      continue;
    }
    return command;
  }
  return null;
}

function latestCommandExitCode(buffer, doneMarker = null) {
  const markerPattern = doneMarker
    ? doneMarker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    : "__BOXTEAM_CMD_DONE_[a-f0-9]+__";
  const matches = [
    ...normalizeTerminalLines(buffer).matchAll(
      new RegExp(`^${markerPattern}:(\\d+)\\s*$`, "gm"),
    ),
  ];
  if (matches.length === 0) {
    return null;
  }
  return Number(matches.at(-1)?.[1]);
}

function displayBuffer(buffer) {
  const parts = buffer.split(/(\r\n|\n)/);
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

export class TerminalSession {
  constructor({ record, manager }) {
    this.manager = manager;
    this.id = record.terminal_id;
    this.sessionId = record.session_id;
    this.title = record.title || "Persistent Terminal";
    this.command = record.command || resolveShell();
    this.args = Array.isArray(record.args) ? record.args : shellArgs();
    this.cwd = record.cwd;
    this.cols = record.cols || 100;
    this.rows = record.rows || 30;
    this.createdAt = record.created_at || nowIso();
    this.updatedAt = record.updated_at || this.createdAt;
    this.startedAt = record.started_at || null;
    this.endedAt = record.ended_at || null;
    this.status = record.status || "created";
    this.exitCode = record.exit_code ?? null;
    this.signal = record.signal ?? null;
    this.osPid = record.os_pid ?? null;
    this.processGroupId = record.process_group_id ?? null;
    this.processSessionId = record.process_session_id ?? null;
    this.processStartTime = record.process_start_time ?? null;
    this.releaseReason = record.release_reason ?? null;
    this.sequence = record.sequence || 0;
    this.buffer = record.buffer || "";
    this.lastCommand = record.last_command || null;
    this.lastCommandStatus = record.last_command_status || null;
    this.lastCommandExitCode = record.last_command_exit_code ?? null;
    this.lastCommandStartedAt = record.last_command_started_at || null;
    this.lastCommandCompletedAt = record.last_command_completed_at || null;
    this.lastCommandStartMarker = record.last_command_start_marker || null;
    this.lastCommandDoneMarker = record.last_command_done_marker || null;
    // TODO: 旧终端记录没有 last_input 字段时，通过已持久化 buffer 推断最近交互输入。
    const inferredLastInput = record.last_input || latestInteractiveInputFromBuffer(this.buffer);
    this.lastInput = inferredLastInput || null;
    this.lastInputSource = record.last_input_source || (inferredLastInput ? "interactive" : null);
    this.lastInputAt = record.last_input_at || (inferredLastInput ? this.updatedAt : null);
    this.ptyProcess = null;
    this.releasePromise = null;
    this.outputMultiplexer = new TerminalOutputMultiplexer({
      terminalId: this.id,
      getSequence: () => this.sequence,
      getSnapshot: () => this.snapshot(),
    });
    if (this.lastCommand && this.lastCommandStatus === null) {
      const exitCode = latestCommandExitCode(this.buffer, this.lastCommandDoneMarker);
      if (exitCode !== null) {
        this.lastCommandStatus = "completed";
        this.lastCommandExitCode = exitCode;
      }
    }
  }

  async start() {
    if (this.ptyProcess) {
      return;
    }

    const isolatedPty = new IsolatedPtyProcess();
    this.ptyProcess = isolatedPty;
    isolatedPty.onData((data) => {
      this.buffer = trimBuffer(this.buffer + data);
      const timestamp = nowIso();
      for (const chunk of splitUtf8Chunks(data)) {
        this.sequence += 1;
        this.outputMultiplexer.recordAndBroadcast({
          type: "output",
          terminalId: this.id,
          sequence: this.sequence,
          data: chunk,
          timestamp,
        });
      }
      const exitCode = latestCommandExitCode(this.buffer, this.lastCommandDoneMarker);
      if (exitCode !== null && this.lastCommandStatus === "running") {
        this.lastCommandStatus = "completed";
        this.lastCommandExitCode = exitCode;
        this.lastCommandCompletedAt = nowIso();
      }
      this.touch();
      void this.manager.persist();
    });
    isolatedPty.onExit((event) => {
      void this.handlePtyExit(event).catch((error) => {
        this.status = "exited";
        this.endedAt = nowIso();
        this.ptyProcess = null;
        this.releaseReason = `pty_exit_cleanup_failed: ${
          error instanceof Error ? error.message : String(error)
        }`;
        if (this.lastCommandStatus === "running") {
          this.lastCommandStatus = "exited";
          this.lastCommandCompletedAt = this.endedAt;
        }
        this.touch();
        console.error(
          `[terminal-session] PTY 退出清理失败: terminal_id=${this.id}`,
          error,
        );
        this.broadcast({
          type: "exit",
          terminalId: this.id,
          exitCode: event.exitCode,
          signal: event.signal,
          error: this.releaseReason,
          timestamp: this.endedAt,
        });
        void this.manager.persist().catch((persistError) => {
          console.error(
            `[terminal-session] PTY 退出失败状态持久化失败: terminal_id=${this.id}`,
            persistError,
          );
        });
      });
    });
    const ready = await isolatedPty.start({
      command: this.command,
      args: this.args,
      options: {
        name: "xterm-256color",
        cwd: this.cwd,
        cols: this.cols,
        rows: this.rows,
        env: {
          ...process.env,
          TERM: "xterm-256color",
        },
      },
    });
    this.status = "running";
    this.startedAt = this.startedAt || nowIso();
    this.osPid = ready.pid;
    void readProcessStat(this.osPid).then((stat) => {
      if (!stat) {
        this.processGroupId = this.processGroupId || this.osPid;
        this.processSessionId = this.processSessionId || null;
        return;
      }
      this.processGroupId = stat.processGroupId;
      this.processSessionId = stat.processSessionId;
      this.processStartTime = stat.processStartTime;
      this.touch();
      void this.manager.persist();
    });
    this.touch();
  }

  async handlePtyExit({
    exitCode,
    signal,
    workerExit,
    cleanupResult = null,
    error,
  }) {
    if (this.releasePromise) {
      this.exitCode = exitCode;
      this.signal = signal;
      return;
    }
    const wasRunning = this.status === "running";
    if (this.status !== "terminated" && this.status !== "deleted") {
      this.status = "exited";
    }
    this.exitCode = exitCode;
    this.signal = signal;
    this.endedAt = nowIso();
    if (workerExit || cleanupResult === "still_running" || cleanupResult === "cleanup_failed") {
      this.releaseReason = error
        ? `pty_worker_error: ${error.message}`
        : workerExit
          ? "pty_worker_exit"
          : `pty_cleanup_${cleanupResult}`;
      if (wasRunning && this.osPid) {
        const managerCleanupResult = await terminateTerminalProcessTree({
          pid: this.osPid,
          processSessionId: this.processSessionId,
          processStartTime: this.processStartTime,
        });
        if (managerCleanupResult === "still_running") {
          throw new Error(
            `PTY Worker 退出后进程树仍在运行: terminal_id=${this.id}, pid=${this.osPid}`,
          );
        }
      }
    }
    this.ptyProcess = null;
    if (this.lastCommandStatus === "running") {
      this.lastCommandStatus = this.status === "terminated" ? "terminated" : "exited";
      this.lastCommandCompletedAt = this.endedAt;
    }
    this.touch();
    this.broadcast({
      type: "exit",
      terminalId: this.id,
      exitCode,
      signal,
      timestamp: this.endedAt,
    });
    await this.manager.persist();
  }

  async attach(client, { afterSequence = null } = {}) {
    this.touch();
    try {
      await this.outputMultiplexer.attach(client, {
        afterSequence,
        beforeNotify: () => this.manager.persist(),
      });
    } catch (error) {
      this.touch();
      await this.manager.persist();
      throw error;
    }
  }

  async detach(client) {
    this.touch();
    await this.outputMultiplexer.detach(client, {
      beforeNotify: () => this.manager.persist(),
    });
  }

  acknowledge(client, sequence) {
    this.outputMultiplexer.acknowledge(client, sequence);
  }

  write(data, { source = "user", command = null } = {}) {
    if (!this.ptyProcess || this.status !== "running" || this.releasePromise) {
      throw new Error(`终端未运行: terminal_id=${this.id}, status=${this.status}`);
    }
    if (command) {
      const markers = commandMarkersFromInput(data);
      this.lastCommand = command;
      this.lastCommandStatus = "running";
      this.lastCommandExitCode = null;
      this.lastCommandStartedAt = nowIso();
      this.lastCommandCompletedAt = null;
      this.lastCommandStartMarker = markers.startMarker;
      this.lastCommandDoneMarker = markers.doneMarker;
    } else {
      const label = inputLabel(data);
      if (label) {
        this.lastInput = label;
        this.lastInputSource = source;
        this.lastInputAt = nowIso();
      }
    }
    this.ptyProcess.write(data);
    this.touch();
    this.broadcast({
      type: "input",
      terminalId: this.id,
      source,
      data,
      timestamp: this.updatedAt,
    });
    void this.manager.persist();
  }

  resize(cols, rows) {
    if (this.cols === cols && this.rows === rows) {
      return false;
    }
    this.cols = cols;
    this.rows = rows;
    if (this.ptyProcess && this.status === "running" && !this.releasePromise) {
      this.ptyProcess.resize(cols, rows);
    }
    this.touch();
    return true;
  }

  async terminateForRelease({ status, commandStatus, reason }) {
    if (this.releasePromise) {
      return await this.releasePromise;
    }
    if (this.status !== "running") {
      return false;
    }
    const releasePromise = this._terminateForRelease({
      status,
      commandStatus,
      reason,
    });
    this.releasePromise = releasePromise;
    try {
      return await releasePromise;
    } finally {
      if (this.releasePromise === releasePromise) {
        this.releasePromise = null;
      }
    }
  }

  async _terminateForRelease({ status, commandStatus, reason }) {
    const pid = this.osPid || this.ptyProcess?.pid;
    const isolatedPty = this.ptyProcess;
    const result = await terminateTerminalProcessTree({
      pid,
      processSessionId: this.processSessionId,
      processStartTime: this.processStartTime,
    });
    if (result === "still_running") {
      throw new Error(
        `终端进程树清理失败: terminal_id=${this.id}, pid=${pid}`,
      );
    }
    this.ptyProcess = null;
    await isolatedPty?.shutdown({ terminatePty: false });
    this.status = status;
    this.endedAt = nowIso();
    this.signal = result === "force_killed" ? "SIGKILL" : "SIGTERM";
    this.releaseReason = reason;
    if (this.lastCommandStatus === "running") {
      this.lastCommandStatus = commandStatus;
      this.lastCommandCompletedAt = this.endedAt;
    }
    this.touch();
    this.broadcast({
      type: status === "deleted" ? "deleted" : "exit",
      terminalId: this.id,
      exitCode: this.exitCode,
      signal: this.signal,
      timestamp: this.endedAt,
      snapshot: this.snapshot(),
    });
    void this.manager.persist();
    return result !== "missing";
  }

  async dispose() {
    if (this.releasePromise) {
      await this.releasePromise;
    }
    const isolatedPty = this.ptyProcess;
    this.ptyProcess = null;
    this.outputMultiplexer.dispose();
    await isolatedPty?.shutdown();
  }

  async kill() {
    return await this.terminateForRelease({
      status: "terminated",
      commandStatus: "terminated",
      reason: "terminal_cancel",
    });
  }

  async delete() {
    if (this.status === "deleted") {
      return;
    }
    if (this.releasePromise) {
      await this.releasePromise;
    }
    if (this.status === "running") {
      await this.terminateForRelease({
        status: "deleted",
        commandStatus: "deleted",
        reason: "terminal_delete",
      });
      if (this.status === "deleted") {
        return;
      }
    }
    this.status = "deleted";
    this.endedAt = nowIso();
    this.releaseReason = "terminal_delete";
    if (this.lastCommandStatus === "running") {
      this.lastCommandStatus = "deleted";
      this.lastCommandCompletedAt = this.endedAt;
    }
    this.touch();
    this.broadcast({
      type: "deleted",
      terminalId: this.id,
      timestamp: this.endedAt,
      snapshot: this.snapshot(),
    });
  }

  touch() {
    this.updatedAt = nowIso();
  }

  broadcast(message) {
    this.outputMultiplexer.broadcast(message);
  }

  snapshot() {
    return {
      terminal_id: this.id,
      session_id: this.sessionId,
      title: this.title,
      command: this.command,
      args: this.args,
      cwd: this.cwd,
      cols: this.cols,
      rows: this.rows,
      status: this.status,
      created_at: this.createdAt,
      updated_at: this.updatedAt,
      started_at: this.startedAt,
      ended_at: this.endedAt,
      exit_code: this.exitCode,
      signal: this.signal,
      process_group_id: this.processGroupId,
      process_session_id: this.processSessionId,
      process_start_time: this.processStartTime,
      release_reason: this.releaseReason,
      os_pid: this.osPid,
      pty_worker_pid: this.ptyProcess?.workerPid ?? null,
      sequence: this.sequence,
      buffer: this.buffer,
      display_buffer: displayBuffer(this.buffer),
      last_command: this.lastCommand,
      last_command_status: this.lastCommandStatus,
      last_command_exit_code: this.lastCommandExitCode,
      last_command_started_at: this.lastCommandStartedAt,
      last_command_completed_at: this.lastCommandCompletedAt,
      last_command_start_marker: this.lastCommandStartMarker,
      last_command_done_marker: this.lastCommandDoneMarker,
      last_input: this.lastInput,
      last_input_source: this.lastInputSource,
      last_input_at: this.lastInputAt,
      client_count: this.outputMultiplexer.clientCount,
      attach_url: this.manager.attachUrl(this.id),
    };
  }

  toRecord() {
    return this.snapshot();
  }
}
