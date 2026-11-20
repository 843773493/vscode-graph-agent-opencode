import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { startMaintenance } from "@boxteam/plugin-sdk/maintenance";

const flush = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

describe("Node SDK 后台维护", () => {
  test("先校验整个静态集合，非法后项不会启动前项", () => {
    let calls = 0;
    expect(() => startMaintenance({
      identity: { instance_id: "plugin-1" },
      reportError() {},
      tasks: [
        { id: "valid", interval_ms: 5, run_immediately: true, run: async () => { calls += 1; } },
        { id: "invalid", interval_ms: 0, run_immediately: true, run: async () => {} },
      ],
    })).toThrow(/interval_ms/);
    expect(() => startMaintenance({ identity: {}, reportError() {}, tasks: [] })).toThrow(/非空数组/);
    expect(() => startMaintenance({ identity: {}, reportError() {}, tasks: [
      { id: "same", interval_ms: 1, run_immediately: true, run: async () => {} },
      { id: "same", interval_ms: 1, run_immediately: true, run: async () => {} },
    ] })).toThrow(/唯一/);
    expect(calls).toBe(0);
  });

  test("错误报告失败沿显式关闭返回，不会静默吞掉周期错误", async () => {
    const script = `
      import { startMaintenance } from "@boxteam/plugin-sdk/maintenance";
      startMaintenance({ identity: {}, reportError() { throw new Error("logger failed"); }, tasks: [
        { id: "report", interval_ms: 100, run_immediately: true, run: async () => { throw new Error("maintenance failed"); } },
      ] });
    `;
    const result = spawnSync(process.env.BOXTEAM_NODE_BIN ?? "node", ["--input-type=module", "--eval", script], {
      encoding: "utf8", timeout: 5000,
    });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("maintenance failed");
    expect(result.stderr).toContain("logger failed");
  });

  test("任务失败独立报告并在下一周期重试，关闭后停止新调用", async () => {
    let calls = 0, healthyCalls = 0;
    const reports = [];
    const maintenance = startMaintenance({
      identity: { instance_id: "plugin-1" },
      reportError: (...args) => reports.push(args),
      tasks: [{
        id: "screenshots", interval_ms: 20, run_immediately: true,
        run: async () => { calls += 1; if (calls === 1) throw new Error("transient"); },
      }, { id: "independent", interval_ms: 10, run_immediately: true, run: async () => { healthyCalls += 1; } }],
    });
    while (calls < 2) await flush(5);
    expect(reports).toHaveLength(1);
    expect(reports[0][0]).toBe("screenshots");
    expect(reports[0][1]).toEqual({ instance_id: "plugin-1" });
    expect(reports[0][2]).toBeGreaterThanOrEqual(0);
    expect(reports[0][3].message).toBe("transient");
    expect(healthyCalls).toBeGreaterThan(1);
    await maintenance.close();
    const callsAtClose = calls;
    await flush(30);
    expect(calls).toBe(callsAtClose);
  });

  test("慢任务不重入，关闭等待在途结果且只向首次关闭交付错误", async () => {
    let calls = 0;
    let rejectRun;
    const maintenance = startMaintenance({
      identity: {}, reportError() {},
      tasks: [{
        id: "slow", interval_ms: 5, run_immediately: true,
        run: () => {
          calls += 1;
          return new Promise((_resolve, reject) => { rejectRun = reject; });
        },
      }],
    });
    await flush(25);
    expect(calls).toBe(1);
    const firstClose = maintenance.close();
    expect(maintenance.close()).toBe(firstClose);
    let settled = false;
    void firstClose.catch(() => { settled = true; });
    await flush(5);
    expect(settled).toBe(false);
    rejectRun(new Error("late write failure"));
    await expect(firstClose).rejects.toThrow("late write failure");
    await expect(maintenance.close()).resolves.toBeUndefined();
    expect(calls).toBe(1);
  });

  test("配合 AbortSignal 退出的回调由关闭正常排空", async () => {
    let started = false;
    const maintenance = startMaintenance({ identity: {}, reportError() {}, tasks: [{
      id: "cancel", interval_ms: 100, run_immediately: true,
      run: signal => new Promise((_resolve, reject) => {
        started = true;
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      }),
    }] });
    while (!started) await flush(1);
    await expect(maintenance.close()).resolves.toBeUndefined();
  });
});
