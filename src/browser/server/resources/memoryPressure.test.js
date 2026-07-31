import { describe, expect, test } from "bun:test";
import {
  BrowserMemoryPressureMonitor,
  classifyMemoryPressure,
} from "./memoryPressure.js";

describe("浏览器内存压力", () => {
  test("使用 MemAvailable 判定主机压力", async () => {
    const monitor = new BrowserMemoryPressureMonitor({
      platform: "linux",
      pathExists: () => false,
      readText: async (filePath) => {
        expect(filePath).toBe("/proc/meminfo");
        return "MemTotal:       1000000 kB\nMemAvailable:    100000 kB\n";
      },
      now: () => 1_000,
    });

    const sample = await monitor.sample();

    expect(sample.level).toBe("critical");
    expect(sample.effective_used_ratio).toBeCloseTo(0.9);
    expect(sample.cgroup).toBeNull();
  });

  test("OOM 事件立即升级为 emergency", () => {
    const result = classifyMemoryPressure({
      usedRatio: 0.4,
      eventDelta: { oom_kill: 1 },
      nowMs: 1_000,
    });
    expect(result.level).toBe("emergency");
  });

  test("压力退出必须逐级经过各自的稳定迟滞窗口", () => {
    const first = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: "emergency",
      nowMs: 1_000,
    });
    const second = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: first.level,
      exitCandidateSince: first.exitCandidateSince,
      nowMs: 11_001,
    });
    const third = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: second.level,
      exitCandidateSince: second.exitCandidateSince,
      nowMs: 11_002,
    });
    const fourth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: third.level,
      exitCandidateSince: third.exitCandidateSince,
      nowMs: 31_003,
    });
    const fifth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fourth.level,
      exitCandidateSince: fourth.exitCandidateSince,
      nowMs: 31_004,
    });
    const sixth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fifth.level,
      exitCandidateSince: fifth.exitCandidateSince,
      nowMs: 61_005,
    });
    expect(first.level).toBe("emergency");
    expect(second.level).toBe("critical");
    expect(fourth.level).toBe("warning");
    expect(sixth.level).toBe("normal");
  });
});
