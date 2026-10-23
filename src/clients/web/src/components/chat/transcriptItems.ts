import type { ConversationView } from "../../types/frontend";
import type {
  InternalExecutionDisplayPart,
  InternalExecutionHistory,
} from "../../types/backend";
import { conversationTurnKey } from "../../state/session/turnIdentity";

export type ChatTranscriptItem =
  | {
      kind: "turn";
      key: string;
      conversation: ConversationView;
    }
  | {
      kind: "internal_execution";
      key: string;
      executionId: string;
      displayPart: InternalExecutionDisplayPart;
    };

function logicalItemOrdinal(value: number | undefined, field: string): number {
  if (!Number.isSafeInteger(value) || value === undefined || value < 0) {
    throw new Error(`history projection integrity: ${field} 缺少有效的 membership ordinal`);
  }
  return value;
}

function turnOrdinal(value: number | undefined): number {
  if (!Number.isSafeInteger(value) || value === undefined || value < 1) {
    throw new Error("history projection integrity: Turn 缺少有效的 ordinal");
  }
  return value;
}

function internalExecutionTranscriptItems(
  executions: readonly InternalExecutionHistory[],
): Extract<ChatTranscriptItem, { kind: "internal_execution" }>[] {
  const items: Extract<ChatTranscriptItem, { kind: "internal_execution" }>[] = [];
  for (const execution of executions) {
    for (const displayPart of execution.display_parts) {
      items.push({
        kind: "internal_execution",
        key: `internal:${displayPart.item_id}`,
        executionId: execution.execution_id,
        displayPart,
      });
    }
  }
  return items;
}

export function chatTranscriptItems(
  conversations: readonly ConversationView[],
  executions: readonly InternalExecutionHistory[],
): ChatTranscriptItem[] {
  const historyTurns: Extract<ChatTranscriptItem, { kind: "turn" }>[] = [];
  const otherTurns: Extract<ChatTranscriptItem, { kind: "turn" }>[] = [];
  for (const conversation of conversations) {
    const item: Extract<ChatTranscriptItem, { kind: "turn" }> = {
      kind: "turn",
      key: conversationTurnKey(conversation),
      conversation,
    };
    if (conversation.displayMode === "history" && !conversation.pending) {
      historyTurns.push(item);
    } else {
      otherTurns.push(item);
    }
  }
  const sharedOrdinals = new Map<number, string>();
  const turnsByOrdinal = historyTurns
    .map((item) => ({
      item,
      ordinal: turnOrdinal(item.conversation.turnOrdinal),
    }))
    .sort((left, right) => left.ordinal - right.ordinal);
  let previousTurnOrdinal = 0;
  let previousRootOrdinal = -1;
  for (const { item, ordinal: currentTurnOrdinal } of turnsByOrdinal) {
    const rootOrdinal = logicalItemOrdinal(
      item.conversation.rootLogicalItemOrdinal,
      "root_logical_item_ordinal",
    );
    if (currentTurnOrdinal <= previousTurnOrdinal) {
      throw new Error("history projection integrity: Turn ordinal 重复或顺序冲突");
    }
    if (rootOrdinal === previousRootOrdinal) {
      throw new Error(
        `history projection integrity: root membership ordinal 重复: ${rootOrdinal}`,
      );
    }
    if (rootOrdinal < previousRootOrdinal) {
      throw new Error(
        "history projection integrity: Turn ordinal 与 root membership ordinal 顺序冲突",
      );
    }
    sharedOrdinals.set(rootOrdinal, `Turn ${item.conversation.turnId ?? item.key}`);
    previousTurnOrdinal = currentTurnOrdinal;
    previousRootOrdinal = rootOrdinal;
  }
  const displayItemIds = new Set<string>();
  const displayItemSequences = new Set<number>();
  for (const execution of executions) {
    for (const displayPart of execution.display_parts) {
      if (!displayPart.item_id || !Number.isSafeInteger(displayPart.item_sequence)
        || displayPart.item_sequence < 1) {
        throw new Error("history projection integrity: internal display 缺少有效的 canonical item 坐标");
      }
      if (displayItemIds.has(displayPart.item_id)) {
        throw new Error(
          `history projection integrity: display item identity 重复: ${displayPart.item_id}`,
        );
      }
      if (displayItemSequences.has(displayPart.item_sequence)) {
        throw new Error(
          `history projection integrity: display item_sequence 重复: ${displayPart.item_sequence}`,
        );
      }
      displayItemIds.add(displayPart.item_id);
      displayItemSequences.add(displayPart.item_sequence);
      const ordinal = logicalItemOrdinal(
        displayPart.logical_item_ordinal,
        "logical_item_ordinal",
      );
      const previousIdentity = sharedOrdinals.get(ordinal);
      if (previousIdentity !== undefined) {
        throw new Error(
          `history projection integrity: membership ordinal ${ordinal} 同时属于 ${previousIdentity} 与 display item ${displayPart.item_id}`,
        );
      }
      sharedOrdinals.set(ordinal, `display item ${displayPart.item_id}`);
    }
  }
  const orderedHistory: ChatTranscriptItem[] = [
    ...historyTurns,
    ...internalExecutionTranscriptItems(executions),
  ];
  orderedHistory.sort((left, right) => {
    const leftOrdinal = left.kind === "turn"
      ? logicalItemOrdinal(
        left.conversation.rootLogicalItemOrdinal,
        "root_logical_item_ordinal",
      )
      : logicalItemOrdinal(
        left.displayPart.logical_item_ordinal,
        "logical_item_ordinal",
      );
    const rightOrdinal = right.kind === "turn"
      ? logicalItemOrdinal(
        right.conversation.rootLogicalItemOrdinal,
        "root_logical_item_ordinal",
      )
      : logicalItemOrdinal(
        right.displayPart.logical_item_ordinal,
        "logical_item_ordinal",
      );
    return leftOrdinal - rightOrdinal;
  });
  return [
    ...orderedHistory,
    ...otherTurns,
  ];
}
