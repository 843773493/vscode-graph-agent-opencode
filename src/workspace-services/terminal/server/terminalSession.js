import {
  readProcessStat,
  terminateTerminalProcessTree,
} from "./processes/terminalProcessLifecycle.js";
import { stat } from "node:fs/promises";
import path from "node:path";
import { IsolatedPtyProcess } from "./processes/isolatedPtyProcess.js";
import { TerminalOutputMultiplexer } from "./output/terminalOutputMultiplexer.js";
import { splitTerminalOutputChunks, trimTerminalBuffer } from "./output/terminalUtf8Output.js";
import {
  commandMarkersFromInput,
  lastNonEmptyInputLine,
  latestCommandExitCode,
  stripCommandMarkerLines,
} from "../shared/commandText.js";

export function nowIso() {
  return new Date().toISOString();
}

// 终端工作目录唯一解析入口：把持久记录里的工作区内相对路径（或调用方传入的
// 相对/绝对路径）还原为本次调用栈内的绝对路径。绝对路径 MUST NOT 落盘。
export async function resolveTerminalCwd(workspaceRoot, cwdRelative) {
  const rawCwd = typeof cwdRelative === "string" && cwdRelative.trim() !== ""
    ? cwdRelative.trim()
    : ".";
  const resolved = path.resolve(workspaceRoot, rawCwd);
  let directoryStat;
  try {
    directoryStat = await stat(resolved);
  } catch (error) {
    console.error("[terminal-session] 终端工作目录检查失败", { cwd: resolved }, error);
    const message = new Set(["ENOENT", "ENOTDIR"]).has(error.code)
      ? "终端工作目录不存在或不是目录"
      : "终端工作目录检查失败";
    throw new Error(message, { cause: error });
  }
  if (!directoryStat.isDirectory()) {
    console.error("[terminal-session] 终端工作目录不是目录", { cwd: resolved });
    throw new Error("终端工作目录不存在或不是目录");
  }
  return resolved;
}

const RECONCILIATION_STATUSES = new Set([
  "verified",
  "unknown",
  "reconcile_required",
]);

export function validateTerminalRecord(record, workspaceId) {
  if (record.workspace_id !== workspaceId) {
    throw new Error(
      `终端记录工作区身份不匹配: terminal_id=${record.terminal_id}, expected=${workspaceId}, actual=${record.workspace_id}`,
    );
  }
  if (Object.hasOwn(record, "cwd")) {
    throw new Error(`终端记录包含已废弃的真实工作目录 cwd: terminal_id=${record.terminal_id}`);
  }
  if (
    typeof record.cwd_relative !== "string"
    || path.posix.isAbsolute(record.cwd_relative)
    || path.win32.isAbsolute(record.cwd_relative)
  ) {
    throw new Error(`终端记录必须包含相对 cwd_relative: terminal_id=${record.terminal_id}`);
  }
  if (!Number.isSafeInteger(record.transport_owner_revision) || record.transport_owner_revision < 1) {
    throw new Error(`终端传输版本无效: terminal_id=${record.terminal_id}`);
  }
  if (!RECONCILIATION_STATUSES.has(record.reconciliation_status)) {
    throw new Error(`终端 PTY 核实状态无效: terminal_id=${record.terminal_id}`);
  }
  for (const field of ["session_id", "source_thread_id", "retention_scope", "thread_associations", "unattached_since", "owner_revision"]) {
    if (Object.hasOwn(record, field)) {
      throw new Error(`Node PTY 记录不得持有领域 owner 字段: ${field}, terminal_id=${record.terminal_id}`);
    }
  }
  return record;
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

/**
 * 完成事件的对照字段；输出基线与事件ID联合核对，不单独充当命令代际。
 * @typedef {object} TerminalCompletionComparison
 * @property {string} completionEventId Session记录的完成事件标识，不是终端资源ID。
 * @property {number} commandStartSequence 接纳命令前最后已有的输出序号，不是命令首条输出序号或独立generation。
 */

export class TerminalSession {
  constructor({ record, manager }) {
    validateTerminalRecord(record, manager.workspaceId);
    this.manager = manager;
    this.id = record.terminal_id;
    this.workspaceId = record.workspace_id;
    // 唯一领域版本的传输影子，仅用于拒绝过期 grant；不决定保留、关联或回收。
    this.transportOwnerRevision = record.transport_owner_revision;
    /** @type {Map<object, import("./terminalManager.js").TerminalTransportGrant>} */
    this.transports = new Map();
    this.reconciliationStatus = record.reconciliation_status;
    this.ownerAgentId = record.owner_agent_id || null;
    this.title = record.title || "Persistent Terminal";
    this.command = record.command || resolveShell();
    this.args = Array.isArray(record.args) ? record.args : shellArgs();
    this.cwdRelative = record.cwd_relative;
    this.cwd = path.resolve(manager.workspaceRoot, record.cwd_relative);
    this.cols = record.cols || 100;
    this.rows = record.rows || 30;
    // 本地执行记录的 UTC ISO 创建时间，不是操作系统进程的创建标记。
    this.createdAt = record.created_at || nowIso();
    this.updatedAt = record.updated_at || this.createdAt;
    this.lastUsedAt = record.last_used_at || this.updatedAt;
    // Manager 收到 worker 就绪、确认执行结束的 UTC ISO 时间，均为观察时刻。
    this.startedAt = record.started_at || null;
    this.endedAt = record.ended_at || null;
    this.status = record.status || "created";
    this.exitCode = record.exit_code ?? null;
    this.signal = record.signal ?? null;
    // PTY 执行根进程的 PID；隔离 worker PID 由 ptyProcess.workerPid 单独提供。
    this.osPid = record.os_pid ?? null;
    this.processGroupId = record.process_group_id ?? null;
    /** @type {number|string|null} 操作系统 SID / Darwin sess，不是业务 Session ID。 */
    this.processSessionId = record.process_session_id ?? null;
    /** @type {number|string|null} 同 TerminalProcessStat：Linux 时钟滴答数或 POSIX lstart 原文，只作进程身份比较。 */
    this.processStartTime = record.process_start_time ?? null;
    this.releaseReason = record.release_reason ?? null;
    /** @type {number} 已产生的最后一条PTY输出事件序号；不等于worker的IPC包号。 */
    this.outputSequence = record.sequence || 0;
    /** @type {number} 模型已读到的输出序号（包含该条），与UI的逐连接ACK分开。 */
    this.modelReadThroughOutputSequence = record.model_output_sequence || 0;
    /** @type {number|null} 当前命令写入前最后已有的输出序号；null表示尚无命令基线。 */
    this.commandOutputBaselineSequence = record.command_start_sequence ?? null;
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
    const inferredLastInput = record.last_input;
    this.lastInput = inferredLastInput || null;
    this.lastInputSource = record.last_input_source || (inferredLastInput ? "interactive" : null);
    this.lastInputAt = record.last_input_at || (inferredLastInput ? this.updatedAt : null);
    this.ptyProcess = null;
    this.releasePromise = null;
    this.outputMultiplexer = new TerminalOutputMultiplexer({
      terminalId: this.id,
      getSequence: () => this.outputSequence,
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
      this.buffer = trimTerminalBuffer(this.buffer + data);
      const outputReceivedAt = nowIso();
      for (const chunk of splitTerminalOutputChunks(data)) {
        this.outputSequence += 1;
        this.outputMultiplexer.recordAndBroadcast({
          type: "output",
          terminalId: this.id,
          sequence: this.outputSequence,
          data: chunk,
          timestamp: outputReceivedAt,
        });
      }
      const exitCode = this.lastCommandStatus === "running"
        ? latestCommandExitCode(this.buffer, this.lastCommandDoneMarker)
        : null;
      if (exitCode !== null) {
        this.lastCommandStatus = "completed";
        this.lastCommandExitCode = exitCode;
        this.lastCommandCompletedAt = nowIso();
        this.completionEventId =
          this.completionEventId || `terminal_completed:${this.id}:${this.outputSequence}`;
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
        this.releaseReason = "pty_exit_cleanup_failed";
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
      resolvedCwd = await resolveTerminalCwd(this.manager.workspaceRoot, this.cwdRelative);
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
      console.error(
        `[terminal-session] 终端 PTY 启动失败: terminal_id=${this.id}`,
        { cwd: resolvedCwd ?? this.cwd, command: this.command },
        error,
      );
      throw new Error(
        `终端 PTY 启动失败: terminal_id=${this.id}`,
        { cause: error },
      );
    }
    this.status = "running";
    this.startedAt = this.startedAt || nowIso();
    this.osPid = ready.pid;
    const stat = await readProcessStat(this.osPid);
    if (stat) {
      this.processGroupId = stat.processGroupId;
      this.processSessionId = stat.processSessionId;
      this.processStartTime = stat.processStartTime;
    } else {
      this.processGroupId = this.processGroupId || this.osPid;
    }
    this.touch();
    await this.manager.persist();
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
      this.markReconciliationStatus("unknown");
      if (error) {
        console.error(`[terminal-session] PTY Worker 失败: terminal_id=${this.id}`, error);
      }
      this.releaseReason = error
        ? "pty_worker_error"
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

  async attach(client, { afterSequence = null, grant } = {}) {
    this.assertPtyConnected("attach");
    this.touchUsage();
    try {
      this.transports.set(client, grant);
      this.assertTransport(client);
      await this.outputMultiplexer.attach(client, {
        afterSequence,
        beforeNotify: async () => {
          await this.manager.persist();
          this.assertTransport(client);
        },
      });
      this.assertTransport(client);
    } catch (error) {
      this.transports.delete(client);
      await this.detach(client);
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
    const read = this.outputMultiplexer.readAfter(this.modelReadThroughOutputSequence);
    this.modelReadThroughOutputSequence = read.sequence;
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
    this.assertPtyConnected("write");
    if (!this.ptyProcess || this.status !== "running" || this.releasePromise) {
      throw new Error(`终端未运行: terminal_id=${this.id}, status=${this.status}`);
    }
    if (command) {
      const markers = commandMarkersFromInput(data);
      this.commandOutputBaselineSequence = this.outputSequence;
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
      const label = lastNonEmptyInputLine(data);
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
    const hasLivePty = this.ptyProcess && this.status === "running" && !this.releasePromise;
    if (!hasLivePty && this.reconciliationStatus !== "verified") {
      throw new Error(
        `终端尚待 owner 核实，不能调整尺寸: terminal_id=${this.id}, reconciliation_status=${this.reconciliationStatus}`,
      );
    }
    this.cols = cols;
    this.rows = rows;
    if (hasLivePty) {
      this.ptyProcess.resize(cols, rows);
    }
    this.touch();
    return true;
  }

  async terminateForRelease({ status, commandStatus, reason }) {
    if (this.releasePromise) {
      return await this.releasePromise;
    }
    const retainedRelease = this.ptyProcess && this.reconciliationStatus !== "verified"
      && new Set(["completed", "exited", "terminated", "deleted"]).has(this.status);
    if (this.status !== "running" && !retainedRelease) {
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
    } catch (error) {
      this.markReconciliationStatus("unknown");
      try {
        await this.manager.persist();
      } catch (persistError) {
        throw new AggregateError([error, persistError],
          `终端停止失败且未知状态持久化失败: terminal_id=${this.id}`, { cause: error });
      }
      throw error;
    } finally {
      if (this.releasePromise === releasePromise) {
        this.releasePromise = null;
      }
    }
  }

  async _terminateForRelease({ status, commandStatus, reason }) {
    const pid = this.osPid || this.ptyProcess?.pid;
    const isolatedPty = this.ptyProcess;
    if (!isolatedPty) {
      await this.verifyPersistedProcessIdentity(pid);
    }
    const result = await terminateTerminalProcessTree({
      pid,
      processSessionId: this.processSessionId,
      processStartTime: this.processStartTime,
    });
    if (result === "still_running") {
      this.markReconciliationStatus("unknown");
      throw new Error(
        `终端进程树清理失败: terminal_id=${this.id}, pid=${pid}`,
      );
    }
    if (!new Set(["terminated", "force_killed", "missing"]).has(result)) {
      this.markReconciliationStatus("unknown");
      throw new Error(
        `终端进程停止结果无法核实: terminal_id=${this.id}, result=${result}`,
      );
    }
    // 核实 Worker 退出及终态落盘前保留原句柄，失败重试仍操作同一个执行。
    await isolatedPty?.shutdown({ terminatePty: false });
    this.markReconciliationStatus("verified");
    this.status = status;
    this.endedAt = nowIso();
    this.signal = result === "force_killed" ? "SIGKILL" : "SIGTERM";
    this.releaseReason = reason;
    if (this.lastCommandStatus === "running") {
      this.lastCommandStatus = commandStatus;
      this.lastCommandCompletedAt = this.endedAt;
    }
    await this.manager.persist();
    this.ptyProcess = null;
    this.closeTransports("terminal stopped");
    this.broadcast({
      type: status === "deleted" ? "deleted" : "exit",
      terminalId: this.id,
      exitCode: this.exitCode,
      signal: this.signal,
      timestamp: this.endedAt,
      snapshot: this.snapshot(),
    });
    return true;
  }

  async verifyPersistedProcessIdentity(pid) {
    if (
      !Number.isInteger(pid)
      || !this.processSessionId
      || this.processStartTime === null
    ) {
      this.markReconciliationStatus("unknown");
      throw new Error(
        `终端持久进程身份不完整，拒绝停止或删除: terminal_id=${this.id}`,
      );
    }
    const stat = await readProcessStat(pid);
    if (
      !stat
      || stat.processSessionId !== this.processSessionId
      || stat.processStartTime !== this.processStartTime
      || stat.processGroupId !== this.processGroupId
    ) {
      this.markReconciliationStatus("unknown");
      throw new Error(
        `终端持久进程身份无法核实，拒绝停止或删除: terminal_id=${this.id}, pid=${pid}`,
      );
    }
  }

  assertPtyConnected(operation) {
    if (this.reconciliationStatus !== "verified") {
      throw new Error(
        `终端 owner 状态尚待核实: operation=${operation}, terminal_id=${this.id}, reconciliation_status=${this.reconciliationStatus}`,
      );
    }
  }

  markReconciliationStatus(status) {
    if (!RECONCILIATION_STATUSES.has(status)) {
      throw new Error(`终端 reconciliation_status 无效: ${status}`);
    }
    if (this.reconciliationStatus === status) {
      return false;
    }
    this.reconciliationStatus = status;
    this.touch();
    return true;
  }

  syncOwnerRevision(transportOwnerRevision) {
    if (!Number.isSafeInteger(transportOwnerRevision) || transportOwnerRevision < this.transportOwnerRevision) {
      throw new Error(`终端传输 owner revision 过期或无效: terminal_id=${this.id}, revision=${transportOwnerRevision}`);
    }
    if (transportOwnerRevision !== this.transportOwnerRevision) {
      this.transportOwnerRevision = transportOwnerRevision;
      this.closeTransports("terminal owner changed");
      this.touch();
    }
  }

  closeTransports(reason) {
    for (const client of this.transports.keys()) {
      client.close(4009, reason);
    }
    this.transports.clear();
  }

  assertTransport(client) {
    if (this.transports.get(client)?.transportOwnerRevision !== this.transportOwnerRevision) {
      throw new Error(`终端连接授权已过期: terminal_id=${this.id}`);
    }
  }

  async dispose({ terminatePty = true } = {}) {
    if (this.releasePromise) {
      await this.releasePromise;
    }
    const isolatedPty = this.ptyProcess;
    this.outputMultiplexer.dispose();
    await isolatedPty?.shutdown({ terminatePty });
    this.ptyProcess = null;
  }

  async failStartup(error) {
    const isolatedPty = this.ptyProcess;
    // TODO（5.2）：native 未交付 PID 或没有正式停止证明时，保留未知记录供原 owner 接管。
    const stopped = isolatedPty !== null && new Set(["missing", "terminated", "force_killed"])
      .has(isolatedPty.startupCleanupResult);
    this.osPid = isolatedPty?.pid ?? this.osPid;
    this.markReconciliationStatus("unknown");
    this.releaseReason = "terminal_startup_failed";
    await this.manager.persist();
    try {
      await this.dispose();
    } catch (cleanupError) {
      console.error(`[terminal-session] 启动失败清理未完成: terminal_id=${this.id}`, cleanupError);
      throw new AggregateError([error, cleanupError], `终端启动失败且清理未完成: terminal_id=${this.id}`, { cause: error });
    }
    if (stopped) {
      this.status = "terminated";
      this.endedAt = nowIso();
      this.markReconciliationStatus("verified");
    }
    await this.manager.persist();
    if (stopped) error.creationReceipt = {
      resource_id: this.id, workspace_id: this.workspaceId, owner_revision: this.transportOwnerRevision,
      outcome: "failed", resource: this.snapshot(),
    };
    throw error;
  }

  async kill({ reason = "terminal_cancel" } = {}) {
    return await this.terminateForRelease({
      status: "terminated",
      commandStatus: "terminated",
      reason,
    });
  }

  async delete() {
    if (this.status === "deleted" && this.reconciliationStatus === "verified") {
      return;
    }
    await this.terminateForRelease({
      status: "deleted",
      commandStatus: "deleted",
      reason: "terminal_delete",
    });
    if (this.status === "deleted" && this.reconciliationStatus === "verified") return;
    this.assertPtyConnected("delete");
    this.status = "deleted";
    this.closeTransports("terminal deleted");
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

  /** @param {TerminalCompletionComparison} identity */
  assertSteeringIdentity({ completionEventId, commandStartSequence }) {
    if (typeof completionEventId !== "string" || !completionEventId
        || !Number.isInteger(commandStartSequence) || commandStartSequence < 0) {
      throw new TypeError("Terminal completion 必须提供真实 event 和命令 generation");
    }
    if (this.completionEventId !== completionEventId
        || this.commandOutputBaselineSequence !== commandStartSequence) {
      throw new Error(`Terminal completion event/generation 不匹配: ${this.id}`);
    }
  }

  /** @param {TerminalCompletionComparison} identity */
  claimSteering(identity) {
    this.assertSteeringIdentity(identity);
    // 未结清的持久claim只恢复同一个完成事件与输出基线。
    if (this.steeringDispatching) {
      return true;
    }
    const claimable = new Set(["running", "completed"]).has(this.status)
      && this.modelBackgrounded
      && this.lastCommandStatus === "completed"
      && !this.completionObservedByModel
      && !this.steeringDispatched;
    if (!claimable) {
      return false;
    }
    this.steeringDispatching = true;
    this.touch();
    return true;
  }

  /** @param {TerminalCompletionComparison & { dispatched: boolean }} result */
  finishSteering({ completionEventId, commandStartSequence, dispatched }) {
    this.assertSteeringIdentity({ completionEventId, commandStartSequence });
    if (typeof dispatched !== "boolean") {
      throw new TypeError("dispatched 必须是布尔值");
    }
    if (!this.steeringDispatching) {
      if (this.steeringDispatched === dispatched) {
        return;
      }
      throw new Error(`终端 execution 没有待完成的 steering claim: ${this.id}`);
    }
    this.steeringDispatching = false;
    this.steeringDispatched = dispatched;
    this.touch();
  }

  broadcast(message) {
    this.outputMultiplexer.broadcast(message);
  }

  toRecord() {
    // 持久记录只保存恢复所需事实；展示正文和当前连接信息在对外快照中派生。
    return {
      terminal_id: this.id,
      workspace_id: this.workspaceId,
      transport_owner_revision: this.transportOwnerRevision,
      reconciliation_status: this.reconciliationStatus,
      owner_agent_id: this.ownerAgentId,
      title: this.title,
      command: this.command,
      args: this.args,
      // 持久记录和对外快照只承载工作区内相对目录；绝对真实路径 MUST NOT 出现在 API 响应体、
      // WebSocket 状态或模型可见载荷（裁定 D-A2/D-A3）。
      cwd_relative: this.cwdRelative,
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
      sequence: this.outputSequence,
      model_output_sequence: this.modelReadThroughOutputSequence,
      command_start_sequence: this.commandOutputBaselineSequence,
      model_backgrounded: this.modelBackgrounded,
      completion_observed_by_model: this.completionObservedByModel,
      steering_dispatching: this.steeringDispatching,
      steering_dispatched: this.steeringDispatched,
      completion_event_id: this.completionEventId,
      buffer: this.buffer,
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
    };
  }

  snapshot() {
    return {
      ...this.toRecord(),
      pty_worker_pid: this.ptyProcess?.workerPid ?? null,
      display_buffer: stripCommandMarkerLines(this.buffer),
      client_count: this.outputMultiplexer.clientCount,
    };
  }
}
