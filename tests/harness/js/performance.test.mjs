import { test, expect } from "bun:test";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { TestRunContext } from "./run-context.mjs";
import { Perf } from "./performance.mjs";

test("阶段异常保持原错误，并发阶段共享正确父节点", async () => {
  const context = TestRunContext.fromTestFile(fileURLToPath(import.meta.url));
  const perf = new Perf(context, { mode: "basic" });
  const error = new Error("阶段失败");
  try {
    await perf.stage("parent", {}, async () => {
      const results = await Promise.allSettled([
        perf.stage("failed", {}, async () => { throw error; }),
        perf.stage("passed", {}, async () => 3),
      ]);
      expect(results[0].reason).toBe(error);
      expect(results[1].value).toBe(3);
    });
  } finally { await context.close(); }
  const records = (await readFile(perf.path, "utf8")).trim().split("\n").map(line => JSON.parse(line));
  const parent = records.find(row => row.event === "start" && row.stage === "parent");
  expect(records.filter(row => row.event === "start" && row.parent_stage_id === parent.stage_id)).toHaveLength(2);
  expect(records.find(row => row.event === "end" && row.stage === "failed").status).toBe("failed");
});
