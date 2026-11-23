import { t } from "../../../../shared/core/i18n/index.js";
import type { ConversationView } from "../../types/frontend";
import type { TimelineItem } from "./timelineTypes";
import { responsePartsToTimelineItems } from "./responseParts";

export type ToolTimelineItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type WorkItem = Extract<TimelineItem, { kind: "aggregated_text" }> | Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type RenderGroup = { kind: "work"; id: string; items: WorkItem[] } | { kind: "response"; id: string; item: TimelineItem };
export type HistoricalBoundaryStatus = { kind: "user-interrupted" | "cancelled" | "turn-failed" | "turn-timed-out"; role: "status" | "alert"; className: string; icon: string; title: string; detail?: string };

export function historicalBoundaryStatus(conversation: ConversationView): HistoricalBoundaryStatus | null {
  if (conversation.displayMode !== "history") return null;
  const responseParts = conversation.responseParts ?? [];
  const userInterrupted = responseParts.some((part) => part.partial === true && part.completion_reason === "user_interrupt");
  if (userInterrupted) return { kind: "user-interrupted", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: t("chat.status.interrupted"), detail: t("chat.status.contentKept") };
  if (conversation.turnStatus === "cancelled") return { kind: "cancelled", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: t("chat.status.cancelled"), detail: t("chat.status.contentKept") };

  // 工具 attempt 的失败留在对应工具行；整轮状态只取权威终态。
  if (conversation.turnStatus === "failed") return { kind: "turn-failed", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-error", title: t("chat.status.turnFailed"), detail: conversation.turnFailure?.message ?? t("chat.status.noDetails") };
  if (conversation.turnStatus === "timed_out") return { kind: "turn-timed-out", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-watch", title: t("chat.status.timedOut"), detail: conversation.turnFailure?.message ?? t("chat.status.noDetails") };
  return null;
}

export function responseItemsForConversation(conversation: ConversationView): TimelineItem[] {
  const terminalCancellation = conversation.turnStatus === "cancelled" || conversation.messageStream?.streamStatus === "interrupted";
  const items = responsePartsToTimelineItems(conversation.responseParts ?? [], { terminalFailure: !terminalCancellation && (conversation.turnStatus === "failed" || conversation.turnStatus === "timed_out" || (!conversation.turnStatus && conversation.status === "error")), terminalCancellation });
  return items;
}

export function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  for (const item of items) {
    const isWork = item.kind === "aggregated_tool" || (item.kind === "aggregated_text" && item.partKind === "reasoning");
    if (!isWork) { groups.push({ kind: "response", id: item.id, item }); continue; }
    const previous = groups[groups.length - 1];
    const source = item.responseSources?.[0]?.source;
    const group = source?.activity_group_item_id;
    const groupId = group ? `group:${group}:${source.activity_group_part_ordinal ?? 0}` : undefined;
    if (previous?.kind === "work" && (!groupId || previous.id === groupId)) previous.items.push(item); else {
      groups.push({ kind: "work", id: group ? `group:${group}:${source.activity_group_part_ordinal ?? 0}` : "work-" + item.id, items: [item] });
    }
  }
  return groups;
}
