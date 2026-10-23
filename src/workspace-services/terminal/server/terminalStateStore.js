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
    let stage = "目录创建";
    try {
      await mkdir(this.stateDir, { recursive: true });
      stage = "正文读取";
      const raw = await readFile(this.stateFile, "utf8");
      stage = "正文解析";
      const parsed = JSON.parse(raw);
      stage = "记录核验";
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
    } catch (error) {
      if (stage === "正文读取" && error?.code === "ENOENT") return null;
      throw this.operationError("读取", stage, error);
    }
  }

  /** @param {TerminalStateWriteSnapshot} stateSnapshot */
  async write(stateSnapshot) {
    const temporaryFile = `${this.stateFile}.${process.pid}.tmp`;
    let stage = "记录核验";
    try {
      if (!Array.isArray(stateSnapshot.terminals)) {
        throw new TypeError("终端状态写入必须包含 terminals 数组");
      }
      stage = "目录创建";
      await mkdir(this.stateDir, { recursive: true });
      const stateFilePayload = {
        schema_version: STATE_SCHEMA_VERSION,
        workspace_id: this.workspaceId,
        updated_at: stateSnapshot.updated_at,
        terminals: stateSnapshot.terminals,
      };
      stage = "正文编码";
      const serialized = `${JSON.stringify(stateFilePayload, null, 2)}\n`;
      stage = "正文写入";
      await writeFile(temporaryFile, serialized, "utf8");
      stage = "文件发布";
      await rename(temporaryFile, this.stateFile);
    } catch (writeError) {
      let cause = writeError;
      if (stage === "正文写入" || stage === "文件发布") {
        try {
          await rm(temporaryFile, { force: true });
        } catch (cleanupError) {
          cause = new AggregateError([writeError, cleanupError], "终端状态写入失败，临时文件也未能清理", { cause: writeError });
        }
      }
      throw this.operationError("写入", stage, writeError, cause);
    }
  }

  operationError(operation, stage, primary, cause = primary) {
    const code = primary.code || (operation === "读取" ? "terminal_records_read_failed" : "terminal_records_write_failed");
    console.error(`[terminal-state-store] ${operation}终端状态失败`, {
      workspaceId: this.workspaceId, filePath: this.stateFile, stage,
    }, cause);
    return Object.assign(new Error(
      `${operation}终端状态失败: workspace_id=${this.workspaceId}, artifact=terminal_records, stage=${stage}, code=${code}`,
      { cause },
    ), { code });
  }
}
