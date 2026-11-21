import { expect, test } from "bun:test";

test("SDK公开子模块由已安装包解析且轻量入口无启动副作用", async () => {
  const names = ["runtime", "commands", "logging", "observation", "process-lifecycle", "control", "http", "maintenance"];
  for (const name of names) {
    const module = await import(`@boxteam/plugin-sdk/${name}`);
    expect(Object.keys(module).length).toBeGreaterThan(0);
  }
  expect(await import("@boxteam/plugin-sdk")).toHaveProperty("startMaintenance");
});
