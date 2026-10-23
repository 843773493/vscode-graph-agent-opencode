import type { ConversationView } from "../../types/frontend";
import type { TimelineItem } from "./timelineTypes";
import { responsePartsToTimelineItems } from "./responseParts";

export type ToolTimelineItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type WorkItem = Extract<TimelineItem, { kind: "aggregated_text" }> | Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type RenderGroup = { kind: "work"; id: string; items: WorkItem[] } | { kind: "response"; id: string; item: TimelineItem };
export type HistoricalBoundaryStatus = { kind: "user-interrupted" | "cancelled" | "tool-incomplete" | "tool-outcome-unknown" | "tool-failed" | "turn-failed" | "turn-timed-out"; role: "status" | "alert"; className: string; icon: string; title: string; detail?: string };

export function historicalBoundaryStatus(conversation: ConversationView): HistoricalBoundaryStatus | null {
  if (conversation.displayMode !== "history") return null;
  const responseParts = conversation.responseParts ?? [];
  const userInterrupted = responseParts.some((part) => part.partial === true && part.completion_reason === "user_interrupt");
  if (userInterrupted) return { kind: "user-interrupted", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: "已由用户中断", detail: "已保留本轮已经生成的内容" };
  if (conversation.turnStatus === "cancelled") return { kind: "cancelled", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: "任务已取消", detail: "已保留本轮已经生成的内容" };

  const toolItems = responsePartsToTimelineItems(responseParts, {
    terminalFailure: conversation.turnStatus === "failed" || conversation.turnStatus === "timed_out",
  }).filter((item): item is ToolTimelineItem => item.kind === "aggregated_tool");
  const incompleteToolNames = new Set<string>();
  const unknownToolNames = new Set<string>();
  const failedToolNames = new Set<string>();
  for (const item of toolItems) {
    if (item.incomplete) incompleteToolNames.add(item.toolName);
    if (item.outcomeUnknown) unknownToolNames.add(item.toolName);
    if (item.failed) failedToolNames.add(item.toolName);
  }
  if (incompleteToolNames.size > 0) return { kind: "tool-incomplete", role: "status", className: "chat-inline-cancelled chat-inline-tool-status", icon: "codicon-debug-stop", title: "工具调用未完成", detail: [...incompleteToolNames].join("、") + "：调用在完成前结束" };
  if (unknownToolNames.size > 0) return { kind: "tool-outcome-unknown", role: "alert", className: "chat-inline-error chat-inline-tool-unknown", icon: "codicon-warning", title: "工具执行结果未知", detail: [...unknownToolNames].join("、") + "：后端未返回结果，无法确认是否成功" };
  if (failedToolNames.size > 0) return { kind: "tool-failed", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-error", title: "工具执行失败", detail: [...failedToolNames].join("、") + "：工具返回了失败结果" };
  if (conversation.turnStatus === "failed") return { kind: "turn-failed", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-error", title: "本轮执行失败", detail: "后端没有提供可用的失败详情" };
  if (conversation.turnStatus === "timed_out") return { kind: "turn-timed-out", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-watch", title: "本轮执行超时", detail: "任务超过总执行时间上限，已停止执行" };
  return null;
}

export function responseItemsForConversation(conversation: ConversationView): TimelineItem[] {
  const terminalCancellation = conversation.turnStatus === "cancelled" || conversation.messageStream?.streamStatus === "interrupted";
  const items = responsePartsToTimelineItems(conversation.responseParts ?? [], { terminalFailure: !terminalCancellation && (conversation.turnStatus === "failed" || conversation.turnStatus === "timed_out" || (!conversation.turnStatus && conversation.status === "error")), terminalCancellation });
  const assistantMessages = conversation.assistantMessages ?? [];
  let longestAssistantMessage: (typeof assistantMessages)[number] | undefined;
  for (const candidate of assistantMessages) {
    if (!longestAssistantMessage || candidate.content.length > longestAssistantMessage.content.length) {
      longestAssistantMessage = candidate;
    }
  }
  const longestAssistantText = longestAssistantMessage?.content?.trim() ?? "";
  if (!longestAssistantText) return items;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind !== "aggregated_text" || item.partKind !== "markdown") continue;
    if (item.text.trim() === longestAssistantText) return items;
    break;
  }
  return [...items, { kind: "aggregated_text", id: conversation.conversationId + ":assistant-final", text: longestAssistantMessage?.content ?? "", partKind: "markdown", active: false, timestamp: longestAssistantMessage?.created_at ?? null, eventCount: 1, rawEvents: [] }];
}

export function buildRenderGroups(items: TimelineItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  for (const item of items) {
    const isWork = item.kind === "aggregated_tool" || (item.kind === "aggregated_text" && item.partKind === "reasoning");
    if (!isWork) { groups.push({ kind: "response", id: item.id, item }); continue; }
    const previous = groups[groups.length - 1];
    if (previous?.kind === "work") previous.items.push(item); else groups.push({ kind: "work", id: "work-" + item.id, items: [item] });
  }
  return groups;
}
