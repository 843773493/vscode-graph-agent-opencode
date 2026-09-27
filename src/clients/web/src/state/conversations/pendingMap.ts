import type { ConversationView } from "../../types/frontend";

/**
 * 终态 pending 回合的保留上限。
 *
 * 终态回合只在下一次 bootstrap 用 canonical 历史替换它之前维持可见视图；
 * 超出该窗口的条目不再提供任何新信息，却会在同一会话连续对话时逐轮累积，
 * 让 pending 列表随已完成轮次无界增长（每次渲染都要对其去重与排序）。
 */
const PENDING_TERMINAL_CONVERSATION_RETENTION_LIMIT = 8;

/** 终态回合的保留排序锚点：优先用户消息时间，其次首条事件时间。 */
function pendingConversationTime(conversation: ConversationView): number {
  const candidates = [
    conversation.userMessage?.created_at,
    conversation.events[0]?.timestamp,
  ];
  for (const candidate of candidates) {
    if (candidate) {
      const parsed = Date.parse(candidate);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return 0;
}

/**
 * 有界化 pending 列表：终态回合只保留最近的
 * `PENDING_TERMINAL_CONVERSATION_RETENTION_LIMIT` 条，其余条目一律原样保留。
 *
 * 这是 pending 列表有界性的唯一收口点，所有写回路径都经由此处，
 * 因此不存在某条写入链绕过上限继续累积的可能。
 */
function boundTerminalConversations(
  list: ConversationView[],
): ConversationView[] {
  const terminalConversations = list.filter(
    (conversation) =>
      conversation.source === "pending"
      && Boolean(conversation.jobId)
      && !conversation.pending,
  );
  if (terminalConversations.length <= PENDING_TERMINAL_CONVERSATION_RETENTION_LIMIT) {
    return list;
  }
  // 只裁剪被淘汰的终态条目本身，其余任何条目（排队请求、活动 Job 覆盖层、
  // 乐观 replay）都不受影响。
  const droppedTerminalIds = new Set(
    [...terminalConversations]
      .sort(
        (left, right) =>
          pendingConversationTime(right) - pendingConversationTime(left),
      )
      .slice(PENDING_TERMINAL_CONVERSATION_RETENTION_LIMIT)
      .map((conversation) => conversation.conversationId),
  );
  return list.filter(
    (conversation) => !droppedTerminalIds.has(conversation.conversationId),
  );
}

/** pending 会话列表的写回入口；空列表表示该 mapKey 不再有待处理会话。 */
export function writePendingList(
  map: Map<string, ConversationView[]>,
  sessionId: string,
  list: ConversationView[],
  mapKey: string = sessionId,
) {
  const bounded = boundTerminalConversations(list);
  if (bounded.length === 0) {
    map.delete(mapKey);
    return;
  }
  map.set(mapKey, bounded);
}
