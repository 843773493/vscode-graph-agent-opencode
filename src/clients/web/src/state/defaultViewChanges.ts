import type { ConversationContentView } from "../types/frontend";
import type { SessionTurnTimeline } from "./session/turnTimeline";

export function shouldLoadDefaultViewChangesHint({
  contentView,
  sessionId,
  timeline,
  conversationCount,
}: {
  contentView: ConversationContentView;
  sessionId: string | null;
  timeline: SessionTurnTimeline | null;
  conversationCount: number;
}): boolean {
  return contentView === "default"
    && Boolean(sessionId)
    && timeline?.phase === "ready"
    && timeline.projectionState === "ready"
    && timeline.orderedTurnIds.length === 0
    && conversationCount === 0;
}
