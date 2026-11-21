import type { InternalExecutionHistory, TurnResponsePart } from "../../types/backend";
import { compareInternalResponseParts } from "../timeline/responseParts";

export type InternalExecutionRecord = Omit<InternalExecutionHistory, "display_parts" | "response_parts"> & {
  display_parts: Array<InternalExecutionHistory["display_parts"][number] & { bodyEvicted?: true }>;
  response_parts: Array<TurnResponsePart & { bodyEvicted?: true }>;
};
export interface ExecutionItemReference { executionId: string; itemId: string }

/** 所有历史窗口沿相同 canonical identity 合并，不复制另一份执行正文。 */
export function mergeInternalExecutions(previous: readonly InternalExecutionRecord[], incoming: readonly InternalExecutionHistory[]): InternalExecutionRecord[] {
  const internalExecutions = new Map(
    previous.map((execution) => [execution.execution_id, execution]),
  );
  const displayItemExecutions = new Map<string, string>();
  for (const execution of previous) {
    for (const part of execution.display_parts) {
      const owner = displayItemExecutions.get(part.item_id);
      if (owner !== undefined && owner !== execution.execution_id) {
        throw new Error(
          `history projection integrity: display item ${part.item_id} 属于多个 execution`,
        );
      }
      displayItemExecutions.set(part.item_id, execution.execution_id);
    }
  }
  for (const execution of incoming) {
    const previous = internalExecutions.get(execution.execution_id);
    const displayParts = new Map(
      (previous?.display_parts ?? []).map((part) => [part.item_id, part]),
    );
    for (const part of execution.display_parts) {
      const owner = displayItemExecutions.get(part.item_id);
      if (owner !== undefined && owner !== execution.execution_id) {
        throw new Error(
          `history projection integrity: display item ${part.item_id} 属于多个 execution`,
        );
      }
      displayItemExecutions.set(part.item_id, execution.execution_id);
      const previousPart = displayParts.get(part.item_id);
      if (
        previousPart !== undefined
        && (
          previousPart.item_sequence !== part.item_sequence
          || previousPart.logical_item_ordinal !== part.logical_item_ordinal
        )
      ) {
        throw new Error(
          `history projection integrity: 同一 display item 的 item_sequence 或 membership ordinal 冲突: ${part.item_id}`,
        );
      }
      displayParts.set(part.item_id, part);
    }
    const responseParts = new Map(
      (previous?.response_parts ?? []).map((part) => [part.part_id, part]),
    );
    for (const part of execution.response_parts) {
      const previousPart = responseParts.get(part.part_id);
      if (
        previousPart !== undefined
        && (
          previousPart.source.item_id !== part.source.item_id
          || previousPart.source.item_sequence !== part.source.item_sequence
          || previousPart.source.logical_item_ordinal
            !== part.source.logical_item_ordinal
          || previousPart.source.part_ordinal !== part.source.part_ordinal
        )
      ) {
        throw new Error(
          `history projection integrity: 同一 response part 的 canonical 坐标冲突: ${part.part_id}`,
        );
      }
      responseParts.set(part.part_id, part);
    }
    const orderedResponseParts = [...responseParts.values()];
    orderedResponseParts.sort(compareInternalResponseParts);
    internalExecutions.set(execution.execution_id, {
      ...execution,
      display_parts: [...displayParts.values()].sort((left, right) =>
        left.logical_item_ordinal - right.logical_item_ordinal,
      ),
      response_parts: orderedResponseParts,
    });
  }
  return [...internalExecutions.values()];
}
