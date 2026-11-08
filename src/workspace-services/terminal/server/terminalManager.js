import { createRuntimeLogger } from "../../../shared/serverRuntimeLogging.mjs";

import path from "node:path";
import { randomBytes } from "node:crypto";
import { validateWorkspaceId, verifyWorkspaceIdentity } from "./workspaceIdentity.js";
import { readProcessStat } from "./processes/terminalProcessLifecycle.js";
import {
  nowIso,
  resolveShell,
  resolveTerminalCwd,
  shellArgs,
  TerminalSession,
  validateTerminalRecord,
} from "./terminalSession.js";
import { TerminalStateStore } from "./terminalStateStore.js";

const logger = createRuntimeLogger("terminal", "terminal.terminalManager");

export const MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE = 64;
export const MAX_RETAINED_TERMINAL_HISTORY = 256;

export function matchesTerminalProcessIdentity(record, stat) {
  return Boolean(
    stat
    && Number(record.os_pid) === stat.pid
    && record.process_group_id !== null
    && record.process_group_id === stat.processGroupId
    && record.process_session_id !== null
    && record.process_session_id === stat.processSessionId
    && record.process_start_time !== null
    && record.process_start_time === stat.processStartTime,
  );
}

export function resolveWorkspaceRoot() {
  if (process.env.WORKSPACE_ROOT) {
    return path.resolve(process.env.WORKSPACE_ROOT);
  }
  throw new Error(
    "Terminal 插件启动必须由 Gateway 提供 WORKSPACE_ROOT。",
  );
}

/**
 * Node持有的一次性终端传输授权；领域关联的合法性由工作区owner核实。
 * @typedef {object} TerminalTransportGrant
 * @property {string} terminalId 被授权连接的稳定终端ID，不是PTY进程ID。
 * @property {number} transportOwnerRevision 发放时冻结的owner版本影子，仅用于连接撤权。
 * @property {unknown} association 领域owner传入的不透明关联，Node不据此决定资源生命周期。
 * @property {number} expiresAtUnixMs 授权过期的Unix毫秒时刻，不是存活时长。
 */

export class TerminalManager {
  constructor({
    workspaceRoot = resolveWorkspaceRoot(),
    workspaceId,
  } = {}) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.workspaceId = validateWorkspaceId(workspaceId);
    this.stateStore = new TerminalStateStore({
      workspaceRoot: this.workspaceRoot,
      workspaceId: this.workspaceId,
    });
    this.stateDir = this.stateStore.stateDir;
    this.stateFile = this.stateStore.stateFile;
    /** 按 terminal_id 保存终端资源，含保留的非运行记录，与聊天 Session/Thread 分开。
     * @type {Map<string, TerminalSession>}
     */
    this.terminalsById = new Map();
    this.capacityLock = Promise.resolve();
    this.closingRequested = false;
    this.shutdownPromise = null;
    this.shutdownStage = null;
    this.shutdownTerminalIds = new Set();
    this.persistRequested = false;
    this.persistPromise = null;
    /** @type {Map<string, TerminalTransportGrant>} */
    this.transportGrants = new Map();
  }

  async init() {
    await verifyWorkspaceIdentity(this.workspaceRoot, this.workspaceId);
    const records = await this.stateStore.readRecords();
    if (records === null) {
      await this.persist();
      return;
    }

    for (const record of records) {
      validateTerminalRecord(record, this.workspaceId);
      if (record.steering_dispatching) {
        // TODO: 兼容上次后端在 steering claim 后异常退出的恢复场景。
        record.steering_dispatching = false;
      }
      const restored = await this.restoreRecord(record);
      const session = new TerminalSession({ record: restored, manager: this });
      this.terminalsById.set(session.id, session);
    }
    await this.pruneTerminalHistory();
    await this.persist();
  }

  async restoreRecord(record) {
    if (record.status !== "running") {
      return record;
    }
    const pid = Number(record.os_pid);
    let stat = null;
    try {
      stat = await readProcessStat(pid);
    } catch (error) {
      logger.error(
        `[terminal-manager] 启动时核实进程身份失败: terminal_id=${record.terminal_id}, pid=${pid}`,
        error,
      );
    }
    const reconciliationStatus = matchesTerminalProcessIdentity(record, stat)
      ? "reconcile_required"
      : "unknown";
    if (record.reconciliation_status === reconciliationStatus) {
      return record;
    }
    return {
      ...record,
      reconciliation_status: reconciliationStatus,
      updated_at: nowIso(),
    };
  }

  async persist() {
    this.persistRequested = true;
    if (!this.persistPromise) {
      // 先登记共享Promise再取快照，同轮请求合并；同步异常也能释放本次保存。
      this.persistPromise = Promise.resolve().then(() => this.drainPersistRequests());
    }
    await this.persistPromise;
  }

  async drainPersistRequests() {
    try {
      while (this.persistRequested) {
        this.persistRequested = false;
        await this.stateStore.write({
          updated_at: nowIso(),
          terminals: [...this.terminalsById.values()].map((session) => session.toRecord()),
        });
      }
    } catch (error) {
      // 保留尚未落盘的当前状态，交下一次显式请求保存，不自动重试。
      this.persistRequested = true;
      throw error;
    } finally {
      this.persistPromise = null;
    }
  }

  list() {
    return [...this.terminalsById.values()]
      .map((session) => session.snapshot())
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  }

  get(id) {
    const session = this.terminalsById.get(id);
    if (!session) {
      throw new Error(`终端不存在: ${id}`);
    }
    return session;
  }

  assertCreationAdmission({ workspaceId, terminalId, ownerRevision }) {
    if (workspaceId !== this.workspaceId) {
      throw new Error(
        `终端工作区身份不匹配: expected=${this.workspaceId}, actual=${workspaceId}`,
      );
    }
    if (typeof terminalId !== "string" || !/^term_[a-f0-9]{32}$/.test(terminalId)) {
      throw new Error("Terminal 必须由 Python owner 提供 resource_id");
    }
    if (!Number.isSafeInteger(ownerRevision) || ownerRevision < 1 || this.terminalsById.has(terminalId)) {
      throw new Error(`Terminal owner revision 或身份冲突: ${terminalId}`);
    }
    if (this.closingRequested) {
      const error = new Error(`Terminal Manager 正在关闭，不能接纳创建: workspace_id=${this.workspaceId}`);
      error.code = "terminal_manager_closing";
      error.statusCode = 503;
      error.creationReceipt = {
        resource_id: terminalId, workspace_id: workspaceId, owner_revision: ownerRevision,
        outcome: "not_created",
      };
      throw error;
    }
  }

  async create({
    workspaceId,
    terminalId,
    ownerRevision,
    agentId,
    title = "Persistent Terminal",
    cwd = this.workspaceRoot,
    cols = 100,
    rows = 30,
    command = resolveShell(),
    args = shellArgs(),
  }) {
    this.assertCreationAdmission({ workspaceId, terminalId, ownerRevision });
    const previousLock = this.capacityLock;
    let releaseLock;
    this.capacityLock = new Promise((resolve) => {
      releaseLock = resolve;
    });
    await previousLock;
    try {
      return await this.createWithCapacity({
        workspaceId,
        terminalId,
        ownerRevision,
        agentId,
        title,
        cwd,
        cols,
        rows,
        command,
        args,
      });
    } finally {
      releaseLock();
    }
  }

  async createWithCapacity({
    workspaceId,
    terminalId,
    ownerRevision,
    agentId,
    title,
    cwd,
    cols,
    rows,
    command,
    args,
  }) {
    this.assertCreationAdmission({ workspaceId, terminalId, ownerRevision });
    let resolvedCwd;
    try {
      await this.pruneTerminalHistory();
      await this.ensureExecutionCapacity();
      resolvedCwd = await resolveTerminalCwd(this.workspaceRoot, cwd);
    } catch (error) {
      error.creationReceipt = {
        resource_id: terminalId, workspace_id: workspaceId, owner_revision: ownerRevision,
        outcome: "not_created",
      };
      throw error;
    }
    this.assertCreationAdmission({ workspaceId, terminalId, ownerRevision });
    // 按工作区根保存原生相对路径，允许 .. 保留外部 cwd；绝对路径只在调用栈内解析。
    const cwdRelative = path.relative(this.workspaceRoot, resolvedCwd);
    const id = terminalId;
    const session = new TerminalSession({
      manager: this,
      record: {
        terminal_id: id,
        workspace_id: this.workspaceId,
        transport_owner_revision: ownerRevision,
        reconciliation_status: "unknown",
        owner_agent_id: agentId || null,
        title,
        command,
        args,
        cwd_relative: cwdRelative,
        cols,
        rows,
        status: "created",
        created_at: nowIso(),
        updated_at: nowIso(),
      },
    });
    this.terminalsById.set(id, session);
    let startDispatched = false;
    try {
      // 原生创建前先发布原恢复记录；硬退出后不能留下没有资源身份的PTY。
      await this.persist();
      startDispatched = true;
      await session.start();
    } catch (error) {
      // 保留原身份与恢复记录；原owner核实进程树、Worker退出及终态落盘后才结清创建。
      await session.failStartup(error, { notCreated: !startDispatched });
    }
    await this.persist();
    return session.snapshot();
  }

  async write(id, data, { source = "agent", command = null } = {}) {
    const session = this.get(id);
    session.write(data, { source, command });
    await this.persist();
    return session.snapshot();
  }

  async cancelCreation(id, ownerRevision) {
    if (!/^term_[a-f0-9]{32}$/.test(id) || !Number.isSafeInteger(ownerRevision) || ownerRevision < 1) {
      throw new Error("Terminal取消创建缺少有效身份或版本");
    }
    let session = this.terminalsById.get(id);
    if (session && (session.releaseReason !== "creation_cancelled" || session.transportOwnerRevision !== ownerRevision)) {
      return { resource_id: id, workspace_id: this.workspaceId, owner_revision: ownerRevision,
        outcome: "present", resource: session.snapshot() };
    }
    if (!session) {
      const stamp = nowIso();
      session = new TerminalSession({ manager: this, record: {
        terminal_id: id, workspace_id: this.workspaceId, transport_owner_revision: ownerRevision,
        status: "deleted", reconciliation_status: "verified", release_reason: "creation_cancelled",
        cwd_relative: "", title: "已取消的创建", created_at: stamp, updated_at: stamp, ended_at: stamp,
      } });
      // 先阻止排队/解析中的创建；回执及verified快照须等同一writer真正发布。
      session.creationCancellationPending = true;
      this.terminalsById.set(id, session);
    }
    await this.persist();
    session.creationCancellationPending = false;
    return { resource_id: id, workspace_id: this.workspaceId, owner_revision: ownerRevision, outcome: "not_created" };
  }

  async syncOwnerRevision(id, transportOwnerRevision) {
    this.get(id).syncOwnerRevision(transportOwnerRevision);
    for (const [token, grant] of this.transportGrants) {
      if (grant.terminalId === id && grant.transportOwnerRevision !== transportOwnerRevision) {
        this.transportGrants.delete(token);
      }
    }
    await this.persist();
  }

  issueTransportGrant(id, { ownerRevision, association }) {
    const terminal = this.get(id);
    terminal.assertPtyConnected("transport-grant");
    if (terminal.transportOwnerRevision !== ownerRevision) {
      throw new Error(`Terminal transport revision 冲突: ${id}`);
    }
    // 只保存短期传输授权，Thread 关联的合法性已由 Python owner 核实。
    const token = randomBytes(32).toString("base64url");
    const expiresAtUnixMs = Date.now() + 30_000;
    for (const [existing, grant] of this.transportGrants) {
      if (grant.expiresAtUnixMs <= Date.now()) this.transportGrants.delete(existing);
    }
    this.transportGrants.set(token, {
      terminalId: id, transportOwnerRevision: ownerRevision, association, expiresAtUnixMs,
    });
    return {
      resource_id: id, owner_revision: ownerRevision,
      grant_token: token, expires_at: new Date(expiresAtUnixMs).toISOString(),
    };
  }

  consumeTransportGrant(id, token) {
    const grant = this.transportGrants.get(token);
    this.transportGrants.delete(token);
    if (!grant || grant.terminalId !== id || grant.expiresAtUnixMs <= Date.now()
      || this.get(id).transportOwnerRevision !== grant.transportOwnerRevision) {
      throw new Error("Terminal 一次传输授权无效、过期或已消费");
    }
    return grant;
  }

  async read(id) {
    return await this.get(id).readModelOutput();
  }

  async markModelBackgrounded(id) {
    const session = this.get(id);
    session.markModelBackgrounded();
    await this.persist();
    return session.snapshot();
  }

  async claimSteering(id, identity) {
    const session = this.get(id);
    const claimed = session.claimSteering(identity);
    if (claimed) {
      await this.persist();
    }
    return { claimed, terminal: session.snapshot() };
  }

  async finishSteering(id, { completionEventId, commandStartSequence, dispatched }) {
    const session = this.get(id);
    session.finishSteering({ completionEventId, commandStartSequence, dispatched });
    await this.persist();
    return { terminal: session.snapshot() };
  }

  async ensureExecutionCapacity() {
    const active = [...this.terminalsById.values()]
      .filter(
        (session) => session.status === "running" || session.status === "created",
      );
    if (active.length < MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE) {
      return;
    }

    // PTY 进程上限只阻断创建；停止/删除由 Python owner 核对关联与 lease 后决定。
    throw new Error(
      `工作区终端进程已达到上限: workspace_id=${this.workspaceId}, limit=${MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE}；请通过工作区 Terminal owner 停止资源`,
    );
  }

  async pruneTerminalHistory() {
    const historical = [...this.terminalsById.values()]
      .filter((session) => session.status === "deleted" && session.reconciliationStatus === "verified"
        && session.releaseReason !== "creation_cancelled")
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    const removed = historical.slice(MAX_RETAINED_TERMINAL_HISTORY);
    for (const session of removed) {
      await session.dispose();
      this.terminalsById.delete(session.id);
    }
    if (removed.length > 0) {
      await this.persist();
    }
  }

  async resize(id, cols, rows) {
    const session = this.get(id);
    const resized = session.resize(cols, rows);
    if (resized || this.persistRequested) {
      await this.persist();
    } else if (this.persistPromise) {
      await this.persistPromise;
    }
    return session.snapshot();
  }

  async kill(id, { reason = "terminal_cancel" } = {}) {
    const session = this.get(id);
    session.touchUsage();
    const killed = await session.kill({ reason });
    await this.persist();
    return { killed, terminal: session.snapshot() };
  }

  async delete(id) {
    const session = this.get(id);
    await session.delete();
    await this.persist();
    return { deleted: true, terminal_id: id, terminal: session.snapshot() };
  }

  async shutdown(reason = "terminal_manager_shutdown", { shutdownWaitTimeoutMs = 5000 } = {}) {
    this.closingRequested = true;
    if (!this.shutdownPromise) {
      // 原容量队尾持有所有已接纳创建；观察超时不取消它或另起一批关闭。
      const execution = Promise.resolve().then(() => this.drainShutdown(reason)).catch((error) => {
        this.shutdownPromise = null;
        try {
          logger.withContext({ workspace_id: this.workspaceId }).error(
            "[terminal-manager] 原关闭任务失败，保留未完成资源供显式重试",
            { stage: this.shutdownStage, terminal_ids: [...this.terminalsById.keys()] }, error,
          );
        } catch (diagnosticError) {
          throw new AggregateError([error, diagnosticError], "Terminal关闭与诊断输出均失败", { cause: error });
        }
        throw error;
      });
      this.shutdownPromise = execution;
      // 观察者可能已到期；原执行失败仍由上面记录，不产生悬空rejection。
      void execution.catch(() => undefined);
    }
    let timer;
    try {
      await Promise.race([this.shutdownPromise, new Promise((_, reject) => {
        timer = setTimeout(() => {
          const pendingTerminalIds = this.shutdownStage === "creations"
            ? [...this.terminalsById.values()].filter((session) => session.status === "created").map((session) => session.id)
            : [...this.shutdownTerminalIds];
          const error = new Error(`Terminal Manager 关闭尚未完成，保留原任务供核实: workspace_id=${this.workspaceId}, stage=${this.shutdownStage}, terminals=${pendingTerminalIds.join(",")}`);
          error.code = "terminal_manager_shutdown_timeout";
          error.stage = this.shutdownStage;
          error.pending_terminal_ids = pendingTerminalIds;
          reject(error);
        }, shutdownWaitTimeoutMs);
      })]);
    } finally {
      clearTimeout(timer);
    }
  }

  async drainShutdown(reason) {
    this.shutdownStage = "creations";
    await this.capacityLock;
    const unverified = [...this.terminalsById.values()].filter((session) => session.status === "created"
      || (session.releaseReason === "terminal_startup_failed" && session.reconciliationStatus !== "verified"));
    if (unverified.length > 0) {
      const error = new Error(`Terminal 创建收尾尚未核实: ${unverified.map((session) => session.id).join(",")}`);
      error.code = "terminal_shutdown_unverified_creation";
      throw error;
    }
    // 收齐所有结果才释放原关闭任务，首个失败不能让重试越过仍在途的其它资源。
    this.shutdownStage = "resources";
    const sessions = [...this.terminalsById.values()];
    this.shutdownTerminalIds = new Set(sessions.map((session) => session.id));
    const results = await Promise.allSettled(sessions.map(async (session) => {
      try {
        if (session.status === "running") {
          session.markReconciliationStatus("reconcile_required");
          session.releaseReason = reason;
          await session.dispose({ terminatePty: false });
        } else {
          await session.dispose();
        }
      } catch (error) {
        session.markReconciliationStatus("unknown");
        throw error;
      } finally {
        this.shutdownTerminalIds.delete(session.id);
      }
    }));
    const failures = results.filter((result) => result.status === "rejected").map((result) => result.reason);
    this.shutdownStage = "persist";
    try {
      await this.persist();
    } catch (error) {
      failures.push(error);
    }
    if (failures.length === 1) throw failures[0];
    if (failures.length > 1) throw new AggregateError(failures, "Terminal Manager 关闭失败", { cause: failures[0] });
    this.shutdownStage = "complete";
  }
}
