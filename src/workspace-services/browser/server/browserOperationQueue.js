import { nowIso } from "./url.js";

export class BrowserOperationQueue {
  constructor({ owner, revision = 0 }) {
    this.owner = owner;
    this.revision = Number(revision || 0);
    this.tail = Promise.resolve();
  }

  async enqueue({ actor, action, visible = true, interactive = false }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    const execution = this.tail.then(async () => {
      await this.owner.prepareForOperation({ actor, action });
      this.owner.beginOperation(operation);
      operation.started_at = nowIso();
      if (visible) {
        this.owner.record.active_operation = operation;
        this.owner.emit("state", this.owner.snapshot());
      }
      try {
        const result = await callback();
        const completed = {
          ...operation,
          status: "completed",
          completed_at: nowIso(),
        };
        await this.finish(completed, visible);
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
    return await execution;
  }

  async runConcurrent({ actor, action, visible = true, interactive = false }, callback) {
    const operation = this.createOperation({ actor, action, visible, interactive });
    await this.owner.prepareForOperation({ actor, action });
    this.owner.beginOperation(operation);
    operation.started_at = nowIso();
    try {
      const result = await callback();
      const completed = {
        ...operation,
        status: "completed",
        completed_at: nowIso(),
      };
      await this.finish(completed, false);
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
      await this.finish(failed, false);
      throw error;
    } finally {
      this.owner.endOperation(operation);
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
