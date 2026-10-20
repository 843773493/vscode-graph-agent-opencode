import { describe, expect, test } from "bun:test";
import {
  BrowserMemoryPressureMonitor,
  classifyMemoryPressure,
} from "./memoryPressure.js";

function createLinuxMemoryMonitor(reading) {
  return new BrowserMemoryPressureMonitor({
    platform: "linux",
    now: () => 1_000,
    pathExists: (filePath) => filePath === "/sys/fs/cgroup/cgroup.controllers"
      || filePath === `/sys/fs/cgroup/${reading.directoryName}/memory.max`,
    readText: async (filePath) => {
      const directory = `/sys/fs/cgroup/${reading.directoryName}`;
      const files = {
        "/proc/meminfo": "MemTotal: 1000000 kB\nMemAvailable: 600000 kB\n",
        "/proc/self/cgroup": `0::/${reading.directoryName}\n`,
        [`${directory}/memory.max`]: reading.memoryMax,
        [`${directory}/memory.current`]: "400",
        [`${directory}/memory.events`]: `high 0\noom 0\noom_kill ${reading.oomKillCount}\n`,
      };
      if (!(filePath in files)) throw new Error(`测试未提供内存观测文件: ${filePath}`);
      return files[filePath];
    },
  });
}

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
      nowUnixMs: 1_000,
    });
    expect(result.level).toBe("emergency");
  });

  test("压力退出必须逐级经过各自的稳定迟滞窗口", () => {
    const first = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: "emergency",
      nowUnixMs: 1_000,
    });
    const second = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: first.level,
      exitCandidateSinceUnixMs: first.exitCandidateSinceUnixMs,
      nowUnixMs: 11_001,
    });
    const third = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: second.level,
      exitCandidateSinceUnixMs: second.exitCandidateSinceUnixMs,
      nowUnixMs: 11_002,
    });
    const fourth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: third.level,
      exitCandidateSinceUnixMs: third.exitCandidateSinceUnixMs,
      nowUnixMs: 31_003,
    });
    const fifth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fourth.level,
      exitCandidateSinceUnixMs: fourth.exitCandidateSinceUnixMs,
      nowUnixMs: 31_004,
    });
    const sixth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fifth.level,
      exitCandidateSinceUnixMs: fifth.exitCandidateSinceUnixMs,
      nowUnixMs: 61_005,
    });
    expect(first.level).toBe("emergency");
    expect(second.level).toBe("critical");
    expect(fourth.level).toBe("warning");
    expect(sixth.level).toBe("normal");
  });

  test("切换 cgroup 时重新建立累计事件基线，只将同组后续增量视为新事件", async () => {
    const reading = { directoryName: "first", memoryMax: "1000", oomKillCount: 1 };
    const monitor = createLinuxMemoryMonitor(reading);

    const first = await monitor.sample();
    expect(first.event_delta).toEqual({});
    expect(first.raw_level).toBe("normal");
    expect((await monitor.sample()).event_delta.oom_kill).toBe(0);

    reading.directoryName = "second";
    reading.oomKillCount = 20;
    const switched = await monitor.sample();
    expect(switched.cgroup.directory).toBe("/sys/fs/cgroup/second");
    expect(switched.event_delta).toEqual({});
    expect(switched.level).toBe("normal");

    reading.oomKillCount = 21;
    const newEvent = await monitor.sample();
    expect(newEvent.event_delta.oom_kill).toBe(1);
    expect(newEvent.level).toBe("emergency");
  });

  test("暂停有限额 cgroup 观测后丢弃旧基线，恢复时不补报历史 OOM", async () => {
    const reading = { directoryName: "first", memoryMax: "max", oomKillCount: 2 };
    const monitor = createLinuxMemoryMonitor(reading);

    expect((await monitor.sample()).cgroup).toBeNull();
    reading.memoryMax = "1000";
    const initialLimited = await monitor.sample();
    expect(initialLimited.event_delta).toEqual({});
    expect(initialLimited.level).toBe("normal");

    reading.memoryMax = "max";
    const unlimited = await monitor.sample();
    expect(unlimited.cgroup).toBeNull();
    expect(unlimited.event_delta).toEqual({});

    reading.oomKillCount = 10;
    reading.memoryMax = "1000";
    const resumed = await monitor.sample();
    expect(resumed.event_delta).toEqual({});
    expect(resumed.level).toBe("normal");

    reading.oomKillCount = 11;
    const newEvent = await monitor.sample();
    expect(newEvent.event_delta.oom_kill).toBe(1);
    expect(newEvent.level).toBe("emergency");
  });
});
