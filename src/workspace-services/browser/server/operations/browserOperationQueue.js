import { nowIso } from "../url.js";
import { DEFAULT_OPERATION_DRAIN_TIMEOUT_MS, drainBrowserExecutions } from "../browserRuntime.js";

/**
 * @typedef {object} BrowserOperationOptions
 * @property {string} actor 操作发起者。
 * @property {string} action 操作名称。
 * @property {boolean} [visible] 可见性标记；仅串行队列据此发布并持久化操作状态。
 * @property {boolean} [interactive] 用户交互标记；是否并发由执行入口决定。
 * @property {number} transportOwnerRevision 接纳操作时冻结的传输授权版本，仅用于撤权检查。
 */

export class BrowserOperationQueue {
  /**
   * @param {{browserSession: import("../browserSession.js").BrowserSession, operationRevision?: number}} options
   * browserSession是浏览器资源宿主，不是聊天会话或传输授权版本。
   */
  constructor({ browserSession, operationRevision = 0 }) {
    this.browserSession = browserSession;
    // 已分配的最大操作序号；排队时递增，与传输授权版本及完成顺序无关。
    this.operationRevision = Number(operationRevision || 0);
    /** @type {Promise<void>} 仅串行操作的尾部；无论前项成功或失败，后项都可进入。 */
    this.serialOperationTail = Promise.resolve();
    /** @type {Set<Promise<unknown>>} 未结束的执行句柄，包含请求、原脚本及页面恢复，不是业务操作计数。 */
    this.acceptedOperationExecutions = new Set();
  }

  /** @param {BrowserOperationOptions} options */
  async enqueue({ actor, action, visible = true, interactive = false, transportOwnerRevision }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    const execution = this.serialOperationTail.then(async () => {
      this.assertTransportOwnerRevision(transportOwnerRevision);
      await this.browserSession.prepareForOperation({ actor, action });
      this.browserSession.beginOperation(operation);
      operation.started_at = nowIso();
      try {
        this.assertTransportOwnerRevision(transportOwnerRevision);
        if (visible) {
          this.browserSession.record.active_operation = operation;
          this.browserSession.emit("state", this.browserSession.snapshot());
        }
        const result = await callback();
        this.assertTransportOwnerRevision(transportOwnerRevision);
        const completed = {
          ...operation,
          status: "completed",
          completed_at: nowIso(),
        };
        await this.finish(completed, visible);
        this.assertTransportOwnerRevision(transportOwnerRevision);
        return result && typeof result === "object" && !Array.isArray(result)
          ? { ...result, operation: completed, operation_revision: operation.operation_revision }
          : { result, operation: completed, operation_revision: operation.operation_revision };
      } catch (error) {
        const failed = {
          ...operation,
          status: "failed",
          completed_at: nowIso(),
          error: error instanceof Error ? error.message : String(error),
        };
        await this.finish(failed, visible);
        throw error;
      } finally {
        this.browserSession.endOperation(operation);
      }
    });
    this.serialOperationTail = execution.then(() => undefined, () => undefined);
    return await this.track(execution);
  }

  /** @param {BrowserOperationOptions} options */
  async runConcurrent({ actor, action, visible = true, interactive = false, transportOwnerRevision }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    const execution = (async () => {
      this.assertTransportOwnerRevision(transportOwnerRevision);
      await this.browserSession.prepareForOperation({ actor, action });
      this.browserSession.beginOperation(operation);
      operation.started_at = nowIso();
      try {
        this.assertTransportOwnerRevision(transportOwnerRevision);
        const result = await callback();
        this.assertTransportOwnerRevision(transportOwnerRevision);
        const completed = { ...operation, status: "completed", completed_at: nowIso() };
        await this.finish(completed, false);
        this.assertTransportOwnerRevision(transportOwnerRevision);
        return result && typeof result === "object" && !Array.isArray(result)
          ? { ...result, operation: completed, operation_revision: operation.operation_revision }
          : { result, operation: completed, operation_revision: operation.operation_revision };
      } catch (error) {
        await this.finish({
          ...operation, status: "failed", completed_at: nowIso(),
          error: error instanceof Error ? error.message : String(error),
        }, false);
        throw error;
      } finally {
        this.browserSession.endOperation(operation);
      }
    })();
    return await this.track(execution);
  }

  async track(execution) {
    this.acceptedOperationExecutions.add(execution);
    try {
      return await execution;
    } finally {
      this.acceptedOperationExecutions.delete(execution);
    }
  }

  // false只表示本次等待超时；原Promise仍留在唯一集合中，由原执行链结束。
  async drain(timeoutMs = DEFAULT_OPERATION_DRAIN_TIMEOUT_MS) {
    return await drainBrowserExecutions(this.acceptedOperationExecutions, timeoutMs);
  }

  assertTransportOwnerRevision(transportOwnerRevision) {
    if (transportOwnerRevision !== this.browserSession.transportOwnerRevision) {
      const error = new Error("Browser 操作授权版本已撤销");
      error.code = "browser_transport_revoked";
      throw error;
    }
  }

  createOperation({ actor, action, visible = true, interactive = false }) {
    const operationRevision = this.operationRevision + 1;
    this.operationRevision = operationRevision;
    return {
      operation_id: `op_${this.browserSession.id}_${operationRevision}`,
      operation_revision: operationRevision,
      actor,
      action,
      visible,
      interactive,
      queued_at: nowIso(),
      started_at: null,
    };
  }

  async finish(operation, visible) {
    this.browserSession.record.operation_revision = Math.max(
      Number(this.browserSession.record.operation_revision || 0),
      operation.operation_revision,
    );
    if (operation.operation_revision >= Number(this.browserSession.record.last_operation?.operation_revision || 0)) {
      this.browserSession.record.last_operation = operation;
    }
    if (visible && this.browserSession.record.active_operation?.operation_id === operation.operation_id) {
      this.browserSession.record.active_operation = null;
    }
    this.browserSession.record.updated_at = operation.completed_at;
    if (visible) {
      await this.browserSession.manager.persist();
      this.browserSession.emit("state", this.browserSession.snapshot());
    }
  }
}
