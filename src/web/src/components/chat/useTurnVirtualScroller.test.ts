import { describe, expect, test } from "bun:test";
import {
  restoreTurnAnchorPosition,
  requestTurnFollowLatestFrame,
  turnFollowOutput,
  turnDataIndexFromAbsolute,
} from "./useTurnVirtualScroller";

describe("Turn 虚拟列表锚点恢复", () => {
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

  test("前插后用当前 firstItemIndex 把稳定绝对索引映射为 data index", () => {
    expect(turnDataIndexFromAbsolute(999_999_980, 999_999_960)).toBe(20);
    expect(() => turnDataIndexFromAbsolute(999_999_940, 999_999_960))
      .toThrow("Turn 绝对索引无法映射到当前列表");
  });

  test("首次稳定后仍持续观察，并修正迟到的高度变化", async () => {
    const measuredDeltas = [
      ...Array.from({ length: 28 }, () => 0.1),
      37.71875,
      0.2,
      0.1,
      0.1,
      0.1,
      0.1,
    ];
    const applied: number[] = [];
    let frame = 0;

    await restoreTurnAnchorPosition({
      nextFrame: async () => {
        frame += 1;
      },
      isActive: () => true,
      measureDelta: () => measuredDeltas[frame - 1] ?? 0,
      applyDelta: (delta) => applied.push(delta),
    });

    expect(applied).toEqual([37.71875]);
    expect(frame).toBeGreaterThanOrEqual(32);
  });

  test("新一轮前插会取消旧锚点恢复", async () => {
    let active = true;
    let frame = 0;
    const applied: number[] = [];

    await restoreTurnAnchorPosition({
      nextFrame: async () => {
        frame += 1;
        active = false;
      },
      isActive: () => active,
      measureDelta: () => 12,
      applyDelta: (delta) => applied.push(delta),
    });

    expect(frame).toBe(1);
    expect(applied).toEqual([]);
  });
});
