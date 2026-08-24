import { describe, expect, test } from "bun:test";
import type { ConversationView } from "../../types/frontend";
import {
  conversationTurnKey,
  selectVisibleTurnDetailBatches,
  TURN_DETAIL_BATCH_LIMIT,
} from "../session/turnDetailHydration";

function conversation(index: number, itemsView: "summary" | "full" = "summary"): ConversationView {
  return {
    conversationId: `turn-${index}`,
    displayMode: "history",
    turnId: `turn-${index}`,
    turnRevision: 1,
    turnItemsView: itemsView,
    sessionId: "session-long",
    userMessage: null,
    assistantMessages: [],
    events: [],
    status: "done",
    jobId: `job-${index}`,
    pending: false,
    source: "turn",
  };
}

describe("可视 Turn 详情水合", () => {
  test("summary 原位水合为 detail 时列表 key 保持 Turn ID", () => {
    const summary = conversation(3, "summary");
    const detail = { ...conversation(3, "full"), turnRevision: 2 };

    expect(conversationTurnKey(summary)).toBe("turn-3");
    expect(conversationTurnKey(detail)).toBe("turn-3");
  });

  test("只选择可视范围和上下各一条 overscan", () => {
    const conversations = Array.from({ length: 20 }, (_, index) => conversation(index));

    const batches = selectVisibleTurnDetailBatches({
      conversations,
      range: { startIndex: 99_008, endIndex: 99_010 },
      firstItemIndex: 99_000,
      loadingTurnIds: [],
    });

    expect(batches.flat()).toEqual([
      "turn-8",
      "turn-9",
      "turn-10",
      "turn-7",
      "turn-11",
    ]);
    expect(batches.every((batch) => batch.length <= TURN_DETAIL_BATCH_LIMIT)).toBe(true);
  });

  test("快速滚动时排除已水合和正在请求的 Turn", () => {
    const conversations = Array.from({ length: 12 }, (_, index) =>
      conversation(index, index === 7 ? "full" : "summary"),
    );

    const batches = selectVisibleTurnDetailBatches({
      conversations,
      range: { startIndex: 6, endIndex: 8 },
      firstItemIndex: 99_988,
      loadingTurnIds: ["turn-6", "turn-8"],
    });

    expect(batches.flat()).toEqual(["turn-5", "turn-9"]);
  });

  test("宽可视窗口仍按服务端上限拆分详情请求", () => {
    const conversations = Array.from({ length: 10 }, (_, index) => conversation(index));

    const batches = selectVisibleTurnDetailBatches({
      conversations,
      range: { startIndex: 99_990, endIndex: 99_999 },
      firstItemIndex: 99_990,
      loadingTurnIds: [],
    });

    expect(batches.map((batch) => batch.length)).toEqual([4, 4, 2]);
  });
});
