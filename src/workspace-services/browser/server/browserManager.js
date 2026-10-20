import path from "node:path";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { existsSync, statSync } from "node:fs";
import { validateWorkspaceId } from "./workspaceIdentity.js";
import { BrowserSession } from "./browserSession.js";
import { BrowserStateStore } from "./browserStateStore.js";
import { normalizeBrowserUrl, nowIso } from "./url.js";
import { BrowserRuntimePool } from "./resources/browserRuntimePool.js";
import { BrowserResourceGovernor } from "./resources/resourceGovernor.js";
import {
  DEFAULT_BROWSER_DEVICE_ORIENTATION,
  DEFAULT_BROWSER_DEVICE_PROFILE,
  resolveBrowserDeviceState,
} from "./browserDeviceProfiles.js";

export const DEFAULT_MAX_LOGICAL_BROWSERS_PER_WORKSPACE = 500;

export class BrowserTransportGrantRejected extends Error {
  constructor(snapshot) {
    super("Browser 传输版本或运行状态冲突");
    this.name = "BrowserTransportGrantRejected";
    this.code = "browser_transport_grant_rejected";
    this.operationOutcome = "rejected";
    this.browser = snapshot;
  }
}

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

/**
 * Node 持有的一次性浏览器传输授权；领域关联的合法性由工作区 owner 核实。
 * @typedef {object} BrowserTransportGrant
 * @property {string} browserId 被授权连接的稳定浏览器资源 ID，不是内部标签页 ID。
 * @property {number} transportOwnerRevision 发放时冻结的 owner 版本影子，仅用于连接撤权。
 * @property {unknown} association 领域 owner 传入的不透明关联，Node 不据此决定资源生命周期。
 * @property {number} expiresAtUnixMs 授权过期的 Unix 毫秒时刻，不是存活时长或 ISO 字符串。
 */

export class BrowserManager {
  constructor({
    workspaceRoot = resolveWorkspaceRoot(),
    workspaceId,
    runtimePool = null,
    maxLogicalBrowsersPerWorkspace = DEFAULT_MAX_LOGICAL_BROWSERS_PER_WORKSPACE,
    checkpointReservationBytes = DEFAULT_CHECKPOINT_RESERVATION_BYTES,
  } = {}) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.workspaceId = validateWorkspaceId(workspaceId);
    this.stateStore = new BrowserStateStore({ workspaceRoot: this.workspaceRoot, workspaceId: this.workspaceId });
    /** @type {Map<string, BrowserSession>} 以browser_id定位浏览器资源实例，不是聊天Session集合。 */
    this.browsersById = new Map();
    /** @type {Map<string, BrowserTransportGrant>} */
    this.transportGrants = new Map();
    this.stateWriteRequested = false;
    this.stateWritePromise = null;
    this.persistTimer = null;
    this.createAdmissionTail = Promise.resolve();
    this.checkpointCaptureTail = Promise.resolve();
    this.runtimePool = runtimePool || new BrowserRuntimePool({
      onDisconnect: (chromiumInstanceGeneration) => this.handleChromiumDisconnect(chromiumInstanceGeneration),
    });
    this.resourceGovernor = null;
    this.maxLogicalBrowsersPerWorkspace = maxLogicalBrowsersPerWorkspace;
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
    return [...this.browsersById.values()].filter((session) => session.status === "running");
  }

  async runCheckpointCapture(callback) {
    const execution = this.checkpointCaptureTail.then(callback);
    this.checkpointCaptureTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  handleChromiumDisconnect(chromiumInstanceGeneration) {
    for (const session of this.runningSessions()) {
      if (session.chromiumInstanceGeneration === chromiumInstanceGeneration) {
        void session.handleChromiumDisconnect(chromiumInstanceGeneration);
      }
    }
  }

  async init() {
    const identity = JSON.parse(await readFile(path.join(this.workspaceRoot, ".boxteam", "workspace-identity.json"), "utf8"));
    if (validateWorkspaceId(identity.workspace_id) !== this.workspaceId) {
      throw new Error("Browser 工作区 UUID 与权威身份文件不一致");
    }
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
      const agentLockExpiresAtUnixMs = Date.parse(record.agent_lock_expires_at || "");
      if (record.agent_access_locked === true
        && (!Number.isFinite(agentLockExpiresAtUnixMs) || agentLockExpiresAtUnixMs <= Date.now())) {
        record.agent_access_locked = false;
        record.agent_lock_owner_id = null;
        record.agent_lock_expires_at = null;
      }
      // TODO: Browser Manager 在无客户端的后台会话重启时可能先标记 lost；只要检查点仍在，就应允许后续唤醒恢复。
      const canRecoverFromCheckpoint = (
        record.status === "running"
        || record.status === "lost"
      ) && (
        ["frozen", "discarded"].includes(record.resource_state)
        || Boolean(record.checkpoint)
      );
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
      this.browsersById.set(session.id, session);
    }
    await this.persist();
  }

  async writeScreenshot(id, buffer) {
    return await this.stateStore.writeScreenshot(id, buffer);
  }

  screenshot(id, screenshotId) {
    // 先核实浏览器资源存在，避免对任意 id 生成产物路径（不存在则抛 404）。
    this.get(id);
    return {
      path: this.stateStore.resolveScreenshotPath(id, screenshotId),
    };
  }

  async writeDownload(id, download) {
    return await this.stateStore.writeDownload(id, download);
  }

  download(id, downloadId) {
    const record = this.get(id).download(downloadId);
    return {
      path: this.stateStore.resolveDownloadPath(
        id,
        record.download_id,
        record.filename,
      ),
      filename: record.filename,
    };
  }

  async persist() {
    if (this.persistTimer !== null) {
      clearTimeout(this.persistTimer);
      this.persistTimer = null;
    }
    this.stateWriteRequested = true;
    if (!this.stateWritePromise) {
      // 先登记共享Promise再取快照；同步异常也必须经过同一清理入口。
      this.stateWritePromise = Promise.resolve().then(() => this.drainStateWrites());
    }
    await this.stateWritePromise;
  }

  async drainStateWrites() {
    try {
      while (this.stateWriteRequested) {
        this.stateWriteRequested = false;
        await this.stateStore.write({
          updated_at: nowIso(),
          browsers: [...this.browsersById.values()].map((session) => session.toRecord()),
        });
      }
    } finally {
      this.stateWritePromise = null;
    }
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

  list() {
    return [...this.browsersById.values()]
      .map((session) => session.snapshot())
      .sort((left, right) => String(right.updated_at || "").localeCompare(String(left.updated_at || "")));
  }

  get(id) {
    const session = this.browsersById.get(id);
    if (!session) throw new Error(`浏览器页面不存在: ${id}`);
    return session;
  }

  async syncOwnerRevision(id, transportOwnerRevision) {
    this.get(id).syncOwnerRevision(transportOwnerRevision);
    for (const [token, grant] of this.transportGrants) {
      if (grant.browserId === id && grant.transportOwnerRevision !== transportOwnerRevision) this.transportGrants.delete(token);
    }
    await this.get(id).operationQueue.drain();
    await this.persist();
  }

  issueTransportGrant(id, { ownerRevision, association }) {
    const browser = this.get(id);
    browser.assertReconciled();
    if (browser.transportOwnerRevision !== ownerRevision || browser.status !== "running") {
      // 拒绝发生在生成 token 和修改 grant Map 前，快照就是本次无副作用拒绝的事实。
      throw new BrowserTransportGrantRejected(browser.snapshot());
    }
    const token = randomBytes(32).toString("base64url");
    const expiresAtUnixMs = Date.now() + 30_000;
    for (const [existing, grant] of this.transportGrants) {
      if (grant.expiresAtUnixMs <= Date.now()) this.transportGrants.delete(existing);
    }
    this.transportGrants.set(token, { browserId: id, transportOwnerRevision: ownerRevision, association, expiresAtUnixMs });
    return {
      resource_id: id, owner_revision: ownerRevision,
      grant_token: token, expires_at: new Date(expiresAtUnixMs).toISOString(),
    };
  }

  consumeTransportGrant(id, token) {
    const grant = this.transportGrants.get(token);
    this.transportGrants.delete(token);
    if (!grant || grant.browserId !== id || grant.expiresAtUnixMs <= Date.now()
      || this.get(id).transportOwnerRevision !== grant.transportOwnerRevision) {
      throw new Error("Browser 一次传输授权无效、过期或已消费");
    }
    return grant;
  }

  async create({
    workspaceId,
    browserId,
    ownerRevision,
    title = "Browser Page",
    url = "about:blank",
    viewport = null,
    deviceProfile = DEFAULT_BROWSER_DEVICE_PROFILE,
    deviceOrientation = DEFAULT_BROWSER_DEVICE_ORIENTATION,
  }) {
    const admission = this.createAdmissionTail.then(async () => {
      if (workspaceId !== this.workspaceId || !/^browser_[0-9a-f]{32}$/.test(browserId)
        || !Number.isSafeInteger(ownerRevision) || ownerRevision < 1 || this.browsersById.has(browserId)) {
        throw new Error("Browser 创建必须使用当前 Python owner 的唯一身份、工作区与版本");
      }
      const pressureLevel = this.resourceGovernor?.snapshot().last_sample?.level || "normal";
      if (["critical", "emergency"].includes(pressureLevel)) {
        const error = new Error(`当前内存压力为 ${pressureLevel}，暂时不能新建浏览器`);
        error.code = "browser_creation_paused_memory_pressure";
        throw error;
      }
      const managedStatuses = new Set(["created", "running"]);
      const logicalBrowserCount = [...this.browsersById.values()].filter((session) => (
        managedStatuses.has(session.status)
      )).length;
      if (logicalBrowserCount >= this.maxLogicalBrowsersPerWorkspace) {
        const error = new Error(
          `工作区浏览器数量达到硬上限: workspace_id=${workspaceId}, count=${logicalBrowserCount}, max=${this.maxLogicalBrowsersPerWorkspace}`,
        );
        error.code = "browser_workspace_logical_limit_exceeded";
        throw error;
      }
      const budget = await this.stateStore.checkpointBudgetSnapshot();
      const uncheckpointedManagedCount = [...this.browsersById.values()].filter((session) => (
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
      const id = browserId;
      const browserCreatedAtIso = nowIso();
      const deviceState = resolveBrowserDeviceState(deviceProfile, deviceOrientation);
      const session = new BrowserSession({
        manager: this,
        record: {
          browser_id: id,
          page_id: id,
          workspace_id: workspaceId,
          transport_owner_revision: ownerRevision,
          reconciliation_status: "verified",
          title,
          url: normalizeBrowserUrl(url),
          viewport: viewport || { ...deviceState.viewport },
          device_profile: deviceState.id,
          device_orientation: deviceState.orientation,
          device_scale_factor: deviceState.deviceScaleFactor,
          touch_simulation_enabled: deviceState.hasTouch,
          resource_state: "background",
          resource_policy: "automatic",
          // 尚无用户交互观察时，以资源创建时刻初始化空闲计算基线。
          last_user_interaction_at: browserCreatedAtIso,
          last_agent_operation_at: null,
          last_attach_at: null,
          last_detach_at: null,
          last_network_activity_at: null,
          agent_access_locked: false,
          agent_lock_updated_at: null,
          status: "created",
          created_at: browserCreatedAtIso,
          updated_at: browserCreatedAtIso,
        },
      });
      this.browsersById.set(id, session);
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
    return { deleted: true, browser_id: session.id, browser: snapshot };
  }

  async discard(id, reason = "browser_discarded_by_user") {
    return await this.get(id).discard({ reason });
  }

  async shutdown(reason = "browser_manager_shutdown") {
    this.stopResourceGovernor();
    const checkpointFailures = [];
    for (const session of this.browsersById.values()) {
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
