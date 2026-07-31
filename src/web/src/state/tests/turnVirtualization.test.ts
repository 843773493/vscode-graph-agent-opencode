import { describe, expect, test } from "bun:test";
import { advanceTurnVirtualIndex } from "../session/turnVirtualization";

describe("Turn 虚拟列表索引", () => {
  test("只在历史前插时递减，尾部新增保持索引不变", () => {
    const initial = advanceTurnVirtualIndex(null, "workspace:session", ["turn-3"]);
    const appended = advanceTurnVirtualIndex(
      initial,
      "workspace:session",
      ["turn-3", "turn-4"],
    );
    const prepended = advanceTurnVirtualIndex(
      appended,
      "workspace:session",
      ["turn-1", "turn-2", "turn-3", "turn-4"],
    );

    expect(appended.firstItemIndex).toBe(initial.firstItemIndex);
    expect(prepended.firstItemIndex).toBe(initial.firstItemIndex - 2);
    expect(prepended.firstTurnId).toBe("turn-1");
  });

  test("切换会话或投影替换时重置虚拟索引基线", () => {
    const initial = advanceTurnVirtualIndex(null, "workspace:session-1", ["old"]);
    const switched = advanceTurnVirtualIndex(
      initial,
      "workspace:session-2",
      ["new"],
    );
    const replaced = advanceTurnVirtualIndex(
      initial,
      "workspace:session-1",
      ["rebuilt"],
    );

    expect(switched.firstItemIndex).toBe(initial.firstItemIndex);
    expect(replaced.firstItemIndex).toBe(initial.firstItemIndex);
  });
});
