import { describe, expect, test } from "bun:test";
import { LatestPointerMoveDispatcher } from "./latestPointerMoveDispatcher.js";

describe("浏览器指针移动最新值调度", () => {
  test("执行中只保留最后一个移动事件", async () => {
    const dispatched = [];
    let releaseFirst;
    const firstBarrier = new Promise((resolve) => { releaseFirst = resolve; });
    const dispatcher = new LatestPointerMoveDispatcher(async (message) => {
      dispatched.push(message.x);
      if (message.x === 1) await firstBarrier;
    });

    dispatcher.offer({ x: 1 });
    await Promise.resolve();
    dispatcher.offer({ x: 2 });
    dispatcher.offer({ x: 3 });
    releaseFirst();
    await dispatcher.flush();

    expect(dispatched).toEqual([1, 3]);
    expect(dispatcher.superseded).toBe(1);
  });
});
