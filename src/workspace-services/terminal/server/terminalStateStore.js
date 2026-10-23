import path from "node:path";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";

export const STATE_FILE_NAME = "terminals.json";
export const STATE_SCHEMA_VERSION = 3;

/**
 * Manager交付的一轮终端恢复记录，不是HTTP/WebSocket展示快照。
 * @typedef {object} TerminalStateWriteSnapshot
 * @property {string} updated_at Manager开始组装本轮快照的UTC ISO时刻，早于逐资源toRecord；不是资源更新时间或写盘完成时间。
 * @property {ReturnType<import("./terminalSession.js").TerminalSession["toRecord"]>[]} terminals 本轮保留的全部终端恢复记录，包括非运行记录。
 */

export class TerminalStateStore {
  constructor({ workspaceRoot, workspaceId }) {
    this.workspaceId = workspaceId;
    this.stateDir = path.join(workspaceRoot, ".boxteam", "terminal-manager");
    this.stateFile = path.join(this.stateDir, STATE_FILE_NAME);
  }

  async readRecords() {
    await mkdir(this.stateDir, { recursive: true });
    let raw = null;
    try {
      raw = await readFile(this.stateFile, "utf8");
    } catch (error) {
      if (error?.code === "ENOENT") {
        return null;
      }
      throw error;
    }

    const parsed = JSON.parse(raw);
    if (parsed?.schema_version !== STATE_SCHEMA_VERSION) {
      throw new Error(
        `终端状态文件版本不受支持: expected=${STATE_SCHEMA_VERSION}, actual=${parsed?.schema_version ?? "missing"}`,
      );
    }
    if (parsed?.workspace_id !== this.workspaceId) {
      throw new Error(
        `终端状态文件工作区身份不匹配: expected=${this.workspaceId}, actual=${parsed.workspace_id}`,
      );
    }
    if (!Array.isArray(parsed.terminals)) {
      throw new TypeError("终端状态文件缺少 terminals 数组");
    }
    return parsed.terminals;
  }

  /** @param {TerminalStateWriteSnapshot} stateSnapshot */
  async write(stateSnapshot) {
    if (!Array.isArray(stateSnapshot.terminals)) {
      throw new TypeError("终端状态写入必须包含 terminals 数组");
    }
    await mkdir(this.stateDir, { recursive: true });
    const temporaryFile = `${this.stateFile}.${process.pid}.tmp`;
    const stateFilePayload = {
      schema_version: STATE_SCHEMA_VERSION,
      workspace_id: this.workspaceId,
      updated_at: stateSnapshot.updated_at,
      terminals: stateSnapshot.terminals,
    };
    try {
      await writeFile(temporaryFile, `${JSON.stringify(stateFilePayload, null, 2)}\n`, "utf8");
      await rename(temporaryFile, this.stateFile);
    } catch (writeError) {
      try {
        await rm(temporaryFile, { force: true });
      } catch (cleanupError) {
        throw new AggregateError(
          [writeError, cleanupError],
          "终端状态写入失败，临时文件也未能清理",
          { cause: writeError },
        );
      }
      throw writeError;
    }
  }
}
