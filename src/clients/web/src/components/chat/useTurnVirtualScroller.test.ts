import { describe, expect, test } from "bun:test";
import {
  requestTurnFollowLatestFrame,
  turnFollowOutput,
} from "./useTurnVirtualScroller";

describe("Turn 虚拟列表滚动跟随", () => {
  test("用户离开底部后终态更新不得重新抢占滚动位置", () => {
    expect(turnFollowOutput(true, true)).toBe("auto");
    expect(turnFollowOutput(false, true)).toBe(false);
    expect(turnFollowOutput(true, false)).toBe(false);
  });

  test("已排队的自动跟随帧在用户上滚后失效", () => {
    let pendingFrame: FrameRequestCallback | null = null;
    let followsLatest = true;
    let scrollCount = 0;
    requestTurnFollowLatestFrame(
      (callback) => {
        pendingFrame = callback;
        return 1;
      },
      () => followsLatest,
      () => {
        scrollCount += 1;
      },
    );

    followsLatest = false;
    (pendingFrame as FrameRequestCallback | null)?.(16);

    expect(scrollCount).toBe(0);
  });

});
