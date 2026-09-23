import type { ConversationView } from "../../types/frontend";
import type { TimelineItem } from "./timelineTypes";
import { responsePartsToTimelineItems } from "./responseParts";

export type ToolTimelineItem = Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type WorkItem = Extract<TimelineItem, { kind: "aggregated_text" }> | Extract<TimelineItem, { kind: "aggregated_tool" }>;
export type RenderGroup = { kind: "work"; id: string; items: WorkItem[] } | { kind: "response"; id: string; item: TimelineItem };
export type HistoricalBoundaryStatus = { kind: "user-interrupted" | "cancelled" | "tool-incomplete" | "tool-outcome-unknown" | "tool-failed" | "turn-failed" | "turn-timed-out"; role: "status" | "alert"; className: string; icon: string; title: string; detail?: string };

export function uniqueToolNames(items: ToolTimelineItem[]): string[] { return items.map((item) => item.toolName).filter((toolName, index, names) => names.indexOf(toolName) === index); }

export function historicalBoundaryStatus(conversation: ConversationView): HistoricalBoundaryStatus | null {
  if (conversation.displayMode !== "history" || conversation.messageStream) return null;
  const responseParts = conversation.responseParts ?? [];
  const userInterrupted = responseParts.some((part) => part.partial === true && part.completion_reason === "user_interrupt");
  const terminalCancellation = conversation.turnStatus === "cancelled";
  const toolItems = responsePartsToTimelineItems(responseParts.filter((part) => part.kind === "tool_call"), { terminalFailure: conversation.turnStatus === "failed" || conversation.turnStatus === "timed_out", terminalCancellation }).filter((item): item is ToolTimelineItem => item.kind === "aggregated_tool");
  if (userInterrupted) return { kind: "user-interrupted", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: "已由用户中断", detail: "已保留本轮已经生成的内容" };
  if (terminalCancellation) return { kind: "cancelled", role: "status", className: "chat-inline-cancelled", icon: "codicon-debug-stop", title: "任务已取消", detail: "已保留本轮已经生成的内容" };
  const incompleteToolNames = uniqueToolNames(toolItems.filter((item) => item.incomplete));
  if (incompleteToolNames.length > 0) return { kind: "tool-incomplete", role: "status", className: "chat-inline-cancelled chat-inline-tool-status", icon: "codicon-debug-stop", title: "工具调用未完成", detail: incompleteToolNames.join("、") + "：调用在完成前结束" };
  const unknownToolNames = uniqueToolNames(toolItems.filter((item) => item.outcomeUnknown));
  if (unknownToolNames.length > 0) return { kind: "tool-outcome-unknown", role: "alert", className: "chat-inline-error chat-inline-tool-unknown", icon: "codicon-warning", title: "工具执行结果未知", detail: unknownToolNames.join("、") + "：后端未返回结果，无法确认是否成功" };
  const failedToolNames = uniqueToolNames(toolItems.filter((item) => item.failed));
  if (failedToolNames.length > 0) return { kind: "tool-failed", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-error", title: "工具执行失败", detail: failedToolNames.join("、") + "：工具返回了失败结果" };
  if (conversation.turnStatus === "failed") return { kind: "turn-failed", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-error", title: "本轮执行失败", detail: "后端没有提供可用的失败详情" };
  if (conversation.turnStatus === "timed_out") return { kind: "turn-timed-out", role: "alert", className: "chat-inline-error chat-inline-tool-status", icon: "codicon-watch", title: "本轮执行超时", detail: "任务超过总执行时间上限，已停止执行" };
  return null;
}

export function responseItemsForConversation(conversation: ConversationView): TimelineItem[] {
  const terminalCancellation = conversation.turnStatus === "cancelled" || conversation.messageStream?.streamStatus === "interrupted";
  const items = responsePartsToTimelineItems((conversation.responseParts ?? []).filter((part) => part.kind !== "final_text"), { terminalFailure: !terminalCancellation && (conversation.turnStatus === "failed" || conversation.turnStatus === "timed_out" || (!conversation.turnStatus && conversation.status === "error")), terminalCancellation });
  const assistantMessages = conversation.assistantMessages ?? [];
  const finalAssistantMessage = assistantMessages.reduce<NonNullable<ConversationView["assistantMessages"]>[number] | undefined>((longest, candidate) => !longest || candidate.content.length > longest.content.length ? candidate : longest, undefined);
  const finalText = finalAssistantMessage?.content?.trim() ?? "";
  if (!finalText) return items;
  const lastMarkdown = [...items].reverse().find((item): item is Extract<TimelineItem, { kind: "aggregated_text" }> => item.kind === "aggregated_text" && item.partKind === "markdown");
  if (lastMarkdown?.text.trim() === finalText) return items;
  return [...items, { kind: "aggregated_text", id: conversation.conversationId + ":assistant-final", text: finalAssistantMessage?.content ?? "", partKind: "markdown", active: false, timestamp: finalAssistantMessage?.created_at ?? null, eventCount: 1, rawEvents: [] }];
}

export function persistedWorkItems(conversation: ConversationView): WorkItem[] { return responseItemsForConversation(conversation).filter((item): item is WorkItem => item.kind === "aggregated_tool" || (item.kind === "aggregated_text" && item.partKind === "reasoning")); }
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
