import { expect, test } from "bun:test";
import type {
  InternalExecutionDisplayPart,
  InternalExecutionHistory,
  TurnHistoryPage,
  TurnResponsePart,
} from "../types/backend";
import { mergeChildThreadHistory } from "./childThreadDisplay";

function displayPart(
  itemId: string,
  ordinal: number,
  text: string,
): InternalExecutionDisplayPart {
  return {
    item_id: itemId,
    item_sequence: ordinal,
    kind: "generated_session_result",
    text,
    created_at: "2026-07-13T00:00:00.000Z",
    provenance: {},
    logical_item_ordinal: ordinal,
  };
}

function responsePart(
  partId: string,
  ordinal: number,
  text: string,
): TurnResponsePart {
  return {
    part_id: partId,
    kind: "text",
    projection: "detail",
    status: "completed",
    source: {
      message_sequence: 1,
      part_ordinal: ordinal,
      logical_item_ordinal: ordinal,
    },
    text,
  };
}

function execution(
  displayParts: InternalExecutionDisplayPart[],
  responseParts: TurnResponsePart[],
): InternalExecutionHistory {
  return {
    execution_id: "exec_merge_history",
    job_id: "job_merge_history",
    session_id: "ses_merge_history",
    thread_id: "thr_merge_history",
    status: "completed",
    created_at: "2026-07-13T00:00:00.000Z",
    updated_at: "2026-07-13T00:00:01.000Z",
    display_parts: displayParts,
    response_parts: responseParts,
  };
}

function historyPage(executionHistory: InternalExecutionHistory): TurnHistoryPage {
  return {
    projection_epoch: 4,
    items: [],
    internal_executions: [executionHistory],
  };
}

test("子线程历史续页按稳定 ID 覆盖并恢复规范顺序", () => {
  const previous = historyPage(execution(
    [displayPart("display_first", 1, "首项"), displayPart("display_shared", 3, "旧值")],
    [responsePart("response_first", 1, "首项"), responsePart("response_shared", 3, "旧值")],
  ));
  const next = historyPage(execution(
    [
      displayPart("display_shared", 3, "新值"),
      displayPart("display_middle", 2, "中项"),
    ],
    [
      responsePart("response_shared", 3, "新值"),
      responsePart("response_middle", 2, "中项"),
    ],
  ));

  const merged = mergeChildThreadHistory(previous, next);
  const mergedExecution = merged.internal_executions?.[0];

  expect(mergedExecution?.display_parts.map((part) => [part.item_id, part.text])).toEqual([
    ["display_first", "首项"],
    ["display_middle", "中项"],
    ["display_shared", "新值"],
  ]);
  expect(mergedExecution?.response_parts.map((part) => [part.part_id, part.text])).toEqual([
    ["response_first", "首项"],
    ["response_middle", "中项"],
    ["response_shared", "新值"],
  ]);
});
