import React from "react";
import type {
  AttachmentRef,
  MessageReplayRequest,
} from "../../../types/backend";
import type { ConversationView } from "../../../types/frontend";

export interface ChatTurnActionCallbacks {
  onReplayTurn: (
    targetMessageId: string,
    action: MessageReplayRequest["action"],
    displayContent: string,
    content?: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  onUpdatePending: (
    messageId: string,
    content: string,
    attachments?: AttachmentRef[],
  ) => Promise<void>;
  onRemovePending: (messageId: string) => Promise<void>;
  onSendPendingImmediately: (messageId: string) => Promise<void>;
  onChangePendingKind: (
    messageId: string,
    kind: "queued" | "steering",
  ) => Promise<void>;
}

export type ReplayConfirmation = "retry_failed" | "regenerate" | null;

function internalMessageLabel(
  metadata: Record<string, unknown> | undefined,
): string | null {
  switch (metadata?.internal_display_kind) {
    case "delegated_task": return "委派任务";
    case "generated_session_result": return "会话生成";
    default: return null;
  }
}

export interface ChatTurnActions {
  editing: boolean;
  editContent: string;
  editAttachments: AttachmentRef[];
  confirmAction: ReplayConfirmation;
  actionRunning: boolean;
  actionError: string | null;
  internalLabel: string | null;
  isInternalDisplayMessage: boolean;
  startEditing: () => void;
  cancelEditing: () => void;
  setEditContent: (content: string) => void;
  setEditAttachments: React.Dispatch<React.SetStateAction<AttachmentRef[]>>;
  setConfirmAction: (action: ReplayConfirmation) => void;
  executeReplay: (
    action: MessageReplayRequest["action"],
    content?: string,
  ) => Promise<void>;
  executePendingEdit: () => Promise<void>;
  executePendingAction: (action: () => Promise<void>) => Promise<void>;
  reportActionError: (error: unknown) => void;
}

export function useChatTurnActions({
  conversation,
  sessionBusy,
  onReplayTurn,
  onUpdatePending,
}: Pick<ChatTurnActionCallbacks, "onReplayTurn" | "onUpdatePending"> & {
  conversation: ConversationView;
  sessionBusy: boolean;
}): ChatTurnActions {
  const [editing, setEditing] = React.useState(false);
  const [editContent, setEditContent] = React.useState("");
  const [editAttachments, setEditAttachments] = React.useState<AttachmentRef[]>([]);
  const [confirmAction, setConfirmAction] = React.useState<ReplayConfirmation>(null);
  const [actionRunning, setActionRunning] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);
  const userMessage = conversation.userMessage;
  const summaryOnly = conversation.turnItemsView === "summary";
  const internalLabel = internalMessageLabel(userMessage?.metadata);
  const isInternalDisplayMessage = internalLabel !== null;
  const userAttachments = userMessage?.attachments ?? [];

  const startEditing = React.useCallback(() => {
    if (
      !userMessage
      || summaryOnly
      || isInternalDisplayMessage
      || (sessionBusy && !conversation.pending)
    ) return;
    setEditContent(userMessage.content);
    setEditAttachments(userAttachments);
    setActionError(null);
    setConfirmAction(null);
    setEditing(true);
  }, [
    conversation.pending,
    isInternalDisplayMessage,
    sessionBusy,
    summaryOnly,
    userAttachments,
    userMessage,
  ]);

  const executeReplay = React.useCallback(async (
    action: MessageReplayRequest["action"],
    content?: string,
  ) => {
    if (!userMessage || summaryOnly || actionRunning || sessionBusy) return;
    setActionRunning(true);
    setActionError(null);
    try {
      await onReplayTurn(
        userMessage.message_id,
        action,
        userMessage.content,
        content,
        userAttachments,
      );
      setEditing(false);
      setConfirmAction(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setActionRunning(false);
    }
  }, [actionRunning, onReplayTurn, sessionBusy, summaryOnly, userAttachments, userMessage]);

  const executePendingEdit = React.useCallback(async () => {
    if (!userMessage || actionRunning || !conversation.pending) return;
    setActionRunning(true);
    setActionError(null);
    try {
      await onUpdatePending(
        userMessage.message_id,
        editContent.trim(),
        editAttachments,
      );
      setEditing(false);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setActionRunning(false);
    }
  }, [
    actionRunning,
    conversation.pending,
    editAttachments,
    editContent,
    onUpdatePending,
    userMessage,
  ]);

  const executePendingAction = React.useCallback(async (
    action: () => Promise<void>,
  ) => {
    if (actionRunning) return;
    setActionRunning(true);
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setActionRunning(false);
    }
  }, [actionRunning]);

  return {
    editing,
    editContent,
    editAttachments,
    confirmAction,
    actionRunning,
    actionError,
    internalLabel,
    isInternalDisplayMessage,
    startEditing,
    cancelEditing: React.useCallback(() => {
      setEditing(false);
      setActionError(null);
    }, []),
    setEditContent,
    setEditAttachments,
    setConfirmAction,
    executeReplay,
    executePendingEdit,
    executePendingAction,
    reportActionError: React.useCallback((error: unknown) => {
      setActionError(error instanceof Error ? error.message : String(error));
    }, []),
  };
}
