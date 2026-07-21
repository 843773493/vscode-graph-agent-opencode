import path from "node:path";
import { randomUUID } from "node:crypto";
import {
  readProcessStat,
  terminateTerminalProcessTree,
} from "./terminalProcessUtils.js";
import {
  nowIso,
  resolveShell,
  shellArgs,
  TerminalSession,
} from "./terminalSession.js";
import { TerminalStateStore } from "./terminalStateStore.js";

function terminalId() {
  return `term_${randomUUID().replaceAll("-", "")}`;
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
    terminalFrontendBaseUrl = "http://127.0.0.1:8013",
  } = {}) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.stateStore = new TerminalStateStore({ workspaceRoot: this.workspaceRoot });
    this.stateDir = this.stateStore.stateDir;
    this.stateFile = this.stateStore.stateFile;
    this.terminalFrontendBaseUrl = terminalFrontendBaseUrl.replace(/\/$/, "");
    this.sessions = new Map();
    this.persistRequested = false;
    this.persistPromise = null;
  }

  async init() {
    const records = await this.stateStore.readRecords();
    if (records === null) {
      await this.persist();
      return;
    }

    for (const record of records) {
      const restored = await this.restoreRecord(record);
      const session = new TerminalSession({ record: restored, manager: this });
      this.sessions.set(session.id, session);
    }
    await this.persist();
  }

  async restoreRecord(record) {
    if (record.status !== "running") {
      return record;
    }

    const restoredAt = nowIso();
    const pid = Number(record.os_pid);
    const stat = await readProcessStat(pid);
    const processSessionId = record.process_session_id ?? stat?.processSessionId ?? null;
    const processStartTime = record.process_start_time ?? stat?.processStartTime ?? null;
    const cleanupResult = await terminateTerminalProcessTree({
      pid,
      processSessionId,
      processStartTime,
    });

    if (cleanupResult === "still_running") {
      throw new Error(
        `终端管理器启动清理失败: terminal_id=${record.terminal_id}, pid=${pid}`,
      );
    }

    const restored = {
      ...record,
      status: "terminated",
      updated_at: restoredAt,
      ended_at: record.ended_at || restoredAt,
      signal: cleanupResult === "force_killed" ? "SIGKILL" : "SIGTERM",
      process_group_id: record.process_group_id ?? stat?.processGroupId ?? null,
      process_session_id: processSessionId,
      process_start_time: processStartTime,
      release_reason: "terminal_manager_startup_cleanup",
    };
    if (restored.last_command_status === "running") {
      restored.last_command_status = "terminated";
      restored.last_command_completed_at = restored.ended_at;
    }
    return restored;
  }

  attachUrl(id) {
    return `${this.terminalFrontendBaseUrl}/?terminalId=${encodeURIComponent(id)}`;
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
          workspace_root: this.workspaceRoot,
          updated_at: nowIso(),
          terminals: [...this.sessions.values()].map((session) => session.toRecord()),
        });
      }
    } finally {
      this.persistPromise = null;
    }
  }

  list({ sessionId = null } = {}) {
    return [...this.sessions.values()]
      .filter((session) => !sessionId || session.sessionId === sessionId)
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
    sessionId,
    title = "Persistent Terminal",
    cwd = this.workspaceRoot,
    cols = 100,
    rows = 30,
    command = resolveShell(),
    args = shellArgs(),
  }) {
    if (!sessionId) {
      throw new Error("session_id 不能为空");
    }
    const resolvedCwd = path.resolve(cwd);
    const id = terminalId();
    const session = new TerminalSession({
      manager: this,
      record: {
        terminal_id: id,
        session_id: sessionId,
        title,
        command,
        args,
        cwd: resolvedCwd,
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

  async resize(id, cols, rows) {
    const session = this.get(id);
    const resized = session.resize(cols, rows);
    if (resized) {
      await this.persist();
    }
    return session.snapshot();
  }

  async kill(id) {
    const session = this.get(id);
    const killed = await session.kill();
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
        await session.terminateForRelease({
          status: "terminated",
          commandStatus: "terminated",
          reason,
        });
        return;
      }
      await session.dispose();
    }));
    await this.persist();
  }
}
