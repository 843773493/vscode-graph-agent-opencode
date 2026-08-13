import { describe, expect, test } from "bun:test";
import { chooseResourceAction, chooseResourcePlan } from "./resourcePolicy.js";

function snapshot(overrides = {}) {
  return {
    browser_id: "browser_default",
    resource_state: "background",
    resource_policy: "automatic",
    resource_protection_reasons: [],
    client_count: 0,
    created_at: "2026-01-01T00:00:00.000Z",
    last_user_interaction_at: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

describe("浏览器资源候选策略", () => {
  test("普通状态冻结最早空闲且无保护的资源", () => {
    const nowMs = Date.parse("2026-01-01T00:20:00.000Z");
    const decision = chooseResourceAction([
      snapshot({ browser_id: "browser_new", last_user_interaction_at: "2026-01-01T00:15:00.000Z" }),
      snapshot({ browser_id: "browser_old" }),
    ], { nowMs });
    expect(decision).toMatchObject({ action: "freeze", browserId: "browser_old" });
  });

  test("紧急压力仍保护 hard 资源但允许回收 soft keep-alive", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const decision = chooseResourceAction([
      snapshot({ resource_protection_reasons: ["operation_in_flight"] }),
      snapshot({ browser_id: "browser_keep", resource_policy: "keep_alive" }),
    ], { nowMs, pressureLevel: "emergency" });
    expect(decision).toMatchObject({ action: "freeze", browserId: "browser_keep" });
  });

  test("严重压力下优先回收已冻结资源", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const decision = chooseResourceAction([
      snapshot({
        browser_id: "browser_frozen",
        resource_state: "frozen",
        frozen_at: "2026-01-01T00:30:00.000Z",
      }),
      snapshot({ browser_id: "browser_background" }),
    ], { nowMs, pressureLevel: "critical" });
    expect(decision).toMatchObject({ action: "discard", browserId: "browser_frozen" });
  });

  test("正常压力的容量回压只等待冻结稳定期而不等待十分钟空闲", () => {
    const nowMs = Date.parse("2026-01-01T01:00:31.000Z");
    const snapshots = [
      ...Array.from({ length: 9 }, (_, index) => snapshot({
        browser_id: `browser_hot_${index}`,
        last_user_interaction_at: "2026-01-01T01:00:00.000Z",
      })),
      ...Array.from({ length: 16 }, (_, index) => snapshot({
        browser_id: `browser_frozen_${index}`,
        resource_state: "frozen",
        last_user_interaction_at: "2026-01-01T01:00:00.000Z",
        frozen_at: "2026-01-01T01:00:00.000Z",
      })),
    ];

    expect(chooseResourcePlan(snapshots, { nowMs, maxActions: 1 }).actions[0])
      .toMatchObject({ action: "discard", reason: "resident_capacity" });
  });

  test("一次生成去重批量计划并遵守热与冻结容量", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const plan = chooseResourcePlan(
      Array.from({ length: 100 }, (_, index) => snapshot({ browser_id: `browser_${index}` })),
      { nowMs, maxActions: 100 },
    );

    expect(plan.actions).toHaveLength(16);
    expect(new Set(plan.actions.map((action) => action.browserId)).size).toBe(16);
    expect(plan.actions.every((action) => action.action === "freeze")).toBe(true);
    expect(plan.capacity.after_plan).toEqual({ hot: 84, frozen: 16, resident: 100 });
    expect(plan.has_backlog).toBe(false);
    expect(plan.capacity.overflow_after_plan).toEqual({ hot: 76, frozen: 0, resident: 76 });
  });

  test("转换中资源计入常驻容量且 hard 保护不会被强制回收", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const snapshots = [
      ...Array.from({ length: 24 }, (_, index) => snapshot({
        browser_id: `browser_transition_${index}`,
        resource_state: "restoring",
      })),
      snapshot({
        browser_id: "browser_protected",
        resource_protections: [{ code: "user_attached", class: "hard" }],
      }),
    ];
    const plan = chooseResourcePlan(snapshots, { nowMs, pressureLevel: "emergency", maxActions: 32 });

    expect(plan.actions).toEqual([]);
    expect(plan.capacity.before.resident).toBe(25);
    expect(plan.capacity.protected_capacity_overflow).toBe(false);
    expect(plan.capacity.blockers).toEqual({ protected: 1, not_yet_eligible: 0, transitioning: 24 });
  });

  test("容量仅被hard保护资源占满时明确归因保护溢出", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const snapshots = Array.from({ length: 25 }, (_, index) => snapshot({
      browser_id: `browser_protected_${index}`,
      resource_protections: [{ code: "user_attached", class: "hard" }],
    }));
    const plan = chooseResourcePlan(snapshots, { nowMs, pressureLevel: "emergency", maxActions: 32 });

    expect(plan.actions).toEqual([]);
    expect(plan.capacity.protected_capacity_overflow).toBe(true);
    expect(plan.capacity.blockers).toEqual({ protected: 25, not_yet_eligible: 0, transitioning: 0 });
  });

  test("100个空闲资源在持续critical压力下经多轮批处理全部冷回收", () => {
    let nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const snapshots = Array.from({ length: 100 }, (_, index) => snapshot({
      browser_id: `browser_${index}`,
    }));

    for (let round = 0; round < 20; round += 1) {
      const plan = chooseResourcePlan(snapshots, {
        nowMs,
        maxActions: 32,
        pressureLevel: "critical",
      });
      for (const action of plan.actions) {
        const current = snapshots.find((item) => item.browser_id === action.browserId);
        current.resource_state = action.action === "freeze" ? "frozen" : "discarded";
        if (action.action === "freeze") current.frozen_at = new Date(nowMs).toISOString();
      }
      nowMs += 31_000;
      if (plan.actions.length === 0) break;
    }

    const finalPlan = chooseResourcePlan(snapshots, {
      nowMs,
      maxActions: 32,
      pressureLevel: "critical",
    });
    expect(finalPlan.actions).toEqual([]);
    expect(finalPlan.capacity.before).toEqual({ hot: 0, frozen: 0, resident: 0 });
  });

  test("500个逻辑资源在8热16冻结预算下保持固定常驻量", () => {
    const nowMs = Date.parse("2026-01-01T01:00:00.000Z");
    const snapshots = [
      ...Array.from({ length: 8 }, (_, index) => snapshot({ browser_id: `browser_hot_${index}` })),
      ...Array.from({ length: 16 }, (_, index) => snapshot({
        browser_id: `browser_frozen_${index}`,
        resource_state: "frozen",
        frozen_at: "2026-01-01T00:30:00.000Z",
      })),
      ...Array.from({ length: 476 }, (_, index) => snapshot({
        browser_id: `browser_discarded_${index}`,
        resource_state: "discarded",
      })),
    ];
    const durations = Array.from({ length: 50 }, () => {
      const startedAt = performance.now();
      const plan = chooseResourcePlan(snapshots, { nowMs, maxActions: 32 });
      expect(plan.actions).toEqual([]);
      expect(plan.capacity.before).toEqual({ hot: 8, frozen: 16, resident: 24 });
      return performance.now() - startedAt;
    }).sort((left, right) => left - right);
    const p95 = durations[Math.floor(durations.length * 0.95)];

    expect(p95).toBeLessThan(100);
  });
});
