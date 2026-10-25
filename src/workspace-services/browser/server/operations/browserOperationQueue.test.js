import { expect, test } from "bun:test";
import { BrowserOperationQueue } from "./browserOperationQueue.js";

test.each([false, true])("排空等待期间启动的原执行仍由同一队列核实：reject=%s", async (reject) => {
  const queue = new BrowserOperationQueue({ browserSession: {} });
  let begin;
  let release;
  let child;
  const started = new Promise((resolve) => { begin = resolve; });
  const pending = new Promise((resolve) => { release = resolve; });
  const parent = queue.track((async () => {
    await started;
    // 外层请求先结束，内部原执行仍在等待；两者共享现有执行集合。
    child = queue.track(pending.then(() => { if (reject) throw new Error("原执行迟到失败"); }));
    child.catch(() => undefined);
  })());
  try {
    const draining = queue.drain(20);
    begin();
    await parent;
    expect(await draining).toBe(false);
    expect(queue.acceptedOperationExecutions.size).toBe(1);
    release();
    await Promise.allSettled([child]);
    expect(await queue.drain(0)).toBe(true);
  } finally {
    begin();
    release();
    await parent;
    await Promise.allSettled([child]);
  }
});
