import { createRuntimeLogger } from "@boxteam/plugin-sdk/logging";
import { readProcessStat, terminateProcessTree } from "@boxteam/plugin-sdk/process-lifecycle";
import { IsolatedPtyProcess } from "./isolatedPtyProcess.js";
import { resolveTerminalCwd, recordTerminalFailure } from "./terminalEnvironment.js";

const logger = createRuntimeLogger("terminal", "terminal.terminalSession");

export function nowIso() {
  return new Date().toISOString();
}

/** PTY 启停协调；终端业务记录仍只由原 Session 持有。 */
export class TerminalPtyLifecycle {
  constructor(session) {
    this.session = session;
    this.ptyProcess = null;
    this.releasePromise = null;
  }

  async start() {
    if (this.ptyProcess) {
      return;
    }

    const isolatedPty = new IsolatedPtyProcess();
    this.ptyProcess = isolatedPty;
    isolatedPty.onData(data => this.session.handlePtyOutput(data));
    isolatedPty.onExit((event) => this.handlePtyExit(event).catch(async (error) => {
      this.session.status = "exited";
      this.session.endedAt = nowIso();
      this.session.markReconciliationStatus("unknown");
      if (this.ptyProcess?.workerState === "exited") this.ptyProcess = null;
      this.session.releaseReason = "pty_exit_cleanup_failed";
      if (this.session.lastCommandStatus === "running") {
        this.session.lastCommandStatus = "exited";
        this.session.lastCommandCompletedAt = this.session.endedAt;
      }
      this.session.touch();
      const failure = recordTerminalFailure(error,
        `[terminal-session] PTY 退出清理失败: terminal_id=${this.session.id}`,
        error,
      );
      try {
        this.session.broadcast({
          type: "exit",
          terminalId: this.session.id,
          exitCode: event.exitCode,
          signal: event.signal,
          error: this.session.releaseReason,
          timestamp: this.session.endedAt,
        });
      } catch (broadcastError) {
        throw new AggregateError([failure, broadcastError],
          `终端退出失败状态广播失败: terminal_id=${this.session.id}`, { cause: error });
      }
      try {
        await this.session.manager.persist();
      } catch (persistError) {
        const persistFailure = recordTerminalFailure(persistError,
          `[terminal-session] PTY 退出失败状态持久化失败: terminal_id=${this.session.id}`,
          persistError,
        );
        if (failure !== error || persistFailure !== persistError) {
          throw new AggregateError([failure, persistFailure],
            `终端退出状态持久化及诊断失败: terminal_id=${this.session.id}`, { cause: error });
        }
      }
      if (failure !== error) throw failure;
    }));
    let resolvedCwd = null;
    try {
      resolvedCwd = await resolveTerminalCwd(this.session.manager.workspaceRoot, this.session.cwdRelative);
      await isolatedPty.start({
        command: this.session.command,
        args: this.session.args,
        options: {
          name: "xterm-256color",
          cwd: resolvedCwd,
          cols: this.session.cols,
          rows: this.session.rows,
          env: {
            ...process.env,
            TERM: "xterm-256color",
          },
        },
      });
    } catch (error) {
      const cause = recordTerminalFailure(error,
        `[terminal-session] 终端 PTY 启动失败: terminal_id=${this.session.id}`,
        { cwd: resolvedCwd ?? this.session.cwd, command: this.session.command },
        error,
      );
      throw new Error(
        `终端 PTY 启动失败: terminal_id=${this.session.id}`,
        { cause },
      );
    }
    this.session.status = "running";
    this.session.startedAt = this.session.startedAt || nowIso();
    this.capturePtyIdentity(isolatedPty);
    this.session.markReconciliationStatus("verified");
    this.session.touch();
    await this.session.manager.persist();
  }

  async handlePtyExit({
    exitCode,
    signal,
    workerExit,
    cleanupResult = null,
    error,
  }) {
    if (this.releasePromise) {
      this.session.exitCode = exitCode;
      this.session.signal = signal;
      return;
    }
    const wasRunning = this.session.status === "running";
    if (this.session.status !== "terminated" && this.session.status !== "deleted") {
      this.session.status = "exited";
    }
    this.session.exitCode = exitCode;
    this.session.signal = signal;
    this.session.endedAt = nowIso();
    if (workerExit || cleanupResult === "still_running" || cleanupResult === "cleanup_failed") {
      this.session.markReconciliationStatus("unknown");
      let diagnosticFailure = null;
      if (error) {
        const failure = recordTerminalFailure(error, `[terminal-session] PTY Worker 失败: terminal_id=${this.session.id}`, error);
        if (failure !== error) diagnosticFailure = failure;
      }
      this.session.releaseReason = error
        ? "pty_worker_error"
        : workerExit
          ? "pty_worker_exit"
          : `pty_cleanup_${cleanupResult}`;
      if (wasRunning && this.session.osPid) {
        try {
          const managerCleanupResult = await terminateProcessTree({
            pid: this.session.osPid,
            processSessionId: this.session.processSessionId,
            processStartTime: this.session.processStartTime,
          });
          if (managerCleanupResult === "still_running") {
            throw new Error(
              `PTY Worker 退出后进程树仍在运行: terminal_id=${this.session.id}, pid=${this.session.osPid}`,
            );
          }
        } catch (cleanupError) {
          if (diagnosticFailure) {
            throw new AggregateError([diagnosticFailure, cleanupError],
              `终端退出清理及诊断失败: terminal_id=${this.session.id}`, { cause: diagnosticFailure });
          }
          throw cleanupError;
        }
      }
      if (diagnosticFailure) throw diagnosticFailure;
    }
    // PTY退出观察不证明隔离宿主已退出；失败时仍由原管理停止链持有句柄。
    const reconciliationStatus = this.session.reconciliationStatus;
    this.session.markReconciliationStatus("unknown");
    await this.ptyProcess?.shutdown({ terminatePty: false });
    if (this.releasePromise) return;
    this.session.markReconciliationStatus(reconciliationStatus);
    this.ptyProcess = null;
    if (this.session.lastCommandStatus === "running") {
      this.session.lastCommandStatus = this.session.status === "terminated" ? "terminated" : "exited";
      this.session.lastCommandCompletedAt = this.session.endedAt;
    }
    this.session.touch();
    this.session.broadcast({
      type: "exit",
      terminalId: this.session.id,
      exitCode,
      signal,
      timestamp: this.session.endedAt,
    });
    await this.session.manager.persist();
  }

  async terminateForRelease({ status, commandStatus, reason }) {
    if (this.releasePromise) {
      return await this.releasePromise;
    }
    const hasPersistedIdentity = Number.isSafeInteger(this.session.osPid) && this.session.osPid > 0
      && this.session.processSessionId && this.session.processStartTime !== null;
    const retainedRelease = (this.ptyProcess || hasPersistedIdentity) && this.session.reconciliationStatus !== "verified"
      && new Set(["created", "completed", "exited", "terminated", "deleted"]).has(this.session.status);
    if (this.session.status !== "running" && !retainedRelease) {
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
      this.capturePtyIdentity(this.ptyProcess);
      this.session.markReconciliationStatus("unknown");
      try {
        await this.session.manager.persist();
      } catch (persistError) {
        throw new AggregateError([error, persistError],
          `终端停止失败且未知状态持久化失败: terminal_id=${this.session.id}`, { cause: error });
      }
      throw error;
    } finally {
      if (this.releasePromise === releasePromise) {
        this.releasePromise = null;
      }
    }
  }

  async _terminateForRelease({ status, commandStatus, reason }) {
    const pid = this.session.osPid || this.ptyProcess?.pid;
    const isolatedPty = this.ptyProcess;
    if (this.session.status === "created" && (!Number.isSafeInteger(pid) || pid <= 0)) {
      throw new Error(`终端原生创建身份尚未核实: terminal_id=${this.session.id}`);
    }
    if (!isolatedPty) {
      await this.verifyPersistedProcessIdentity(pid);
    }
    const result = await terminateProcessTree({
      pid,
      processSessionId: this.session.processSessionId,
      processStartTime: this.session.processStartTime,
    });
    if (result === "still_running") {
      this.session.markReconciliationStatus("unknown");
      throw new Error(
        `终端进程树清理失败: terminal_id=${this.session.id}, pid=${pid}`,
      );
    }
    if (!new Set(["terminated", "force_killed", "missing"]).has(result)) {
      this.session.markReconciliationStatus("unknown");
      throw new Error(
        `终端进程停止结果无法核实: terminal_id=${this.session.id}, result=${result}`,
      );
    }
    // 核实 Worker 退出及终态落盘前保留原句柄，失败重试仍操作同一个执行。
    await isolatedPty?.shutdown({ terminatePty: false });
    this.session.markReconciliationStatus("verified");
    this.session.status = status;
    this.session.endedAt = nowIso();
    this.session.signal = result === "force_killed" ? "SIGKILL" : "SIGTERM";
    this.session.releaseReason = reason;
    if (this.session.lastCommandStatus === "running") {
      this.session.lastCommandStatus = commandStatus;
      this.session.lastCommandCompletedAt = this.session.endedAt;
    }
    await this.session.manager.persist();
    this.ptyProcess = null;
    this.session.closeTransports("terminal stopped");
    this.session.broadcast({
      type: status === "deleted" ? "deleted" : "exit",
      terminalId: this.session.id,
      exitCode: this.session.exitCode,
      signal: this.session.signal,
      timestamp: this.session.endedAt,
      snapshot: this.session.snapshot(),
    });
    return true;
  }

  async verifyPersistedProcessIdentity(pid) {
    if (
      !Number.isInteger(pid)
      || !this.session.processSessionId
      || this.session.processStartTime === null
    ) {
      this.session.markReconciliationStatus("unknown");
      throw new Error(
        `终端持久进程身份不完整，拒绝停止或删除: terminal_id=${this.session.id}`,
      );
    }
    const stat = await readProcessStat(pid);
    // Linux 根退出后仍须沿原 SID 核实后代；缺根不能代替整棵进程树的停止证明。
    if (!stat && process.platform === "linux" && this.session.processSessionId === pid && this.session.processGroupId === pid) {
      return;
    }
    if (
      !stat
      || stat.processSessionId !== this.session.processSessionId
      || stat.processStartTime !== this.session.processStartTime
      || stat.processGroupId !== this.session.processGroupId
    ) {
      this.session.markReconciliationStatus("unknown");
      throw new Error(
        `终端持久进程身份无法核实，拒绝停止或删除: terminal_id=${this.session.id}, pid=${pid}`,
      );
    }
  }

  capturePtyIdentity(isolatedPty) {
    if (!isolatedPty) return false;
    const pid = isolatedPty.pid ?? this.session.osPid;
    const identity = isolatedPty.processIdentity;
    const group = identity?.processGroupId ?? this.session.processGroupId ?? pid;
    const session = identity?.processSessionId ?? this.session.processSessionId ?? (process.platform === "linux" ? pid : null);
    const started = identity?.processStartTime ?? this.session.processStartTime;
    const changed = this.session.osPid !== pid || this.session.processGroupId !== group
      || this.session.processSessionId !== session || this.session.processStartTime !== started;
    this.session.osPid = pid;
    this.session.processGroupId = group;
    this.session.processSessionId = session;
    this.session.processStartTime = started;
    return changed;
  }

  async failStartup(error, { notCreated = false } = {}) {
    if (notCreated) {
      // 只有仍在场的创建调用能证明尚未调用start；冷记录缺少PID不能套用此结论。
      this.session.status = "deleted";
      this.session.endedAt = nowIso();
      this.session.releaseReason = "terminal_creation_not_dispatched";
      this.session.markReconciliationStatus("verified");
      try {
        await this.session.manager.persist();
      } catch (persistError) {
        this.session.status = "created";
        this.session.endedAt = null;
        this.session.markReconciliationStatus("unknown");
        throw new AggregateError([error, persistError],
          `终端未派发创建的收尾持久化失败: terminal_id=${this.session.id}`, { cause: error });
      }
      error.creationReceipt = {
        resource_id: this.session.id, workspace_id: this.session.workspaceId,
        owner_revision: this.session.transportOwnerRevision, outcome: "not_created",
      };
      throw error;
    }
    const isolatedPty = this.ptyProcess;
    // TODO（5.2）：native 尚未交付 PID 的阻塞仍须由原 owner 接管，不能凭 Worker 退出结清。
    let stopped = isolatedPty !== null && new Set(["missing", "terminated", "force_killed"])
      .has(isolatedPty.startupCleanupResult);
    this.capturePtyIdentity(isolatedPty);
    this.session.markReconciliationStatus("unknown");
    this.session.releaseReason = "terminal_startup_failed";
    await this.session.manager.persist();
    try {
      if (!stopped && Number.isSafeInteger(this.session.osPid) && this.session.osPid > 0) {
        stopped = await this.terminateForRelease({
          status: "terminated", commandStatus: "terminated", reason: "terminal_startup_failed",
        });
      } else {
        await this.session.dispose();
        // 收尾期间可取得同一Worker迟到的原生身份和正式停止回执。
        this.capturePtyIdentity(isolatedPty);
        stopped = isolatedPty !== null && new Set(["missing", "terminated", "force_killed"])
          .has(isolatedPty.startupCleanupResult);
      }
    } catch (cleanupError) {
      if (this.capturePtyIdentity(isolatedPty)) {
        try {
          await this.session.manager.persist();
        } catch (persistError) {
          cleanupError = new AggregateError([cleanupError, persistError],
            "终端启动清理及迟到身份持久化均失败", { cause: cleanupError });
        }
      }
      const failure = new AggregateError([error, cleanupError], `终端启动失败且清理未完成: terminal_id=${this.session.id}`, { cause: error });
      throw recordTerminalFailure(failure, `[terminal-session] 启动失败清理未完成: terminal_id=${this.session.id}`, cleanupError);
    }
    if (stopped && this.session.status !== "terminated") {
      this.session.status = "terminated";
      this.session.endedAt = nowIso();
      this.session.markReconciliationStatus("verified");
      await this.session.manager.persist();
    } else if (!stopped) {
      await this.session.manager.persist();
    }
    if (stopped) error.creationReceipt = {
      resource_id: this.session.id, workspace_id: this.session.workspaceId, owner_revision: this.session.transportOwnerRevision,
      outcome: "failed", resource: this.session.snapshot(),
    };
    throw error;
  }
}
