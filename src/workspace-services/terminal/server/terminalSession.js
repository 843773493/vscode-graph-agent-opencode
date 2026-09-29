import {
  readProcessStat,
  terminateTerminalProcessTree,
} from "./terminalProcessUtils.js";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import { IsolatedPtyProcess } from "./isolatedPtyProcess.js";
import { TerminalOutputMultiplexer } from "./terminalOutputMultiplexer.js";

const MAX_BUFFER_BYTES = 256 * 1024;
const MAX_OUTPUT_EVENT_BYTES = 64 * 1024;

export function nowIso() {
  return new Date().toISOString();
}

// 终端工作目录唯一解析入口：把持久记录里的工作区内相对路径（或调用方传入的
// 相对/绝对路径）还原为本次调用栈内的绝对路径。绝对路径 MUST NOT 落盘。
export function resolveTerminalCwd(workspaceRoot, cwdRelative) {
  const rawCwd = typeof cwdRelative === "string" && cwdRelative.trim() !== ""
    ? cwdRelative.trim()
    : ".";
  const resolved = path.resolve(workspaceRoot, rawCwd);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(
      `终端工作目录不存在或不是目录: cwd_relative=${rawCwd}, resolved=${resolved}`,
    );
  }
  return resolved;
}

// 一次性迁移旧持久记录：把绝对 `cwd` 还原为工作区内相对路径 `cwd_relative`。
// 迁移必须能失败：非字符串/空串、非绝对路径、目录不存在、以及无法表达为工作区
// 内相对路径（跨设备）都 MUST fail-closed 并给出可定位错误，绝不静默丢弃终端，
// 也绝不回退进程 cwd。工作区外但仍可逐字节还原的现存目录（例如 `/etc`）按
// `path.relative` 的 `..` 表达保留原语义，不在此处做边界收窄。
export function deriveLegacyCwdRelative(workspaceRoot, cwd, terminalId) {
  if (typeof cwd !== "string" || cwd.trim() === "") {
    throw new Error(
      `终端记录 cwd 缺失或非字符串，无法迁移为相对路径: terminal_id=${terminalId}, cwd=${JSON.stringify(cwd)}`,
    );
  }
  const rawCwd = cwd.trim();
  if (!path.isAbsolute(rawCwd)) {
    throw new Error(
      `终端记录 cwd 不是绝对路径，无法迁移为相对路径: terminal_id=${terminalId}, cwd=${rawCwd}`,
    );
  }
  const resolved = path.resolve(rawCwd);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(
      `终端记录 cwd 不存在或不是目录，迁移拒绝静默丢弃: terminal_id=${terminalId}, cwd=${resolved}`,
    );
  }
  const relative = path.relative(path.resolve(workspaceRoot), resolved);
  if (path.isAbsolute(relative)) {
    throw new Error(
      `终端记录 cwd 无法表达为工作区内相对路径: terminal_id=${terminalId}, cwd=${resolved}, workspace_root=${workspaceRoot}`,
    );
  }
  return relative === "" ? "." : relative;
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
  const normalizedBuffer = stripAnsi(normalizeTerminalLines(buffer));
  const matches = [
    ...normalizedBuffer.matchAll(
      new RegExp(`${markerPattern}:(\\d+)`, "g"),
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
    this.workspaceId = record.workspace_id || manager.workspaceId;
    this.sessionId = record.session_id;
    this.ownerAgentId = record.owner_agent_id || null;
    this.title = record.title || "Persistent Terminal";
    this.command = record.command || resolveShell();
    this.args = Array.isArray(record.args) ? record.args : shellArgs();
    if (typeof record.cwd_relative !== "string") {
      throw new Error(
        `终端记录缺少工作目录相对路径: terminal_id=${record.terminal_id}`,
      );
    }
    this.cwdRelative = record.cwd_relative;
    this.cwd = path.resolve(manager.workspaceRoot, record.cwd_relative);
    this.cols = record.cols || 100;
    this.rows = record.rows || 30;
    this.createdAt = record.created_at || nowIso();
    this.updatedAt = record.updated_at || this.createdAt;
    this.lastUsedAt = record.last_used_at || this.updatedAt;
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
    this.modelOutputSequence = record.model_output_sequence || 0;
    this.commandStartSequence = record.command_start_sequence ?? null;
    this.modelBackgrounded = record.model_backgrounded || false;
    this.completionObservedByModel = record.completion_observed_by_model || false;
    this.steeringDispatching = record.steering_dispatching || false;
    this.steeringDispatched = record.steering_dispatched || false;
    this.completionEventId = record.completion_event_id || null;
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
        this.completionEventId =
          this.completionEventId || `terminal_completed:${this.id}:${this.sequence}`;
        void this.terminateForRelease({
          status: "completed",
          commandStatus: "completed",
          reason: "command_completed",
        }).catch((error) => {
          console.error(
            `[terminal-session] 命令完成后释放隐藏 PTY 失败: terminal_id=${this.id}`,
            error,
          );
        });
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
    let ready;
    let resolvedCwd = null;
    try {
      resolvedCwd = resolveTerminalCwd(this.manager.workspaceRoot, this.cwdRelative);
      ready = await isolatedPty.start({
        command: this.command,
        args: this.args,
        options: {
          name: "xterm-256color",
          cwd: resolvedCwd,
          cols: this.cols,
          rows: this.rows,
          env: {
            ...process.env,
            TERM: "xterm-256color",
          },
        },
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      throw new Error(
        `终端 PTY 启动失败: terminal_id=${this.id}, cwd=${resolvedCwd ?? this.cwd}, command=${this.command}; ${message}`,
        { cause: error },
      );
    }
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
    this.touchUsage();
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

  async readModelOutput() {
    this.touchUsage();
    const read = this.outputMultiplexer.readAfter(this.modelOutputSequence);
    this.modelOutputSequence = read.sequence;
    if (this.lastCommandStatus === "completed") {
      this.completionObservedByModel = true;
    }
    this.touch();
    await this.manager.persist();
    return {
      terminal: this.snapshot(),
      output: read.output,
      sequence: read.sequence,
      replay_mode: read.replayMode,
      omitted_before_sequence: read.omittedBeforeSequence,
    };
  }

  write(data, { source = "user", command = null } = {}) {
    if (!this.ptyProcess || this.status !== "running" || this.releasePromise) {
      throw new Error(`终端未运行: terminal_id=${this.id}, status=${this.status}`);
    }
    if (command) {
      const markers = commandMarkersFromInput(data);
      this.commandStartSequence = this.sequence;
      this.lastCommand = command;
      this.lastCommandStatus = "running";
      this.lastCommandExitCode = null;
      this.lastCommandStartedAt = nowIso();
      this.lastCommandCompletedAt = null;
      this.lastCommandStartMarker = markers.startMarker;
      this.lastCommandDoneMarker = markers.doneMarker;
      this.modelBackgrounded = false;
      this.completionObservedByModel = false;
      this.steeringDispatching = false;
      this.steeringDispatched = false;
      this.completionEventId = null;
    } else {
      const label = inputLabel(data);
      if (label) {
        this.lastInput = label;
        this.lastInputSource = source;
        this.lastInputAt = nowIso();
      }
    }
    this.ptyProcess.write(data);
    this.touchUsage();
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

  async kill({ reason = "terminal_cancel" } = {}) {
    return await this.terminateForRelease({
      status: "terminated",
      commandStatus: "terminated",
      reason,
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

  touchUsage() {
    this.lastUsedAt = nowIso();
    this.updatedAt = this.lastUsedAt;
  }

  markModelBackgrounded() {
    this.modelBackgrounded = true;
    this.touchUsage();
  }

  claimSteering() {
    const claimable = new Set(["running", "completed"]).has(this.status)
      && this.modelBackgrounded
      && this.lastCommandStatus === "completed"
      && !this.completionObservedByModel
      && !this.steeringDispatching
      && !this.steeringDispatched;
    if (!claimable) {
      return false;
    }
    this.steeringDispatching = true;
    this.touch();
    return true;
  }

  finishSteering({ dispatched }) {
    if (!this.steeringDispatching) {
      throw new Error(`终端 execution 没有待完成的 steering claim: ${this.id}`);
    }
    this.steeringDispatching = false;
    this.steeringDispatched = dispatched;
    this.touch();
  }

  broadcast(message) {
    this.outputMultiplexer.broadcast(message);
  }

  snapshot() {
    return {
      terminal_id: this.id,
      workspace_id: this.workspaceId,
      session_id: this.sessionId,
      owner_agent_id: this.ownerAgentId,
      title: this.title,
      command: this.command,
      args: this.args,
      cwd: this.cwd,
      cols: this.cols,
      rows: this.rows,
      status: this.status,
      created_at: this.createdAt,
      updated_at: this.updatedAt,
      last_used_at: this.lastUsedAt,
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
      model_output_sequence: this.modelOutputSequence,
      command_start_sequence: this.commandStartSequence,
      model_backgrounded: this.modelBackgrounded,
      completion_observed_by_model: this.completionObservedByModel,
      steering_dispatching: this.steeringDispatching,
      steering_dispatched: this.steeringDispatched,
      completion_event_id: this.completionEventId,
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
    const record = this.snapshot();
    // 持久记录只承载工作区内相对路径；绝对路径仅在调用栈内解析（裁定 D-A2/D-A7）。
    delete record.cwd;
    record.cwd_relative = this.cwdRelative;
    return record;
  }
}
