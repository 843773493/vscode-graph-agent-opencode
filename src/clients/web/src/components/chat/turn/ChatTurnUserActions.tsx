import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { nodeKey, nodeAttributes } from "../../../state/timeline/transcriptNodes";
import { t } from "../../../../../shared/core/i18n/index.js";
import React from "react";
import type { AttachmentRef, DeliveryPolicy } from "../../../types/backend";
import type { ConversationView } from "../../../types/frontend";
import { fileToSelectedAttachment } from "../../../utils/media/mediaAttachments";
import { copyItemAction, useChatItemMenu } from "../itemContextMenu";
import MessageAttachments from "./MessageAttachments";
import PendingRequestActions from "../pending/PendingRequestActions";
import ProgressiveUserMessage from "./ProgressiveUserMessage";
import type { ChatTurnActions } from "./useChatTurnActions";
import type { ChatTurnHandlers } from "./types";
import { errorMessage } from "../../../utils/errorMessage";

export function ChatTurnUserSection({
  apiPort,
  workspaceId,
  conversation,
  sessionBusy,
  actions,
  onLoadAgentStateMessageRawContent,
  onRemovePending,
  onChangePendingPolicy,
  onOpenAttachment,
  onForkConversation,
}: Pick<
  ChatTurnHandlers,
  "onRemovePending" | "onChangePendingPolicy" | "onForkConversation"
> & {
  apiPort: number;
  workspaceId?: string | null;
  conversation: ConversationView;
  sessionBusy: boolean;
  actions: ChatTurnActions;
  onLoadAgentStateMessageRawContent: (
    sessionId: string,
    messageId: string,
  ) => Promise<string>;
  onOpenAttachment?: (sessionId: string, attachment: AttachmentRef) => void;
}): React.ReactNode {
  const editAttachmentInputRef = React.useRef<HTMLInputElement | null>(null);
  const userMessage = conversation.userMessage;
  const userMessageId = userMessage?.message_id ?? null;
  const [rawMessageContent, setRawMessageContent] = React.useState<string | null>(null);
  const [rawMessageLoading, setRawMessageLoading] = React.useState(false);
  const [rawMessageError, setRawMessageError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setRawMessageContent(null);
    setRawMessageLoading(false);
    setRawMessageError(null);
  }, [userMessageId]);

  const summaryOnly = conversation.turnItemsView === "summary";
  const canInspectRawMessage = Boolean(
    userMessage
    && (!userMessage.content.trim() || userMessage.metadata?.internal === true),
  );
  const canEditUserMessage = Boolean(
    userMessage
    && !summaryOnly
    && !conversation.pending
    && !actions.isInternalDisplayMessage,
  );
  const rawMessageAriaLabel = userMessage?.content.trim()
    ? t("chat.turn.internalHint")
    : t("chat.turn.emptyHint");
  const toggleRawMessageDetails = React.useCallback(async () => {
    if (!userMessage || !canInspectRawMessage || rawMessageLoading) return;
    if (rawMessageContent !== null) {
      setRawMessageContent(null);
      setRawMessageError(null);
      return;
    }
    setRawMessageLoading(true);
    setRawMessageError(null);
    try {
      const content = await onLoadAgentStateMessageRawContent(
        conversation.sessionId,
        userMessage.message_id,
      );
      setRawMessageContent(content);
    } catch (error) {
      setRawMessageError(errorMessage(error));
    } finally {
      setRawMessageLoading(false);
    }
  }, [
    canInspectRawMessage,
    conversation.sessionId,
    onLoadAgentStateMessageRawContent,
    rawMessageContent,
    rawMessageLoading,
    userMessage,
  ]);
  const itemMenu = useChatItemMenu(() => {
    if (!userMessage) return [];
    const canFork = conversation.displayMode === "history" && Boolean(conversation.turnId && onForkConversation);
    const menu = [copyItemAction(rawMessageContent ?? userMessage.content), {
      id: "forkItem", label: t("chat.itemMenu.fork"),
      tooltip: t(canFork ? "chat.itemMenu.forkHint" : "chat.itemMenu.forkUnavailableHint"),
      enabled: canFork,
      run: () => {
        if (!workspaceId || !conversation.threadId || !conversation.sourceViewId || conversation.sourceViewRevision === undefined) {
          throw new Error("历史消息缺少实际读取的来源快照");
        }
        onForkConversation?.({
          workspaceId, sessionId: conversation.sessionId, threadId: conversation.threadId,
          sourceViewId: conversation.sourceViewId, sourceViewRevision: conversation.sourceViewRevision,
          turnId: conversation.turnId,
        });
      },
    }];
    if (canEditUserMessage) menu.push({
      id: "editAndContinue", label: t("chat.turn.continue"),
      tooltip: t(sessionBusy ? "chat.itemMenu.editBusyHint" : "chat.itemMenu.editHint"),
      enabled: !sessionBusy, run: actions.startEditing,
    });
    if (canInspectRawMessage) menu.push({
      id: "rawContent", label: t(rawMessageContent !== null ? "chat.turn.hideRaw" : "chat.turn.showRaw"),
      tooltip: t("chat.itemMenu.rawHint"), enabled: !rawMessageLoading, run: toggleRawMessageDetails,
    });
    return menu;
  }, !actions.editing, () => conversation.displayMode === "history" && !conversation.pending
    ? { messageId: typeof userMessage?.metadata?.last_item_message_id === "string"
      ? userMessage.metadata.last_item_message_id : userMessageId ?? undefined } : undefined);

  if (!userMessage) return null;
  const userAttachments = userMessage.attachments ?? [];

  return (
    <div className={`chat-user-row${actions.isInternalDisplayMessage ? " is-internal" : ""}`}>
      <div
        className={`chat-user-bubble${actions.editing ? " is-editing" : ""}${actions.isInternalDisplayMessage ? " is-internal" : ""}${canInspectRawMessage ? " is-inspectable" : ""}`}
        {...nodeAttributes(nodeKey({kind:"turn",id:conversation.turnId ?? conversation.conversationId},"user"))}
        role="group"
        aria-label={canInspectRawMessage ? rawMessageAriaLabel : t("chat.itemMenu.user")}
        data-chat-item-id={typeof userMessage.metadata?.last_item_message_id === "string" ? userMessage.metadata.last_item_message_id : userMessageId}
        {...itemMenu}
      >
        {actions.editing ? (
          <form
            className="chat-request-edit-form"
            onSubmit={(event) => {
              event.preventDefault();
              void actions.executeReplay("edit_and_continue", actions.editContent.trim());
            }}
          >
            <textarea
              className="chat-request-edit-input"
              value={actions.editContent}
              aria-label={t("chat.turn.edit")}
              autoFocus
              disabled={actions.actionRunning}
              onChange={(event) => actions.setEditContent(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") actions.cancelEditing();
              }}
            />
            {actions.editAttachments.length > 0 ? (
              <div className="chat-request-edit-attachments">
                {actions.editAttachments.map((attachment) => (
                  <span key={attachment.file_id} className="chat-request-edit-attachment">
                    {attachment.name ?? attachment.file_id}
                    <button
                      type="button"
                      disabled={actions.actionRunning}
                      aria-label={t("chat.turn.removeAttachment", attachment.name ?? attachment.file_id)}
                      onClick={() => actions.setEditAttachments((current) =>
                        current.filter((item) => item.file_id !== attachment.file_id)
                      )}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            ) : null}
            {conversation.pending ? (
              <>
                <input
                  ref={editAttachmentInputRef}
                  type="file"
                  multiple
                  className="visually-hidden"
                  onChange={(event) => {
                    const files = Array.from(event.target.files ?? []);
                    event.target.value = "";
                    void Promise.all(
                      files.map((file, index) =>
                        fileToSelectedAttachment(
                          file,
                          actions.editAttachments.length + index,
                        ),
                      ),
                    ).then((added) => {
                      actions.setEditAttachments((current) => [...current, ...added]);
                    }).catch(actions.reportActionError);
                  }}
                />
                <button
                  type="button"
                  className="chat-request-edit-add-attachment"
                  disabled={actions.actionRunning}
                  onClick={() => editAttachmentInputRef.current?.click()}
                >
                  <span className="codicon codicon-attach" aria-hidden="true" />
                  {t("chat.turn.attach")}
                </button>
              </>
            ) : null}
            {!summaryOnly && !conversation.pending && !actions.isInternalDisplayMessage ? (
              <div className="chat-turn-action-warning">
                {t("chat.turn.rewindWarning")}
              </div>
            ) : null}
            <div className="chat-request-edit-actions">
              <button
                type="button"
                disabled={actions.actionRunning}
                onClick={actions.cancelEditing}
              >
                {t("chat.cancel")}
              </button>
              <button
                type="submit"
                className="primary"
                disabled={
                  actions.actionRunning
                  || (!actions.editContent.trim() && actions.editAttachments.length === 0)
                }
              >
                {actions.actionRunning
                  ? t("chat.turn.saving")
                  : t("chat.turn.continue")}
              </button>
            </div>
          </form>
        ) : (
          <>
            {userMessage.content ? (
              <ProgressiveUserMessage
                content={userMessage.content}
                internalLabel={actions.internalLabel}
              />
            ) : null}
            {userAttachments.length > 0 ? (
              <MessageAttachments
                apiPort={apiPort}
                workspaceId={workspaceId}
                sessionId={conversation.sessionId}
                attachments={userAttachments}
                onOpenAttachment={onOpenAttachment
                  ? (attachment) => onOpenAttachment(conversation.sessionId, attachment)
                  : undefined}
              />
            ) : null}
          </>
        )}
        {canInspectRawMessage && rawMessageLoading ? (
          <div className="chat-user-raw-status" role="status">{t("chat.turn.loadingRaw")}</div>
        ) : null}
        {canInspectRawMessage && rawMessageError ? (
          <div className="chat-user-raw-error failure-summary" role="alert">{failureSummary(rawMessageError)}</div>
        ) : null}
        {canInspectRawMessage && rawMessageContent !== null ? (
          <div className="chat-user-raw-details">
            <div className="chat-user-raw-details-title">{t("chat.turn.rawDetails")}</div>
            <pre className="chat-user-raw-content">
              {rawMessageContent || t("chat.turn.rawEmpty")}
            </pre>
          </div>
        ) : null}
        {conversation.pending
          && !conversation.activeJobOverlay
          && !actions.isInternalDisplayMessage
          && conversation.deliveryPolicy ? (
          <PendingRequestActions
            deliveryPolicy={conversation.deliveryPolicy}
            disabled={actions.actionRunning}
            onRemove={() => {
              void actions.executePendingAction(
                () => onRemovePending(userMessage.message_id),
              );
            }}
            onChangePolicy={(policy: DeliveryPolicy) => {
              void actions.executePendingAction(
                () => onChangePendingPolicy(userMessage.message_id, policy),
              );
            }}
          />
        ) : null}
      </div>
    </div>
  );
}
