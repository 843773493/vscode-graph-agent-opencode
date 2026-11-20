import { createRuntimeLogger } from "@boxteam/plugin-sdk/logging";
import path from "node:path";
import { Worker } from "node:worker_threads";

const logger = createRuntimeLogger("terminal", "terminal.terminalStateStore");
export const STATE_FILE_NAME = "terminals.sqlite";

/**
 * Manager交付的一轮终端恢复记录，不是HTTP/WebSocket展示快照。
 * @typedef {object} TerminalStateWriteSnapshot
 * @property {string} updated_at Manager开始组装本轮快照的UTC ISO时刻，不是写盘完成时间。
 * @property {ReturnType<import("./terminalSession.js").TerminalSession["toRecord"]>[]} terminals 本轮全部终端恢复记录；Manager串行交付，记录及args不再原地修改。
 */

export class TerminalStateStore {
  constructor({ workspaceRoot, workspaceId }) {
    this.workspaceId = workspaceId;
    this.stateDir = path.join(workspaceRoot, ".boxteam", "terminal-manager");
    this.stateFile = path.join(this.stateDir, STATE_FILE_NAME);
    this.worker = null;
    this.requests = new Map();
    this.requestId = 0;
    // 仅保存最近成功提交的不可变记录引用，用于差异提交；SQLite仍是唯一恢复事实。
    this.committed = null;
  }

  request(operation, payload = {}) {
    if (!this.worker) {
      let worker;
      try {
        worker = new Worker(new URL("./storage/stateWorker.js", import.meta.url), { workerData: {
          workspaceId: this.workspaceId, stateDir: this.stateDir, stateFile: this.stateFile,
          legacyFile: path.join(this.stateDir, "terminals.json"),
        } });
      } catch (error) {
        return Promise.reject(this.operationError(operation, "写入线程", error));
      }
      this.worker = worker;
      worker.on("message", ({ id, result, error, details, rollbackError, cleanupError, stage }) => {
        const pending = this.requests.get(id);
        this.requests.delete(id);
        if (error) {
          Object.assign(error, details);
          const errors = [error, rollbackError, cleanupError].filter(Boolean);
          const cause = errors.length > 1 ? new AggregateError(errors, "终端状态操作与收尾均失败", { cause: error }) : error;
          pending.reject(this.operationError(pending.operation, stage, error, cause));
        } else pending.resolve(result);
        if (this.requests.size === 0) worker.unref();
      });
      const fail = error => {
        if (this.worker !== worker) return;
        this.worker = null;
        this.committed = null;
        if (this.requests.size === 0) throw this.operationError("write", "写入线程", error);
        for (const pending of this.requests.values()) pending.reject(this.operationError(pending.operation, "写入线程", error));
        this.requests.clear();
      };
      worker.on("error", fail);
      worker.on("exit", code => fail(new Error(`终端存储线程退出: code=${code}`)));
    }
    const worker = this.worker;
    const id = ++this.requestId;
    const pending = Promise.withResolvers();
    this.requests.set(id, { ...pending, operation });
    worker.ref();
    try {
      worker.postMessage({ id, operation, ...payload });
    } catch (error) {
      this.requests.delete(id);
      if (this.requests.size === 0) worker.unref();
      pending.reject(this.operationError(operation, "线程交接", error));
    }
    return pending.promise;
  }

  async readRecords() {
    const records = await this.request("read");
    this.committed = new Map((records ?? []).map((record, position) => [record.terminal_id, { record: { ...record }, position }]));
    return records;
  }

  /** @param {TerminalStateWriteSnapshot} stateSnapshot */
  async write(stateSnapshot) {
    if (this.committed === null) await this.readRecords();
    const next = new Map();
    const changed = [], positions = [];
    try {
      if (!Array.isArray(stateSnapshot.terminals)) throw new TypeError("终端状态写入必须包含 terminals 数组");
      stateSnapshot.terminals.forEach((record, position) => {
        const id = record.terminal_id;
        if (typeof id !== "string" || !id || next.has(id)) throw new Error("终端状态记录身份为空或重复");
        next.set(id, { record, position });
        const previous = this.committed.get(id);
        const keys = Object.keys(record);
        if (!previous || Object.keys(previous.record).length !== keys.length || keys.some(key => previous.record[key] !== record[key])) changed.push({ record, position });
        else if (previous.position !== position) positions.push({ id, position });
      });
    } catch (error) {
      throw this.operationError("write", "记录核验", error);
    }
    await this.request("write", {
      changed, positions, removed: [...this.committed.keys()].filter(id => !next.has(id)), updatedAt: stateSnapshot.updated_at,
    });
    this.committed = next;
  }

  async close() {
    if (!this.worker) return;
    const worker = this.worker;
    await this.request("close");
    if (this.worker === worker) this.worker = null;
    await worker.terminate();
    this.committed = null;
  }

  operationError(operation, stage, primary, cause = primary) {
    const label = operation === "read" ? "读取" : operation === "import" ? "导入" : operation === "close" ? "关闭" : "写入";
    const code = primary.code || `terminal_records_${operation}_failed`;
    try {
      logger.withContext({ workspace_id: this.workspaceId }).error(`[terminal-state-store] ${label}终端状态失败`, {
        workspaceId: this.workspaceId, filePath: this.stateFile, stage,
      }, cause);
    } catch (diagnosticError) {
      cause = new AggregateError([cause, diagnosticError], "终端状态操作与诊断输出均失败", { cause });
    }
    return Object.assign(new Error(
      `${label}终端状态失败: workspace_id=${this.workspaceId}, artifact=terminal_records, stage=${stage}, code=${code}`, { cause },
    ), { code });
  }
}
