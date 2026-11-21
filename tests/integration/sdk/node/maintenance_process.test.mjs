import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";

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
