import { describe, expect, test } from "bun:test";
import { BrowserResourceGovernor } from "./resourceGovernor.js";

describe("浏览器资源调度器", () => {
  test("普通状态按批次冻结多个候选", async () => {
    const frozen = [];
    const sessions = ["browser_a", "browser_b"].map((browserId) => ({
      id: browserId,
      resourceSnapshot: async () => ({
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
      postActionDelayMs: 0,
    });

    await governor.runCycle();

    governor.stop();
    expect(frozen).toEqual(["browser_a", "browser_b"]);
  });

  test("严重压力下执行一个已冻结资源的冷回收", async () => {
    const discarded = [];
    const session = {
      id: "browser_frozen",
      resourceSnapshot: async () => ({
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
      postActionDelayMs: 0,
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
      postActionDelayMs: 0,
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
    let nowMs = Date.parse("2026-01-01T01:00:00.000Z");
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
      now: () => nowMs,
    });

    await governor.runCycle();
    nowMs += 1_000;
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
    let nowMs = Date.parse("2026-01-01T01:00:00.000Z");
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
          session.frozenAt = new Date(nowMs).toISOString();
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
      now: () => nowMs,
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
      nowMs += nextDelayMs;
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
    const durations = [];
    for (let index = 0; index < 50; index += 1) {
      const startedAt = performance.now();
      const state = await governor.runCycle();
      durations.push(performance.now() - startedAt);
      expect(state.capacity.before).toEqual({ hot: 8, frozen: 16, resident: 24 });
      expect(state.last_actions).toEqual([]);
    }
    durations.sort((left, right) => left - right);

    expect(durations[Math.floor(durations.length * 0.95)]).toBeLessThan(100);
  });
});
