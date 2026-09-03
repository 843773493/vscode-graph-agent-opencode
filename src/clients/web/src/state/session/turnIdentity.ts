import type { ConversationView } from "../../types/frontend";

export function conversationTurnKey(conversation: ConversationView): string {
  return conversation.turnId ?? conversation.conversationId;
}
