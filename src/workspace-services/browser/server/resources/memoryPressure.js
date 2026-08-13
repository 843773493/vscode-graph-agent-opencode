import os from "node:os";
import path from "node:path";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";

export const DEFAULT_MEMORY_PRESSURE_THRESHOLDS = Object.freeze({
  warningUsedRatio: 0.75,
  criticalUsedRatio: 0.85,
  emergencyUsedRatio: 0.92,
  emergencyExitUsedRatio: 0.85,
  criticalExitUsedRatio: 0.75,
  warningExitUsedRatio: 0.65,
  warningExitAvailableRatio: 0.30,
  warningExitPsiSomeAvg10: 5,
  emergencyExitStableMs: 10_000,
  criticalExitStableMs: 20_000,
  warningExitStableMs: 30_000,
});

const LEVEL_RANK = Object.freeze({ normal: 0, warning: 1, critical: 2, emergency: 3 });

function parseMeminfo(raw) {
  const values = new Map();
  for (const line of raw.split("\n")) {
    const match = line.match(/^([^:]+):\s+(\d+)\s+kB$/);
    if (match) {
      values.set(match[1], Number(match[2]) * 1024);
    }
  }
  const totalBytes = values.get("MemTotal");
  const availableBytes = values.get("MemAvailable");
  if (!Number.isFinite(totalBytes) || !Number.isFinite(availableBytes) || totalBytes <= 0) {
    throw new Error("/proc/meminfo 缺少有效的 MemTotal/MemAvailable");
  }
  return { totalBytes, availableBytes };
}

function parseKeyValueCounters(raw, source) {
  const result = {};
  for (const line of raw.trim().split("\n")) {
    if (!line) continue;
    const [key, value] = line.trim().split(/\s+/, 2);
    const parsed = Number(value);
    if (!key || !Number.isFinite(parsed)) {
      throw new Error(`${source} 包含非法计数器: ${line}`);
    }
    result[key] = parsed;
  }
  return result;
}

function parsePsi(raw) {
  const some = raw.split("\n").find((line) => line.startsWith("some "));
  if (!some) {
    throw new Error("memory.pressure 缺少 some 行");
  }
  const values = Object.fromEntries(
    some.slice(5).trim().split(/\s+/).map((entry) => {
      const [key, value] = entry.split("=", 2);
      return [key, Number(value)];
    }),
  );
  if (!Number.isFinite(values.avg10)) {
    throw new Error("memory.pressure 缺少有效的 some avg10");
  }
  return { someAvg10: values.avg10 };
}

function pressureLevelForRatio(usedRatio, thresholds) {
  if (usedRatio >= thresholds.emergencyUsedRatio) return "emergency";
  if (usedRatio >= thresholds.criticalUsedRatio) return "critical";
  if (usedRatio >= thresholds.warningUsedRatio) return "warning";
  return "normal";
}

function moreSevere(left, right) {
  return LEVEL_RANK[left] >= LEVEL_RANK[right] ? left : right;
}

export function classifyMemoryPressure({
  usedRatio,
  psiSomeAvg10 = 0,
  eventDelta = {},
  previousLevel = "normal",
  exitCandidateSince = null,
  nowMs,
  thresholds = DEFAULT_MEMORY_PRESSURE_THRESHOLDS,
}) {
  let rawLevel = pressureLevelForRatio(usedRatio, thresholds);
  if ((eventDelta.oom || 0) > 0 || (eventDelta.oom_kill || 0) > 0) {
    rawLevel = "emergency";
  } else if ((eventDelta.high || 0) > 0) {
    rawLevel = moreSevere(rawLevel, "critical");
  }

  if (LEVEL_RANK[rawLevel] >= LEVEL_RANK[previousLevel]) {
    return { level: rawLevel, exitCandidateSince: null, rawLevel };
  }

  let nextLevel;
  let exitEligible;
  let exitStableMs;
  if (previousLevel === "emergency") {
    nextLevel = "critical";
    exitEligible = usedRatio < thresholds.emergencyExitUsedRatio;
    exitStableMs = thresholds.emergencyExitStableMs;
  } else if (previousLevel === "critical") {
    nextLevel = "warning";
    exitEligible = usedRatio < thresholds.criticalExitUsedRatio;
    exitStableMs = thresholds.criticalExitStableMs;
  } else if (previousLevel === "warning") {
    nextLevel = "normal";
    exitEligible = usedRatio <= thresholds.warningExitUsedRatio
      && 1 - usedRatio >= thresholds.warningExitAvailableRatio
      && psiSomeAvg10 < thresholds.warningExitPsiSomeAvg10;
    exitStableMs = thresholds.warningExitStableMs;
  } else {
    return { level: rawLevel, exitCandidateSince: null, rawLevel };
  }
  if (!exitEligible) {
    return { level: previousLevel, exitCandidateSince: null, rawLevel };
  }
  const candidateSince = exitCandidateSince ?? nowMs;
  const level = nowMs - candidateSince >= exitStableMs ? nextLevel : previousLevel;
  return {
    level,
    exitCandidateSince: level === previousLevel ? candidateSince : null,
    rawLevel,
  };
}

async function linuxCgroupDirectory(readText, pathExists) {
  if (!pathExists("/sys/fs/cgroup/cgroup.controllers")) return null;
  const membership = await readText("/proc/self/cgroup");
  const unified = membership.split("\n").find((line) => line.startsWith("0::"));
  if (!unified) {
    throw new Error("/proc/self/cgroup 缺少 cgroup v2 统一层级记录");
  }
  return path.join("/sys/fs/cgroup", unified.slice(3).replace(/^\/+/, ""));
}

export class BrowserMemoryPressureMonitor {
  constructor({
    readText = (filePath) => readFile(filePath, "utf8"),
    platform = process.platform,
    totalMemory = () => os.totalmem(),
    freeMemory = () => os.freemem(),
    pathExists = existsSync,
    now = () => Date.now(),
    thresholds = DEFAULT_MEMORY_PRESSURE_THRESHOLDS,
  } = {}) {
    this.readText = readText;
    this.platform = platform;
    this.totalMemory = totalMemory;
    this.freeMemory = freeMemory;
    this.pathExists = pathExists;
    this.now = now;
    this.thresholds = { ...DEFAULT_MEMORY_PRESSURE_THRESHOLDS, ...thresholds };
    this.level = "normal";
    this.exitCandidateSince = null;
    this.previousEvents = null;
  }

  async hostMemory() {
    if (this.platform === "linux") {
      return parseMeminfo(await this.readText("/proc/meminfo"));
    }
    // TODO: Windows/macOS 后续改用平台原生内存压力通知，当前使用 Node 可用值。
    const totalBytes = this.totalMemory();
    const availableBytes = this.freeMemory();
    if (!Number.isFinite(totalBytes) || !Number.isFinite(availableBytes) || totalBytes <= 0) {
      throw new Error("无法取得有效的主机内存数据");
    }
    return { totalBytes, availableBytes };
  }

  async cgroupMemory() {
    if (this.platform !== "linux") return null;
    const directory = await linuxCgroupDirectory(this.readText, this.pathExists);
    if (!directory || !this.pathExists(path.join(directory, "memory.max"))) return null;
    const maxRaw = (await this.readText(path.join(directory, "memory.max"))).trim();
    if (maxRaw === "max") return null;
    const maxBytes = Number(maxRaw);
    const currentBytes = Number((await this.readText(path.join(directory, "memory.current"))).trim());
    if (!Number.isFinite(maxBytes) || !Number.isFinite(currentBytes) || maxBytes <= 0) {
      throw new Error(`cgroup 内存数据非法: current=${currentBytes}, max=${maxRaw}`);
    }
    const events = parseKeyValueCounters(
      await this.readText(path.join(directory, "memory.events")),
      "memory.events",
    );
    const pressurePath = path.join(directory, "memory.pressure");
    const psi = this.pathExists(pressurePath)
      ? parsePsi(await this.readText(pressurePath))
      : { someAvg10: 0 };
    return {
      directory,
      currentBytes,
      maxBytes,
      availableBytes: Math.max(0, maxBytes - currentBytes),
      usedRatio: Math.min(1, currentBytes / maxBytes),
      events,
      ...psi,
    };
  }

  async sample() {
    const sampledAtMs = this.now();
    const host = await this.hostMemory();
    const hostUsedRatio = Math.min(1, Math.max(0, 1 - host.availableBytes / host.totalBytes));
    const cgroup = await this.cgroupMemory();
    const effectiveUsedRatio = Math.max(hostUsedRatio, cgroup?.usedRatio ?? 0);
    const currentEvents = cgroup?.events ?? {};
    const eventDelta = {};
    if (this.previousEvents) {
      for (const key of new Set([...Object.keys(this.previousEvents), ...Object.keys(currentEvents)])) {
        eventDelta[key] = Math.max(0, (currentEvents[key] || 0) - (this.previousEvents[key] || 0));
      }
    }
    this.previousEvents = currentEvents;
    const classified = classifyMemoryPressure({
      usedRatio: effectiveUsedRatio,
      psiSomeAvg10: cgroup?.someAvg10 ?? 0,
      eventDelta,
      previousLevel: this.level,
      exitCandidateSince: this.exitCandidateSince,
      nowMs: sampledAtMs,
      thresholds: this.thresholds,
    });
    this.level = classified.level;
    this.exitCandidateSince = classified.exitCandidateSince;
    return {
      sampled_at: new Date(sampledAtMs).toISOString(),
      level: classified.level,
      raw_level: classified.rawLevel,
      effective_used_ratio: effectiveUsedRatio,
      host: {
        ...host,
        usedRatio: hostUsedRatio,
      },
      cgroup,
      event_delta: eventDelta,
    };
  }
}
