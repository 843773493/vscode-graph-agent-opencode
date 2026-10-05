import path from "node:path";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";

export const STATE_FILE_NAME = "terminals.json";
export const STATE_SCHEMA_VERSION = 3;

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

  async write(data) {
    if (!Array.isArray(data.terminals)) {
      throw new TypeError("终端状态写入必须包含 terminals 数组");
    }
    await mkdir(this.stateDir, { recursive: true });
    const temporaryFile = `${this.stateFile}.${process.pid}.tmp`;
    const payload = {
      schema_version: STATE_SCHEMA_VERSION,
      workspace_id: this.workspaceId,
      updated_at: data.updated_at,
      terminals: data.terminals,
    };
    await writeFile(temporaryFile, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
    await rename(temporaryFile, this.stateFile);
  }
}
