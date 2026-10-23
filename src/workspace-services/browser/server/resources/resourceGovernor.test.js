import { describe, expect, test } from "bun:test";
import { BrowserResourceGovernor } from "./resourceGovernor.js";

describe("浏览器资源调度器", () => {
  test("普通状态按批次冻结多个候选", async () => {
    const frozen = [];
    const sessions = ["browser_a", "browser_b"].map((browserId) => ({
      id: browserId,
      resourcePolicySnapshot: () => ({
        browser_id: browserId,
        resource_state: "background",
        resource_policy: "automatic",
        resource_protection_reasons: [],
        client_count: 0,
        created_at: "2026-01-01T00:00:00.000Z",
        last_user_interaction_at: "2026-01-01T00:00:00.000Z",
      }),
      freeze: async () => { frozen.push(browserId); },
    }));
    const manager = {
      runningSessions: () => sessions,
      get: (browserId) => sessions.find((session) => session.id === browserId),
    };
    const governor = new BrowserResourceGovernor({
      manager,
      memoryMonitor: { sample: async () => ({ level: "warning" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
      backlogIntervalMs: 0,
    });

    await governor.runCycle();

    governor.stop();
    expect(frozen).toEqual(["browser_a", "browser_b"]);
  });

  test("严重压力下执行一个已冻结资源的冷回收", async () => {
    const discarded = [];
    const session = {
      id: "browser_frozen",
      resourcePolicySnapshot: () => ({
        browser_id: "browser_frozen",
        resource_state: "frozen",
        resource_policy: "automatic",
        resource_protection_reasons: [],
        client_count: 0,
        created_at: "2026-01-01T00:00:00.000Z",
        last_user_interaction_at: "2026-01-01T00:00:00.000Z",
        frozen_at: "2026-01-01T00:10:00.000Z",
      }),
      discard: async () => { discarded.push("browser_frozen"); },
    };
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => [session],
        get: () => session,
      },
      memoryMonitor: { sample: async () => ({ level: "critical" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
      backlogIntervalMs: 0,
    });

    await governor.runCycle();

    governor.stop();
    expect(discarded).toEqual(["browser_frozen"]);
    expect(governor.snapshot().last_action.action).toBe("discard");
  });

  test("单项失败不会停止 governor 且同批其他资源继续执行", async () => {
    const frozen = [];
    const sessions = ["browser_fail", "browser_ok"].map((browserId) => ({
      id: browserId,
      resourcePolicySnapshot: () => ({
        browser_id: browserId,
        resource_state: "background",
        resource_policy: "automatic",
        resource_protections: [],
        client_count: 0,
        created_at: "2026-01-01T00:00:00.000Z",
        last_user_interaction_at: "2026-01-01T00:00:00.000Z",
      }),
      freeze: async () => {
        if (browserId === "browser_fail") throw new Error("freeze failed");
        frozen.push(browserId);
      },
    }));
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (browserId) => sessions.find((session) => session.id === browserId),
      },
      memoryMonitor: { sample: async () => ({ level: "warning" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
      backlogIntervalMs: 0,
    });
    governor.running = true;
    governor.state.running = true;

    await governor.runCycle();

    expect(frozen).toEqual(["browser_ok"]);
    expect(governor.snapshot().running).toBe(true);
    expect(governor.snapshot().recent_action_errors).toHaveLength(1);
    expect(governor.snapshot().recent_action_errors[0].browser_id).toBe("browser_fail");
    governor.stop();
  });

  test("配额失败资源进入一分钟退避而不会每秒重复命中", async () => {
    let freezeCalls = 0;
    let nowUnixMs = Date.parse("2026-01-01T01:00:00.000Z");
    const session = {
      id: "browser_quota_failed",
      resourcePolicySnapshot: () => ({
        browser_id: "browser_quota_failed",
        resource_state: "background",
        resource_policy: "automatic",
        resource_protections: [],
        client_count: 0,
        created_at: "2026-01-01T00:00:00.000Z",
        last_user_interaction_at: "2026-01-01T00:00:00.000Z",
      }),
      freeze: async () => {
        freezeCalls += 1;
        const error = new Error("quota exhausted");
        error.code = "browser_checkpoint_workspace_quota_exceeded";
        throw error;
      },
    };
    const governor = new BrowserResourceGovernor({
      manager: { runningSessions: () => [session], get: () => session },
      memoryMonitor: { sample: async () => ({ level: "critical" }) },
      now: () => nowUnixMs,
    });

    await governor.runCycle();
    nowUnixMs += 1_000;
    const second = await governor.runCycle();

    expect(freezeCalls).toBe(1);
    expect(second.last_cycle.action_backoff_count).toBe(1);
    expect(second.last_actions).toEqual([]);
  });

  test("单个策略快照失败会被hard保护且不阻塞其他资源回收", async () => {
    const frozen = [];
    const sessions = [
      {
        id: "browser_snapshot_fail",
        record: { resource_state: "background", resource_policy: "automatic" },
        resourcePolicySnapshot: () => {
          throw new Error("snapshot failed");
        },
      },
      {
        id: "browser_ok",
        resourcePolicySnapshot: () => ({
          browser_id: "browser_ok",
          resource_state: "background",
          resource_policy: "automatic",
          resource_protections: [],
          client_count: 0,
          created_at: "2026-01-01T00:00:00.000Z",
          last_user_interaction_at: "2026-01-01T00:00:00.000Z",
        }),
        freeze: async () => {
          frozen.push("browser_ok");
          return { resource_state: "frozen" };
        },
      },
    ];
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (browserId) => sessions.find((session) => session.id === browserId),
      },
      memoryMonitor: { sample: async () => ({ level: "warning" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
    });

    await governor.runCycle();

    expect(frozen).toEqual(["browser_ok"]);
    expect(governor.snapshot().recent_action_errors).toHaveLength(1);
    expect(governor.snapshot().recent_action_errors[0]).toMatchObject({
      browser_id: "browser_snapshot_fail",
      action: "snapshot",
    });
  });

  test("500个过载资源在正常压力下多轮收敛到8热16冻结24常驻", async () => {
    let nowUnixMs = Date.parse("2026-01-01T01:00:00.000Z");
    const oldActivity = "2026-01-01T00:00:00.000Z";
    const sessions = Array.from({ length: 500 }, (_, index) => {
      const session = {
        id: `browser_${index}`,
        state: "background",
        frozenAt: null,
        resourcePolicySnapshot: () => ({
          browser_id: session.id,
          resource_state: session.state,
          resource_policy: "automatic",
          resource_protections: [],
          client_count: 0,
          created_at: oldActivity,
          last_user_interaction_at: oldActivity,
          frozen_at: session.frozenAt,
        }),
        freeze: async () => {
          session.state = "frozen";
          session.frozenAt = new Date(nowUnixMs).toISOString();
          return { resource_state: "frozen" };
        },
        discard: async () => {
          session.state = "discarded";
          return { resource_state: "discarded" };
        },
      };
      return session;
    });
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (browserId) => sessions.find((session) => session.id === browserId),
      },
      memoryMonitor: { sample: async () => ({ level: "normal" }) },
      now: () => nowUnixMs,
    });
    let nextDelayMs = 0;
    governor.schedule = (delayMs) => {
      nextDelayMs = delayMs;
    };
    let finalState;
    let cycles = 0;
    for (; cycles < 500; cycles += 1) {
      finalState = await governor.runCycle();
      const capacity = finalState.capacity.actual_after_execution;
      if (capacity.hot === 8
        && capacity.frozen === 16
        && capacity.resident === 24
        && finalState.last_actions.length === 0) break;
      nowUnixMs += nextDelayMs;
    }

    expect(finalState.capacity.actual_after_execution).toEqual({ hot: 8, frozen: 16, resident: 24 });
    expect(cycles).toBeLessThan(500);
    expect(nextDelayMs).toBe(5_000);
  });

  test("500个已收敛逻辑资源的完整稳态治理扫描p95低于100ms", async () => {
    const sessions = [
      ...Array.from({ length: 8 }, (_, index) => ({
        id: `browser_hot_${index}`,
        state: "background",
      })),
      ...Array.from({ length: 16 }, (_, index) => ({
        id: `browser_frozen_${index}`,
        state: "frozen",
      })),
      ...Array.from({ length: 476 }, (_, index) => ({
        id: `browser_discarded_${index}`,
        state: "discarded",
      })),
    ].map(({ id, state }) => ({
      id,
      resourcePolicySnapshot: () => ({
        browser_id: id,
        resource_state: state,
        resource_policy: "automatic",
        resource_protections: [],
        client_count: 0,
        created_at: "2026-01-01T00:00:00.000Z",
        last_user_interaction_at: "2026-01-01T00:59:59.000Z",
        frozen_at: state === "frozen" ? "2026-01-01T00:59:59.000Z" : null,
      }),
    }));
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (browserId) => sessions.find((session) => session.id === browserId),
      },
      memoryMonitor: { sample: async () => ({ level: "normal" }) },
      now: () => Date.parse("2026-01-01T01:00:00.000Z"),
    });
    const scanDurationsMs = [];
    for (let index = 0; index < 50; index += 1) {
      const scanStartedAtPerformanceMs = performance.now();
      const state = await governor.runCycle();
      scanDurationsMs.push(performance.now() - scanStartedAtPerformanceMs);
      expect(state.capacity.before).toEqual({ hot: 8, frozen: 16, resident: 24 });
      expect(state.last_actions).toEqual([]);
    }
    scanDurationsMs.sort((left, right) => left - right);

    expect(scanDurationsMs[Math.floor(scanDurationsMs.length * 0.95)]).toBeLessThan(100);
  });

  test("资源持续退出治理列表时退避数量不积累，失败历史仍按上限保留", async () => {
    const nowUnixMs = Date.parse("2026-01-01T01:00:00.000Z");
    let sessions = [];
    let freezeCalls = 0;
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (browserId) => sessions.find((session) => session.id === browserId),
      },
      memoryMonitor: { sample: async () => ({ level: "warning" }) },
      now: () => nowUnixMs,
    });
    for (let index = 0; index < 32; index += 1) {
      const browserId = `browser_retired_${index}`;
      sessions = [{
        id: browserId,
        resourcePolicySnapshot: () => ({
          browser_id: browserId, resource_state: "background", resource_policy: "automatic",
          resource_protections: [], client_count: 0, created_at: "2026-01-01T00:00:00.000Z",
        }),
        freeze: async () => { freezeCalls += 1; throw new Error("本轮资源冻结失败"); },
      }];

      const state = await governor.runCycle();

      expect(state.last_cycle.action_backoff_count).toBe(1);
      expect(state.last_cycle.failed_action_count).toBe(1);
    }
    sessions = [];
    const finalState = await governor.runCycle();

    expect(freezeCalls).toBe(32);
    expect(finalState.last_cycle.action_backoff_count).toBe(0);
    expect(finalState.recent_action_errors).toHaveLength(20);
    expect(finalState.recent_action_errors.at(-1).browser_id).toBe("browser_retired_31");
  });

  test("退避到期后即使资源快照失败，也清除期限且保留快照错误", async () => {
    let nowUnixMs = Date.parse("2026-01-01T01:00:00.000Z");
    let snapshotFails = false;
    const session = {
      id: "browser_expired_snapshot",
      record: { resource_state: "background", resource_policy: "automatic" },
      resourcePolicySnapshot: () => {
        if (snapshotFails) throw new Error("到期后快照读取失败");
        return {
          browser_id: "browser_expired_snapshot", resource_state: "background", resource_policy: "automatic",
          resource_protections: [], client_count: 0, created_at: "2026-01-01T00:00:00.000Z",
        };
      },
      freeze: async () => { throw new Error("建立五秒动作退避"); },
    };
    const governor = new BrowserResourceGovernor({
      manager: { runningSessions: () => [session], get: () => session },
      memoryMonitor: { sample: async () => ({ level: "warning" }) },
      now: () => nowUnixMs,
    });
    const first = await governor.runCycle();
    expect(first.last_cycle.action_backoff_count).toBe(1);
    nowUnixMs += 5_000;
    snapshotFails = true;

    const expired = await governor.runCycle();

    expect(expired.last_cycle.action_backoff_count).toBe(0);
    expect(expired.last_cycle.snapshot_failure_count).toBe(1);
    expect(expired.last_actions).toEqual([]);
    expect(expired.recent_action_errors.at(-1)).toMatchObject({
      browser_id: session.id, action: "snapshot", message: "到期后快照读取失败",
    });
  });

  test.each([-60_000, 0, 60_000])("系统校时不改变完整治理轮次的动作预算与耗时: wall_clock_jump_ms=%s", async (wallClockJumpMs) => {
    const startedAtUnixMs = Date.parse("2026-01-01T01:00:00.000Z");
    let elapsedMs = 0;
    let wallOffsetMs = 0;
    const frozen = [];
    const sessions = Array.from({ length: 4 }, (_, index) => {
      const id = `browser_clock_${index}`;
      return {
        id,
        resourcePolicySnapshot: () => ({
          browser_id: id, resource_state: "background", resource_policy: "automatic",
          resource_protections: [], client_count: 0,
          created_at: "2026-01-01T00:00:00.000Z",
          last_user_interaction_at: "2026-01-01T00:00:00.000Z",
        }),
        freeze: async () => {
          elapsedMs += 150;
          wallOffsetMs = wallClockJumpMs;
          frozen.push(id);
          return { resource_state: "frozen" };
        },
      };
    });
    const governor = new BrowserResourceGovernor({
      manager: {
        runningSessions: () => sessions,
        get: (id) => sessions.find((session) => session.id === id),
      },
      memoryMonitor: { sample: async () => { elapsedMs += 10; return { level: "warning" }; } },
      now: () => startedAtUnixMs + elapsedMs + wallOffsetMs,
      monotonicNow: () => elapsedMs,
      batchPolicy: { warning: { maxActions: 4, maxDurationMs: 250, freezeConcurrency: 1, discardConcurrency: 4 } },
    });
    const delays = [];
    governor.schedule = (delayMs) => { delays.push(delayMs); };

    const state = await governor.runCycle();

    // 软预算只在动作块之间检查；第二个动作允许完成，第三个留给下一轮。
    expect(frozen).toEqual(["browser_clock_0", "browser_clock_1"]);
    expect(state.last_cycle).toMatchObject({
      started_at: new Date(startedAtUnixMs).toISOString(), duration_ms: 310,
      snapshot_count: 4, planned_action_count: 4, completed_action_count: 2,
      failed_action_count: 0, soft_action_budget_ms: 250,
    });
    expect(state.last_actions.map((action) => action.duration_ms)).toEqual([150, 150]);
    expect(state.last_actions.map((action) => action.started_at)).toEqual([
      new Date(startedAtUnixMs + 10).toISOString(),
      new Date(startedAtUnixMs + 160 + wallClockJumpMs).toISOString(),
    ]);
    expect(state.capacity.actual_after_execution).toEqual({ hot: 2, frozen: 2, resident: 4 });
    expect(delays).toEqual([100]);
    expect(state.recent_action_errors).toEqual([]);
  });
});
