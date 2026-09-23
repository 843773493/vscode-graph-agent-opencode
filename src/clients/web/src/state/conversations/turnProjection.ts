import type { Message, TraceEvent } from "../../types/backend";
import type { AppState, ConversationView } from "../../types/frontend";
import { isTurnDetail, type TurnRecord } from "../session/turnTimeline";

function turnConversationStatus(
  turn: TurnRecord,
): ConversationView["status"] {
  if (turn.status === "accepted" || turn.status === "queued") {
    return "queued";
  }
  if (turn.status === "completed" || turn.status === "succeeded") {
    return "done";
  }
  if (
    turn.status === "failed"
    || turn.status === "cancelled"
    || turn.status === "timed_out"
  ) {
    return "error";
  }
  return "running";
}

function conversationFromTurn(turn: TurnRecord): ConversationView {
  const userMessages = turn.user_messages ?? [];
  const firstUserMessage = userMessages[0];
  const userContent = userMessages.map((message) =>
    "content" in message ? message.content : message.preview ?? "",
  ).join("\n\n");
  const attachments = isTurnDetail(turn)
    ? userMessages.flatMap((message) =>
      "attachments" in message ? message.attachments ?? [] : [],
    )
    : [];
  const userMessage: Message | null = firstUserMessage
    ? {
        message_id: firstUserMessage.message_id,
        session_id: turn.session_id,
        role: "user",
        content: userContent,
        attachments,
        metadata: {
          ...("metadata" in firstUserMessage
            ? firstUserMessage.metadata ?? {}
            : {}),
          source: "turn_projection",
          job_id: turn.job_id,
          turn_id: turn.turn_id,
          turn_revision: turn.revision,
          summary: !isTurnDetail(turn),
        },
        created_at: firstUserMessage.created_at,
        updated_at: turn.updated_at,
      }
    : null;
  const assistantContent = isTurnDetail(turn)
    ? turn.final_response ?? turn.response_preview ?? ""
    : turn.response_preview ?? "";
  const assistantMessages: Message[] = assistantContent
    ? [{
        message_id: `${turn.turn_id}:assistant`,
        session_id: turn.session_id,
        role: "assistant",
        content: assistantContent,
        attachments: [],
        metadata: {
          source: "turn_projection",
          job_id: turn.job_id,
          turn_id: turn.turn_id,
          turn_revision: turn.revision,
          summary: !isTurnDetail(turn),
        },
        created_at: turn.completed_at ?? turn.updated_at,
        updated_at: turn.updated_at,
      }]
    : [];
  const thinkingBlocks = (turn.thinking_blocks ?? []).map((block) => ({
    kind: block.kind,
    text: block.text ?? "",
  }));
  const toolSummary = turn.tool_summary ?? [];

  return {
    conversationId: turn.turn_id,
    displayMode: "history",
    turnId: turn.turn_id,
    turnRevision: turn.revision,
    turnItemsView: isTurnDetail(turn) ? "full" : "summary",
    turnStatus: turn.status as ConversationView["turnStatus"],
    activityStats: turn.activity_stats
      ? {
          duration_ms: turn.activity_stats.duration_ms ?? null,
          item_count: turn.activity_stats.item_count ?? 0,
          ...(turn.activity_stats.first_item_sequence !== undefined
            ? {
                first_item_sequence:
                  turn.activity_stats.first_item_sequence ?? null,
              }
            : {}),
          ...(turn.activity_stats.last_item_sequence !== undefined
            ? {
                last_item_sequence:
                  turn.activity_stats.last_item_sequence ?? null,
              }
            : {}),
        }
      : undefined,
    sessionId: turn.session_id,
    userMessage,
    assistantMessages,
    thinkingBlocks,
    toolSummary,
    responseParts: turn.response_parts ?? [],
    events: isTurnDetail(turn) ? (turn.items ?? []) as TraceEvent[] : [],
    status: turnConversationStatus(turn),
    jobId: turn.job_id,
    pending: false,
    source: "turn",
  };
}

const turnConversationCache = new WeakMap<TurnRecord, ConversationView>();

export function turnTimelineConversations(
  state: AppState,
  sessionCacheKey: string,
  sessionId: string,
): ConversationView[] {
  const timeline = state.turnTimelinesBySession?.get(sessionCacheKey);
  if (!timeline) {
    return [];
  }
  return timeline.orderedTurnIds.flatMap((turnId) => {
    const turn = timeline.turnsById[turnId];
    if (turn?.session_id !== sessionId) {
      return [];
    }
    const cached = turnConversationCache.get(turn);
    if (cached) {
      return [cached];
    }
    const conversation = conversationFromTurn(turn);
    turnConversationCache.set(turn, conversation);
    return [conversation];
  });
}
