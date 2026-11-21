import { createRuntimeLogger } from "@boxteam/plugin-sdk/logging";

import path from "node:path";
import { TerminalPtyLifecycle, nowIso } from "./processes/terminalPtyLifecycle.js";
import { resolveShell, shellArgs } from "./processes/terminalEnvironment.js";
import { TerminalOutputMultiplexer } from "./output/terminalOutputMultiplexer.js";
import { splitTerminalOutputChunks, trimTerminalBuffer } from "./output/terminalUtf8Output.js";
import {
  commandMarkersFromInput,
  lastNonEmptyInputLine,
  latestCommandExitCode,
  stripCommandMarkerLines,
} from "../shared/commandText.js";

const logger = createRuntimeLogger("terminal", "terminal.terminalSession");

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
    /** @type {number|string|null} 同 ProcessStat：Linux 时钟滴答数或 POSIX lstart 原文，只作进程身份比较。 */
    this.processStartTime = record.process_start_time ?? null;
    this.releaseReason = record.release_reason ?? null;
    this.creationCancellationPending = false;
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
    this.lifecycle = new TerminalPtyLifecycle(this);
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

  handlePtyOutput(data) {
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
      void this.lifecycle.terminateForRelease({
        status: "completed",
        commandStatus: "completed",
        reason: "command_completed",
      }).catch((error) => {
        logger.error(
          `[terminal-session] 命令完成后释放隐藏 PTY 失败: terminal_id=${this.id}`,
          error,
        );
      });
    }
    this.touch();
    void this.manager.persist();
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
    if (!this.lifecycle.ptyProcess || this.status !== "running" || this.lifecycle.releasePromise) {
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
    this.lifecycle.ptyProcess.write(data);
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
    const hasLivePty = this.lifecycle.ptyProcess && this.status === "running" && !this.lifecycle.releasePromise;
    if (!hasLivePty && this.reconciliationStatus !== "verified") {
      throw new Error(
        `终端尚待 owner 核实，不能调整尺寸: terminal_id=${this.id}, reconciliation_status=${this.reconciliationStatus}`,
      );
    }
    this.cols = cols;
    this.rows = rows;
    if (hasLivePty) {
      this.lifecycle.ptyProcess.resize(cols, rows);
    }
    this.touch();
    return true;
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
    if (this.lifecycle.releasePromise) {
      await this.lifecycle.releasePromise;
    }
    const isolatedPty = this.lifecycle.ptyProcess;
    this.outputMultiplexer.dispose();
    await isolatedPty?.shutdown({ terminatePty });
    this.lifecycle.ptyProcess = null;
  }

  async kill({ reason = "terminal_cancel" } = {}) {
    return await this.lifecycle.terminateForRelease({
      status: "terminated",
      commandStatus: "terminated",
      reason,
    });
  }

  async delete() {
    if (this.status === "deleted" && this.reconciliationStatus === "verified") {
      return;
    }
    await this.lifecycle.terminateForRelease({
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

  summary() {
    const record = this.toRecord();
    delete record.buffer;
    return {
      ...record,
      reconciliation_status: this.creationCancellationPending ? "unknown" : this.reconciliationStatus,
      pty_worker_pid: this.lifecycle.ptyProcess?.workerPid ?? null,
      client_count: this.outputMultiplexer.clientCount,
    };
  }

  snapshot() {
    return {
      ...this.summary(),
      buffer: this.buffer,
      display_buffer: stripCommandMarkerLines(this.buffer),
    };
  }
}
