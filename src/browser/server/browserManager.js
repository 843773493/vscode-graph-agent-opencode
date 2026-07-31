import path from "node:path";
import { randomUUID } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import { BrowserSession } from "./browserSession.js";
import { BrowserStateStore } from "./browserStateStore.js";
import { normalizeBrowserUrl, nowIso } from "./url.js";
import { BrowserRuntimePool } from "./resources/browserRuntimePool.js";
import { BrowserResourceGovernor } from "./resources/resourceGovernor.js";

function browserId() {
  return `browser_${randomUUID().replaceAll("-", "")}`;
}

const DEFAULT_MAX_LOGICAL_BROWSERS_PER_SESSION = 500;
const DEFAULT_CHECKPOINT_RESERVATION_BYTES = 4 * 1024 * 1024;

function checkpointPageSummaries(checkpoint) {
  return checkpoint.pages.map((page) => ({
    page_id: page.page_id,
    title: page.title || "无标题",
    url: page.requested_url || page.url,
    actual_url: page.url,
    navigation_error: page.navigation_error || null,
    active: page.page_id === checkpoint.active_page_id,
    created_at: page.created_at,
  }));
}

export function resolveWorkspaceRoot() {
  if (process.env.BOXTEAM_BROWSER_WORKSPACE_ROOT) {
    return path.resolve(process.env.BOXTEAM_BROWSER_WORKSPACE_ROOT);
  }
  if (process.env.WORKSPACE_ROOT) {
    return path.resolve(process.env.WORKSPACE_ROOT);
  }
  throw new Error(
    "BrowserManager 启动必须显式提供 workspace root："
      + "请传入 --workspace-root、BOXTEAM_BROWSER_WORKSPACE_ROOT 或 WORKSPACE_ROOT。",
  );
}

export function resolveRequiredWorkspaceRoot(args) {
  const raw = args.has("workspace-root")
    ? args.get("workspace-root")
    : resolveWorkspaceRoot();
  if (typeof raw !== "string" || raw.trim() === "" || raw === "true") {
    throw new Error("--workspace-root 必须提供有效路径值");
  }
  const resolved = path.resolve(raw);
  if (!existsSync(resolved) || !statSync(resolved).isDirectory()) {
    throw new Error(`--workspace-root 必须指向已存在的目录: ${resolved}`);
  }
  return resolved;
}

export class BrowserManager {
  constructor({
    workspaceRoot = resolveWorkspaceRoot(),
    browserFrontendBaseUrl = "http://127.0.0.1:8016",
    runtimePool = null,
    maxLogicalBrowsersPerSession = DEFAULT_MAX_LOGICAL_BROWSERS_PER_SESSION,
    checkpointReservationBytes = DEFAULT_CHECKPOINT_RESERVATION_BYTES,
  } = {}) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.stateStore = new BrowserStateStore({ workspaceRoot: this.workspaceRoot });
    this.browserFrontendBaseUrl = browserFrontendBaseUrl.replace(/\/$/, "");
    this.sessions = new Map();
    this.persistTail = Promise.resolve();
    this.persistTimer = null;
    this.createAdmissionTail = Promise.resolve();
    this.checkpointCaptureTail = Promise.resolve();
    this.runtimePool = runtimePool || new BrowserRuntimePool({
      onDisconnect: (generation) => this.handleRuntimeDisconnect(generation),
    });
    this.resourceGovernor = null;
    this.maxLogicalBrowsersPerSession = maxLogicalBrowsersPerSession;
    this.checkpointReservationBytes = checkpointReservationBytes;
  }

  startResourceGovernor(options = {}) {
    if (this.resourceGovernor) return this.resourceGovernor.snapshot();
    this.resourceGovernor = new BrowserResourceGovernor({ manager: this, ...options });
    this.resourceGovernor.start();
    return this.resourceGovernor.snapshot();
  }

  stopResourceGovernor() {
    this.resourceGovernor?.stop();
  }

  resourceGovernorSnapshot() {
    return this.resourceGovernor?.snapshot() || {
      running: false,
      last_sample: null,
      last_action: null,
      error: null,
    };
  }

  runningSessions() {
    return [...this.sessions.values()].filter((session) => session.status === "running");
  }

  async runCheckpointCapture(callback) {
    const execution = this.checkpointCaptureTail.then(callback);
    this.checkpointCaptureTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  handleRuntimeDisconnect(generation) {
    for (const session of this.runningSessions()) {
      if (session.runtimeGeneration === generation) {
        void session.handleRuntimeDisconnect(generation);
      }
    }
  }

  async init() {
    const records = await this.stateStore.readRecords();
    if (records === null) {
      await this.persist();
      return;
    }
    for (const rawRecord of records) {
      if (!rawRecord || typeof rawRecord !== "object") {
        throw new Error(`浏览器状态文件包含非对象记录: ${this.stateStore.stateFile}`);
      }
      const record = { ...rawRecord };
      const lockExpiresAt = Date.parse(record.agent_lock_expires_at || "");
      if (record.agent_access_locked === true
        && (!Number.isFinite(lockExpiresAt) || lockExpiresAt <= Date.now())) {
        record.agent_access_locked = false;
        record.agent_lock_owner_id = null;
        record.agent_lock_expires_at = null;
      }
      const canRecoverFromCheckpoint = (
        record.status === "running"
        || (record.status === "lost" && record.release_reason === "browser_manager_startup_cleanup")
      ) && ["frozen", "discarded"].includes(record.resource_state);
      if (canRecoverFromCheckpoint) {
        const checkpoint = await this.stateStore.readCheckpoint(record.browser_id);
        if (checkpoint) {
          const recoveredAt = nowIso();
          record.status = "running";
          record.resource_state = "discarded";
          record.client_count = 0;
          record.ended_at = null;
          record.updated_at = recoveredAt;
          record.discarded_at = recoveredAt;
          record.release_reason = "browser_manager_startup_checkpoint_recovery";
          record.error_message = null;
          record.resource_transition_error = null;
          record.discarded_pages = checkpointPageSummaries(checkpoint);
        } else {
          record.status = "lost";
          record.resource_state = "lost";
          record.ended_at = record.ended_at || nowIso();
          record.updated_at = nowIso();
          record.release_reason = "browser_checkpoint_missing_on_startup";
          record.error_message = `浏览器记录声明已冷回收，但检查点不存在: browser_id=${record.browser_id}`;
        }
      } else if (record.status === "running") {
        record.status = "lost";
        record.resource_state = "lost";
        record.ended_at = record.ended_at || nowIso();
        record.updated_at = nowIso();
        record.release_reason = "browser_manager_startup_cleanup";
        record.client_count = 0;
      }
      const session = new BrowserSession({ manager: this, record });
      this.sessions.set(session.id, session);
    }
    await this.persist();
  }

  attachUrl(id) {
    return `${this.browserFrontendBaseUrl}/?browserId=${encodeURIComponent(id)}`;
  }

  async writeScreenshot(id, buffer) {
    return await this.stateStore.writeScreenshot(id, buffer);
  }

  async writeDownload(id, download) {
    return await this.stateStore.writeDownload(id, download);
  }

  download(id, downloadId) {
    const record = this.get(id).download(downloadId);
    return {
      ...record,
      path: this.stateStore.assertDownloadPath(record.path),
    };
  }

  async persist() {
    if (this.persistTimer !== null) {
      clearTimeout(this.persistTimer);
      this.persistTimer = null;
    }
    const execution = this.persistTail.then(async () => {
      await this.stateStore.write({
        workspace_root: this.workspaceRoot,
        updated_at: nowIso(),
        browsers: [...this.sessions.values()].map((session) => session.snapshot()),
      });
    });
    this.persistTail = execution.catch(() => undefined);
    await execution;
  }

  schedulePersist(delayMs = 150) {
    if (this.persistTimer !== null) {
      return;
    }
    this.persistTimer = setTimeout(() => {
      this.persistTimer = null;
      void this.persist();
    }, delayMs);
  }

  list({ sessionId = null } = {}) {
    return [...this.sessions.values()]
      .filter((session) => !sessionId || session.sessionId === sessionId)
      .map((session) => session.snapshot())
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  }

  get(id) {
    const session = this.sessions.get(id);
    if (!session) {
      throw new Error(`浏览器页面不存在: ${id}`);
    }
    return session;
  }

  async create({
    sessionId,
    title = "Browser Page",
    url = "about:blank",
    viewport = { width: 1280, height: 800 },
  }) {
    const admission = this.createAdmissionTail.then(async () => {
      if (!sessionId) {
        throw new Error("session_id 不能为空");
      }
      const pressureLevel = this.resourceGovernor?.snapshot().last_sample?.level || "normal";
      if (["critical", "emergency"].includes(pressureLevel)) {
        const error = new Error(`当前内存压力为 ${pressureLevel}，暂时不能新建浏览器`);
        error.code = "browser_creation_paused_memory_pressure";
        throw error;
      }
      const managedStatuses = new Set(["created", "running"]);
      const logicalBrowserCount = [...this.sessions.values()].filter((session) => (
        session.sessionId === sessionId && managedStatuses.has(session.status)
      )).length;
      if (logicalBrowserCount >= this.maxLogicalBrowsersPerSession) {
        const error = new Error(
          `会话浏览器数量达到硬上限: session_id=${sessionId}, count=${logicalBrowserCount}, max=${this.maxLogicalBrowsersPerSession}`,
        );
        error.code = "browser_session_logical_limit_exceeded";
        throw error;
      }
      const budget = await this.stateStore.checkpointBudgetSnapshot();
      const uncheckpointedManagedCount = [...this.sessions.values()].filter((session) => (
        managedStatuses.has(session.status) && !session.record.checkpoint
      )).length;
      const projectedReservedBytes = budget.used_bytes
        + (uncheckpointedManagedCount + 1) * this.checkpointReservationBytes;
      if (projectedReservedBytes > budget.max_bytes) {
        const error = new Error(
          `工作区浏览器检查点预留预算不足: used=${budget.used_bytes}, uncheckpointed=${uncheckpointedManagedCount}, reservation=${this.checkpointReservationBytes}, max=${budget.max_bytes}`,
        );
        error.code = "browser_checkpoint_workspace_quota_exceeded";
        error.projected_workspace_bytes = projectedReservedBytes;
        error.max_workspace_bytes = budget.max_bytes;
        throw error;
      }
      const id = browserId();
      const timestamp = nowIso();
      const session = new BrowserSession({
        manager: this,
        record: {
          browser_id: id,
          page_id: id,
          session_id: sessionId,
          title,
          url: normalizeBrowserUrl(url),
          viewport,
          resource_state: "background",
          resource_policy: "automatic",
          last_user_interaction_at: timestamp,
          last_agent_operation_at: null,
          last_attach_at: null,
          last_detach_at: null,
          last_network_activity_at: null,
          agent_access_locked: false,
          agent_lock_updated_at: null,
          status: "created",
          created_at: timestamp,
          updated_at: timestamp,
        },
      });
      this.sessions.set(id, session);
      return session;
    });
    this.createAdmissionTail = admission.then(() => undefined, () => undefined);
    const session = await admission;
    await session.start();
    await this.persist();
    return session.snapshot();
  }

  async close(id) {
    const session = this.get(id);
    return await session.close({ status: "closed", reason: "browser_closed_by_user" });
  }

  async setAgentAccessLocked(id, locked, ownerId) {
    return await this.get(id).setAgentAccessLocked(locked, ownerId);
  }

  async delete(id) {
    const session = this.get(id);
    const snapshot = await session.close({ status: "deleted", reason: "browser_deleted_by_user" });
    await this.persist();
    return { deleted: true, browser_id: id, browser: snapshot };
  }

  async discard(id, reason = "browser_discarded_by_user") {
    return await this.get(id).discard({ reason });
  }

  async shutdown(reason = "browser_manager_shutdown") {
    this.stopResourceGovernor();
    const checkpointFailures = [];
    for (const session of this.sessions.values()) {
      if (session.status !== "running") {
        continue;
      }
      if (session.record.resource_state === "discarded") {
        continue;
      }
      try {
        await session.checkpointForManagerShutdown(reason);
      } catch (error) {
        checkpointFailures.push({ browserId: session.id, error });
        await session.markManagerShutdownCheckpointFailed(reason, error);
      }
    }
    await this.runtimePool.shutdown();
    await this.persist();
    if (checkpointFailures.length > 0) {
      throw new AggregateError(
        checkpointFailures.map((item) => item.error),
        `Browser Manager 关闭时有 ${checkpointFailures.length} 个浏览器无法生成检查点: ${checkpointFailures.map((item) => item.browserId).join(",")}`,
      );
    }
  }
}
