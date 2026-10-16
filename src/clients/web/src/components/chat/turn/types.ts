import type { AttachmentRef, DeliveryPolicy, MessageReplayRequest } from "../../../types/backend";
import type { TurnHistoryInclude } from "../../../api/session/sessionTurnHistory";

export type LoadTurnDetails = (
  turnIds: string[],
  requestIdentity?: string | null,
  refreshAfterInFlight?: boolean,
  include?: TurnHistoryInclude[],
  toolCallIds?: string[],
) => Promise<void>;

export type LoadToolDetails = (turnId: string, toolCallId: string) => Promise<void>;

export interface ChatTurnHandlers {
  onForkConversation?: (point: {
    workspaceId: string; sessionId: string; threadId: string; sourceViewId: string; sourceViewRevision: number;
    turnId?: string; itemId?: string;
  }) => void;
  onReplayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  onRemovePending: (messageId: string) => Promise<void>;
  onChangePendingPolicy: (
    messageId: string,
    policy: DeliveryPolicy,
    expectedSnapshotVersion?: number,
  ) => Promise<void>;
}
