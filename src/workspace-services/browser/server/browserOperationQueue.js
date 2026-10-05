import { nowIso } from "./url.js";

export class BrowserOperationQueue {
  constructor({ owner, revision = 0 }) {
    this.owner = owner;
    this.revision = Number(revision || 0);
    this.tail = Promise.resolve();
    this.executions = new Set();
  }

  async enqueue({ actor, action, visible = true, interactive = false, revision }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    const execution = this.tail.then(async () => {
      this.assertRevision(revision);
      await this.owner.prepareForOperation({ actor, action });
      this.owner.beginOperation(operation);
      operation.started_at = nowIso();
      try {
        this.assertRevision(revision);
        if (visible) {
          this.owner.record.active_operation = operation;
          this.owner.emit("state", this.owner.snapshot());
        }
        const result = await callback();
        this.assertRevision(revision);
        const completed = {
          ...operation,
          status: "completed",
          completed_at: nowIso(),
        };
        await this.finish(completed, visible);
        this.assertRevision(revision);
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
        this.owner.endOperation(operation);
      }
    });
    this.tail = execution.then(() => undefined, () => undefined);
    return await this.track(execution);
  }

  async runConcurrent({ actor, action, visible = true, interactive = false, revision }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    const execution = (async () => {
      this.assertRevision(revision);
      await this.owner.prepareForOperation({ actor, action });
      this.owner.beginOperation(operation);
      operation.started_at = nowIso();
      try {
        this.assertRevision(revision);
        const result = await callback();
        this.assertRevision(revision);
        const completed = { ...operation, status: "completed", completed_at: nowIso() };
        await this.finish(completed, false);
        this.assertRevision(revision);
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
        this.owner.endOperation(operation);
      }
    })();
    return await this.track(execution);
  }

  async track(execution) {
    this.executions.add(execution);
    try {
      return await execution;
    } finally {
      this.executions.delete(execution);
    }
  }

  async drain() {
    await Promise.allSettled([...this.executions]);
  }

  assertRevision(revision) {
    if (revision !== this.owner.transportOwnerRevision) {
      const error = new Error("Browser 操作授权版本已撤销");
      error.code = "browser_transport_revoked";
      throw error;
    }
  }

  createOperation({ actor, action, visible = true, interactive = false }) {
    const operationRevision = this.revision + 1;
    this.revision = operationRevision;
    return {
      operation_id: `op_${this.owner.id}_${operationRevision}`,
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
    this.owner.record.operation_revision = Math.max(
      Number(this.owner.record.operation_revision || 0),
      operation.operation_revision,
    );
    if (operation.operation_revision >= Number(this.owner.record.last_operation?.operation_revision || 0)) {
      this.owner.record.last_operation = operation;
    }
    if (visible && this.owner.record.active_operation?.operation_id === operation.operation_id) {
      this.owner.record.active_operation = null;
    }
    this.owner.record.updated_at = operation.completed_at;
    if (visible) {
      await this.owner.manager.persist();
      this.owner.emit("state", this.owner.snapshot());
    }
  }
}
