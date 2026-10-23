import { describe, expect, test } from "bun:test";
import { createCanvasRectCache } from "../src/client/canvasRectCache.js";
import { createAnimationFrameQueueFixture } from "./animationFrameQueueFixture.js";

function createCacheHarness() {
  const frames = createAnimationFrameQueueFixture();
  let rect = { left: 0, top: 0, width: 100, height: 80 };
  let readCount = 0;
  const cache = createCanvasRectCache({
    readRect: () => {
      readCount += 1;
      return rect;
    },
    requestFrame: (callback) => frames.requestFrame(callback),
    cancelFrame: (frameId) => frames.cancelFrame(frameId),
  });
  return {
    cache,
    frames,
    get readCount() { return readCount; },
    get rect() { return rect; },
    set rect(nextRect) { rect = nextRect; },
  };
}

describe("canvas 矩形缓存", () => {
  test("同一帧的滚动和 resize 只读取一次布局矩形", () => {
    const harness = createCacheHarness();
    const firstRect = harness.cache.getRect();
    const nextRect = { left: 10, top: 20, width: 200, height: 160 };
    harness.rect = nextRect;

    harness.cache.scheduleRefresh();
    harness.cache.scheduleRefresh();
    harness.cache.scheduleRefresh();

    expect(harness.frames.pendingCallbacks.size).toBe(1);
    expect(harness.readCount).toBe(1);
    harness.frames.runNext();

    expect(harness.cache.getRect()).toBe(nextRect);
    expect(harness.cache.getRect()).not.toBe(firstRect);
    expect(harness.readCount).toBe(2);
  });

  test("离散输入读取最新矩形并取消延后的重复读取", () => {
    const harness = createCacheHarness();
    harness.cache.getRect();
    harness.rect = { left: 30, top: 40, width: 300, height: 240 };
    harness.cache.scheduleRefresh();
    const pendingFrameId = harness.frames.pendingCallbacks.keys().next().value;

    expect(harness.cache.getRect()).toBe(harness.rect);
    expect(harness.frames.pendingCallbacks.size).toBe(0);
    expect(harness.frames.cancelledFrameIds).toEqual([pendingFrameId]);
    expect(harness.readCount).toBe(2);
  });

  test("dispose 取消尚未执行的几何刷新", () => {
    const harness = createCacheHarness();
    harness.cache.scheduleRefresh();
    const pendingFrameId = harness.frames.pendingCallbacks.keys().next().value;

    harness.cache.dispose();

    expect(harness.frames.pendingCallbacks.size).toBe(0);
    expect(harness.frames.cancelledFrameIds).toEqual([pendingFrameId]);
  });
});
