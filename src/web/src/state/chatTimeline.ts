import type { ConversationView } from "../types/frontend";
import {
  aggregateConversationEvents,
  buildPendingStatusItem,
} from "./traceAggregation";
import { normalizeTimelineMessage } from "./timelineMessages";
import type { TimelineItem } from "./timelineTypes";

export type { TimelineItem } from "./timelineTypes";
export { normalizeTraceData } from "./tracePayload";

export function buildTraceTimelineItems(
  conversations: ConversationView[],
): TimelineItem[] {
  const items: TimelineItem[] = [];

  for (let ci = 0; ci < conversations.length; ci++) {
    const conv = conversations[ci];

    items.push({
      kind: "conversation_marker",
      id: `${conv.conversationId}-marker`,
      label: `第 ${ci + 1} 轮对话`,
    });

    if (conv.userMessage) {
      items.push(normalizeTimelineMessage(conv.userMessage));
    }

    const aggregated = aggregateConversationEvents(
      conv.events,
      conv.conversationId,
      conv.status === "running" || conv.status === "queued",
    );
    if (aggregated.length === 0) {
      const statusItem = buildPendingStatusItem(conv);
      if (statusItem) {
        items.push(statusItem);
      }
    }
    for (const item of aggregated) {
      items.push(item);
    }
  }

  return items;
}
