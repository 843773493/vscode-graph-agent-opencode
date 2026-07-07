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
  const idCounts = new Map<string, number>();

  const pushItem = (item: TimelineItem) => {
    const count = idCounts.get(item.id) ?? 0;
    idCounts.set(item.id, count + 1);
    items.push(count === 0 ? item : { ...item, id: `${item.id}-dup-${count}` });
  };

  for (let ci = 0; ci < conversations.length; ci++) {
    const conv = conversations[ci];

    pushItem({
      kind: "conversation_marker",
      id: `${conv.conversationId}-marker`,
      label: `第 ${ci + 1} 轮对话`,
      jobId: conv.jobId,
    });

    if (conv.userMessage) {
      pushItem(normalizeTimelineMessage(conv.userMessage));
    }

    const aggregated = aggregateConversationEvents(
      conv.events,
      conv.conversationId,
      conv.status === "running" || conv.status === "queued",
    );
    if (aggregated.length === 0) {
      const statusItem = buildPendingStatusItem(conv);
      if (statusItem) {
        pushItem(statusItem);
      }
    }
    for (const item of aggregated) {
      pushItem(item);
    }
  }

  return items;
}
