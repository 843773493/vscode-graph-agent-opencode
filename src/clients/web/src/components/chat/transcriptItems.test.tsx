import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "bun:test";
import type { ConversationView } from "../../types/frontend";
import type { InternalExecutionHistory } from "../../types/backend";
import ChatInternalExecutionDisplay from "./ChatInternalExecutionDisplay";
import { chatTranscriptItems } from "./transcriptItems";

function turn(
  turnId: string,
  turnOrdinal: number,
  rootLogicalItemOrdinal: number,
  firstItemSequence: number,
  lastItemSequence: number,
): ConversationView {
  return {
    conversationId: turnId,
    displayMode: "history",
    turnId,
    turnRevision: 1,
    turnOrdinal,
    turnItemsView: "summary",
    rootLogicalItemOrdinal,
    turnStatus: "completed",
    activityStats: {
      duration_ms: null,
      item_count: 1,
      first_item_sequence: firstItemSequence,
      last_item_sequence: lastItemSequence,
    },
    sessionId: "session-main",
    userMessage: null,
    assistantMessages: [],
    events: [],
    status: "done",
    jobId: turnId,
    pending: false,
    source: "turn",
  };
}

const internalExecution: InternalExecutionHistory = {
  execution_id: "execution-private",
  job_id: "job-private",
  session_id: "ses_internal",
  thread_id: "thr_internal",
  status: "completed",
  created_at: "2026-10-03T00:00:00Z",
  updated_at: "2026-10-03T00:00:01Z",
  response_parts: [],
  display_parts: [
    {
      kind: "generated_session_result",
      text: "生成分支已结束，主会话正在处理返回结果。",
      item_id: "item-report-back",
      item_sequence: 25,
      logical_item_ordinal: 4,
      created_at: "2026-10-03T00:00:00Z",
      provenance: {
        source_session_id: "session-branch-private",
        source_execution_id: "execution-source-private",
      },
    },
  ],
};

describe("standalone internal execution display", () => {
  test("densely interleaves Turn roots and internal displays by shared view ordinal", () => {
    const executions: InternalExecutionHistory[] = [
      {
        ...internalExecution,
        display_parts: [
          { ...internalExecution.display_parts[0], item_id: "item-20", item_sequence: 20, logical_item_ordinal: 20 },
          { ...internalExecution.display_parts[0], item_id: "item-30", item_sequence: 30, logical_item_ordinal: 30 },
          { ...internalExecution.display_parts[0], item_id: "item-50", item_sequence: 50, logical_item_ordinal: 50 },
          { ...internalExecution.display_parts[0], item_id: "item-60", item_sequence: 60, logical_item_ordinal: 60 },
          { ...internalExecution.display_parts[0], item_id: "item-70", item_sequence: 70, logical_item_ordinal: 70 },
          { ...internalExecution.display_parts[0], item_id: "item-90", item_sequence: 90, logical_item_ordinal: 90 },
        ],
      },
    ];
    const items = chatTranscriptItems(
      [
        turn("turn-2", 2, 10, 1, 1),
        turn("turn-7", 7, 40, 4, 4),
        turn("turn-13", 13, 80, 8, 8),
      ],
      executions,
    );

    expect(items.map((item) => item.key)).toEqual([
      "turn-2",
      "internal:item-20",
      "internal:item-30",
      "turn-7",
      "internal:item-50",
      "internal:item-60",
      "internal:item-70",
      "turn-13",
      "internal:item-90",
    ]);
  });

  test("uses the selected view coordinate without attaching the notice to a Turn", () => {
    const items = chatTranscriptItems(
      [turn("turn-before", 1, 2, 10, 20), turn("turn-after", 2, 7, 30, 40)],
      [internalExecution],
    );

    expect(items.map((item) => item.key)).toEqual([
      "turn-before",
      "internal:item-report-back",
      "turn-after",
    ]);
    expect(items[1]?.kind).toBe("internal_execution");
  });

  test("keeps an empty Turn at its root coordinate despite diagnostic activity ranges", () => {
    const emptyTurn = turn("turn-empty", 1, 3, 100, 100);
    emptyTurn.activityStats = {
      duration_ms: null,
      item_count: 0,
    };
    const items = chatTranscriptItems([emptyTurn, turn("turn-later", 2, 9, 1, 1)], [
      {
        ...internalExecution,
        display_parts: [
          { ...internalExecution.display_parts[0], logical_item_ordinal: 5 },
        ],
      },
    ]);

    expect(items.map((item) => item.key)).toEqual([
      "turn-empty",
      "internal:item-report-back",
      "turn-later",
    ]);
  });

  test("Turn root 必须和 Turn ordinal 保持严格同序", () => {
    expect(() => chatTranscriptItems([
      turn("turn-1", 1, 20, 1, 1),
      turn("turn-2", 2, 10, 2, 2),
    ], [])).toThrow("Turn ordinal 与 root membership ordinal 顺序冲突");
  });

  test("重复或缺失的 shared membership ordinal 显式报 projection integrity", () => {
    const duplicateRoot = turn("turn-1", 1, 4, 1, 1);
    expect(() => chatTranscriptItems([duplicateRoot], [internalExecution]))
      .toThrow("membership ordinal 4");

    const missingRoot = turn("turn-missing", 1, 10, 1, 1);
    missingRoot.rootLogicalItemOrdinal = undefined;
    expect(() => chatTranscriptItems([missingRoot], []))
      .toThrow("history projection integrity");

    const duplicateDisplays: InternalExecutionHistory = {
      ...internalExecution,
      display_parts: [
        { ...internalExecution.display_parts[0], item_id: "item-a", item_sequence: 1, logical_item_ordinal: 6 },
        { ...internalExecution.display_parts[0], item_id: "item-b", item_sequence: 2, logical_item_ordinal: 6 },
      ],
    };
    expect(() => chatTranscriptItems([], [duplicateDisplays]))
      .toThrow("membership ordinal 6");
  });

  test("renders only registry-safe text and a kind label", () => {
    const html = renderToStaticMarkup(
      <ChatInternalExecutionDisplay
        displayPart={internalExecution.display_parts[0]}
      />,
    );

    expect(html).toContain("会话生成");
    expect(html).toContain("生成分支已结束，主会话正在处理返回结果。");
    expect(html).not.toContain("data-turn-id");
    expect(html).not.toContain("session-branch-private");
    expect(html).not.toContain("execution-source-private");
    expect(html).not.toContain("job-private");
  });
});
