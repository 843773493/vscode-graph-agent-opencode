import path from "node:path";
import { mkdir, readFile, readdir, rename, rm, stat, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";

export const DEFAULT_CHECKPOINT_LIMITS = Object.freeze({
  perCheckpointMaxBytes: 20 * 1024 * 1024,
  workspaceCheckpointMaxBytes: 2 * 1024 * 1024 * 1024,
});

function checkpointQuotaError(code, message, details) {
  const error = new Error(`${message}: ${Object.entries(details)
    .map(([key, value]) => `${key}=${value}`)
    .join(", ")}`);
  error.code = code;
  Object.assign(error, details);
  return error;
}

function sameCheckpointSizes(left, right) {
  if (left.size !== right.size) return false;
  for (const [browserId, size] of left) {
    if (right.get(browserId) !== size) return false;
  }
  return true;
}

export class BrowserStateStore {
  constructor({ workspaceRoot, workspaceId, checkpointLimits = DEFAULT_CHECKPOINT_LIMITS }) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.workspaceId = workspaceId;
    this.stateDir = path.join(this.workspaceRoot, ".boxteam", "browser-manager");
    this.screenshotDir = path.join(this.stateDir, "screenshots");
    this.downloadDir = path.join(this.stateDir, "downloads");
    this.checkpointDir = path.join(this.stateDir, "checkpoints");
    this.stateFile = path.join(this.stateDir, "browsers.json");
    this.checkpointLimits = { ...DEFAULT_CHECKPOINT_LIMITS, ...checkpointLimits };
    // 配额索引记录序列化文件的字节数（含末尾换行），不是内容hash或检查点版本。
    /** @type {Map<string, number> | null} */
    this.checkpointSizeBytesByBrowserId = null;
    // 读、写、删除及配额查询共用串行尾部；失败不阻断下一次显式操作。
    this.checkpointOperationTail = Promise.resolve();
  }

  checkpointPath(browserId) {
    if (typeof browserId !== "string" || !/^browser_[a-zA-Z0-9]+$/.test(browserId)) {
      throw new Error(`浏览器 ID 不能用于检查点路径: ${browserId}`);
    }
    return path.join(this.checkpointDir, `${browserId}.json`);
  }

  async readCheckpoint(browserId) {
    const filePath = this.checkpointPath(browserId);
    const execution = this.checkpointOperationTail.then(async () => {
      try {
        await this.verifyCheckpointSizes();
        const metadata = await stat(filePath);
        if (metadata.size > this.checkpointLimits.perCheckpointMaxBytes) {
          throw checkpointQuotaError(
            "browser_checkpoint_too_large",
            "磁盘上的浏览器检查点超过单资源硬上限",
            {
              browser_id: browserId,
              actual_bytes: metadata.size,
              max_bytes: this.checkpointLimits.perCheckpointMaxBytes,
            },
          );
        }
        return JSON.parse(await readFile(filePath, "utf8"));
      } catch (error) {
        if (error?.code === "ENOENT") {
          this.checkpointSizeBytesByBrowserId?.delete(browserId);
          return null;
        }
        if ([
          "browser_checkpoint_too_large",
          "browser_checkpoint_workspace_quota_exceeded",
          "browser_checkpoint_index_changed_externally",
        ].includes(error?.code)) throw error;
        const wrapped = new Error(
          `读取浏览器检查点失败: browser_id=${browserId}, path=${filePath}, error=${error instanceof Error ? error.message : String(error)}`,
        );
        wrapped.code = error?.code || "browser_checkpoint_read_failed";
        throw wrapped;
      }
    });
    this.checkpointOperationTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async writeCheckpoint(browserId, checkpoint) {
    const serialized = `${JSON.stringify(checkpoint)}\n`;
    const sizeBytes = Buffer.byteLength(serialized, "utf8");
    if (sizeBytes > this.checkpointLimits.perCheckpointMaxBytes) {
      throw checkpointQuotaError(
        "browser_checkpoint_too_large",
        "浏览器检查点超过单资源硬上限",
        {
          browser_id: browserId,
          actual_bytes: sizeBytes,
          max_bytes: this.checkpointLimits.perCheckpointMaxBytes,
        },
      );
    }
    const filePath = this.checkpointPath(browserId);
    const execution = this.checkpointOperationTail.then(async () => {
      await this.verifyCheckpointSizes();
      const previousBytes = this.checkpointSizeBytesByBrowserId.get(browserId) || 0;
      const totalBytes = [...this.checkpointSizeBytesByBrowserId.values()].reduce((total, size) => total + size, 0);
      const projectedBytes = totalBytes - previousBytes + sizeBytes;
      if (projectedBytes > this.checkpointLimits.workspaceCheckpointMaxBytes) {
        throw checkpointQuotaError(
          "browser_checkpoint_workspace_quota_exceeded",
          "浏览器检查点超过工作区硬上限",
          {
            browser_id: browserId,
            actual_bytes: sizeBytes,
            projected_workspace_bytes: projectedBytes,
            max_workspace_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes,
          },
        );
      }
      await mkdir(this.checkpointDir, { recursive: true });
      const temporaryFile = `${filePath}.${process.pid}.${randomUUID()}.tmp`;
      try {
        await writeFile(temporaryFile, serialized, "utf8");
        await rename(temporaryFile, filePath);
      } catch (error) {
        await rm(temporaryFile, { force: true });
        const wrapped = new Error(
          `写入浏览器检查点失败: browser_id=${browserId}, path=${filePath}, error=${error instanceof Error ? error.message : String(error)}`,
        );
        wrapped.code = error?.code || "browser_checkpoint_write_failed";
        throw wrapped;
      }
      this.checkpointSizeBytesByBrowserId.set(browserId, sizeBytes);
      return { path: filePath, size_bytes: sizeBytes, workspace_bytes: projectedBytes };
    });
    this.checkpointOperationTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async deleteCheckpoint(browserId) {
    const execution = this.checkpointOperationTail.then(async () => {
      await this.verifyCheckpointSizes();
      await rm(this.checkpointPath(browserId), { force: true });
      this.checkpointSizeBytesByBrowserId.delete(browserId);
    });
    this.checkpointOperationTail = execution.then(() => undefined, () => undefined);
    await execution;
  }

  async checkpointBudgetSnapshot() {
    const execution = this.checkpointOperationTail.then(async () => {
      await this.verifyCheckpointSizes();
      const usedBytes = [...this.checkpointSizeBytesByBrowserId.values()].reduce((total, size) => total + size, 0);
      return {
        used_bytes: usedBytes,
        max_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes,
        remaining_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes - usedBytes,
      };
    });
    this.checkpointOperationTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async ensureCheckpointSizes() {
    if (this.checkpointSizeBytesByBrowserId !== null) return;
    this.checkpointSizeBytesByBrowserId = await this.scanCheckpointSizes({ cleanupTemporaryFiles: true });
  }

  async verifyCheckpointSizes() {
    if (this.checkpointSizeBytesByBrowserId === null) {
      await this.ensureCheckpointSizes();
      return;
    }
    const actual = await this.scanCheckpointSizes({ cleanupTemporaryFiles: false });
    if (!sameCheckpointSizes(this.checkpointSizeBytesByBrowserId, actual)) {
      throw checkpointQuotaError(
        "browser_checkpoint_index_changed_externally",
        "浏览器检查点目录绕过软件发生变化",
        {
          indexed_count: this.checkpointSizeBytesByBrowserId.size,
          actual_count: actual.size,
        },
      );
    }
  }

  async scanCheckpointSizes({ cleanupTemporaryFiles }) {
    const sizes = new Map();
    let entries;
    try {
      entries = await readdir(this.checkpointDir, { withFileTypes: true });
    } catch (error) {
      if (error?.code === "ENOENT") {
        return sizes;
      }
      throw error;
    }
    await Promise.all(entries.map(async (entry) => {
      if (entry.isFile() && entry.name.endsWith(".tmp")) {
        if (cleanupTemporaryFiles) {
          await rm(path.join(this.checkpointDir, entry.name), { force: true });
        }
        return;
      }
      const match = entry.isFile() ? entry.name.match(/^(browser_[a-zA-Z0-9]+)\.json$/) : null;
      if (!match) return;
      const metadata = await stat(path.join(this.checkpointDir, entry.name));
      sizes.set(match[1], metadata.size);
    }));
    const totalBytes = [...sizes.values()].reduce((total, size) => total + size, 0);
    if (totalBytes > this.checkpointLimits.workspaceCheckpointMaxBytes) {
      throw checkpointQuotaError(
        "browser_checkpoint_workspace_quota_exceeded",
        "磁盘上的浏览器检查点超过工作区硬上限",
        {
          actual_workspace_bytes: totalBytes,
          max_workspace_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes,
        },
      );
    }
    return sizes;
  }

  async readRecords() {
    try {
      const raw = await readFile(this.stateFile, "utf8");
      const parsed = JSON.parse(raw);
      if (parsed.schema_version !== 2 || parsed.workspace_id !== this.workspaceId
        || !Array.isArray(parsed.browsers)) {
        throw new Error(`浏览器状态文件格式错误: ${this.stateFile}`);
      }
      const ids = new Set();
      for (const record of parsed.browsers) {
        this.validateRecord(record);
        if (ids.has(record.browser_id)) throw new Error("Browser 运行记录身份重复");
        ids.add(record.browser_id);
      }
      return parsed.browsers;
    } catch (error) {
      if (error?.code === "ENOENT") {
        return null;
      }
      throw error;
    }
  }

  validateRecord(record) {
    if (!record || typeof record !== "object" || Array.isArray(record)
      || record.workspace_id !== this.workspaceId || !/^browser_[0-9a-f]{32}$/.test(record.browser_id)
      || !Number.isSafeInteger(record.transport_owner_revision) || record.transport_owner_revision < 1
      || !["verified", "unknown", "reconcile_required"].includes(record.reconciliation_status)) {
      throw new Error("Browser 运行记录缺少有效的 owner 身份、工作区或版本");
    }
    for (const key of ["session_id", "source_thread", "thread_associations", "retention_scope", "attach_url", "workspace_root"]) {
      if (Object.hasOwn(record, key)) throw new Error(`Browser 运行记录禁止包含领域字段: ${key}`);
    }
    if (record.checkpoint?.path || record.downloads?.some((item) => item.path || item.file_path)) {
      throw new Error("Browser 运行记录禁止持久化真实产物路径");
    }
  }

  async write(payload) {
    for (const record of payload.browsers) this.validateRecord(record);
    payload = { ...payload, schema_version: 2, workspace_id: this.workspaceId };
    await mkdir(this.stateDir, { recursive: true });
    const temporaryFile = `${this.stateFile}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporaryFile, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    await rename(temporaryFile, this.stateFile);
  }

  async writeScreenshot(browserId, buffer) {
    await mkdir(this.screenshotDir, { recursive: true });
    // 非 canonical 身份：Node 进程（无 crypto.randomUUIDv7）生成的 screenshot_/*
    // 本地 id 允许继续使用 v4，不进入 canonical 校验器命名空间。
    const screenshotId = `screenshot_${randomUUID().replaceAll("-", "")}`;
    const fileName = `${browserId}-${screenshotId}.png`;
    const filePath = path.join(this.screenshotDir, fileName);
    await writeFile(filePath, buffer);
    // 只返回不含文件系统路径的可寻址身份；真实路径仅在本调用栈内使用（裁定 D-A2/D-A3）。
    return { screenshot_id: screenshotId, byte_length: buffer.byteLength };
  }

  async writeDownload(browserId, download) {
    const browserDownloadDir = path.join(this.downloadDir, browserId);
    await mkdir(browserDownloadDir, { recursive: true });
    // 非 canonical 身份：Node 进程生成的 download_/* 本地 id 允许继续使用 v4。
    const downloadId = `download_${randomUUID().replaceAll("-", "")}`;
    const suggestedName = path.basename(download.suggestedFilename() || "download");
    const storedName = `${downloadId}-${suggestedName}`;
    const filePath = path.join(browserDownloadDir, storedName);
    await download.saveAs(filePath);
    return {
      download_id: downloadId,
      filename: suggestedName,
      url: download.url(),
      created_at: new Date().toISOString(),
      status: "completed",
    };
  }

  assertBrowserId(browserId) {
    if (typeof browserId !== "string" || !/^browser_[a-zA-Z0-9]+$/.test(browserId)) {
      throw new Error(`浏览器 ID 不能用于产物路径: ${browserId}`);
    }
    return browserId;
  }

  // 文件名必须来自 owner 生成的安全字符集，避免 `../` 逃出产物目录。
  static assertSafeName(name) {
    if (typeof name !== "string" || name === "" || name !== path.basename(name)) {
      throw new Error(`产物文件名不能包含路径分隔符: ${name}`);
    }
    return name;
  }

  // 下载产物路径由 (browser_id, download_id, filename) 在调用栈内重推导，
  // 持久记录 MUST NOT 承载真实路径；越界校验强度不降低（裁定 D-A2/D-A6）。
  resolveDownloadPath(browserId, downloadId, filename) {
    this.assertBrowserId(browserId);
    if (typeof downloadId !== "string" || !/^download_[a-z0-9]+$/.test(downloadId)) {
      throw new Error(`下载 ID 不能用于产物路径: ${downloadId}`);
    }
    const storedName = `${downloadId}-${BrowserStateStore.assertSafeName(filename)}`;
    const resolved = path.resolve(this.downloadDir, browserId, storedName);
    const relative = path.relative(this.downloadDir, resolved);
    if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`下载文件越出 Browser Manager 目录: ${resolved}`);
    }
    return resolved;
  }

  resolveScreenshotPath(browserId, screenshotId) {
    this.assertBrowserId(browserId);
    if (typeof screenshotId !== "string" || !/^screenshot_[a-z0-9]+$/.test(screenshotId)) {
      throw new Error(`截图 ID 不能用于产物路径: ${screenshotId}`);
    }
    const resolved = path.resolve(this.screenshotDir, `${browserId}-${screenshotId}.png`);
    const relative = path.relative(this.screenshotDir, resolved);
    if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`截图文件越出 Browser Manager 目录: ${resolved}`);
    }
    return resolved;
  }
}
