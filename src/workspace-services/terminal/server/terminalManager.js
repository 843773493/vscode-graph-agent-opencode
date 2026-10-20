import path from "node:path";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { readProcessStat } from "./terminalProcessUtils.js";
import {
  nowIso,
  resolveShell,
  resolveTerminalCwd,
  shellArgs,
  TerminalSession,
  validateTerminalRecord,
} from "./terminalSession.js";
import { TerminalStateStore } from "./terminalStateStore.js";

export const MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE = 64;
export const MAX_RETAINED_TERMINAL_HISTORY = 256;
const WORKSPACE_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function validateWorkspaceId(workspaceId) {
  if (typeof workspaceId !== "string" || !WORKSPACE_ID_PATTERN.test(workspaceId)) {
    throw new Error("TerminalManager 必须显式提供标准 UUID workspace_id");
  }
  return workspaceId;
}

export async function verifyWorkspaceIdentity(workspaceRoot, workspaceId) {
  const identityPath = path.join(workspaceRoot, ".boxteam", "workspace-identity.json");
  const identity = JSON.parse(await readFile(identityPath, "utf8"));
  const actual = validateWorkspaceId(identity.workspace_id);
  if (actual !== workspaceId) {
    throw new Error(`Terminal 工作区 UUID 与权威身份文件不一致: expected=${workspaceId}, actual=${actual}, identity=${identityPath}`);
  }
}

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
  if (process.env.BOXTEAM_TERMINAL_WORKSPACE_ROOT) {
    return path.resolve(process.env.BOXTEAM_TERMINAL_WORKSPACE_ROOT);
  }
  if (process.env.WORKSPACE_ROOT) {
    return path.resolve(process.env.WORKSPACE_ROOT);
  }
  throw new Error(
    "TerminalManager 启动必须显式提供 workspace root："
      + "请传入 --workspace-root、BOXTEAM_TERMINAL_WORKSPACE_ROOT 或 WORKSPACE_ROOT。",
  );
}

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
    this.sessions = new Map();
    this.capacityLock = Promise.resolve();
    this.persistRequested = false;
    this.persistPromise = null;
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
      this.sessions.set(session.id, session);
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
      console.error(
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
      this.persistPromise = this.drainPersistRequests();
    }
    await this.persistPromise;
  }

  async drainPersistRequests() {
    try {
      while (this.persistRequested) {
        this.persistRequested = false;
        await this.stateStore.write({
          updated_at: nowIso(),
          terminals: [...this.sessions.values()].map((session) => session.toRecord()),
        });
      }
    } finally {
      this.persistPromise = null;
    }
  }

  list() {
    return [...this.sessions.values()]
      .map((session) => session.snapshot())
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  }

  get(id) {
    const session = this.sessions.get(id);
    if (!session) {
      throw new Error(`终端不存在: ${id}`);
    }
    return session;
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
    if (workspaceId !== this.workspaceId) {
      throw new Error(
        `终端工作区身份不匹配: expected=${this.workspaceId}, actual=${workspaceId}`,
      );
    }
    if (typeof terminalId !== "string" || !/^term_[a-f0-9]{32}$/.test(terminalId)) {
      throw new Error("Terminal 必须由 Python owner 提供 resource_id");
    }
    if (!Number.isSafeInteger(ownerRevision) || ownerRevision < 1 || this.sessions.has(terminalId)) {
      throw new Error(`Terminal owner revision 或身份冲突: ${terminalId}`);
    }
    await this.pruneTerminalHistory();
    await this.ensureExecutionCapacity();
    // 按工作区根保存原生相对路径，允许 .. 保留外部 cwd；绝对路径只在调用栈内解析。
    const resolvedCwd = resolveTerminalCwd(this.workspaceRoot, cwd);
    const cwdRelative = path.relative(this.workspaceRoot, resolvedCwd);
    const id = terminalId;
    const session = new TerminalSession({
      manager: this,
      record: {
        terminal_id: id,
        workspace_id: this.workspaceId,
        transport_owner_revision: ownerRevision,
        reconciliation_status: "verified",
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
    this.sessions.set(id, session);
    try {
      await session.start();
    } catch (error) {
      this.sessions.delete(id);
      await session.dispose();
      await this.persist();
      throw error;
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

  async syncOwnerRevision(id, revision) {
    this.get(id).syncOwnerRevision(revision);
    for (const [token, grant] of this.transportGrants) {
      if (grant.terminalId === id && grant.revision !== revision) {
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
    const expiresAt = Date.now() + 30_000;
    for (const [existing, grant] of this.transportGrants) {
      if (grant.expiresAt <= Date.now()) this.transportGrants.delete(existing);
    }
    this.transportGrants.set(token, {
      terminalId: id, revision: ownerRevision, association, expiresAt,
    });
    return {
      resource_id: id, owner_revision: ownerRevision,
      grant_token: token, expires_at: new Date(expiresAt).toISOString(),
    };
  }

  consumeTransportGrant(id, token) {
    const grant = this.transportGrants.get(token);
    this.transportGrants.delete(token);
    if (!grant || grant.terminalId !== id || grant.expiresAt <= Date.now()
      || this.get(id).transportOwnerRevision !== grant.revision) {
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
    const active = [...this.sessions.values()]
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
    const historical = [...this.sessions.values()]
      .filter((session) => session.status === "deleted")
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    const removed = historical.slice(MAX_RETAINED_TERMINAL_HISTORY);
    for (const session of removed) {
      await session.dispose();
      this.sessions.delete(session.id);
    }
    if (removed.length > 0) {
      await this.persist();
    }
  }

  async resize(id, cols, rows) {
    const session = this.get(id);
    const resized = session.resize(cols, rows);
    if (resized) {
      await this.persist();
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

  async shutdown(reason = "terminal_manager_shutdown") {
    await Promise.all([...this.sessions.values()].map(async (session) => {
      if (session.status === "running") {
        session.markReconciliationStatus("reconcile_required");
        session.releaseReason = reason;
        await session.dispose({ terminatePty: false });
        return;
      }
      await session.dispose();
    }));
    await this.persist();
  }
}
