import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { startMaintenance } from "@boxteam/plugin-sdk/maintenance";

test("关闭期间保留另一个原生操作的取消异常，不按同名 AbortError 吞掉", async () => {
  const other = new AbortController();
  other.abort(new Error("独立操作取消"));
  let unrelated;
  try {
    await sleep(1000, undefined, { signal: other.signal });
  } catch (error) {
    unrelated = error;
  }
  assert.equal(unrelated.name, "AbortError");
  assert.equal(unrelated.cause, other.signal.reason);
  let rejectRun;
  const maintenance = startMaintenance({ identity: {}, reportError() {}, tasks: [{
    id: "native-abort", interval_ms: 1000, run_immediately: true,
    run: () => new Promise((_resolve, reject) => { rejectRun = reject; }),
  }] });
  const closing = maintenance.close();
  rejectRun(unrelated);
  await assert.rejects(closing, error => error === unrelated);
  await maintenance.close();
});

test("本次停止信号导致的原生 timer 取消正常排空", async () => {
  const maintenance = startMaintenance({ identity: {}, reportError() {}, tasks: [{
    id: "own-abort", interval_ms: 1000, run_immediately: true,
    run: signal => sleep(10_000, undefined, { signal }),
  }] });
  await maintenance.close();
});
