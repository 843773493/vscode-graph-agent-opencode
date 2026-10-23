import { describe, expect, test } from "bun:test";
import { createLatestPointerMoveScheduler } from "../src/client/latestPointerMoveScheduler.js";
import { createAnimationFrameQueueFixture } from "./animationFrameQueueFixture.js";

function createSchedulerHarness() {
  const frames = createAnimationFrameQueueFixture();
  const sentMessages = [];
  const scheduler = createLatestPointerMoveScheduler({
    requestFrame: (callback) => frames.requestFrame(callback),
    cancelFrame: (frameId) => frames.cancelFrame(frameId),
    send: (message) => sentMessages.push(message),
  });
  return { frames, scheduler, sentMessages };
}

describe("latest pointer move scheduler", () => {
  test("每个动画帧只发送最新指针位置", () => {
    const { frames, scheduler, sentMessages } = createSchedulerHarness();
    const firstMove = { x: 10, y: 20 };
    const latestMove = { x: 30, y: 40 };

    scheduler.schedule(firstMove);
    scheduler.schedule(latestMove);

    expect(frames.pendingCallbacks.size).toBe(1);
    expect(sentMessages).toEqual([]);
    frames.runNext();
    expect(sentMessages).toEqual([latestMove]);
  });

  test("离散输入前 flush 最新坐标并取消待执行帧", () => {
    const { frames, scheduler, sentMessages } = createSchedulerHarness();
    const latestMove = { x: 50, y: 60 };
    scheduler.schedule(latestMove);

    scheduler.flush();
    scheduler.flush();

    expect(frames.pendingCallbacks.size).toBe(0);
    expect(sentMessages).toEqual([latestMove]);
  });

  test("清除连接状态时丢弃尚未发送的位置", () => {
    const { frames, scheduler, sentMessages } = createSchedulerHarness();
    scheduler.schedule({ x: 70, y: 80 });

    scheduler.clear();

    expect(frames.pendingCallbacks.size).toBe(0);
    expect(sentMessages).toEqual([]);
  });
});
