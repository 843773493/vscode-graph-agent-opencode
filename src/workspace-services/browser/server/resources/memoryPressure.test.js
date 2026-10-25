import { describe, expect, test } from "bun:test";
import path from "node:path";
import {
  BrowserMemoryPressureMonitor,
  classifyMemoryPressure,
} from "./memoryPressure.js";

function createLinuxMemoryMonitor(reading) {
  return new BrowserMemoryPressureMonitor({
    platform: "linux",
    now: () => 1_000,
    pathExists: (filePath) => filePath === "/sys/fs/cgroup/cgroup.controllers"
      || filePath === path.join("/sys/fs/cgroup", reading.directoryName, "memory.max"),
    readText: async (filePath) => {
      const directory = path.join("/sys/fs/cgroup", reading.directoryName);
      const files = {
        "/proc/meminfo": "MemTotal: 1000000 kB\nMemAvailable: 600000 kB\n",
        "/proc/self/cgroup": `0::/${reading.directoryName}\n`,
        [path.join(directory, "memory.max")]: reading.memoryMax,
        [path.join(directory, "memory.current")]: "400",
        [path.join(directory, "memory.events")]: `high 0\noom 0\noom_kill ${reading.oomKillCount}\n`,
      };
      if (!(filePath in files)) throw new Error(`测试未提供内存观测文件: ${filePath}`);
      return files[filePath];
    },
  });
}

describe("浏览器内存压力", () => {
  test("并行启动主机与 cgroup 内存采样", async () => {
    let hostReadStarted = false;
    let cgroupReadStarted = false;
    let resolveHost;
    let resolveCgroup;
    const hostSample = new Promise((resolve) => { resolveHost = resolve; });
    const cgroupSample = new Promise((resolve) => { resolveCgroup = resolve; });
    const monitor = new BrowserMemoryPressureMonitor({
      now: () => 1_000,
      monotonicNow: () => 500,
    });
    monitor.hostMemory = () => {
      hostReadStarted = true;
      return hostSample;
    };
    monitor.cgroupMemory = () => {
      cgroupReadStarted = true;
      return cgroupSample;
    };

    const sampling = monitor.sample();

    expect(hostReadStarted).toBe(true);
    expect(cgroupReadStarted).toBe(true);
    resolveHost({ totalBytes: 1_000, availableBytes: 500 });
    resolveCgroup(null);
    const sample = await sampling;

    expect(sample.effective_used_ratio).toBe(0.5);
    expect(sample.cgroup).toBeNull();
  });

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

  test.each(["win32", "darwin"])("%s 使用 Node 主机内存接口且不探测 Linux 文件", async platform => {
    let linuxFileProbeCount = 0;
    const monitor = new BrowserMemoryPressureMonitor({
      platform,
      totalMemory: () => 1_000_000,
      freeMemory: () => 200_000,
      pathExists: () => {
        linuxFileProbeCount += 1;
        return true;
      },
      readText: async () => {
        linuxFileProbeCount += 1;
        throw new Error("非 Linux 平台不应读取 Linux 内存文件");
      },
      now: () => 1_000,
    });

    const sample = await monitor.sample();

    expect(sample.host).toEqual({
      totalBytes: 1_000_000,
      availableBytes: 200_000,
      usedRatio: 0.8,
    });
    expect(sample.level).toBe("warning");
    expect(sample.cgroup).toBeNull();
    expect(linuxFileProbeCount).toBe(0);
  });

  test("OOM 事件立即升级为 emergency", () => {
    const result = classifyMemoryPressure({
      usedRatio: 0.4,
      eventDelta: { oom_kill: 1 },
      nowMonotonicMs: 1_000,
    });
    expect(result.level).toBe("emergency");
  });

  test("压力退出必须逐级经过各自的稳定迟滞窗口", () => {
    const first = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: "emergency",
      nowMonotonicMs: 1_000,
    });
    const second = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: first.level,
      exitCandidateSinceMonotonicMs: first.exitCandidateSinceMonotonicMs,
      nowMonotonicMs: 11_001,
    });
    const third = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: second.level,
      exitCandidateSinceMonotonicMs: second.exitCandidateSinceMonotonicMs,
      nowMonotonicMs: 11_002,
    });
    const fourth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: third.level,
      exitCandidateSinceMonotonicMs: third.exitCandidateSinceMonotonicMs,
      nowMonotonicMs: 31_003,
    });
    const fifth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fourth.level,
      exitCandidateSinceMonotonicMs: fourth.exitCandidateSinceMonotonicMs,
      nowMonotonicMs: 31_004,
    });
    const sixth = classifyMemoryPressure({
      usedRatio: 0.5,
      previousLevel: fifth.level,
      exitCandidateSinceMonotonicMs: fifth.exitCandidateSinceMonotonicMs,
      nowMonotonicMs: 61_005,
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
    expect(switched.cgroup.directory).toBe(path.join("/sys/fs/cgroup", "second"));
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

for (const platform of ["linux", "win32", "darwin"]) {
  const pressureExits = [
    ["emergency", 0.95, 10_000, "critical"],
    ["critical", 0.90, 20_000, "warning"],
    ["warning", 0.80, 30_000, "normal"],
  ].flatMap((entry) => [-60_000, 0, 60_000].map((jump) => [...entry, jump]));

  test.each(pressureExits)(
    `${platform}：%s（初始用量 %d）稳定 %d ms 后降为 %s，系统校时 %d ms`,
    async (initialLevel, initialUsedRatio, stableMs, nextLevel, wallClockJumpMs) => {
      let sampledAtUnixMs = Date.parse("2026-10-20T00:00:00.000Z");
      let sampledAtMonotonicMs = 1_000;
      let usedRatio = initialUsedRatio;
      const monitor = new BrowserMemoryPressureMonitor({
        platform,
        now: () => sampledAtUnixMs,
        monotonicNow: () => sampledAtMonotonicMs,
        totalMemory: () => 1_000_000,
        freeMemory: () => 1_000_000 * (1 - usedRatio),
        pathExists: () => false,
        readText: async (filePath) => {
          expect(filePath).toBe("/proc/meminfo");
          return `MemTotal: 1000000 kB\nMemAvailable: ${Math.round(1_000_000 * (1 - usedRatio))} kB\n`;
        },
      });
      expect((await monitor.sample()).level).toBe(initialLevel);

      usedRatio = 0.5;
      sampledAtUnixMs += 1_000;
      sampledAtMonotonicMs += 1_000;
      const firstLowSample = await monitor.sample();
      expect(firstLowSample.level).toBe(initialLevel);
      expect(firstLowSample.raw_level).toBe("normal");
      expect(firstLowSample.sampled_at).toBe(new Date(sampledAtUnixMs).toISOString());

      sampledAtUnixMs += stableMs - 1 + wallClockJumpMs;
      sampledAtMonotonicMs += stableMs - 1;
      const beforeStable = await monitor.sample();
      expect(beforeStable.level).toBe(initialLevel);
      expect(beforeStable.sampled_at).toBe(new Date(sampledAtUnixMs).toISOString());

      sampledAtUnixMs += 1;
      sampledAtMonotonicMs += 1;
      const stable = await monitor.sample();
      expect(stable.level).toBe(nextLevel);
      expect(stable.raw_level).toBe("normal");
      expect(stable.sampled_at).toBe(new Date(sampledAtUnixMs).toISOString());
    },
  );
}
