import { expect, test } from "bun:test";
import type {
  InternalExecutionDisplayItem,
  InternalExecutionHistory,
  TurnHistoryPage,
  TurnResponseItem,
} from "../types/backend";
import { mergeChildThreadHistory } from "./childThreadDisplay";

function displayPart(
  itemId: string,
  ordinal: number,
  text: string,
): InternalExecutionDisplayItem {
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
): TurnResponseItem {
  return {
    id: partId,
    kind: "text",
    projection: "detail",
    status: "completed",
    source: {
      message_sequence: 1,
      logical_item_ordinal: ordinal,
    },
    text,
  };
}

function execution(
  displayParts: InternalExecutionDisplayItem[],
  responseItems: TurnResponseItem[],
): InternalExecutionHistory {
  return {
    execution_id: "exec_merge_history",
    job_id: "job_merge_history",
    session_id: "ses_merge_history",
    thread_id: "thr_merge_history",
    status: "completed",
    created_at: "2026-07-13T00:00:00.000Z",
    updated_at: "2026-07-13T00:00:01.000Z",
    display_items: displayParts,
    response_items: responseItems,
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

  expect(mergedExecution?.display_items.map((part) => [part.item_id, part.text])).toEqual([
    ["display_first", "首项"],
    ["display_middle", "中项"],
    ["display_shared", "新值"],
  ]);
  expect(mergedExecution?.response_items.map((part) => [part.id, part.text])).toEqual([
    ["response_first", "首项"],
    ["response_middle", "中项"],
    ["response_shared", "新值"],
  ]);
});

test("无Turn子线程工具的正文与执行摘要共用合并，换binding清除旧证据", () => {
  const call: TurnResponseItem = { ...responsePart("call-a", 1, "正文"), kind: "tool_call",
    tool_call_id: "a", arguments: '{"path":"A"}' };
  const tool_execution = { session_id: "ses_merge_history", thread_id: "thr_merge_history", turn_stream_id: "stream",
    tool_invocation_id: "inv", tool_attempt_id: "attempt", tool_execution_id: "attempt", started_seq: 1,
    started_at: "2026-11-23T00:00:00Z", execution_completed_at: "2026-11-23T00:00:01Z", status: "completed", outcome: "provider_error" };
  const previous = mergeChildThreadHistory(null, { ...historyPage(execution([], [call])), source_view_id: "view", source_view_revision: 1 });
  const fragment = { ...historyPage(execution([], [{ ...call, projection: "summary", text: "", arguments: null, tool_execution }])),
    source_view_id: "view", source_view_revision: 1 };
  const merged = mergeChildThreadHistory(previous, fragment, true);
  expect(merged.internal_executions?.[0]?.response_items[0]).toMatchObject({ projection: "detail", text: "正文",
    arguments: call.arguments, tool_execution });
  const switched = mergeChildThreadHistory(merged, { ...fragment, source_view_id: "old_view", source_view_revision: 2,
    internal_executions: [execution([], [{ ...call, projection: "summary", text: "", arguments: null, tool_execution: null }])] });
  expect(switched.internal_executions?.[0]?.response_items[0]).toMatchObject({ projection: "summary", arguments: null, tool_execution: null });
});
