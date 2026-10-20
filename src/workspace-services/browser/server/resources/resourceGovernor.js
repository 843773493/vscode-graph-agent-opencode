import { BrowserMemoryPressureMonitor } from "./memoryPressure.js";
import { chooseResourcePlan, DEFAULT_RESOURCE_POLICY } from "./resourcePolicy.js";

/**
 * @typedef {object} ResourceBatchLimits
 * @property {number} maxActions 单轮计划的动作数量上限。
 * @property {number} maxDurationMs 动作块之间检查的软耗时预算；不打断在途动作。
 * @property {number} freezeConcurrency 同一动作块最多并发冻结的资源数。
 * @property {number} discardConcurrency 同一动作块最多并发冷回收的资源数。
 */

export const DEFAULT_RESOURCE_BATCH_POLICY = Object.freeze({
  normal: Object.freeze({ maxActions: 4, maxDurationMs: 250, freezeConcurrency: 1, discardConcurrency: 4 }),
  warning: Object.freeze({ maxActions: 8, maxDurationMs: 500, freezeConcurrency: 1, discardConcurrency: 4 }),
  critical: Object.freeze({ maxActions: 16, maxDurationMs: 1_000, freezeConcurrency: 1, discardConcurrency: 4 }),
  emergency: Object.freeze({ maxActions: 32, maxDurationMs: 1_000, freezeConcurrency: 1, discardConcurrency: 4 }),
});

const RECENT_ACTION_ERROR_LIMIT = 20;

function errorRecord(error, decision, occurredAtUnixMs) {
  return {
    browser_id: decision.browserId,
    action: decision.action,
    reason: decision.reason,
    error_code: error?.code || "browser_resource_action_failed",
    message: error instanceof Error ? error.message : String(error),
    occurred_at: new Date(occurredAtUnixMs).toISOString(),
  };
}

function capacityAfterExecution(capacity, completed) {
  const actual = { ...capacity.before };
  for (const action of completed) {
    if (action.action === "freeze" && action.final_resource_state === "frozen") {
      actual.hot = Math.max(0, actual.hot - 1);
      actual.frozen += 1;
    }
    if (action.action === "discard" && action.final_resource_state === "discarded") {
      actual.frozen = Math.max(0, actual.frozen - 1);
      actual.resident = Math.max(0, actual.resident - 1);
    }
  }
  return actual;
}

export class BrowserResourceGovernor {
  constructor({
    manager,
    memoryMonitor = new BrowserMemoryPressureMonitor(),
    policy = DEFAULT_RESOURCE_POLICY,
    batchPolicy = DEFAULT_RESOURCE_BATCH_POLICY,
    normalIntervalMs = 5_000,
    pressureIntervalMs = 1_000,
    backlogIntervalMs = 100,
    failedBatchBackoffMs = 1_000,
    allowDiscard = true,
    now = () => Date.now(),
  }) {
    this.manager = manager;
    this.memoryMonitor = memoryMonitor;
    this.policy = { ...DEFAULT_RESOURCE_POLICY, ...policy };
    this.batchPolicy = batchPolicy;
    this.normalIntervalMs = normalIntervalMs;
    this.pressureIntervalMs = pressureIntervalMs;
    this.backlogIntervalMs = backlogIntervalMs;
    this.failedBatchBackoffMs = failedBatchBackoffMs;
    this.allowDiscard = allowDiscard;
    /** @type {() => number} 注入时钟返回Unix毫秒绝对时刻，ISO回执与时差预算共用它。 */
    this.now = now;
    this.timer = null;
    this.running = false;
    /** @type {Map<string, number>} 资源下次允许动作的Unix毫秒期限，不是退避时长。 */
    this.actionRetryAfterUnixMs = new Map();
    this.state = {
      running: false,
      last_sample: null,
      last_action: null,
      last_actions: [],
      recent_action_errors: [],
      capacity: null,
      last_cycle: null,
      error: null,
    };
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.state.running = true;
    this.schedule(0);
  }

  stop() {
    this.running = false;
    this.state.running = false;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  schedule(delayMs) {
    if (!this.running) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.runCycle().catch((error) => {
        const message = error instanceof Error ? (error.stack || error.message) : String(error);
        this.state.error = message;
        console.error(`[browser-resource-governor] 资源调度停止: ${message}`);
        this.stop();
      });
    }, delayMs);
    this.timer.unref?.();
  }

  async collectPolicySnapshots() {
    const sessions = this.manager.runningSessions();
    if (this.actionRetryAfterUnixMs.size > 0) {
      const currentBrowserIds = new Set(sessions.map((session) => session.id));
      const nowUnixMs = this.now();
      // 退避只属于当前治理对象；离开列表或已到期的期限不依赖快照成功才能清理。
      for (const [browserId, retryAfterUnixMs] of this.actionRetryAfterUnixMs) {
        if (!currentBrowserIds.has(browserId) || retryAfterUnixMs <= nowUnixMs) {
          this.actionRetryAfterUnixMs.delete(browserId);
        }
      }
    }
    const settled = await Promise.allSettled(sessions.map(async (session) => session.resourcePolicySnapshot()));
    const snapshots = [];
    const errors = [];
    for (let index = 0; index < settled.length; index += 1) {
      const result = settled[index];
      const session = sessions[index];
      if (result.status === "fulfilled") {
        const retryAfterUnixMs = this.actionRetryAfterUnixMs.get(session.id) || 0;
        if (retryAfterUnixMs <= this.now()) {
          this.actionRetryAfterUnixMs.delete(session.id);
          snapshots.push(result.value);
        } else {
          snapshots.push({
            ...result.value,
            resource_protections: [
              ...(result.value.resource_protections || []),
              {
                code: "resource_action_backoff",
                class: "hard",
                observed_at: new Date(this.now()).toISOString(),
                expires_at: new Date(retryAfterUnixMs).toISOString(),
              },
            ],
          });
        }
        continue;
      }
      const failure = errorRecord(
        result.reason,
        { browserId: session.id, action: "snapshot", reason: "policy_snapshot" },
        this.now(),
      );
      errors.push(failure);
      snapshots.push({
        browser_id: session.id,
        resource_state: session.record?.resource_state || "unknown",
        resource_policy: session.record?.resource_policy || "automatic",
        resource_protections: [{
          code: `resource_snapshot_failed:${failure.message}`,
          class: "hard",
          observed_at: failure.occurred_at,
          expires_at: null,
        }],
        client_count: session.clients?.size || 0,
        created_at: session.record?.created_at || failure.occurred_at,
      });
    }
    return { snapshots, errors };
  }

  async executeDecision(decision, pressureLevel) {
    const session = this.manager.get(decision.browserId);
    const actionStartedAtUnixMs = this.now();
    const allowSoftProtection = ["critical", "emergency"].includes(pressureLevel);
    let finalSnapshot;
    if (decision.action === "freeze") {
      finalSnapshot = await session.freeze({ reason: decision.reason, allowSoftProtection });
    } else {
      finalSnapshot = await session.discard({ reason: decision.reason, allowSoftProtection });
    }
    return {
      ...decision,
      pressure_level: pressureLevel,
      started_at: new Date(actionStartedAtUnixMs).toISOString(),
      duration_ms: this.now() - actionStartedAtUnixMs,
      final_resource_state: finalSnapshot?.resource_state
        || (decision.action === "freeze" ? "frozen" : "discarded"),
    };
  }

  /** @param {ResourceBatchLimits} limits */
  async executePlan(actions, pressureLevel, limits) {
    const batchStartedAtUnixMs = this.now();
    const completed = [];
    const errors = [];
    let cursor = 0;
    while (cursor < actions.length) {
      if (completed.length + errors.length > 0 && this.now() - batchStartedAtUnixMs >= limits.maxDurationMs) {
        break;
      }
      const actionName = actions[cursor].action;
      const concurrency = actionName === "discard"
        ? limits.discardConcurrency
        : limits.freezeConcurrency;
      const chunk = [];
      while (cursor < actions.length
        && actions[cursor].action === actionName
        && chunk.length < concurrency) {
        chunk.push(actions[cursor]);
        cursor += 1;
      }
      const settled = await Promise.all(chunk.map(async (decision) => {
        try {
          return { result: await this.executeDecision(decision, pressureLevel) };
        } catch (error) {
          return { error: errorRecord(error, decision, this.now()) };
        }
      }));
      for (const item of settled) {
        if (item.result) completed.push(item.result);
        if (item.error) errors.push(item.error);
      }
    }
    return {
      completed,
      errors,
      unexecuted: actions.slice(cursor),
      duration_ms: this.now() - batchStartedAtUnixMs,
    };
  }

  async runCycle() {
    const cycleStartedAtUnixMs = this.now();
    const sample = await this.memoryMonitor.sample();
    this.state.last_sample = sample;
    this.state.error = null;
    const limits = this.batchPolicy[sample.level];
    if (!limits) {
      throw new Error(`未知浏览器资源压力等级: ${sample.level}`);
    }
    const nowUnixMs = this.now();
    const collection = await this.collectPolicySnapshots();
    const plan = chooseResourcePlan(collection.snapshots, {
      pressureLevel: sample.level,
      nowUnixMs,
      policy: this.policy,
      allowDiscard: this.allowDiscard,
      maxActions: limits.maxActions,
    });
    const execution = await this.executePlan(plan.actions, sample.level, limits);
    this.state.capacity = {
      ...plan.capacity,
      actual_after_execution: capacityAfterExecution(plan.capacity, execution.completed),
    };
    this.state.last_actions = execution.completed;
    this.state.last_action = execution.completed.at(-1) || null;
    const cycleErrors = [...collection.errors, ...execution.errors];
    for (const completed of execution.completed) {
      this.actionRetryAfterUnixMs.delete(completed.browserId);
    }
    for (const error of execution.errors) {
      const backoffMs = [
        "browser_checkpoint_too_large",
        "browser_checkpoint_workspace_quota_exceeded",
      ].includes(error.error_code) ? 60_000 : 5_000;
      this.actionRetryAfterUnixMs.set(error.browser_id, this.now() + backoffMs);
    }
    if (cycleErrors.length > 0) {
      for (const error of cycleErrors) {
        console.error(
          `[browser-resource-governor] 单项资源调度失败: browser_id=${error.browser_id} action=${error.action} error=${error.message}`,
        );
      }
      this.state.recent_action_errors = [
        ...this.state.recent_action_errors,
        ...cycleErrors,
      ].slice(-RECENT_ACTION_ERROR_LIMIT);
    }
    const hasBacklog = plan.has_backlog || execution.unexecuted.length > 0;
    const allAttemptedActionsFailed = plan.actions.length > 0
      && execution.completed.length === 0
      && execution.errors.length > 0;
    const nextCycleDelayMs = allAttemptedActionsFailed
      ? this.failedBatchBackoffMs
      : (hasBacklog
          ? this.backlogIntervalMs
          : (sample.level === "normal" ? this.normalIntervalMs : this.pressureIntervalMs));
    this.state.last_cycle = {
      started_at: new Date(cycleStartedAtUnixMs).toISOString(),
      duration_ms: this.now() - cycleStartedAtUnixMs,
      snapshot_count: collection.snapshots.length,
      planned_action_count: plan.actions.length,
      completed_action_count: execution.completed.length,
      failed_action_count: execution.errors.length,
      snapshot_failure_count: collection.errors.length,
      total_failure_count: cycleErrors.length,
      action_backoff_count: this.actionRetryAfterUnixMs.size,
      soft_action_budget_ms: limits.maxDurationMs,
    };
    this.schedule(nextCycleDelayMs);
    return this.snapshot();
  }

  snapshot() {
    return structuredClone(this.state);
  }
}
