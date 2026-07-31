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
  constructor({ workspaceRoot, checkpointLimits = DEFAULT_CHECKPOINT_LIMITS }) {
    this.workspaceRoot = path.resolve(workspaceRoot);
    this.stateDir = path.join(this.workspaceRoot, ".boxteam", "browser-manager");
    this.screenshotDir = path.join(this.stateDir, "screenshots");
    this.downloadDir = path.join(this.stateDir, "downloads");
    this.checkpointDir = path.join(this.stateDir, "checkpoints");
    this.stateFile = path.join(this.stateDir, "browsers.json");
    this.checkpointLimits = { ...DEFAULT_CHECKPOINT_LIMITS, ...checkpointLimits };
    this.checkpointSizes = null;
    this.checkpointWriteTail = Promise.resolve();
  }

  checkpointPath(browserId) {
    if (typeof browserId !== "string" || !/^browser_[a-zA-Z0-9]+$/.test(browserId)) {
      throw new Error(`浏览器 ID 不能用于检查点路径: ${browserId}`);
    }
    return path.join(this.checkpointDir, `${browserId}.json`);
  }

  async readCheckpoint(browserId) {
    const filePath = this.checkpointPath(browserId);
    const execution = this.checkpointWriteTail.then(async () => {
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
          this.checkpointSizes?.delete(browserId);
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
    this.checkpointWriteTail = execution.then(() => undefined, () => undefined);
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
    const execution = this.checkpointWriteTail.then(async () => {
      await this.verifyCheckpointSizes();
      const previousBytes = this.checkpointSizes.get(browserId) || 0;
      const totalBytes = [...this.checkpointSizes.values()].reduce((total, size) => total + size, 0);
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
      this.checkpointSizes.set(browserId, sizeBytes);
      return { path: filePath, size_bytes: sizeBytes, workspace_bytes: projectedBytes };
    });
    this.checkpointWriteTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async deleteCheckpoint(browserId) {
    const execution = this.checkpointWriteTail.then(async () => {
      await this.verifyCheckpointSizes();
      await rm(this.checkpointPath(browserId), { force: true });
      this.checkpointSizes.delete(browserId);
    });
    this.checkpointWriteTail = execution.then(() => undefined, () => undefined);
    await execution;
  }

  async checkpointBudgetSnapshot() {
    const execution = this.checkpointWriteTail.then(async () => {
      await this.verifyCheckpointSizes();
      const usedBytes = [...this.checkpointSizes.values()].reduce((total, size) => total + size, 0);
      return {
        used_bytes: usedBytes,
        max_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes,
        remaining_bytes: this.checkpointLimits.workspaceCheckpointMaxBytes - usedBytes,
      };
    });
    this.checkpointWriteTail = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async ensureCheckpointSizes() {
    if (this.checkpointSizes !== null) return;
    this.checkpointSizes = await this.scanCheckpointSizes({ cleanupTemporaryFiles: true });
  }

  async verifyCheckpointSizes() {
    if (this.checkpointSizes === null) {
      await this.ensureCheckpointSizes();
      return;
    }
    const actual = await this.scanCheckpointSizes({ cleanupTemporaryFiles: false });
    if (!sameCheckpointSizes(this.checkpointSizes, actual)) {
      throw checkpointQuotaError(
        "browser_checkpoint_index_changed_externally",
        "浏览器检查点目录绕过软件发生变化",
        {
          indexed_count: this.checkpointSizes.size,
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
      if (!Array.isArray(parsed.browsers)) {
        throw new Error(`浏览器状态文件格式错误: ${this.stateFile}`);
      }
      return parsed.browsers;
    } catch (error) {
      if (error?.code === "ENOENT") {
        return null;
      }
      throw error;
    }
  }

  async write(payload) {
    await mkdir(this.stateDir, { recursive: true });
    const temporaryFile = `${this.stateFile}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporaryFile, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    await rename(temporaryFile, this.stateFile);
  }

  async writeScreenshot(browserId, buffer) {
    await mkdir(this.screenshotDir, { recursive: true });
    const fileName = `${browserId}-${Date.now()}.png`;
    const filePath = path.join(this.screenshotDir, fileName);
    await writeFile(filePath, buffer);
    return filePath;
  }

  async writeDownload(browserId, download) {
    const browserDownloadDir = path.join(this.downloadDir, browserId);
    await mkdir(browserDownloadDir, { recursive: true });
    const downloadId = `download_${randomUUID().replaceAll("-", "")}`;
    const suggestedName = path.basename(download.suggestedFilename() || "download");
    const storedName = `${downloadId}-${suggestedName}`;
    const filePath = path.join(browserDownloadDir, storedName);
    await download.saveAs(filePath);
    return {
      download_id: downloadId,
      filename: suggestedName,
      path: filePath,
      url: download.url(),
      created_at: new Date().toISOString(),
      status: "completed",
    };
  }

  assertDownloadPath(filePath) {
    const resolved = path.resolve(filePath);
    const relative = path.relative(this.downloadDir, resolved);
    if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`下载文件越出 Browser Manager 目录: ${resolved}`);
    }
    return resolved;
  }
}
