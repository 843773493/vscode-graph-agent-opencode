import type { ConversationView } from "../../types/frontend";
import type {
  InternalExecutionDisplayPart,
} from "../../types/backend";
import { conversationTurnKey } from "../../state/session/turnIdentity";
import { responsePartsToTimelineItems } from "../../state/timeline/responseParts";
import type { TurnResponsePart } from "../../types/backend";
import type { InternalExecutionRecord } from "../../state/session/internalExecutionHistory";

export type ChatTranscriptItem =
  | { kind: "execution_header"; key: string; executionId: string; status: InternalExecutionRecord["status"]; empty: boolean }
  | { kind: "context_event"; key: string; notices: TurnResponsePart[] }
  | {
      kind: "turn";
      key: string;
      conversation: ConversationView;
    }
  | {
      kind: "internal_execution";
      key: string;
      executionId: string;
      displayPart: InternalExecutionDisplayPart & { bodyEvicted?: true };
    }
  | {
      kind: "execution_response";
      key: string;
      executionId: string;
      itemId: string;
      parts: InternalExecutionRecord["response_parts"];
      bodyEvicted: boolean;
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

export function chatTranscriptItems(
  conversations: readonly ConversationView[],
  executions: readonly InternalExecutionRecord[],
  includeExecutionResponses = false,
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
      rootOrdinal: null as number | null,
    }))
    .sort((left, right) => left.ordinal - right.ordinal);
  let previousTurnOrdinal = 0;
  let previousRootOrdinal = -1;
  for (const turn of turnsByOrdinal) {
    const { item, ordinal: currentTurnOrdinal } = turn;
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
    turn.rootOrdinal = rootOrdinal;
    previousTurnOrdinal = currentTurnOrdinal;
    previousRootOrdinal = rootOrdinal;
  }
  const orderedHistory: Array<{ item: ChatTranscriptItem; ordinal: number }> =
    turnsByOrdinal.map(({ item, rootOrdinal }) => ({ item, ordinal: rootOrdinal! }));
  const displayItemIds = new Set<string>();
  const displayItemSequences = new Set<number>();
  for (const execution of executions) {
    if (includeExecutionResponses && execution.response_parts.every(part => part.kind === "context_notice")) {
      let firstOrdinal = Infinity;
      for (const part of execution.display_parts) firstOrdinal = Math.min(firstOrdinal, part.logical_item_ordinal);
      for (const part of execution.response_parts) firstOrdinal = Math.min(firstOrdinal,
        (part.kind === "context_notice" ? part.context_anchor_ordinal : part.source.logical_item_ordinal) ?? Infinity);
      orderedHistory.push({ item: { kind: "execution_header", key: `execution:${execution.execution_id}`,
        executionId: execution.execution_id, status: execution.status,
        empty: execution.display_parts.length === 0 && execution.response_parts.length === 0 }, ordinal: firstOrdinal });
    }
    for (const event of responsePartsToTimelineItems(execution.response_parts)) {
      if (event.kind !== "context_event") continue;
      const position = event.notices.find(notice => notice.context_before_item_id) ?? event.notices[0];
      orderedHistory.push({ item: { kind: "context_event", key: event.id, notices: event.notices },
        ordinal: logicalItemOrdinal(position.context_anchor_ordinal, "context_notice.anchor_ordinal") });
    }
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
      orderedHistory.push({
        item: {
          kind: "internal_execution",
          key: `internal:${displayPart.item_id}`,
          executionId: execution.execution_id,
          displayPart,
        },
        ordinal,
      });
    }
    if (includeExecutionResponses) {
      const partsByItem = new Map<string, InternalExecutionRecord["response_parts"]>();
      for (const part of execution.response_parts) {
        if (part.kind === "context_notice") continue;
        const itemId = part.source.item_id;
        if (!itemId) throw new Error("internal execution response part 缺少 canonical item_id");
        const parts = partsByItem.get(itemId) ?? [];
        parts.push(part);
        partsByItem.set(itemId, parts);
      }
      for (const [itemId, parts] of partsByItem) {
        orderedHistory.push({ item: { kind: "execution_response", key: `response:${parts[0].source.process_segment_item_id ?? itemId}`,
          executionId: execution.execution_id, itemId: parts[0].source.process_segment_item_id ?? itemId, parts, bodyEvicted: parts.some(part => part.bodyEvicted) },
          ordinal: logicalItemOrdinal(parts[0].source.logical_item_ordinal, "response_part.logical_item_ordinal") });
      }
    }
  }
  orderedHistory.sort((left, right) => left.ordinal - right.ordinal);
  const segments: ChatTranscriptItem[] = [];
  for (const {item} of orderedHistory) {
    const previous = segments.at(-1);
    if (item.kind === "execution_response" && previous?.kind === "execution_response" && item.executionId === previous.executionId && item.itemId === previous.itemId) {
      segments[segments.length-1] = {...previous, parts:[...previous.parts, ...item.parts], bodyEvicted: previous.bodyEvicted || item.bodyEvicted};
    } else segments.push(item);
  }
  return [...segments, ...otherTurns];
}
