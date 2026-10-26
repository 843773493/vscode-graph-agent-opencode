import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  configureRuntimeLogging, createRuntimeLogger, formatRuntimeLogRecord, loadRuntimeLogContract,
  applyRuntimeLogging, runtimeLoggingState,
} from "./serverRuntimeLogging.mjs";

const contract = loadRuntimeLogContract();
const samples = JSON.parse(readFileSync("resources/logging/record-samples.json", "utf8"));
afterEach(() => configureRuntimeLogging({}, contract));

for (const pretty of [true, false]) {
  test(`Python 与 JS 共用完整记录样本：pretty=${pretty}`, () => {
    for (const sample of samples) {
      expect(formatRuntimeLogRecord(sample.record, pretty, contract)).toBe(sample[pretty ? "pretty" : "compact"]);
    }
  });
}

describe("JS 产品日志真实入口", () => {
  for (const [level, expected] of [
    [" DEBUG ", ["debug", "info", "warning", "error", "critical"]],
    ["info", ["info", "warning", "error", "critical"]],
    ["warning", ["warning", "error", "critical"]],
    ["error", ["error", "critical"]], ["critical", ["critical"]],
  ]) {
    test(`五等级过滤：${level}`, () => {
      configureRuntimeLogging({ level, pretty: false }, contract);
      const output = [];
      const logger = createRuntimeLogger("terminal", "test", { write: (value) => output.push(value) }, contract);
      for (const name of ["debug", "info", "warning", "error", "critical"]) logger[name]("中文 %s", name);
      expect(output.map((value) => /level=(\w+)/.exec(value)[1])).toEqual(expected);
      for (const value of output) {
        expect(value).toMatch(/timestamp=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z/);
        expect(value).toContain(`service=terminal pid=${process.pid} runtime_instance_id=`);
        expect(value).toContain("logger=test");
        expect(value).not.toMatch(/workspace_id=|session_id=|thread_id=|request_id=/);
      }
    });
  }

  test("默认设置、热调格式和等级保持同一进程身份，真实上下文和完整异常均保留", () => {
    const initial = configureRuntimeLogging({}, contract);
    expect(initial.level).toBe("info");
    expect(initial.pretty).toBe(true);
    const output = [];
    const logger = createRuntimeLogger("browser", "test", { write: (value) => output.push(value) }, contract);
    logger.debug("默认过滤");
    logger.info("共享进程");
    const updated = configureRuntimeLogging({ level: "debug", pretty: false }, contract);
    expect(updated.runtime_instance_id).toBe(initial.runtime_instance_id);
    const cause = new Error("原始异常 中文");
    logger.withContext({ workspace_id: "workspace-real", request_id: "request-real" }).error("失败\n第二行", cause);
    logger.info("作用域已结束");
    expect(output).toHaveLength(3);
    expect(output[0]).toContain("[INFO]");
    expect(output[0]).not.toContain("workspace_id=");
    expect(output[1]).toContain('workspace_id="workspace-real" request_id="request-real"');
    expect(output[1]).toContain("失败\n第二行");
    expect(output[1]).toContain(cause.stack);
    expect(output[2]).not.toContain("request_id=");
    expect(output.every((line) => line.includes(`runtime_instance_id=${initial.runtime_instance_id}`))).toBe(true);
  });

  for (const level of ["verbose", "", "warn", null, true, 15]) {
    test(`拒绝闭集外的等级 ${JSON.stringify(level)}`, () => {
      expect(() => configureRuntimeLogging({ level }, contract)).toThrow("不支持的日志级别");
    });
  }
  for (const pretty of ["false", 1, [], {}, null]) {
    test(`拒绝非布尔 pretty ${JSON.stringify(pretty)}`, () => {
      expect(() => configureRuntimeLogging({ pretty }, contract)).toThrow("必须是布尔值");
    });
  }

  test("sink 写入失败保留原异常且不重复尝试", () => {
    const original = new Error("写入失败");
    let attempts = 0;
    const logger = createRuntimeLogger("launcher", "test", {
      write() { attempts++; throw original; },
    }, contract);
    let observed;
    try { logger.error("不能静默失败"); } catch (error) { observed = error; }
    expect(observed).toBe(original);
    expect(attempts).toBe(1);
  });

  test("实例与 revision CAS 拒绝迟到请求，同 revision 幂等并改变真实过滤", () => {
    const before = runtimeLoggingState("browser", contract);
    const payload = {
      runtime_instance_id: before.runtime_instance_id, revision: "r1", expected_revision: null,
      level: "WARNING", pretty: false,
    };
    const applied = applyRuntimeLogging(payload, "browser", contract);
    expect(applied).toEqual({ ...before, applied_revision: "r1", level: "warning", pretty: false });
    expect(applyRuntimeLogging(payload, "browser", contract)).toEqual(applied);
    expect(() => applyRuntimeLogging({ ...payload, pretty: true }, "browser", contract)).toThrow("设置不一致");
    expect(() => applyRuntimeLogging({ ...payload, runtime_instance_id: "old" }, "browser", contract)).toThrow("运行实例已变化");
    expect(() => applyRuntimeLogging({ ...payload, revision: "late" }, "browser", contract)).toThrow("当前 revision 已变化");
    expect(runtimeLoggingState("browser", contract)).toEqual(applied);
    const output = [];
    const logger = createRuntimeLogger("browser", "test", { write: (value) => output.push(value) }, contract);
    logger.debug("不能出现"); logger.warning("真实 WARNING");
    expect(output).toHaveLength(1);
    expect(output[0]).toContain("level=warning");
    expect(output[0]).toContain("真实 WARNING");
    const next = applyRuntimeLogging({ ...payload, revision: "r2", expected_revision: "r1", pretty: true }, "browser", contract);
    expect(next.runtime_instance_id).toBe(before.runtime_instance_id);
    expect(() => applyRuntimeLogging(payload, "browser", contract)).toThrow("当前 revision 已变化");
  });

  for (const invalid of [null, [], {}, { pretty: "false" }, { pretty: 0 }, { level: "warn" },
    { revision: " " }, { unknown: 1 }, { expected_revision: 1 }]) {
    test(`非法控制参数不改变进程设置：${JSON.stringify(invalid)}`, () => {
      const before = runtimeLoggingState("terminal", contract);
      const payload = invalid === null || Array.isArray(invalid) || Object.keys(invalid).length === 0
        ? invalid : { runtime_instance_id: before.runtime_instance_id, revision: "r1",
          expected_revision: null, level: "debug", pretty: false, ...invalid };
      expect(() => applyRuntimeLogging(payload, "terminal", contract)).toThrow("日志应用参数非法");
      expect(runtimeLoggingState("terminal", contract)).toEqual(before);
    });
  }
});
