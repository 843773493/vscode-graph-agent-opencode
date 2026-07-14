import path from "node:path";
import { randomUUID } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import { BrowserSession } from "./browserSession.js";
import { BrowserStateStore } from "./browserStateStore.js";
import { normalizeBrowserUrl, nowIso } from "./url.js";

function browserId() {
  return `browser_${randomUUID().replaceAll("-", "").slice(0, 16)}`;
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
  } = {}) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.stateStore = new BrowserStateStore({ workspaceRoot: this.workspaceRoot });
    this.browserFrontendBaseUrl = browserFrontendBaseUrl.replace(/\/$/, "");
    this.sessions = new Map();
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
      if (record.status === "running") {
        record.status = "lost";
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

  async persist() {
    await this.stateStore.write({
      workspace_root: this.workspaceRoot,
      updated_at: nowIso(),
      browsers: [...this.sessions.values()].map((session) => session.snapshot()),
    });
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
    if (!sessionId) {
      throw new Error("session_id 不能为空");
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
        status: "created",
        created_at: timestamp,
        updated_at: timestamp,
      },
    });
    this.sessions.set(id, session);
    await session.start();
    await this.persist();
    return session.snapshot();
  }

  async close(id) {
    const session = this.get(id);
    return await session.close({ status: "closed", reason: "browser_closed_by_user" });
  }

  async delete(id) {
    const session = this.get(id);
    const snapshot = await session.close({ status: "deleted", reason: "browser_deleted_by_user" });
    await this.persist();
    return { deleted: true, browser_id: id, browser: snapshot };
  }

  async shutdown(reason = "browser_manager_shutdown") {
    for (const session of this.sessions.values()) {
      if (session.status !== "running") {
        continue;
      }
      await session.close({ status: "closed", reason });
    }
    await this.persist();
  }
}
